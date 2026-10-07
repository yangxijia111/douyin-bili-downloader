"""MSE 完整捕获：分轨上传、解码校验、时长核对、合并与兼容 MP4 输出。

页面必须提供从零开始的连续缓冲覆盖与完整时长；后端逐轨解码校验并
合并音视频，非 H.264 视频转为 H.264/AAC，写入 faststart MP4。
只有通过验证的文件才能进入 FeedStore 和下载历史。代理端使用
finalize_async，把 FFmpeg 工作放在线程池中，状态登记仍在事件循环中。
MSE 容器不能当作 CDN 原文件的解密头，保存后不再通知旧回放拼接器。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from channels.diagnostics import ChannelsDiagnostics
from channels.feed import ChannelFeed
from channels.feed_store import FeedStore
from channels.media_validation import MediaValidationError, prepare_mp4

__all__ = [
    "MseCaptureError",
    "MseCaptureStore",
    "MAX_STREAM_BYTES",
    "MAX_OPEN_STREAMS",
    "validate_stream_id",
]

# 单流字节上限：10 分钟 1080p（约 1.5Mbps）≈ 110MB，1GB 余量足够长视频。
MAX_STREAM_BYTES = 1024 * 1024 * 1024
# 并发打开的流上限（防泄漏；页面正常一视频 1–2 条 SourceBuffer）。
MAX_OPEN_STREAMS = 16
# stream_id 只允许字母数字（页面随机生成；同时是 objectId 的一部分）。
_STREAM_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")

# 单流陈旧淘汰（begin 后一直不上传完的流）。
_STREAM_TTL_SECONDS = 6 * 3600


def validate_stream_id(stream_id: Any) -> str:
    if not isinstance(stream_id, str) or not _STREAM_ID_RE.match(stream_id):
        raise MseCaptureError("stream_id 非法（需 8–64 位字母数字）")
    return stream_id


class MseCaptureError(RuntimeError):
    """MSE 捕获协议错误（调用方转 400 返回页面）。"""


class _MseStream:
    """一条进行中的拼装流（一个 SourceBuffer 链）。"""

    __slots__ = (
        "stream_id", "mime", "page", "path", "handle", "bytes",
        "started_at", "last_at",
    )

    def __init__(self, stream_id: str, mime: str, page: str, path: Path) -> None:
        self.stream_id = stream_id
        self.mime = mime
        self.page = page
        self.path = path
        self.handle = None  # 同步文件句柄（事件循环线程内使用）
        self.bytes = 0
        self.started_at = time.time()
        self.last_at = self.started_at


class MseCaptureStore:
    """MSE 分片拼装存储（mitmproxy 事件循环线程内同步使用）。"""

    def __init__(
        self,
        feed_store: FeedStore,
        diagnostics: Optional[ChannelsDiagnostics] = None,
        *,
        downloader: Any = None,
        temp_dir: Optional[Path] = None,
        stats: Any = None,
        auto_save: bool = True,
        replay: Any = None,
    ) -> None:
        self.feed_store = feed_store
        self.diagnostics = diagnostics or ChannelsDiagnostics()
        # 生产接线传 ChannelsDownloader：用它的命名上下文落盘 + 历史登记。
        self._downloader = downloader
        # 会话级下载统计（core.downloader_base.DownloadResult）：MSE 保存
        # 成功计入 success——分享链接模式据此判断「目标已下载」并结束会话，
        # 诊断链「下载成功」环节也据此变绿。
        self._stats = stats
        # 兼容旧构造参数；已停用未经验证的回放头部拼接。
        self.replay = replay
        # 页面自动保存开关（channels.mse_auto_save）：经心跳响应下发，
        # 页面据此决定是否在播放结束/切走/链淘汰时自动上传分片。
        self.auto_save = bool(auto_save)
        self._planner: Optional[Callable[[ChannelFeed], Dict[str, Any]]] = None
        if downloader is not None and hasattr(downloader, "build_item_context"):
            self._planner = downloader.build_item_context
        self._temp_dir = Path(temp_dir) if temp_dir else Path("logs") / "channels_mse"
        self._streams: Dict[str, _MseStream] = {}
        self._record_tasks = set()

    # ------------------------------------------------------------------
    # 三段式协议：begin / chunk / end（+ abort）
    # ------------------------------------------------------------------

    def begin(self, stream_id: str, *, mime: str = "", page: str = "") -> int:
        """开始一条流，返回容量上限（页面据此截断）。"""
        stream_id = validate_stream_id(stream_id)
        if stream_id in self._streams:
            raise MseCaptureError("stream_id 已存在（页面需换新 id 重开）")
        self._evict_stale()
        if len(self._streams) >= MAX_OPEN_STREAMS:
            raise MseCaptureError("并发的 MSE 流过多，请稍后重试")
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        part = self._temp_dir / f"mse_{stream_id}.part"
        if part.exists():  # 上次异常残留
            try:
                part.unlink()
            except OSError:
                pass
        self._streams[stream_id] = _MseStream(
            stream_id, mime[:120], page[:32], part
        )
        self.diagnostics.incr("mse_capture_streams")
        return MAX_STREAM_BYTES

    def chunk(self, stream_id: str, b64: str) -> int:
        """追加一段（base64）数据，返回该流累计字节数。"""
        stream_id = validate_stream_id(stream_id)
        stream = self._streams.get(stream_id)
        if stream is None:
            raise KeyError(stream_id)
        if not isinstance(b64, str) or not b64:
            raise MseCaptureError("chunk.b64 不能为空")
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MseCaptureError(f"chunk.b64 解码失败: {exc}") from exc
        if not raw:
            raise MseCaptureError("chunk.b64 解码为空")
        if stream.bytes + len(raw) > MAX_STREAM_BYTES:
            raise MseCaptureError(
                f"流超过字节上限 {MAX_STREAM_BYTES}（录制过长或统计错乱）"
            )
        if stream.handle is None:
            stream.handle = open(stream.path, "ab")
        stream.handle.write(raw)
        stream.bytes += len(raw)
        stream.last_at = time.time()
        self.diagnostics.incr("mse_capture_bytes", len(raw))
        return stream.bytes

    def finalize(
        self, stream_id: str, meta: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Synchronous entry for offline callers; proxy uses finalize_async."""
        meta = meta if isinstance(meta, dict) else {}
        streams = self._take_streams(stream_id, meta)
        duration = self._prepare(streams, meta)
        return self._publish(streams[0], meta, duration)

    async def finalize_async(self, stream_id: str, meta=None) -> Dict[str, Any]:
        """Decode/mux outside the proxy event loop; publish on its owning loop."""
        meta = meta if isinstance(meta, dict) else {}
        streams = self._take_streams(stream_id, meta)
        duration = await asyncio.to_thread(self._prepare, streams, meta)
        return self._publish(streams[0], meta, duration)

    def _take_streams(self, stream_id, meta):
        stream_id = validate_stream_id(stream_id)
        companions = meta.get("companions", [])
        if not isinstance(companions, list) or len(companions) > 3:
            raise MseCaptureError("音视频轨数量不正确")
        ids = [stream_id] + [validate_stream_id(sid) for sid in companions]
        if len(set(ids)) != len(ids):
            raise MseCaptureError("音视频轨重复")
        for sid in ids:
            if sid not in self._streams:
                raise KeyError(sid)
        streams = [self._streams.pop(sid) for sid in ids]
        for stream in streams:
            self._close(stream)
        return streams

    def _prepare(self, streams, meta):
        stream = streams[0]
        output = self._temp_dir / ("validated_" + uuid.uuid4().hex + ".mp4")
        originals = [s.path for s in streams]
        try:
            for item in streams:
                if item.bytes < 64:
                    raise MseCaptureError("分片数据太少，无法组成视频（请播放后再保存）")
                if not self._looks_like_mp4(item.path):
                    raise MseCaptureError("分片缺 ftyp 头，请从视频开头播放后再保存")
            if meta.get("complete") is not True:
                raise MseCaptureError("播放分片尚不完整，请从开头播放至缓冲完成后再保存")
            source_feed = self._source_feed(meta)
            # MediaSource.duration may only describe the current six-second
            # playback window. The captured original Feed is the independent
            # evidence of the target duration; a page cannot lower it.
            expected = self._expected_duration(source_feed, meta)
            duration = prepare_mp4(originals, output, expected)
            stream.path = output
            stream.bytes = output.stat().st_size
            return duration
        except MediaValidationError as exc:
            raise MseCaptureError(str(exc)) from exc
        finally:
            for path in originals:
                self._discard_part(path)
            if stream.path != output:
                self._discard_part(output)

    def _publish(self, stream, meta, duration):
        try:
            feed = self._source_feed(meta)
            # A richer callback may arrive during threaded verification.
            # Recheck its duration before publishing the captured window.
            expected = self._expected_duration(feed, meta)
            if abs(duration - expected) > 0.75:
                raise MseCaptureError(
                    "视频时长不完整：捕获 %.2f 秒 / 目标 %.2f 秒，请继续播放后再保存"
                    % (duration, expected)
                )
        except MseCaptureError:
            self._discard_part(stream.path)
            raise
        # Source download can finish while FFmpeg runs off the event loop.
        # Keep its result and count the Feed only once.
        if feed.status == "done" and feed.downloaded_paths:
            path = Path(feed.downloaded_paths[0])
            if path.is_file() and path.stat().st_size > 0:
                self._discard_part(stream.path)
                return {
                    "feed_id": feed.feed_id,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "duration": feed.duration,
                }
        if feed.status == "downloading":
            self._discard_part(stream.path)
            raise MseCaptureError("完整源文件正在下载，请等待下载完成")

        final_path = self._place_file(stream, feed)
        feed.duration = max(feed.duration, int(round(duration)))
        feed.file_size = stream.bytes
        feed.downloaded_paths = [str(final_path)]
        feed.status = "done"
        feed.error = ""
        if self._stats is not None:
            try:
                self._stats.success += 1
                self._stats.total += 1
            except Exception:  # noqa: BLE001 —— 统计失败不影响落盘
                pass
        # 历史库 + 下载清单：与普通下载同一本账（调度异步任务，失败不影响落盘）。
        if self._downloader is not None and hasattr(self._downloader, "register_downloaded_file"):
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(
                    self._downloader.register_downloaded_file(
                        feed, [final_path], "channels_video"
                    )
                )
                self._record_tasks.add(task)
                task.add_done_callback(self._record_tasks.discard)
            except RuntimeError:
                pass  # 无事件循环（离线单测）：跳过历史登记
        return {
            "feed_id": feed.feed_id,
            "path": str(final_path),
            "bytes": stream.bytes,
            "duration": duration,
        }

    def _source_feed(self, meta: Dict[str, Any]) -> ChannelFeed:
        """Resolve the original video rather than trusting a playback window."""
        feed_id = meta.get("feed_id") or meta.get("object_id")
        feed = self.feed_store.get(feed_id) if isinstance(feed_id, str) else None
        if feed is None or feed.kind != "video" or feed.object_id.startswith("mse-"):
            raise MseCaptureError("尚未识别原视频信息，不能把播放缓存登记为完整下载")
        try:
            duration = float(feed.duration)
        except (TypeError, ValueError):
            duration = 0
        if not math.isfinite(duration) or duration <= 0:
            raise MseCaptureError("尚未取得原视频完整时长，不能把播放缓存登记为完整下载")
        return feed

    @staticmethod
    def _expected_duration(feed: ChannelFeed, meta: Dict[str, Any]) -> float:
        try:
            observed = float(meta.get("duration") or 0)
        except (TypeError, ValueError):
            raise MseCaptureError("视频时长无效") from None
        if not math.isfinite(observed) or observed < 0:
            raise MseCaptureError("视频时长无效")
        return max(observed, float(feed.duration))

    def abort(self, stream_id: str) -> None:
        """放弃一条流（页面取消 / 上传出错）。"""
        stream_id = validate_stream_id(stream_id)
        stream = self._streams.pop(stream_id, None)
        if stream is None:
            return
        self._close(stream)
        self._discard_part(stream.path)

    async def aclose(self) -> None:
        """Called after finalizers drain, before the downloader/database closes."""
        if self._record_tasks:
            await asyncio.gather(*self._record_tasks, return_exceptions=True)
        for sid in list(self._streams):
            self.abort(sid)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _close(stream: _MseStream) -> None:
        if stream.handle is not None:
            try:
                stream.handle.close()
            except OSError:
                pass
            stream.handle = None

    @staticmethod
    def _discard_part(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _looks_like_mp4(path: Path) -> bool:
        """fMP4 init segment 特征：偏移 4 起 ``ftyp`` box（与 Isaac64 校验同一判据）。"""
        try:
            with open(path, "rb") as handle:
                head = handle.read(12)
        except OSError:
            return False
        return len(head) >= 8 and head[4:8] == b"ftyp"

    def _place_file(self, stream: _MseStream, feed: ChannelFeed) -> Path:
        """按命名模板把 .part 移到最终落盘位置。"""
        context: Optional[Dict[str, Any]] = None
        if self._planner is not None:
            try:
                context = self._planner(feed)
            except Exception:  # noqa: BLE001 —— 命名失败回退临时目录
                context = None
        if context and context.get("save_dir") and context.get("file_stem"):
            save_dir = Path(context["save_dir"])
            target = save_dir / f"{context['file_stem']}.mp4"
        else:
            save_dir = self._temp_dir / "saved"
            target = save_dir / f"{feed.token}.mp4"
        save_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():  # 同名覆盖保护：加序号
            seq = 1
            while True:
                candidate = save_dir / f"{context['file_stem'] if context else feed.token}_{seq}.mp4"
                if not candidate.exists():
                    target = candidate
                    break
                seq += 1
        shutil.move(str(stream.path), str(target))
        return target

    def _evict_stale(self) -> None:
        now = time.time()
        for sid, stream in list(self._streams.items()):
            if now - stream.started_at > _STREAM_TTL_SECONDS:
                self.abort(sid)


def new_stream_id() -> str:
    """服务端也可生成 id（页面自己生成，这里只是兜底工具）。"""
    return uuid.uuid4().hex
