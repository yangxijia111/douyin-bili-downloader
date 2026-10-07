"""系统代理崩溃恢复测试（全部用内存后端与临时目录，绝不碰真实注册表）。

覆盖 P1 要求的四个场景：正常退出恢复、重复 restore 幂等、stale recovery、
用户已手动修改代理时不得错误覆盖。
"""

from __future__ import annotations

import base64
import ctypes
import json
import sys
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from channels.interceptor import SystemProxyManager
from channels.proxy_recovery import (
    ProxyRecovery,
    WindowsProxyBackend,
    default_state_dir,
    pid_alive,
    recover_stale_proxy,
)


@pytest.fixture(autouse=True)
def _pretend_windows(monkeypatch):
    """非 Windows 的 CI 上也验证 mock 化的系统代理逻辑（不 skip）。

    所有测试都注入 FakeBackend / 临时目录，真实注册表与 WinINET 不会被触碰。
    """
    if sys.platform != "win32":
        monkeypatch.setattr(sys, "platform", "win32")


class FakeBackend:
    """内存版注册表后端，行为对齐 WindowsProxyBackend（含缺省字段补 None）。"""

    _FIELDS = ("ProxyEnable", "ProxyServer", "ProxyOverride")

    def __init__(self, initial: Dict[str, Any] | None = None):
        self.values: Dict[str, Any] = initial or {
            "ProxyEnable": 0,
            "ProxyServer": None,
            "ProxyOverride": None,
        }
        self.notified = 0

    def snapshot(self) -> Dict[str, Any]:
        return {name: self.values.get(name) for name in self._FIELDS}

    def write(self, values: Dict[str, Any]) -> None:
        for name, value in values.items():
            if value is None:
                self.values.pop(name, None)
            else:
                self.values[name] = value

    def notify(self) -> None:
        self.notified += 1


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOADER_DATA_DIR", str(tmp_path / "appdata"))
    backend = FakeBackend(
        {
            "ProxyEnable": 0,
            "ProxyServer": None,
            "ProxyOverride": None,
        }
    )
    recovery = ProxyRecovery(default_state_dir())
    return {"backend": backend, "recovery": recovery, "tmp": tmp_path}


def test_enable_saves_recovery_record_and_restore_clears_it(env):
    backend, recovery = env["backend"], env["recovery"]
    manager = SystemProxyManager(backend=backend, recovery=recovery)

    manager.enable(host="127.0.0.1", port=8899)
    record = recovery.load()
    assert record is not None
    assert record["proxy"] == "127.0.0.1:8899"
    assert record["previous"]["ProxyEnable"] == 0
    assert record["pid"] > 0
    # 代理已生效。
    assert backend.values["ProxyEnable"] == 1
    assert backend.values["ProxyServer"] == "127.0.0.1:8899"

    # 正常退出路径。
    manager.restore()
    assert recovery.load() is None
    assert backend.snapshot() == {"ProxyEnable": 0, "ProxyServer": None, "ProxyOverride": None}


def test_restore_is_idempotent(env):
    manager = SystemProxyManager(backend=env["backend"], recovery=env["recovery"])
    manager.enable(port=9000)
    manager.restore()
    snapshot_after_first = env["backend"].snapshot()
    manager.restore()  # 第二次 restore 不应报错也不应改变注册表
    assert env["backend"].snapshot() == snapshot_after_first
    assert env["recovery"].load() is None


def test_stale_record_restored_on_startup(env):
    """模拟 taskkill /F：enable 后没有 restore，进程直接消失。"""
    backend, recovery = env["backend"], env["recovery"]
    crashed = SystemProxyManager(backend=backend, recovery=recovery)
    crashed.enable(port=8899)
    assert recovery.load() is not None  # 崩溃残留

    # 「重启后」：同样的 backend（系统状态仍在）+ 新的 manager。
    report = recover_stale_proxy(backend=backend, recovery=recovery)
    assert report["action"] == "restored"
    assert backend.values["ProxyEnable"] == 0
    assert backend.values.get("ProxyServer") is None
    assert recovery.load() is None  # 记录已清理

    # 幂等：再跑一次无动作。
    report = recover_stale_proxy(backend=backend, recovery=recovery)
    assert report["action"] == "none"


def test_user_changed_proxy_is_not_overwritten(env):
    """崩溃后用户手动改了代理：恢复逻辑必须保留用户设置。"""
    backend, recovery = env["backend"], env["recovery"]
    crashed = SystemProxyManager(backend=backend, recovery=recovery)
    crashed.enable(port=8899)

    # 用户手动改成了别的代理（如公司代理）。
    backend.values["ProxyEnable"] = 1
    backend.values["ProxyServer"] = "proxy.corp.local:8080"

    report = recover_stale_proxy(backend=backend, recovery=recovery)
    assert report["action"] == "kept"
    assert backend.values["ProxyServer"] == "proxy.corp.local:8080"  # 用户设置原样保留
    assert recovery.load() is None  # 记录清除，避免反复报告


def test_user_disabled_proxy_is_not_overwritten(env):
    backend, recovery = env["backend"], env["recovery"]
    SystemProxyManager(backend=backend, recovery=recovery).enable(port=8899)
    # 用户手动关闭了系统代理。
    backend.values["ProxyEnable"] = 0
    report = recover_stale_proxy(backend=backend, recovery=recovery)
    assert report["action"] == "kept"
    assert backend.values["ProxyServer"] == "127.0.0.1:8899"  # 不动用户现状


def test_record_from_live_process_is_skipped(env, monkeypatch):
    import json

    backend, recovery = env["backend"], env["recovery"]
    SystemProxyManager(backend=backend, recovery=recovery).enable(port=8899)
    # 把记录改成「另一个进程写的」（同进程写入的记录应立即处理，跳过逻辑
    # 只对跨进程记录生效）。
    record = recovery.load()
    record["pid"] = 424242
    recovery.state_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr("channels.proxy_recovery.pid_alive", lambda pid: pid == 424242)
    report = recover_stale_proxy(backend=backend, recovery=recovery)
    assert report["action"] == "skipped"
    assert backend.snapshot()["ProxyEnable"] == 1  # 未被触碰
    assert recovery.load() is not None  # 记录保留（等真正重启后再恢复）


def test_corrupt_record_is_ignored(env):
    recovery = env["recovery"]
    recovery.state_dir.mkdir(parents=True, exist_ok=True)
    recovery.state_path.write_text("not json{", encoding="utf-8")
    report = recover_stale_proxy(backend=env["backend"], recovery=recovery)
    assert report["action"] == "none"


def test_no_record_is_none(env):
    report = recover_stale_proxy(backend=env["backend"], recovery=env["recovery"])
    assert report["action"] == "none"


def test_default_state_dir_respects_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DOWNLOADER_DATA_DIR", str(tmp_path))
    assert default_state_dir() == tmp_path
    monkeypatch.delenv("DOWNLOADER_DATA_DIR")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    from channels.proxy_recovery import ProxyRecovery as _PR

    assert _PR().state_dir == tmp_path / "roaming" / "douyin-bili-downloader"


def test_real_backend_class_matches_contract():
    """WindowsProxyBackend 的公开接口与替身一致（防止替身漂移）。"""
    for method in ("snapshot", "write", "notify"):
        assert callable(getattr(WindowsProxyBackend, method, None))


def test_pid_alive_rejects_garbage():
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
    assert pid_alive("not-a-pid") is False


@pytest.fixture
def windows_backend(monkeypatch):
    """WinINET + 注册表的内存替身，包括曾不同步的 Connections 二进制值。"""
    import channels.proxy_recovery as module

    key_path = WindowsProxyBackend.KEY_PATH
    values = {
        key_path: {
            "ProxyEnable": 1,
            "ProxyServer": "127.0.0.1:8899",
            "ProxyOverride": "top-level-original",
            "AutoConfigURL": "https://proxy.example/original.pac",
        },
        key_path + r"\Connections": {
            "DefaultConnectionSettings": b"default-7897-original",
            "SavedLegacySettings": b"legacy-7897-original",
        },
    }

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def query(key, name):
        if name not in values[key.path]:
            raise FileNotFoundError(name)
        value = values[key.path][name]
        return value, 3 if isinstance(value, bytes) else 1

    def delete(key, name):
        if name not in values[key.path]:
            raise FileNotFoundError(name)
        del values[key.path][name]

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, REG_BINARY=3, REG_DWORD=4, REG_SZ=1,
        OpenKey=lambda root, path, *args: Key(path),
        CreateKeyEx=lambda root, path, *args: Key(path),
        QueryValueEx=query,
        SetValueEx=lambda key, name, reserved, kind, value: values[key.path].__setitem__(
            name, value
        ),
        DeleteValue=delete,
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)

    class Api:
        def __init__(self):
            self.options = {
                "flags": 15,
                "server": "127.0.0.1:7897",
                "bypass": "connection-original",
                "auto_config": "https://proxy.example/original.pac",
            }
            self.calls = []
            self.fail = None
            self.query_flags_ui_fails = False
            self.buffers = {}
            self.freed = []

        def InternetSetOptionW(self, handle, option, buffer, size):
            self.calls.append(option)
            if self.fail == option:
                return False
            if option == 75:
                option_list = ctypes.cast(
                    buffer, ctypes.POINTER(module._InternetPerConnOptionListW)
                ).contents
                assert not handle and not option_list.pszConnection
                assert size == ctypes.sizeof(module._InternetPerConnOptionListW)
                for index in range(option_list.dwOptionCount):
                    item = option_list.pOptions[index]
                    if item.dwOption == 1:
                        self.options["flags"] = item.Value.dwValue
                    else:
                        name = {2: "server", 3: "bypass", 4: "auto_config"}[item.dwOption]
                        self.options[name] = ctypes.wstring_at(item.Value.pszValue)
                top = values[key_path]
                top["ProxyEnable"] = int(bool(self.options["flags"] & 2))
                top["ProxyServer"] = self.options["server"]
                top["ProxyOverride"] = self.options["bypass"]
                top["AutoConfigURL"] = self.options["auto_config"]
                blob = json.dumps(self.options, sort_keys=True).encode()
                for name in values[key_path + r"\Connections"]:
                    values[key_path + r"\Connections"][name] = blob
            return True

        def InternetQueryOptionW(self, handle, option, buffer, size):
            option_list = ctypes.cast(
                buffer, ctypes.POINTER(module._InternetPerConnOptionListW)
            ).contents
            if self.query_flags_ui_fails and option_list.pOptions[0].dwOption == 10:
                return False
            for index in range(option_list.dwOptionCount):
                item = option_list.pOptions[index]
                if item.dwOption in (1, 10):
                    item.Value.dwValue = self.options["flags"]
                else:
                    name = {2: "server", 3: "bypass", 4: "auto_config"}[item.dwOption]
                    text_buffer = ctypes.create_unicode_buffer(self.options[name])
                    address = ctypes.addressof(text_buffer)
                    self.buffers[address] = text_buffer
                    item.Value.pszValue = address
            return True

        def free(self, address):
            self.freed.append(address)
            self.buffers.pop(address)

    api = Api()
    monkeypatch.setattr(module, "_wininet_library", lambda: api, raising=False)
    monkeypatch.setattr(module, "_free_global", api.free, raising=False)
    # 旧实现直接使用 windll，同样截获，red 测试也不能触碰真机设置。
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(Wininet=api), raising=False)
    return WindowsProxyBackend(), api, values


def test_wininet_proxy_sync_and_exact_restore(windows_backend, tmp_path):
    backend, api, values = windows_backend
    before = {path: dict(entries) for path, entries in values.items()}
    effective_before = dict(api.options)
    recovery = ProxyRecovery(tmp_path)
    manager = SystemProxyManager(backend=backend, recovery=recovery)

    manager.enable(port=8899)
    assert api.options["server"] == "127.0.0.1:8899"
    assert api.options["flags"] == 3  # 嗅探期间不能再被 PAC / 自动检测绕过。
    assert api.options["auto_config"] == effective_before["auto_config"]
    assert api.calls[:3] == [75, 39, 37]
    record = recovery.load()
    assert record["previous"]["connection_settings"]["DefaultConnectionSettings"] == (
        base64.b64encode(b"default-7897-original").decode("ascii")
    )

    manager.restore()
    assert values == before  # 顶层与两个连接值按原状态逐字节恢复，即使原来不一致。
    assert api.options == effective_before
    assert recovery.load() is None
    assert not api.buffers  # WinINET 返回的字符串内存全部 GlobalFree。


def test_connection_only_user_edit_is_kept_on_stop(windows_backend, tmp_path):
    backend, api, values = windows_backend
    recovery = ProxyRecovery(tmp_path)
    manager = SystemProxyManager(backend=backend, recovery=recovery)
    manager.enable(port=8899)
    values[backend.KEY_PATH + r"\Connections"]["DefaultConnectionSettings"] = b"user-edit"
    current = backend.snapshot()
    manager.restore()
    assert backend.snapshot() == current
    assert recovery.load() is None


def test_connection_only_user_edit_is_kept_after_crash(windows_backend, tmp_path):
    backend, api, values = windows_backend
    recovery = ProxyRecovery(tmp_path)
    SystemProxyManager(backend=backend, recovery=recovery).enable(port=8899)
    values[backend.KEY_PATH + r"\Connections"]["SavedLegacySettings"] = b"user-edit"
    current = backend.snapshot()
    assert recover_stale_proxy(backend=backend, recovery=recovery)["action"] == "kept"
    assert backend.snapshot() == current


@pytest.mark.parametrize("failed_option", [75, 39, 37])
def test_wininet_enable_failure_rolls_back_and_preserves_recovery(
    windows_backend, tmp_path, failed_option
):
    backend, api, values = windows_backend
    before = {path: dict(entries) for path, entries in values.items()}
    recovery = ProxyRecovery(tmp_path)
    manager = SystemProxyManager(backend=backend, recovery=recovery)
    api.fail = failed_option
    with pytest.raises(OSError, match="WinINET"):
        manager.enable(port=8899)
    # 通知失败后的回滚仍须恢复原始 registry blobs，失败记录保留以便再试。
    if failed_option in (39, 37):
        assert values == before
    assert recovery.load() is not None
    api.fail = None
    manager.restore()
    assert values == before
    assert recovery.load() is None


def test_effective_proxy_flags_ui_fallback_and_string_free(windows_backend):
    backend, api, values = windows_backend
    api.query_flags_ui_fails = True
    assert backend.effective_proxy() == {
        "enabled": True,
        "flags": 15,
        "server": "127.0.0.1:7897",
        "bypass": "connection-original",
        "auto_config": "https://proxy.example/original.pac",
    }
    assert not api.buffers


def test_version_one_recovery_record_remains_supported(env):
    recovery, backend = env["recovery"], env["backend"]
    recovery.state_dir.mkdir(parents=True, exist_ok=True)
    recovery.state_path.write_text(json.dumps({
        "version": 1, "pid": 0, "proxy": "127.0.0.1:8899",
        "previous": {"ProxyEnable": 0, "ProxyServer": None, "ProxyOverride": None},
    }), encoding="utf-8")
    backend.values.update({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:8899"})
    assert recover_stale_proxy(backend=backend, recovery=recovery)["action"] == "restored"
    assert backend.values["ProxyEnable"] == 0


def test_wininet_crash_recovery_restores_full_previous_settings(windows_backend, tmp_path):
    backend, api, values = windows_backend
    before = {path: dict(entries) for path, entries in values.items()}
    effective_before = dict(api.options)
    recovery = ProxyRecovery(tmp_path)
    SystemProxyManager(backend=backend, recovery=recovery).enable(port=8899)
    assert recovery.load()["version"] == 2
    assert recover_stale_proxy(backend=backend, recovery=recovery)["action"] == "restored"
    assert values == before
    assert api.options == effective_before


def test_wininet_restore_notification_failure_can_be_retried(windows_backend, tmp_path):
    backend, api, values = windows_backend
    before = {path: dict(entries) for path, entries in values.items()}
    recovery = ProxyRecovery(tmp_path)
    manager = SystemProxyManager(backend=backend, recovery=recovery)
    manager.enable(port=8899)
    api.fail = 37
    manager.restore()
    assert values == before
    assert manager.active
    assert recovery.load() is not None
    api.fail = None
    manager.restore()
    assert not manager.active
    assert recovery.load() is None


def test_wininet_restore_notification_failure_recovers_after_crash(windows_backend, tmp_path):
    backend, api, values = windows_backend
    before = {path: dict(entries) for path, entries in values.items()}
    recovery = ProxyRecovery(tmp_path)
    manager = SystemProxyManager(backend=backend, recovery=recovery)
    manager.enable(port=8899)
    api.fail = 39
    manager.restore()
    api.fail = None
    assert recover_stale_proxy(backend=backend, recovery=recovery)["action"] == "restored"
    assert values == before


def test_user_pac_change_is_kept_on_stop(windows_backend, tmp_path):
    backend, api, values = windows_backend
    recovery = ProxyRecovery(tmp_path)
    manager = SystemProxyManager(backend=backend, recovery=recovery)
    manager.enable(port=8899)
    api.options["auto_config"] = "https://proxy.example/user-new.pac"
    api.options["flags"] = 4
    current = backend.snapshot()
    manager.restore()
    assert backend.snapshot() == current
    assert recovery.load() is None


def test_missing_proxy_registry_values_remain_missing(windows_backend, tmp_path):
    backend, api, values = windows_backend
    del values[backend.KEY_PATH]["AutoConfigURL"]
    del values[backend.CONNECTIONS_PATH]["SavedLegacySettings"]
    before = {path: dict(entries) for path, entries in values.items()}
    manager = SystemProxyManager(backend=backend, recovery=ProxyRecovery(tmp_path))
    manager.enable(port=8899)
    manager.restore()
    assert values == before


def test_recovery_persist_failure_does_not_change_system_proxy(windows_backend, tmp_path):
    backend, api, values = windows_backend

    class FailingRecovery(ProxyRecovery):
        def save(self, **kwargs):
            raise OSError("cannot persist recovery")

    before = {path: dict(entries) for path, entries in values.items()}
    manager = SystemProxyManager(backend=backend, recovery=FailingRecovery(tmp_path))
    with pytest.raises(OSError, match="persist"):
        manager.enable(port=8899)
    assert not manager.active
    assert values == before
    assert api.calls == []


def test_corrupt_connection_backup_does_not_mutate_system_proxy(windows_backend):
    backend, api, values = windows_backend
    snapshot = backend.snapshot()
    snapshot["connection_settings"]["DefaultConnectionSettings"] = "!!!not base64"
    with pytest.raises(OSError, match="备份损坏"):
        backend.write(snapshot)
    assert api.calls == []
