"""Offline contracts for Channels quality selection in REST/manual downloads."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from channels.feed import ChannelFeed
from channels.feed_store import FeedStore
from config import ConfigLoader
from core.downloader_base import DownloadResult
from server.channels import ChannelsSessionManager

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from server.app import build_app  # noqa: E402


def _app(tmp_path):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path), database=False)
    return build_app(config)


def test_rest_download_forwards_selected_quality(tmp_path, monkeypatch):
    download = AsyncMock(return_value={"queued": True})
    monkeypatch.setattr(ChannelsSessionManager, "download_feed", download)
    with TestClient(_app(tmp_path)) as client:
        response = client.post("/api/v1/channels/feeds/video1/download",
                               json={"quality": "format:1080p60"})
        assert response.status_code == 200
    download.assert_awaited_once_with("video1", quality="format:1080p60")


@pytest.mark.parametrize("quality", [123, "garbage", "", "format:https://evil.test/key"])
def test_rest_rejects_bad_quality_before_queueing(tmp_path, monkeypatch, quality):
    download = AsyncMock()
    monkeypatch.setattr(ChannelsSessionManager, "download_feed", download)
    with TestClient(_app(tmp_path)) as client:
        response = client.post("/api/v1/channels/feeds/video1/download", json={"quality": quality})
        assert response.status_code == 400
    download.assert_not_awaited()


async def test_done_video_can_be_manually_redownloaded_at_another_quality():
    manager = ChannelsSessionManager(ConfigLoader(None), Mock())
    feed = ChannelFeed(object_id="video1", status="done", downloaded_paths=["existing.mp4"])
    store = FeedStore()
    store.add([feed])
    downloader = Mock(download_feed=AsyncMock(return_value="done"))
    manager._running = {"store": store, "downloader": downloader, "stats": DownloadResult()}
    response = await manager.download_feed("video1", quality="format:1080p60")
    assert response["queued"] is True
    await asyncio.gather(*manager._manual_tasks)
    downloader.download_feed.assert_awaited_once_with(feed, manual=True, quality="format:1080p60")


async def test_running_video_does_not_queue_another_quality_download():
    manager = ChannelsSessionManager(ConfigLoader(None), Mock())
    feed = ChannelFeed(object_id="video1", status="downloading")
    store = FeedStore()
    store.add([feed])
    downloader = Mock(download_feed=AsyncMock())
    manager._running = {"store": store, "downloader": downloader, "stats": DownloadResult()}
    response = await manager.download_feed("video1", quality="format:1080p60")
    assert response["queued"] is False
    downloader.download_feed.assert_not_awaited()


def test_channels_default_quality_updates_without_changing_proxy_preferences(tmp_path):
    app = _app(tmp_path)
    original_channels = dict(app.state.deps.config.get("channels"))
    original_douyin = app.state.deps.config.get("video_quality")
    with TestClient(app) as client:
        response = client.put("/api/v1/config", json={"updates": {
            "channels": {"quality": "1080p", "proxy_port": 1234, "enabled": False},
        }})
        assert response.status_code == 200
        assert client.get("/api/v1/channels/status").json()["default_quality"] == "1080p"
    current = app.state.deps.config.get("channels")
    assert current["quality"] == "1080p"
    assert current["proxy_port"] == original_channels["proxy_port"]
    assert current["enabled"] == original_channels["enabled"]
    assert app.state.deps.config.get("video_quality") == original_douyin


def test_channels_default_quality_rejects_unsupported_setting(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.put("/api/v1/config", json={"updates": {"channels": {"quality": "invented"}}})
        assert response.status_code == 400


def test_saving_channels_quality_preserves_other_channels_fields_on_disk(tmp_path):
    import yaml

    from server.app import _write_config_file

    path = tmp_path / "config.yml"
    path.write_text("channels:\n  quality: highest\n  proxy_port: 9876\n  enabled: false\n", encoding="utf-8")
    assert _write_config_file(str(path), {"channels": {
        "quality": "1080p", "proxy_port": 8899, "enabled": True,
    }}, ("channels",))
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["channels"]
    assert saved == {"quality": "1080p", "proxy_port": 9876, "enabled": False}
