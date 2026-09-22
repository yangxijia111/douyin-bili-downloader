"""视频号流量嗅探引擎：mitmproxy 嵌入 + 根证书 + 系统代理。

工作方式（与 ltaoo/wx_channels_download 的根本差异）：

* 原项目要在微信页面里画下载按钮，必须用约 30 个正则**改写**微信前端 JS
  源码（``res.wx.qq.com`` 的 bundle），微信一改版即失效；
* 本项目不注入任何脚本——``WeChatAppEx.exe``（微信内嵌 Chromium）遵循系统
  代理，我们把 mitmproxy 挂上去做 HTTPS 中间人，**被动读** API 响应里的
  feed 数据（含 ``decodeKey``，见 :mod:`channels.feed`）。页面上没有任何
  痕迹，对微信前端改版零依赖。

嗅探会话的完整编排（CLI ``--channels`` 与 Server 端点共用）::

    with SystemProxyManager() as proxy:           # ① 开系统代理（退出恢复）
        cert = CertificateManager().ensure_ca()   # ② 每机唯一 CA
        async with ChannelsInterceptor(store) as interceptor:
            ...                                    # ③ 用户在微信里刷视频，
                                                   #    feed 落入 FeedStore

安全边界：CA 私钥由 mitmproxy 在本机 ``~/.mitmproxy`` 生成（每机唯一，不
是原项目那种全体用户共享内置证书）；证书只装入当前用户的 Root 存储（安装
时 Windows 会弹确认框，用户点「否」即中止）；系统代理仅会话期间生效，
``try/finally`` 保证恢复。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from channels.feed import ChannelFeed, extract_feeds
from channels.feed_store import FeedStore
from utils.logger import setup_logger

logger = setup_logger("ChannelsInterceptor")

__all__ = [
    "MITMPROXY_INSTALL_HINT",
    "CertificateManager",
    "ChannelsInterceptorError",
    "ChannelsInterceptor",
    "SnifferAddon",
    "SystemProxyError",
    "SystemProxyManager",
    "mitmproxy_available",
]

MITMPROXY_INSTALL_HINT = '视频号嗅探依赖 mitmproxy，请先安装：pip install ".[channels]"'


def mitmproxy_available() -> bool:
    try:
        import mitmproxy  # noqa: F401
    except ImportError:
        return False
    return True


# ----------------------------------------------------------------------
# 根证书
# ----------------------------------------------------------------------

class CertificateManager:
    """mitmproxy 根证书的生成 / 检测 / 安装（Windows 用户存储）。

    mitmproxy 首次使用时在 ``confdir``（默认 ``~/.mitmproxy``）生成自签 CA。
    HTTPS 中间人要求微信的内嵌 Chromium 信任这张 CA：Windows 上装到当前用户
    的 Root 存储（``certutil -addstore -user``，会弹系统确认框，无需管理员；
    用户拒绝则返回失败，调用方给手动安装指引）。
    """

    def __init__(self, confdir: Optional[Path] = None):
        self.confdir = Path(confdir) if confdir else Path.home() / ".mitmproxy"

    @property
    def ca_cert_path(self) -> Path:
        return self.confdir / "mitmproxy-ca-cert.cer"

    def ensure_ca(self) -> Path:
        """确保 CA 存在（幂等），返回证书路径。"""
        if self.ca_cert_path.exists():
            return self.ca_cert_path
        try:
            from mitmproxy.certs import CertStore
        except ImportError as exc:  # pragma: no cover - 环境缺依赖
            raise RuntimeError(MITMPROXY_INSTALL_HINT) from exc
        self.confdir.mkdir(parents=True, exist_ok=True)
        CertStore.from_store(self.confdir, "mitmproxy", 2048)
        if not self.ca_cert_path.exists():  # pragma: no cover - mitmproxy 行为变化
            raise RuntimeError("mitmproxy 未按预期生成 CA 证书")
        return self.ca_cert_path

    def fingerprint_sha1(self) -> Optional[str]:
        """CA 证书的 SHA-1 指纹（hex），用于与证书存储比对。"""
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
            return cert.fingerprint(hashes.SHA1()).hex()
        except Exception as exc:
            logger.warning("读取 CA 指纹失败: %s", exc)
            return None

    def _store_output(self, args: List[str]) -> str:
        # certutil 在中文 Windows 上输出 GBK；指纹是纯 ASCII，容错解码即可。
        proc = subprocess.run(
            ["certutil", *args],
            capture_output=True,
            timeout=15,
        )
        text = (proc.stdout or b"").decode("utf-8", errors="ignore")
        text += (proc.stderr or b"").decode("utf-8", errors="ignore")
        return text.lower()

    def is_installed(self) -> bool:
        """CA 是否已在 Windows 证书存储（先查当前用户 Root，再查机器 Root）。"""
        if sys.platform != "win32":
            return False
        fingerprint = self.fingerprint_sha1()
        if not fingerprint or not self.ca_cert_path.exists():
            return False
        try:
            if fingerprint in self._store_output(["-user", "-store", "Root"]):
                return True
            if fingerprint in self._store_output(["-store", "Root"]):
                return True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("查询证书存储失败: %s", exc)
            return False
        return False

    def install(self) -> tuple:
        """把 CA 装入当前用户 Root 存储（Windows 弹确认框，用户点「是」）。"""
        if sys.platform != "win32":
            raise RuntimeError(
                "自动安装证书仅支持 Windows；macOS/Linux 请手动信任 "
                f"{self.ca_cert_path}"
            )
        cert = self.ensure_ca()
        try:
            proc = subprocess.run(
                ["certutil", "-addstore", "-user", "Root", str(cert)],
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"certutil 执行失败: {exc}"
        ok = proc.returncode == 0
        detail = (proc.stdout or b"").decode("utf-8", errors="ignore")
        return ok, detail


# ----------------------------------------------------------------------
# 系统代理（Windows 用户级）
# ----------------------------------------------------------------------

class SystemProxyError(RuntimeError):
    pass


class SystemProxyManager:
    """Windows 用户级系统代理的设置与恢复。

    写 ``HKCU\\...\\Internet Settings`` 的 ProxyEnable/ProxyServer/ProxyOverride
    并用 WinINET 的 ``InternetSetOption`` 通知系统刷新。备份原值，:meth:`restore`
    原样还原；调用方务必 ``try/finally``（嗅探会话退出路径）。
    非本机地址（localhost 等）通过 ProxyOverride 排除，避免影响本项目的
    server / 其他本地服务。
    """

    _KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    _DEFAULT_OVERRIDE = "<local>;localhost;127.0.0.1"

    def __init__(self):
        self._backup: Optional[dict] = None

    @property
    def active(self) -> bool:
        return self._backup is not None

    # -- 注册表读写 ----------------------------------------------------------

    @staticmethod
    def _read_value(key, name: str):
        import winreg

        try:
            value, _ = winreg.QueryValueEx(key, name)
            return value
        except OSError:
            return None

    def _snapshot(self) -> dict:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._KEY_PATH) as key:
            return {
                "ProxyEnable": self._read_value(key, "ProxyEnable"),
                "ProxyServer": self._read_value(key, "ProxyServer"),
                "ProxyOverride": self._read_value(key, "ProxyOverride"),
            }

    def _write(self, values: dict) -> None:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self._KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            for name, value in values.items():
                if value is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except OSError:
                        pass
                    continue
                reg_type = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, reg_type, value)

    @staticmethod
    def _notify_wininet() -> None:
        import ctypes

        internet_option_settings_changed = 39
        internet_option_refresh = 37
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, internet_option_settings_changed, 0, 0)
        wininet.InternetSetOptionW(0, internet_option_refresh, 0, 0)

    # -- 对外接口 ------------------------------------------------------------

    def enable(self, host: str = "127.0.0.1", port: int = 8899) -> None:
        if sys.platform != "win32":
            raise SystemProxyError(
                f"自动设置系统代理仅支持 Windows；请在系统设置中手动指向 "
                f"{host}:{port}，结束后恢复。"
            )
        if self._backup is not None:
            raise SystemProxyError("系统代理已由本实例接管，不能重复 enable")
        self._backup = self._snapshot()
        self._write(
            {
                "ProxyEnable": 1,
                "ProxyServer": f"{host}:{port}",
                "ProxyOverride": self._DEFAULT_OVERRIDE,
            }
        )
        self._notify_wininet()
        logger.info("系统代理已开启 -> %s:%d", host, port)

    def restore(self) -> None:
        """还原到 enable 前的注册表状态；幂等。"""
        if self._backup is None:
            return
        backup, self._backup = self._backup, None
        try:
            self._write(backup)
            self._notify_wininet()
            logger.info("系统代理已还原")
        except OSError as exc:  # pragma: no cover - 注册表异常兜底
            logger.error("还原系统代理失败，请手动检查代理设置: %s", exc)

    def __enter__(self) -> "SystemProxyManager":
        return self

    def __exit__(self, *exc_info) -> None:
        self.restore()


# ----------------------------------------------------------------------
# mitmproxy addon 与会话
# ----------------------------------------------------------------------

class SnifferAddon:
    """mitmproxy response hook：被动提取视频号 feed。

    过滤策略宽松化以对抗微信改版：不按接口路径白名单，而是「weixin.qq.com
    域 + body 含 objectDesc 才尝试解析」。body 先做字符串预检再
    ``json.loads``，避免对无关大响应做无谓解析。
    """

    # 单响应体上限（列表类接口通常 < 2MB，超过的多半不是 feed 数据）。
    MAX_BODY_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        feed_store: FeedStore,
        on_capture: Optional[Callable[[List[ChannelFeed]], None]] = None,
    ):
        self.feed_store = feed_store
        self.on_capture = on_capture

    def response(self, flow) -> None:
        """mitmproxy hook（同步，运行在事件循环线程内）。"""
        try:
            self._handle(flow)
        except Exception as exc:  # noqa: BLE001 —— 解析失败绝不拖垮代理
            logger.warning("解析视频号响应失败 %s: %s", getattr(flow.request, "pretty_host", "?"), exc)

    def _handle(self, flow) -> None:
        request = flow.request
        host = request.pretty_host or ""
        if not (host == "channels.weixin.qq.com" or host.endswith(".weixin.qq.com")):
            return
        response = flow.response
        if response is None:
            return
        try:
            body = response.get_text(strict=False) or ""
        except ValueError:
            return
        if not body or len(body) > self.MAX_BODY_BYTES:
            return
        if "objectDesc" not in body:
            return
        import json

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return
        feeds = extract_feeds(payload, source_api=request.path)
        if not feeds:
            return
        fresh = self.feed_store.add(feeds)
        if fresh:
            logger.info(
                "捕获 %d 条视频号动态（%s，最新: %s）",
                len(fresh),
                request.path,
                fresh[0].title[:40],
            )
            if self.on_capture:
                try:
                    self.on_capture(fresh)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("on_capture 回调失败: %s", exc)


class ChannelsInterceptorError(RuntimeError):
    pass


class ChannelsInterceptor:
    """编程式启动 mitmproxy 的嗅探会话（async 上下文管理器）。

    mitmproxy 与宿主共享同一个 asyncio 事件循环（CLI 主循环 / FastAPI 的
    loop 里以 task 形式运行），addon 回调因此天然线程安全。
    """

    def __init__(
        self,
        feed_store: FeedStore,
        *,
        host: str = "127.0.0.1",
        port: int = 8899,
        cert_manager: Optional[CertificateManager] = None,
        on_capture: Optional[Callable[[List[ChannelFeed]], None]] = None,
    ):
        if not mitmproxy_available():  # pragma: no cover - 环境缺依赖
            raise RuntimeError(MITMPROXY_INSTALL_HINT)
        self.feed_store = feed_store
        self.host = host
        self.port = port
        self.cert_manager = cert_manager or CertificateManager()
        self.on_capture = on_capture
        self._master = None
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        from mitmproxy import options
        from mitmproxy.tools.dump import DumpMaster

        self.cert_manager.ensure_ca()
        opts = options.Options(
            listen_host=self.host,
            listen_port=self.port,
            confdir=str(self.cert_manager.confdir),
        )
        master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        master.addons.add(SnifferAddon(self.feed_store, on_capture=self.on_capture))
        self._master = master
        self._task = asyncio.create_task(master.run(), name="channels-mitmproxy")
        try:
            await self._wait_listening(timeout=10.0)
        except Exception:
            await self.stop()
            raise
        logger.info("嗅探代理已启动 %s:%d", self.host, self.port)

    async def _wait_listening(self, timeout: float = 10.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._task.done():
                # 启动即失败（端口占用等）：抛出真实异常。
                self._task.result()
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise ChannelsInterceptorError(
            f"代理端口 {self.host}:{self.port} 在 {timeout}s 内未就绪"
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        master, self._master = self._master, None
        if master is not None:
            try:
                master.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭 mitmproxy 异常: %s", exc)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        logger.info("嗅探代理已停止")

    async def __aenter__(self) -> "ChannelsInterceptor":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()
