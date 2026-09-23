"""系统代理崩溃恢复测试（全部用内存后端与临时目录，绝不碰真实注册表）。

覆盖 P1 要求的四个场景：正常退出恢复、重复 restore 幂等、stale recovery、
用户已手动修改代理时不得错误覆盖。
"""

from __future__ import annotations

import sys
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
