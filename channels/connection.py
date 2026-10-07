"""Read-only Windows process/TCP evidence for the Channels connection status.

Proxy connection totals include browsers and other apps. Inspect the owning PID
so the console never treats those connections as evidence that WeChat is routed
through the sniffer. The probe neither writes settings nor restarts WeChat.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import sys
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

_WECHAT_NAMES = frozenset(("wechat.exe", "weixin.exe", "wechatappex.exe"))


def summarize_connections(
    processes: Mapping[int, str],
    connections: Iterable[Tuple[int, str, int]],
    port: int,
) -> Dict[str, Any]:
    wechat_pids = {pid for pid, name in processes.items() if name.lower() in _WECHAT_NAMES}
    connected = False
    other_ports = set()
    active = 0
    for pid, remote, remote_port in connections:
        if pid not in wechat_pids:
            continue
        active += 1
        try:
            address = ipaddress.ip_address(remote)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            loopback = address.is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            continue
        if remote_port == port:
            connected = True
        else:
            other_ports.add(remote_port)
    return {
        "supported": True,
        "wechat_running": bool(wechat_pids),
        "wechat_processes": len(wechat_pids),
        "proxy_connected": connected,
        "other_proxy_ports": sorted(other_ports)[:8],
        "active_connections": active,
    }


def _windows_processes() -> Dict[int, str]:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    processes = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(handle, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            processes[int(entry.th32ProcessID)] = entry.szExeFile
            if not kernel.Process32NextW(handle, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise ctypes.WinError(ctypes.get_last_error())
                break
    finally:
        kernel.CloseHandle(handle)
    return processes


def _windows_tcp_connections() -> Iterable[Tuple[int, str, int]]:
    import ctypes
    import socket
    from ctypes import wintypes

    class Row4(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            "state", "local_addr", "local_port", "remote_addr", "remote_port", "pid",
        )]

    class Row6(ctypes.Structure):
        _fields_ = [
            ("local_addr", ctypes.c_ubyte * 16), ("local_scope", wintypes.DWORD),
            ("local_port", wintypes.DWORD), ("remote_addr", ctypes.c_ubyte * 16),
            ("remote_scope", wintypes.DWORD), ("remote_port", wintypes.DWORD),
            ("state", wintypes.DWORD), ("pid", wintypes.DWORD),
        ]

    api = ctypes.WinDLL("iphlpapi", use_last_error=True)
    api.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        wintypes.ULONG, ctypes.c_int, wintypes.ULONG,
    ]
    api.GetExtendedTcpTable.restype = wintypes.DWORD
    for family, row_type in ((socket.AF_INET, Row4), (socket.AF_INET6, Row6)):
        size = wintypes.DWORD()
        result = api.GetExtendedTcpTable(None, ctypes.byref(size), False, family, 5, 0)
        if result not in (0, 122):  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(result)
        for _attempt in range(3):
            buffer = ctypes.create_string_buffer(size.value)
            result = api.GetExtendedTcpTable(
                buffer, ctypes.byref(size), False, family, 5, 0,
            )
            if result != 122:
                break
        if result:
            raise ctypes.WinError(result)
        count = wintypes.DWORD.from_buffer(buffer).value
        row_size = ctypes.sizeof(row_type)
        if 4 + count * row_size > len(buffer):
            raise OSError("Invalid TCP table size")
        for index in range(count):
            row = row_type.from_buffer_copy(buffer, 4 + index * row_size)
            if row.state != 5:  # MIB_TCP_STATE_ESTAB
                continue
            packed = (row.remote_addr.to_bytes(4, "little")
                      if family == socket.AF_INET else bytes(row.remote_addr))
            remote = socket.inet_ntop(family, packed)
            yield int(row.pid), remote, socket.ntohs(row.remote_port & 0xFFFF)


def read_connection_state(port: int) -> Dict[str, Any]:
    if sys.platform != "win32":
        return {"supported": False, "wechat_running": None, "proxy_connected": None}
    state = summarize_connections(_windows_processes(), _windows_tcp_connections(), port)
    from channels.proxy_recovery import WindowsProxyBackend

    backend = WindowsProxyBackend()
    query = getattr(backend, "effective_proxy", None)
    if callable(query):
        try:
            proxy = query()
            server = proxy.get("server") or ""
            expected = "127.0.0.1:%s" % port
            parts = {item.strip().lower() for item in server.split(";") if item.strip()}
            matches = expected in parts or (
                "http=" + expected in parts and "https=" + expected in parts
            )
            state["connection_proxy_matches"] = bool(proxy.get("enabled") and matches)
            state["connection_proxy_ports"] = sorted({
                int(match) for match in re.findall(r"(?:127\.0\.0\.1|localhost):(\d+)", server)
            })[:8]
        except OSError:
            state["connection_proxy_matches"] = None
    return state


class ConnectionMonitor:
    """A cached, single-flight probe; status polling never blocks on Windows APIs."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._port: Optional[int] = None
        self._last_check = 0.0
        self._state: Dict[str, Any] = {
            "supported": sys.platform == "win32", "wechat_running": None,
            "proxy_connected": None, "checking": False,
        }

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._state)

    def refresh(self, port: int) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._port == port and time.monotonic() - self._last_check < 5.0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._port = port
        self._state["checking"] = True
        self._task = loop.create_task(self._probe(port), name="channels-connection-check")

    async def _probe(self, port: int) -> None:
        try:
            state = await asyncio.get_running_loop().run_in_executor(
                None, read_connection_state, port,
            )
        except OSError:
            state = {"supported": sys.platform == "win32",
                     "wechat_running": None, "proxy_connected": None}
        self._state = dict(state, checking=False, checked_at=time.time())
        self._last_check = time.monotonic()

    async def wait_idle(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._port = None
        self._state = {"supported": sys.platform == "win32", "wechat_running": None,
                       "proxy_connected": None, "checking": False}
