"""ChannelsTaskHub 测试（离线；下载器用假实现）。

覆盖：页面按钮下载全流程、画质透传、封面动作、直播录制停止、
重复提交复用、未知 feed、会话退出清理。
"""

from __future__ import annotations

import asyncio

import pytest

from channels.feed import ChannelFeed
from channels.feed_store import FeedStore
from channels.task_hub import ChannelsTaskHub
from core.downloader_base import DownloadResult


def _video_feed(object_id: str = "v1") -> ChannelFeed:
    return ChannelFeed(
        object_id=object_id,
        nonce_id=f"n_{object_id}",
        kind="video",
        url="https://cdn/v.mp4",
        decode_key=123,
        title=f"视频{object_id}",
    )


def _live_feed(object_id: str = "l1") -> ChannelFeed:
    return ChannelFeed(
        object_id=object_id,
        nonce_id=f"n_{object_id}",
        kind="live",
        url="https://live/flv",
        title="直播中",
    )


class _FakeDownloader:
    """记录调用并返回可编排结果的假下载器。"""

    def __init__(self, *, result: str = "done", delay: float = 0.0,
                 raise_exc: Exception | None = None):
        self.result = result
        self.delay = delay
        self.raise_exc = raise_exc
        self.calls: list = []
        self.cover_calls: list = []

    async def download_feed(self, feed, *, manual=False, quality=None):
        self.calls.append(
            {"feed_id": feed.feed_id, "manual": manual, "quality": quality}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        feed.downloaded_paths = ["/tmp/out.mp4"]
        return self.result

    async def download_cover(self, feed):
        self.cover_calls.append(feed.feed_id)
        feed.downloaded_paths = ["/tmp/cover.jpg"]
        return ["/tmp/cover.jpg"]


async def _wait_idle(hub: ChannelsTaskHub, timeout: float = 2.0) -> None:
    await hub.wait_idle(timeout=timeout)


class TestDownload:
    @pytest.mark.asyncio
    async def test_submit_download_done(self):
        store = FeedStore()
        store.add([_video_feed()])
        downloader = _FakeDownloader()
        stats = DownloadResult()
        hub = ChannelsTaskHub(store, downloader, stats)
        task_id = hub.submit(store.all()[0].feed_id)
        await _wait_idle(hub)
        record = hub.status(task_id)
        assert record["status"] == "done"
        assert record["paths"] == ["/tmp/out.mp4"]
        assert stats.success == 1
        # manual=True（页面按钮跳过增量判重）。
        assert downloader.calls[0]["manual"] is True
        # feed 状态同步到 Web 控制台可见的列表。
        assert store.all()[0].status == "done"

    @pytest.mark.asyncio
    async def test_quality_passthrough(self):
        store = FeedStore()
        store.add([_video_feed()])
        downloader = _FakeDownloader()
        hub = ChannelsTaskHub(store, downloader)
        hub.submit(store.all()[0].feed_id, quality="lowest")
        await _wait_idle(hub)
        assert downloader.calls[0]["quality"] == "lowest"

    @pytest.mark.asyncio
    async def test_cover_action(self):
        store = FeedStore()
        store.add([_video_feed()])
        downloader = _FakeDownloader()
        hub = ChannelsTaskHub(store, downloader)
        hub.submit(store.all()[0].feed_id, action="cover")
        await _wait_idle(hub)
        assert downloader.cover_calls == [store.all()[0].feed_id]
        assert downloader.calls == []  # 不走整片下载
        record = hub.status(list(hub._records)[0])
        assert record["status"] == "done"

    @pytest.mark.asyncio
    async def test_failed_download(self):
        store = FeedStore()
        store.add([_video_feed()])
        downloader = _FakeDownloader(result="failed", raise_exc=RuntimeError("CDN 403"))
        stats = DownloadResult()
        hub = ChannelsTaskHub(store, downloader, stats)
        task_id = hub.submit(store.all()[0].feed_id)
        await _wait_idle(hub)
        record = hub.status(task_id)
        assert record["status"] == "failed"
        assert "CDN 403" in record["error"]
        assert stats.failed == 1
        assert store.all()[0].status == "failed"

    @pytest.mark.asyncio
    async def test_duplicate_submit_reuses_task(self):
        store = FeedStore()
        store.add([_video_feed()])
        downloader = _FakeDownloader(delay=0.3)
        hub = ChannelsTaskHub(store, downloader)
        first = hub.submit(store.all()[0].feed_id)
        second = hub.submit(store.all()[0].feed_id)
        assert first == second  # 进行中不重复下载
        await _wait_idle(hub)
        assert len(downloader.calls) == 1

    @pytest.mark.asyncio
    async def test_unknown_feed_raises_keyerror(self):
        store = FeedStore()
        hub = ChannelsTaskHub(store, _FakeDownloader())
        with pytest.raises(KeyError):
            hub.submit("nonexistent")

    @pytest.mark.asyncio
    async def test_status_unknown_task(self):
        hub = ChannelsTaskHub(FeedStore(), _FakeDownloader())
        assert hub.status("nope") is None


class TestLiveRecording:
    @pytest.mark.asyncio
    async def test_stop_cancels_recording(self):
        store = FeedStore()
        store.add([_live_feed()])
        downloader = _FakeDownloader(delay=10.0)
        hub = ChannelsTaskHub(store, downloader)
        feed_id = store.all()[0].feed_id
        task_id = hub.submit(feed_id, action="record")
        await asyncio.sleep(0.1)  # 让任务进入 downloading
        assert hub.status(task_id)["status"] == "downloading"
        stopped_id = hub.submit(feed_id, action="stop")
        assert stopped_id == task_id
        await _wait_idle(hub)
        record = hub.status(task_id)
        assert record["status"] == "stopped"
        # 停止后 feed 回到 pending（Web 列表可重新发起）。
        assert store.all()[0].status == "pending"

    @pytest.mark.asyncio
    async def test_stop_without_active_raises(self):
        store = FeedStore()
        store.add([_live_feed()])
        hub = ChannelsTaskHub(store, _FakeDownloader())
        with pytest.raises(ValueError):
            hub.submit(store.all()[0].feed_id, action="stop")

    @pytest.mark.asyncio
    async def test_live_download_action_kept(self):
        """live feed 的 download 动作保持原样（downloader 内部路由到录制）。"""
        store = FeedStore()
        store.add([_live_feed()])
        downloader = _FakeDownloader()
        hub = ChannelsTaskHub(store, downloader)
        task_id = hub.submit(store.all()[0].feed_id, action="download")
        await _wait_idle(hub)
        assert hub.status(task_id)["action"] == "download"
        assert hub.status(task_id)["kind"] == "live"


class TestSessionCleanup:
    @pytest.mark.asyncio
    async def test_cancel_all(self):
        store = FeedStore()
        store.add([_video_feed()])
        downloader = _FakeDownloader(delay=10.0)
        hub = ChannelsTaskHub(store, downloader)
        hub.submit(store.all()[0].feed_id)
        await asyncio.sleep(0.1)
        hub.cancel_all()
        await _wait_idle(hub)
        record = hub.status(list(hub._records)[0])
        assert record["status"] == "stopped"
