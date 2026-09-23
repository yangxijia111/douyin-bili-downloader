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
]

RECOVERY_FILENAME = "proxy_recovery_state.json"
STATE_VERSION = 1

# 恢复记录与注册表键的标准字段。
_PROXY_FIELDS = ("ProxyEnable", "ProxyServer", "ProxyOverride")


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
    """HKCU ``Internet Settings`` 读写 + WinINET 刷新通知。

    独立成类便于测试注入替身（绝不为了测试去真的改注册表）。
    """

    KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

    def snapshot(self) -> Dict[str, Any]:
        import winreg

        values: Dict[str, Any] = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY_PATH) as key:
            for name in _PROXY_FIELDS:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                    values[name] = value
                except OSError:
                    values[name] = None
        return values

    def write(self, values: Dict[str, Any]) -> None:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self.KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            for name in _PROXY_FIELDS:
                value = values.get(name)
                if value is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except OSError:
                        pass
                    continue
                reg_type = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, reg_type, value)

    @staticmethod
    def notify() -> None:
        import ctypes

        internet_option_settings_changed = 39
        internet_option_refresh = 37
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, internet_option_settings_changed, 0, 0)
        wininet.InternetSetOptionW(0, internet_option_refresh, 0, 0)


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
        record = {
            "version": STATE_VERSION,
            "session_id": uuid.uuid4().hex,
            "pid": os.getpid(),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "previous": {name: previous.get(name) for name in _PROXY_FIELDS},
            "proxy": proxy,
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
        return record

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and data.get("version") == STATE_VERSION:
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

    if current_enabled and current_proxy == expected_proxy:
        previous = record.get("previous") or {}
        backend.write({name: previous.get(name) for name in _PROXY_FIELDS})
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
