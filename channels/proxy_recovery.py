"""系统代理异常退出恢复（P1 可靠性）。

``SystemProxyManager`` 的 ``try/finally`` 只能覆盖正常退出路径；``taskkill /F``、
Python 崩溃、断电、IDE 强杀都会留下「系统代理仍指向本程序嗅探端口」的残局。
本模块把恢复所需的全部状态持久化到**应用数据目录**（不是项目源码目录）：

* 写代理前先保存 ``ProxyEnable / ProxyServer / ProxyOverride`` 原值、本次
  设置的代理地址、会话 id 与进程 pid；
* ``enable()`` 成功后落盘、``restore()`` 成功后删除；因此**存在恢复记录**
  即意味着上次会话可能没有正常退出；
* 启动时（CLI ``--channels`` / ``--serve`` / ``--repair-network``）检测：

  1. 记录写入进程仍存活 → 视为另一活动会话，跳过；
  2. 当前系统代理仍等于记录中「本次设置的代理」→ 判定为异常残留，
     原样写回先前的设置；
  3. 当前设置与记录不符 → 用户已在崩溃后手动改过代理，**保留现状**，
     只清除记录——绝不覆盖用户新设置。

恢复动作幂等：记录删除后重复调用直接返回 ``none``。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "RECOVERY_FILENAME",
    "WindowsProxyBackend",
    "ProxyRecovery",
    "default_state_dir",
    "recover_stale_proxy",
    "pid_alive",
    "proxy_snapshot_matches",
]

RECOVERY_FILENAME = "proxy_recovery_state.json"
STATE_VERSION = 2

# 恢复记录与注册表键的标准字段。
_PROXY_FIELDS = ("ProxyEnable", "ProxyServer", "ProxyOverride")
_CONNECTION_FIELDS = ("DefaultConnectionSettings", "SavedLegacySettings")


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _InternetPerConnValue(ctypes.Union):
    # 使用固定 32 位 DWORD；LPWSTR 保留原指针，查询后才能正确 GlobalFree。
    _fields_ = [
        ("dwValue", ctypes.c_uint32),
        ("pszValue", ctypes.c_void_p),
        ("ftValue", _FileTime),
    ]


class _InternetPerConnOptionW(ctypes.Structure):
    _fields_ = [("dwOption", ctypes.c_uint32), ("Value", _InternetPerConnValue)]


class _InternetPerConnOptionListW(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("pszConnection", ctypes.c_wchar_p),
        ("dwOptionCount", ctypes.c_uint32),
        ("dwOptionError", ctypes.c_uint32),
        ("pOptions", ctypes.POINTER(_InternetPerConnOptionW)),
    ]


def _wininet_library():
    library = ctypes.WinDLL("wininet", use_last_error=True)
    library.InternetSetOptionW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32
    ]
    library.InternetSetOptionW.restype = ctypes.c_int32
    library.InternetQueryOptionW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
    ]
    library.InternetQueryOptionW.restype = ctypes.c_int32
    return library


def _free_global(address: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree(address)


def _wininet_error(action: str) -> OSError:
    error = getattr(ctypes, "get_last_error", lambda: 0)()
    return OSError(error, "WinINET {} 失败（Windows 错误 {}）".format(action, error))


def proxy_snapshot_matches(current: Dict[str, Any], applied: Dict[str, Any]) -> bool:
    """仅还原仍由本会话持有的设置；包括用户在连接选项/PAC 中的改动。"""
    return all(current.get(name) == value for name, value in applied.items())


def default_state_dir() -> Path:
    """应用数据目录：Windows 用 %APPDATA%，其余走 XDG。可用环境变量覆盖以便测试。"""
    override = os.environ.get("DOWNLOADER_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "douyin-bili-downloader"


class WindowsProxyBackend:
    """WinINET LAN 连接代理设置 + HKCU 原值备份/恢复。

    独立成类便于测试注入替身（绝不为了测试去真的改注册表）。
    """

    KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    CONNECTIONS_PATH = KEY_PATH + r"\Connections"

    def snapshot(self) -> Dict[str, Any]:
        import winreg

        values: Dict[str, Any] = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY_PATH) as key:
            for name in _PROXY_FIELDS + ("AutoConfigURL",):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                    values[name] = value
                except FileNotFoundError:
                    values[name] = None
        connections = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.CONNECTIONS_PATH) as key:
                for name in _CONNECTION_FIELDS:
                    try:
                        value, kind = winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        value = None
                    if value is not None:
                        if kind != winreg.REG_BINARY or not isinstance(value, bytes):
                            raise OSError("无法安全备份非二进制连接设置: {}".format(name))
                        value = base64.b64encode(value).decode("ascii")
                    connections[name] = value
        except FileNotFoundError:
            connections = {name: None for name in _CONNECTION_FIELDS}
        values["connection_settings"] = connections
        effective = self.effective_proxy()
        values["connection_options"] = {
            name: effective[name] for name in ("flags", "server", "bypass", "auto_config")
        }
        return values

    def effective_proxy(self) -> Dict[str, Any]:
        """只读查询微信/Chromium 实际读取的 LAN 选项，区别于顶层注册表显示值。"""
        api = _wininet_library()
        for flags_option in (10, 1):  # FLAGS_UI；旧版本回退 FLAGS。
            options = (_InternetPerConnOptionW * 4)()
            for item, option in zip(options, (flags_option, 2, 3, 4)):
                item.dwOption = option
            option_list = _InternetPerConnOptionListW()
            option_list.dwSize = ctypes.sizeof(option_list)
            option_list.dwOptionCount = len(options)
            option_list.pOptions = options
            size = ctypes.c_uint32(ctypes.sizeof(option_list))
            try:
                success = api.InternetQueryOptionW(
                    None, 75, ctypes.byref(option_list), ctypes.byref(size)
                )
                if success:
                    flags = int(options[0].Value.dwValue)
                    result = {"enabled": bool(flags & 2), "flags": flags}
                    for item, name in zip(options[1:], ("server", "bypass", "auto_config")):
                        address = item.Value.pszValue
                        result[name] = ctypes.wstring_at(address) if address else ""
                    return result
                if flags_option == 1:
                    raise _wininet_error("InternetQueryOptionW(75)")
            finally:
                for item in options[1:]:
                    if item.Value.pszValue:
                        _free_global(item.Value.pszValue)
        raise OSError("WinINET 无法读取 LAN 代理设置")  # pragma: no cover

    def write(self, values: Dict[str, Any]) -> None:
        import winreg

        # 先验证恢复记录，不能在已改动系统设置后才发现 base64 损坏。
        connections = values.get("connection_settings")
        restored_blobs = {}
        if connections is not None:
            try:
                for name in _CONNECTION_FIELDS:
                    encoded = connections.get(name)
                    restored_blobs[name] = (
                        base64.b64decode(encoded, validate=True) if encoded is not None else None
                    )
            except (AttributeError, TypeError, ValueError) as exc:
                raise OSError("代理恢复记录的连接备份损坏") from exc
        connection_options = values.get("connection_options")
        if connection_options is None:
            # 嗅探只使用显式代理，暂时关闭 PAC/自动检测 flags；PAC URL 本身保留。
            effective = self.effective_proxy()
            connection_options = {
                "flags": 3 if values.get("ProxyEnable") else 1,
                "server": str(values.get("ProxyServer") or ""),
                "bypass": str(values.get("ProxyOverride") or ""),
                "auto_config": effective["auto_config"],
            }
        options = (_InternetPerConnOptionW * 4)()
        options[0].dwOption = 1  # 恢复时也必须用 INTERNET_PER_CONN_FLAGS。
        options[0].Value.dwValue = int(connection_options.get("flags", 1))
        buffers = []  # InternetSetOptionW 返回前保持字符串缓冲区存活。
        for item, option, name in zip(options[1:], (2, 3, 4), ("server", "bypass", "auto_config")):
            item.dwOption = option
            buffer = ctypes.create_unicode_buffer(str(connection_options.get(name) or ""))
            buffers.append(buffer)
            item.Value.pszValue = ctypes.addressof(buffer)
        option_list = _InternetPerConnOptionListW()
        option_list.dwSize = ctypes.sizeof(option_list)
        option_list.dwOptionCount = len(options)
        option_list.pOptions = options
        if not _wininet_library().InternetSetOptionW(
            None, 75, ctypes.byref(option_list), ctypes.sizeof(option_list)
        ):
            raise _wininet_error("InternetSetOptionW(75)")

        # API 保证有效代理同步；随后按备份精确还原顶层键/连接 blob 的原始状态。
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self.KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            fields = _PROXY_FIELDS + (("AutoConfigURL",) if "AutoConfigURL" in values else ())
            for name in fields:
                value = values.get(name)
                if value is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                    continue
                reg_type = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, reg_type, value)
        if connections is not None:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, self.CONNECTIONS_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                for name, blob in restored_blobs.items():
                    if blob is None:
                        try:
                            winreg.DeleteValue(key, name)
                        except FileNotFoundError:
                            pass
                    else:
                        winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, blob)

    @staticmethod
    def notify() -> None:
        api = _wininet_library()
        for option in (39, 37):  # SETTINGS_CHANGED，再 REFRESH。
            if not api.InternetSetOptionW(None, option, None, 0):
                raise _wininet_error("InternetSetOptionW({})".format(option))


def pid_alive(pid: int) -> bool:
    """进程是否仍在运行（Windows 用 OpenProcess，绝不能用 os.kill——那会杀进程）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class ProxyRecovery:
    """恢复记录（JSON）的持久化。原子写：先写临时文件再替换。"""

    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()

    @property
    def state_path(self) -> Path:
        return self.state_dir / RECOVERY_FILENAME

    def save(self, *, previous: Dict[str, Any], proxy: str) -> Dict[str, Any]:
        saved_previous = {name: previous.get(name) for name in _PROXY_FIELDS}
        for name in ("AutoConfigURL", "connection_settings", "connection_options"):
            if name in previous:
                saved_previous[name] = previous[name]
        record = {
            "version": STATE_VERSION,
            "session_id": uuid.uuid4().hex,
            "pid": os.getpid(),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "previous": saved_previous,
            "proxy": proxy,
        }
        self._write_record(record)
        return record

    def update_applied(self, applied: Dict[str, Any]) -> None:
        record = self.load()
        if record is None:
            raise OSError("系统代理恢复记录丢失，不能安全完成接管")
        record["applied"] = applied
        self._write_record(record)

    def _write_record(self, record: Dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and data.get("version") in (1, STATE_VERSION):
            return data
        return None

    def clear(self) -> None:
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError:
            pass


def recover_stale_proxy(
    backend: Optional[Any] = None,
    recovery: Optional[ProxyRecovery] = None,
) -> Dict[str, str]:
    """检测并恢复上次异常退出遗留的系统代理。返回可读报告。

    返回值 ``action``：
    * ``none``    —— 无恢复记录（正常）；
    * ``skipped`` —— 记录所属进程仍在运行（可能是另一活动会话）；
    * ``restored``—— 确认残留并已还原；
    * ``kept``    —— 用户已手动修改代理，保留现状（记录清除）。
    """
    if sys.platform != "win32":
        # 非 Windows 从不自动设置系统代理，也就不会有残留记录；防御性清理。
        ProxyRecovery(recovery.state_dir if recovery else None).clear()
        return {"action": "none", "detail": "非 Windows 平台不涉及系统代理恢复"}

    recovery = recovery or ProxyRecovery()
    record = recovery.load()
    if record is None:
        return {"action": "none", "detail": "无恢复记录"}

    record_pid = record.get("pid")
    if record_pid and int(record_pid) != os.getpid() and pid_alive(record_pid):
        return {
            "action": "skipped",
            "detail": f"恢复记录所属进程（pid={record_pid}）仍在运行，可能另有活动会话",
        }

    backend = backend or WindowsProxyBackend()
    current = backend.snapshot()
    current_proxy = str(current.get("ProxyServer") or "")
    current_enabled = bool(current.get("ProxyEnable"))
    expected_proxy = str(record.get("proxy") or "")

    applied = record.get("applied")
    owned = (
        proxy_snapshot_matches(current, applied) if isinstance(applied, dict)
        else current_enabled and current_proxy == expected_proxy
    )
    if owned:
        previous = record.get("previous") or {}
        backend.write(previous)
        backend.notify()
        recovery.clear()
        return {
            "action": "restored",
            "detail": f"已还原系统代理（清除残留的 {expected_proxy}）",
        }

    recovery.clear()
    return {
        "action": "kept",
        "detail": (
            f"当前系统代理（{'已开启: ' + current_proxy if current_enabled else '已关闭'}）"
            f"与程序残留记录（{expected_proxy}）不符，判定为用户手动设置，保持不变"
        ),
    }
