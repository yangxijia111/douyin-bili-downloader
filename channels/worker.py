"""嗅探会话的自动下载 worker（CLI 与 Server 共用）。

持续消费 :class:`~channels.feed_store.FeedStore` 里 ``pending`` 的捕获项；
直播默认跳过（录制时长无上界，须显式开启或手动触发）。worker 由调用方以
task 形式启动、取消即停；被取消时把进行中的条目状态回滚为 ``pending``，
会话重启后可继续。
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from channels.downloader import ChannelsDownloader
from channels.feed_store import FeedStore
from core.downloader_base import DownloadResult

__all__ = ["run_auto_download_worker"]


async def run_auto_download_worker(
    store: FeedStore,
    downloader: ChannelsDownloader,
    *,
    live_record: bool = False,
    stats: Optional[DownloadResult] = None,
    should_run: Optional[Callable[[], bool]] = None,
) -> None:
    """消费捕获列表；永不主动返回（调用方 cancel）。

    ``should_run`` 用于运行中开关自动下载（Server 的 auto-download 端点）：
    返回 False 时暂停消费（已捕获的 pending 保留，重新开启后继续处理）。
    """
    if stats is None:
        stats = DownloadResult()

    while True:
        if should_run is not None and not should_run():
            await asyncio.sleep(1.0)
            continue
        for feed in store.all():
            if feed.status != "pending":
                continue
            if feed.kind == "live" and not live_record:
                store.set_status(feed, "skipped")
                continue
            store.set_status(feed, "downloading")
            stats.total += 1
            try:
                status = await downloader.download_feed(feed)
            except asyncio.CancelledError:
                store.set_status(feed, "pending")
                raise
            store.set_status(
                feed,
                status,
                error=feed.error,
                downloaded_paths=feed.downloaded_paths,
            )
            if status == "done":
                stats.success += 1
            elif status == "skipped":
                stats.skipped += 1
            else:
                stats.failed += 1
        # 有新捕获立刻进下一轮；否则每秒醒一次检查 should_run。
        await store.wait_for_new(timeout=1.0)
        store.clear_new_event()
