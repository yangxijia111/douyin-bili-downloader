"""Server 端视频号嗅探会话管理。

CLI 的 ``--channels`` 是独占前台会话；Server 形态下嗅探会话由 REST 端点控制
启停（网页控制台「视频号」页），进程退出时 FastAPI lifespan 兜底停止并还原
系统代理。

端点（在 ``server.app.build_app`` 里挂载）::

    GET  /api/v1/channels/status                     会话与证书状态
    GET  /api/v1/channels/certificate                证书详情（指纹/subject/有效期）
    POST /api/v1/channels/certificate/install        安装证书（弹 Windows 确认框）
    POST /api/v1/channels/certificate/uninstall      卸载证书（按本机 CA 指纹精确删除）
    POST /api/v1/channels/start                      启动嗅探（可带 port/auto_download）
    POST /api/v1/channels/stop                       停止嗅探并还原系统代理
    GET  /api/v1/channels/feeds                      捕获列表（最新在前，脱敏视图）
    POST /api/v1/channels/feeds/{feed_id}/download   手动下载/录制单条
    POST /api/v1/channels/auto-download              运行中开关自动下载
    POST /api/v1/channels/network/repair             恢复异常退出残留的系统代理

手动下载与自动下载共用一个 :class:`~channels.downloader.ChannelsDownloader`
实例（连接池共享）；手动下载以后台 task 执行，前端通过轮询 feeds 列表里的
``status`` 观察进度（与 job 中心不同，这里条目多、粒度细，走轻量路径）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Set

from channels.feed_store import FeedStore
from channels.interceptor import (
    CertificateManager,
    ChannelsInterceptor,
    SystemProxyManager,
    mitmproxy_available,
)
from config import ConfigLoader
from core.downloader_base import DownloadResult
from storage import Database, FileManager
from utils.logger import setup_logger

logger = setup_logger("ChannelsSessionManager")

__all__ = ["ChannelsSessionError", "ChannelsSessionManager"]


class ChannelsSessionError(RuntimeError):
    """会话操作失败（依赖缺失 / 证书未信任 / 端口占用等）。"""


class ChannelsSessionManager:
    """单例嗅探会话；所有方法并发安全（内部 asyncio.Lock）。"""

    def __init__(self, config: ConfigLoader, file_manager: FileManager):
        self.config = config
        self.file_manager = file_manager
        # 惰性锁（py<=3.10 构造期急切绑定事件循环，见 control/queue_manager）。
        self._lock: Optional[asyncio.Lock] = None
        self._running: Optional[Dict[str, Any]] = None
        self._manual_tasks: Set[asyncio.Task] = set()
        self._cert_manager = CertificateManager()

    @property
    def _start_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def _section(self) -> Dict[str, Any]:
        section = self.config.get("channels")
        return section if isinstance(section, dict) else {}

    def status(self) -> Dict[str, Any]:
        cert_exists = self._cert_manager.ca_cert_path.exists()
        payload: Dict[str, Any] = {
            "running": self._running is not None,
            "mitmproxy_available": mitmproxy_available(),
            "enabled": bool(self._section().get("enabled", True)),
            # 证书详情（含路径 / 指纹 / 有效期）只经
            # GET /api/v1/channels/certificate 按需提供，状态轮询不需要。
            "certificate": {
                "exists": cert_exists,
                "installed": cert_exists and self._cert_manager.is_installed(),
            },
            "auto_download": bool(self._section().get("auto_download", True)),
        }
        if self._running is not None:
            stats: DownloadResult = self._running["stats"]
            payload.update(
                {
                    "port": self._running["port"],
                    "auto_download": self._running["auto"],
                    "feeds_total": len(self._running["store"]),
                    "stats": {
                        "total": stats.total,
                        "success": stats.success,
                        "failed": stats.failed,
                        "skipped": stats.skipped,
                    },
                }
            )
        return payload

    def feeds(self, *, limit: int = 200) -> Dict[str, Any]:
        """捕获列表（最新在前）+ 会话统计。

        只返回 :meth:`ChannelFeed.to_public_dict` 脱敏视图：直链、decodeKey
        等下载敏感字段不出进程。手动下载端点用 feed_id 在服务端取回完整
        对象，前端无需直链。
        """
        if self._running is None:
            return {"running": False, "feeds": []}
        store: FeedStore = self._running["store"]
        stats: DownloadResult = self._running["stats"]
        items = [feed.to_public_dict() for feed in reversed(store.all())]
        return {
            "running": True,
            "auto_download": self._running["auto"],
            "stats": {
                "total": stats.total,
                "success": stats.success,
                "failed": stats.failed,
                "skipped": stats.skipped,
            },
            "feeds": items[: max(1, int(limit))],
            "total": len(items),
        }

    # ------------------------------------------------------------------
    # 证书
    # ------------------------------------------------------------------

    async def install_certificate(self) -> Dict[str, Any]:
        if not mitmproxy_available():
            raise ChannelsSessionError(
                '视频号嗅探依赖 mitmproxy，请先安装：pip install ".[channels]"'
            )
        try:
            self._cert_manager.ensure_ca()
        except RuntimeError as exc:
            raise ChannelsSessionError(str(exc)) from exc
        ok, detail = self._cert_manager.install()
        if not ok:
            raise ChannelsSessionError(
                "证书安装未完成（可能在确认框点了「否」，或非 Windows 环境）。"
                f"详情：{str(detail)[-300:]}"
            )
        return {"ok": True}

    def certificate(self) -> Dict[str, Any]:
        """证书完整状态（生成/信任/指纹/路径/subject/有效期）。"""
        info = self._cert_manager.certificate_info()
        info["mitmproxy_available"] = mitmproxy_available()
        return info

    async def uninstall_certificate(self) -> Dict[str, Any]:
        ok, detail = self._cert_manager.uninstall()
        if not ok:
            raise ChannelsSessionError(detail)
        return {"ok": True, "detail": detail}

    def repair_network(self) -> Dict[str, Any]:
        """恢复上次异常退出残留的系统代理（CLI --repair-network 共用逻辑）。"""
        from channels.proxy_recovery import recover_stale_proxy

        return recover_stale_proxy()

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        port: Optional[int] = None,
        auto_download: Optional[bool] = None,
        database: Optional[Database] = None,
    ) -> Dict[str, Any]:
        async with self._start_lock:
            if self._running is not None:
                return self.status()
            if not mitmproxy_available():
                raise ChannelsSessionError(
                    '视频号嗅探依赖 mitmproxy，请先安装：pip install ".[channels]"'
                )
            if not bool(self._section().get("enabled", True)):
                raise ChannelsSessionError("channels.enabled 已关闭，请先在配置里开启")

            listen_port = int(
                port or self._section().get("proxy_port", 8899) or 8899
            )
            auto = bool(
                auto_download
                if auto_download is not None
                else self._section().get("auto_download", True)
            )
            live_record = bool(self._section().get("live_record", False))
            # MITM 解密白名单扩展（默认只有 weixin.qq.com，见 channels.domains）。
            extra_domains = self._section().get("intercept_domains")
            if not isinstance(extra_domains, (list, tuple)):
                extra_domains = ()

            self._cert_manager.ensure_ca()
            if not self._cert_manager.is_installed():
                raise ChannelsSessionError(
                    "嗅探根证书尚未受信任：请先调用 "
                    "POST /api/v1/channels/certificate/install（会弹 Windows 确认框），"
                    "或在「视频号」页点击「安装证书」。"
                )

            # 先处理上次异常退出可能残留的系统代理（不能覆盖用户新设置）。
            from channels.proxy_recovery import recover_stale_proxy

            recovery_report = recover_stale_proxy()
            if recovery_report["action"] == "restored":
                logger.warning("已自动恢复上次异常退出残留的系统代理: %s", recovery_report["detail"])

            store = FeedStore()
            interceptor = ChannelsInterceptor(
                store, port=listen_port, cert_manager=self._cert_manager,
                extra_domains=extra_domains,
            )
            try:
                await interceptor.start()
            except Exception as exc:  # noqa: BLE001 —— 端口占用等
                raise ChannelsSessionError(f"嗅探代理启动失败: {exc}") from exc

            proxy_manager = SystemProxyManager()
            try:
                proxy_manager.enable(port=listen_port)
            except Exception as exc:  # noqa: BLE001
                await interceptor.stop()
                raise ChannelsSessionError(f"系统代理设置失败: {exc}") from exc

            from channels.downloader import ChannelsDownloader
            from channels.worker import run_auto_download_worker

            stats = DownloadResult()
            downloader = ChannelsDownloader(
                self.config, self.file_manager, database=database
            )
            holder: Dict[str, Any] = {
                "store": store,
                "interceptor": interceptor,
                "proxy": proxy_manager,
                "downloader": downloader,
                "stats": stats,
                "port": listen_port,
                "auto": auto,
                "live_record": live_record,
                "worker": None,
            }
            holder["worker"] = asyncio.create_task(
                run_auto_download_worker(
                    store,
                    downloader,
                    live_record=live_record,
                    stats=stats,
                    should_run=lambda: bool(holder["auto"]),
                ),
                name="channels-auto-download",
            )
            self._running = holder
            logger.info("视频号嗅探会话已启动（端口 %d，自动下载 %s）", listen_port, auto)
            return self.status()

    async def stop(self) -> Dict[str, Any]:
        async with self._start_lock:
            holder, self._running = self._running, None
        if holder is None:
            return self.status()
        for task in self._manual_tasks:
            task.cancel()
        if self._manual_tasks:
            await asyncio.gather(*self._manual_tasks, return_exceptions=True)
        self._manual_tasks.clear()
        worker: Optional[asyncio.Task] = holder.get("worker")
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        try:
            await holder["interceptor"].stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止嗅探代理异常: %s", exc)
        holder["proxy"].restore()
        try:
            await holder["downloader"].aclose()
        except Exception:  # noqa: BLE001
            pass
        logger.info("视频号嗅探会话已停止，系统代理已还原")
        return self.status()

    # ------------------------------------------------------------------
    # 条目操作
    # ------------------------------------------------------------------

    async def set_auto_download(self, enabled: bool) -> Dict[str, Any]:
        if self._running is None:
            raise ChannelsSessionError("嗅探会话未运行")
        self._running["auto"] = bool(enabled)
        return self.status()

    async def download_feed(self, feed_id: str) -> Dict[str, Any]:
        """手动下载（或录制）单条；返回排队状态，进度看 feeds 列表。"""
        if self._running is None:
            raise ChannelsSessionError("嗅探会话未运行")
        store: FeedStore = self._running["store"]
        feed = store.get(feed_id)
        if feed is None:
            raise KeyError(feed_id)
        if feed.status in ("downloading", "done"):
            return {"feed_id": feed_id, "status": feed.status, "queued": False}
        store.set_status(feed, "downloading")

        async def _run() -> None:
            try:
                status = await self._running["downloader"].download_feed(feed, manual=True)
            except asyncio.CancelledError:
                store.set_status(feed, "pending")
                raise
            except Exception as exc:  # noqa: BLE001 —— 手动路径也兜底
                store.set_status(feed, "failed", error=str(exc))
                return
            store.set_status(
                feed, status, error=feed.error, downloaded_paths=feed.downloaded_paths
            )
            stats: DownloadResult = self._running["stats"]
            if status == "done":
                stats.success += 1
            elif status == "failed":
                stats.failed += 1

        task = asyncio.create_task(_run(), name=f"channels-manual-{feed_id[:16]}")
        self._manual_tasks.add(task)
        task.add_done_callback(self._manual_tasks.discard)
        return {"feed_id": feed_id, "status": "downloading", "queued": True}
