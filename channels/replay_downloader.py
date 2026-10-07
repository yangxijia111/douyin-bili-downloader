"""Legacy experimental replay downloader, no longer wired into CLI/Server.

MSE fMP4 and the CDN MP4 do not share container offsets or sample tables.
XORing an arbitrary MSE prefix with the ciphertext and XORing back merely
replaces that prefix; it is not a decryption method. Finalization therefore
refuses all unverified prefixes. Use a captured decodeKey with the normal
ChannelsDownloader, or save a complete MSE capture instead.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from channels.diagnostics import ChannelsDiagnostics
from channels.feed import ChannelFeed
from channels.feed_store import FeedStore
from utils.logger import setup_logger

logger = setup_logger("ChannelsReplay")

__all__ = [
    "ReplayDownloadManager",
    "REPLAY_MIN_TOTAL_BYTES",
    "REPLAY_TRIGGER_SEGMENTS",
    "ISAAC_ENCRYPTED_HEAD_BYTES",
]

# 判定「长视频/直播回放」的门槛：同 URL 分段数与总大小。普通短视频
# （<20MB）不会触发；99MB 回放播放十几秒即超过 3 段 + 30MB。
REPLAY_MIN_TOTAL_BYTES = 30 * 1024 * 1024
REPLAY_TRIGGER_SEGMENTS = 3
# 全量下载的分段大小（4MB；与 MSE 上传批次的数量级一致）。
REPLAY_SEGMENT_BYTES = 4 * 1024 * 1024
# ISAAC64 头部加密的字节范围（与 isaac64 模块的既有实证一致）。
ISAAC_ENCRYPTED_HEAD_BYTES = 131072
# 密文 .part 的保留时长（等待 MSE 明文头期间；过期清理防堆积）。
_PENDING_TTL_SECONDS = 30 * 60


def _encfilekey(url: str) -> str:
    """从 stodownload URL 提取 encfilekey（同视频跨 token 变化的稳定键）。"""
    try:
        qs = parse_qs(urlsplit(url).query)
        values = qs.get("encfilekey") or qs.get("fileid")
        return str(values[0]) if values else ""
    except Exception:  # noqa: BLE001 —— 解析失败按空键处理
        return ""


class _PendingReplay:
    """一份已下载完成的密文 .part，等待 MSE 明文头解密。"""

    __slots__ = ("key", "url", "total", "part", "ready_at")

    def __init__(self, key: str, url: str, total: int, part: Path) -> None:
        self.key = key
        self.url = url
        self.total = total
        self.part = part
        self.ready_at = time.time()


class ReplayDownloadManager:
    """长视频全量下载：代理检测 → 密文全量拉取 → MSE 明文头解密拼装。

    ``consider()`` 在 mitmproxy 事件循环线程同步调用（纯计数判断）；
    下载与拼装是后台任务。单事件循环内使用，无线锁。
    """

    def __init__(
        self,
        feed_store: FeedStore,
        diagnostics: Optional[ChannelsDiagnostics] = None,
        *,
        downloader: Any = None,
        stats: Any = None,
        temp_dir: Optional[Path] = None,
        min_total_bytes: int = REPLAY_MIN_TOTAL_BYTES,
        trigger_segments: int = REPLAY_TRIGGER_SEGMENTS,
    ) -> None:
        self.feed_store = feed_store
        self.diagnostics = diagnostics or ChannelsDiagnostics()
        self._downloader = downloader  # ChannelsDownloader（命名上下文+历史登记）
        self._stats = stats
        self._temp_dir = Path(temp_dir) if temp_dir else Path("logs") / "channels_replay"
        self._min_total = int(min_total_bytes)
        self._trigger_segments = int(trigger_segments)
        # encfilekey → {segments, total, url, triggered}
        self._seen: Dict[str, Dict[str, Any]] = {}
        self._pending: Dict[str, _PendingReplay] = {}
        self._completed: set = set()
        # 最近保存的 MSE 明文文件（on_mse_saved 时缓存，密文晚到时回捞；
        # 有界 8 份，拿错文件由 ftyp 校验拒绝）。
        self._recent_plain: list = []
        # 进行中的拼装任务（同 key 去重 + create_task 引用防 GC）。
        self._finalizing: set = set()
        self._tasks: set = set()

    # ------------------------------------------------------------------
    # 检测（SnifferAddon 同步调用）
    # ------------------------------------------------------------------

    def consider(self, url: str, total_bytes: int) -> bool:
        """记录一次分段请求；判定为长视频且未处理过时返回 True。

        返回 True 表示调用方应启动 ``download()``（一次性触发，同键去重）。
        """
        try:
            key = _encfilekey(url) or url
            rec = self._seen.get(key)
            if rec is None:
                rec = {"segments": 0, "total": 0, "url": url, "triggered": False}
                self._seen[key] = rec
                # 有界保护：极端多视频会话防无界增长。
                if len(self._seen) > 128:
                    for stale in list(self._seen)[: len(self._seen) - 128]:
                        if not self._seen[stale].get("triggered"):
                            del self._seen[stale]
            rec["segments"] += 1
            if total_bytes > rec["total"]:
                rec["total"] = total_bytes
            rec["url"] = url  # 保留最新（token 可能被刷新）
            if rec["triggered"] or key in self._completed:
                return False
            if rec["segments"] >= self._trigger_segments and rec["total"] >= self._min_total:
                rec["triggered"] = True
                return True
            return False
        except Exception:  # noqa: BLE001 —— 检测异常不影响代理
            return False

    # ------------------------------------------------------------------
    # 阶段 1：密文全量下载
    # ------------------------------------------------------------------

    async def download(self, url: str, total_bytes: int) -> None:
        """后台全量拉取（Range 4MB 分段直连 CDN），完成后挂入 pending。"""
        key = _encfilekey(url) or url
        if key in self._completed or key in self._pending:
            return
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        part = self._temp_dir / f"replay_{key[:24]}_{int(time.time())}.part"
        try:
            received = await self._download_all(url, total_bytes, part)
        except Exception as exc:  # noqa: BLE001 —— 后台任务自身兜底
            logger.warning("直播回放全量下载失败: %s", exc)
            self.diagnostics.record_parse_error(f"replay_download: {exc}")
            part.unlink(missing_ok=True)
            return
        expected = int(total_bytes or 0)
        if expected and received != expected:
            logger.warning(
                "直播回放下载不完整（%d/%d 字节），已丢弃", received, expected
            )
            part.unlink(missing_ok=True)
            self.diagnostics.record_parse_error(
                f"replay_incomplete: {received}/{expected}"
            )
            return
        self._pending[key] = _PendingReplay(key, url, received, part)
        self._evict_stale()
        logger.info(
            "直播回放密文已就绪（%.1f MB），等待播放分片明文头解密",
            received / 1048576,
        )
        # MSE 明文可能早已落盘（用户秒切，MSE 保存早于密文下载完成）：
        # 用缓存的最近明文逐个尝试（拿错文件由 ftyp 校验拒绝）。
        for plain in list(self._recent_plain):
            self._schedule_finalize(key, plain)

    async def _download_all(self, url: str, total: int, part: Path) -> int:
        import aiohttp

        received = 0
        timeout = aiohttp.ClientTimeout(total=60 * 60, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 先不带 Range 试探：服务器可能直接给 200 全量。
            offset = 0
            async with session.get(url) as resp:
                if resp.status not in (200, 206):
                    raise RuntimeError(f"CDN 响应 {resp.status}")
                crange = resp.headers.get("Content-Range") or ""
                match = re.search(r"/(\d+)$", crange)
                real_total = int(match.group(1)) if match else total
                with open(part, "wb") as handle:
                    async for chunk in resp.content.iter_chunked(1024 * 512):
                        handle.write(chunk)
                        received += len(chunk)
                offset = received
                if real_total and received >= real_total:
                    return received
            # 服务器只给分段（206 每次一段）：循环 Range 直到取完。
            while real_total and offset < real_total:
                end = min(offset + REPLAY_SEGMENT_BYTES, real_total) - 1
                headers = {"Range": f"bytes={offset}-{end}"}
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 206:
                        raise RuntimeError(f"分段响应 {resp.status}（offset={offset}）")
                    with open(part, "ab") as handle:
                        async for chunk in resp.content.iter_chunked(1024 * 512):
                            handle.write(chunk)
                            received += len(chunk)
                offset = received
        return received

    # ------------------------------------------------------------------
    # 阶段 2：MSE 明文头 → 密钥流 → 解密拼装落盘
    # ------------------------------------------------------------------

    def on_mse_saved(self, mse_path: Path) -> None:
        """MSE 分片保存落盘后的钩子（MseCaptureStore.finalize 调用）。

        每份新 MSE 明文都可能补齐 pending 的密钥流。拼装含大文件拷贝，
        必须异步执行（调用方在 mitmproxy 事件循环线程）。明文可能早于
        密文就绪（用户秒切后 MSE 先落盘）——同时缓存供 download 回捞。
        """
        try:
            path = Path(mse_path)
            if path in self._recent_plain:
                self._recent_plain.remove(path)
            self._recent_plain.insert(0, path)
            del self._recent_plain[8:]
        except Exception:  # noqa: BLE001
            pass
        for key in list(self._pending):
            self._schedule_finalize(key, mse_path)

    def _schedule_finalize(self, key: str, mse_path: Optional[Path] = None) -> None:
        if key in self._finalizing:
            return
        self._finalizing.add(key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无事件循环（离线单测）：同步降级执行。
            try:
                self._finalize(key, mse_path)
            finally:
                self._finalizing.discard(key)
            return

        async def runner() -> None:
            try:
                # 拼装含大文件拷贝与 128KB XOR，放线程池不阻塞事件循环。
                await asyncio.to_thread(self._finalize, key, mse_path)
            finally:
                self._finalizing.discard(key)

        task = loop.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _finalize(self, key: str, mse_path: Optional[Path]) -> None:
        pending = self._pending.get(key)
        if pending is None:
            return
        if mse_path is not None:
            candidates = [mse_path]
        else:
            # 触发时未提供新明文：没有可尝试的来源，继续等待。
            candidates = []
        for plain_path in candidates:
            try:
                if self._finalize_with(pending, plain_path):
                    return
            except Exception as exc:  # noqa: BLE001
                logger.warning("回放解密拼装失败（%s）: %s", plain_path, exc)
        # 没有合适数据：保留 .part 等下一次 MSE 保存（TTL 内）。

    def _finalize_with(self, pending: _PendingReplay, plain_path: Path) -> bool:
        # cipher XOR (plain XOR cipher) == plain: the previous expression was
        # merely overwriting the encrypted prefix, not validating/decrypting it.
        # A remuxed MSE init segment has different sample tables and offsets from
        # the CDN MP4. Even an ftyp match cannot establish identity or alignment.
        # Preserve the pending source; only the normal decodeKey downloader or
        # a complete MSE capture can produce a verified file.
        self.diagnostics.record_parse_error(
            "回放缺少可验证的 decodeKey，不能用播放分片头替换原文件头；"
            "请从开头连续播放至完整缓冲后保存"
        )
        return False

    @staticmethod
    def _read_range(path: Path, start: int, length: int) -> bytes:
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                return handle.read(length)
        except OSError:
            return b""

    @staticmethod
    def _read_head_prefix(path: Path, length: int) -> bytes:
        try:
            with open(path, "rb") as handle:
                return handle.read(length)
        except OSError:
            return b""

    def _place(self, pending: _PendingReplay, decrypted_head: bytes) -> Path:
        """按命名模板落盘完整文件（解密头 + 明文余段）。"""
        feed = ChannelFeed(
            object_id=f"replay-{pending.key[:32]}-{int(time.time())}",
            nonce_id=f"replay-{pending.key[:32]}-{int(time.time())}",
            media_type=4,
            kind="video",
            title=f"直播回放 {time.strftime('%Y%m%d-%H%M%S')}",
            author_name="",
            duration=0,
            file_size=pending.total,
            source_api="replay_full_download",
        )
        save_dir = None
        stem = None
        if self._downloader is not None and hasattr(self._downloader, "build_item_context"):
            try:
                context = self._downloader.build_item_context(feed)
                save_dir = Path(context["save_dir"])
                stem = context["file_stem"]
            except Exception:  # noqa: BLE001 —— 命名失败回退临时目录
                save_dir = stem = None
        if save_dir is None or stem is None:
            save_dir = self._temp_dir / "saved"
            stem = feed.token
        save_dir.mkdir(parents=True, exist_ok=True)
        target = save_dir / f"{stem}.mp4"
        seq = 1
        while target.exists():
            target = save_dir / f"{stem}_{seq}.mp4"
            seq += 1
        with open(target, "wb") as out:
            out.write(decrypted_head)
            with open(pending.part, "rb") as src:
                src.seek(len(decrypted_head))
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        feed.downloaded_paths = [str(target)]
        feed.status = "done"
        fresh = self.feed_store.add([feed])
        self.diagnostics.record_feeds(len(fresh), strategy="replay_full_download")
        self.diagnostics.incr("replay_downloads")
        if self._stats is not None and fresh:
            try:
                self._stats.success += 1
                self._stats.total += 1
            except Exception:  # noqa: BLE001
                pass
        if self._downloader is not None and hasattr(self._downloader, "register_downloaded_file"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._downloader.register_downloaded_file(
                        feed, [target], "channels_video"
                    )
                )
            except RuntimeError:
                pass
        return target

    def _mark_done(self, pending: _PendingReplay, target: Path) -> None:
        self._completed.add(pending.key)
        self._pending.pop(pending.key, None)
        try:
            pending.part.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("直播回放已完整落盘: %s", target)

    def _evict_stale(self) -> None:
        now = time.time()
        for key, pending in list(self._pending.items()):
            if now - pending.ready_at > _PENDING_TTL_SECONDS:
                try:
                    pending.part.unlink(missing_ok=True)
                except OSError:
                    pass
                self._pending.pop(key, None)
                logger.info("回放密文等待明文超时，已清理: %s", key[:24])
