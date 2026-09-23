"""微信页面内下载按钮的后端任务中心。

页面按钮点击后经 ``POST /__cuin/task`` 到达 :class:`ChannelsTaskHub`：
它不重造任务系统——下载 / 录制直接复用嗅探会话里已有的
:class:`~channels.downloader.ChannelsDownloader`、:class:`~channels.feed_store.
FeedStore` 与 :class:`~core.downloader_base.DownloadResult`，只多做两件
前端需要的事::

    1. 任务句柄：task_id → 状态（queued / downloading / done / failed /
       stopped），供页面轮询 GET /__cuin/task/{id}；
    2. 停止录制：直播录制的 asyncio.Task 取消（LiveRecorder 会优雅终止
       ffmpeg 并保留已录制的部分文件）。

与自动下载 worker 共享同一个下载器实例与 FeedStore：页面按钮下载的视频
在 Web 控制台捕获列表里同样可见（状态流转一致），不搞两套账。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from channels.downloader import ChannelsDownloader
from channels.feed import ChannelFeed
from channels.feed_store import FeedStore
from core.downloader_base import DownloadResult

__all__ = ["ChannelsTaskHub", "TaskRecord"]

# 任务记录保留上限（终态任务过期淘汰）。
_MAX_TASK_RECORDS = 200


@dataclass
class TaskRecord:
    """一次页面触发的下载 / 录制任务。"""

    task_id: str
    feed_id: str
    kind: str
    action: str  # download / record
    quality: Optional[str] = None
    status: str = "queued"  # queued / downloading / done / failed / stopped
    error: str = ""
    paths: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "feed_id": self.feed_id,
            "kind": self.kind,
            "action": self.action,
            "status": self.status,
            "error": self.error,
            "paths": list(self.paths),
            "created_at": self.created_at,
        }


class ChannelsTaskHub:
    """页面按钮 → ChannelsDownloader 的任务中心（单会话一个实例）。"""

    def __init__(
        self,
        store: FeedStore,
        downloader: ChannelsDownloader,
        stats: Optional[DownloadResult] = None,
    ) -> None:
        self.store = store
        self.downloader = downloader
        self.stats = stats if stats is not None else DownloadResult()
        self._records: Dict[str, TaskRecord] = {}
        self._active_by_feed: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 提交 / 查询 / 停止
    # ------------------------------------------------------------------

    def submit(
        self, feed_id: str, *, action: str = "download", quality: Optional[str] = None
    ) -> str:
        """提交任务并立即返回 task_id（下载在后台 task 中进行）。

        * 同一 feed 已有进行中的任务时复用其 task_id（不会重复下载）；
        * ``action="stop"`` 取消该 feed 正在进行中的录制任务；
        * feed 不存在抛 ``KeyError``。
        """
        feed = self.store.get(feed_id)
        if feed is None:
            raise KeyError(feed_id)

        if action == "stop":
            active_id = self._active_by_feed.get(feed.dedup_key)
            if active_id is None:
                raise ValueError("当前没有进行中的录制任务")
            record = self._records.get(active_id)
            if record is not None and record.task is not None:
                record.task.cancel()
            return active_id

        active_id = self._active_by_feed.get(feed.dedup_key)
        if active_id is not None:
            record = self._records.get(active_id)
            if record is not None and record.status in ("queued", "downloading"):
                return active_id
            self._active_by_feed.pop(feed.dedup_key, None)

        record = TaskRecord(
            task_id=uuid.uuid4().hex[:16],
            feed_id=feed.feed_id,
            kind=feed.kind,
            action=action if action in ("record", "cover") else "download",
            quality=quality if isinstance(quality, str) else None,
        )
        self._records[record.task_id] = record
        self._active_by_feed[feed.dedup_key] = record.task_id
        self._trim()
        record.task = asyncio.get_event_loop().create_task(
            self._run(record, feed), name=f"channels-task-{record.task_id}"
        )
        return record.task_id

    def status(self, task_id: str) -> Optional[Dict[str, Any]]:
        record = self._records.get(task_id)
        return record.to_dict() if record is not None else None

    def cancel_all(self) -> None:
        """会话停止：取消全部进行中的任务（录制随之优雅停止）。"""
        for record in list(self._records.values()):
            if record.task is not None and not record.task.done():
                record.task.cancel()
        self._active_by_feed.clear()

    async def wait_idle(self, timeout: float = 5.0) -> None:
        """等待全部任务终态（会话退出清理用）。"""
        tasks = [
            r.task for r in self._records.values() if r.task is not None and not r.task.done()
        ]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    # ------------------------------------------------------------------
    # 任务执行
    # ------------------------------------------------------------------

    async def _run(self, record: TaskRecord, feed: ChannelFeed) -> None:
        record.status = "downloading"
        self.store.set_status(feed, "downloading")
        try:
            if record.action == "cover":
                paths = await self.downloader.download_cover(feed)
                record.status = "done"
                record.paths = list(paths)
                self.store.set_status(feed, "done", downloaded_paths=paths)
                self.stats.success += 1
                self._release(record)
                return
            status = await self.downloader.download_feed(
                feed, manual=True, quality=record.quality
            )
        except asyncio.CancelledError:
            record.status = "stopped"
            record.error = "已手动停止"
            # 直播部分文件已由 downloader 登记；普通视频取消不落盘。
            if feed.status == "downloading":
                self.store.set_status(feed, "pending")
            self._release(record)
            raise
        except Exception as exc:  # noqa: BLE001 —— 任务失败不拖垮会话
            record.status = "failed"
            record.error = str(exc)[:300]
            self.store.set_status(feed, "failed", error=record.error)
            self.stats.failed += 1
            self._release(record)
            return
        record.status = status
        record.error = feed.error
        record.paths = list(feed.downloaded_paths)
        self.store.set_status(
            feed, status, error=feed.error, downloaded_paths=feed.downloaded_paths
        )
        if status == "done":
            self.stats.success += 1
        elif status == "failed":
            self.stats.failed += 1
        self._release(record)

    def _release(self, record: TaskRecord) -> None:
        feed = self.store.get(record.feed_id)
        key = feed.dedup_key if feed is not None else None
        if key is not None and self._active_by_feed.get(key) == record.task_id:
            self._active_by_feed.pop(key, None)

    def _trim(self) -> None:
        if len(self._records) <= _MAX_TASK_RECORDS:
            return
        terminal = ("done", "failed", "stopped")
        for task_id, record in list(self._records.items()):
            if len(self._records) <= _MAX_TASK_RECORDS:
                return
            if record.status in terminal:
                self._records.pop(task_id, None)
