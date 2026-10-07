"""Live recording must produce media and respect the configured network route."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import live_downloader as live_module
from core.live_downloader import LiveDownloader


def _live_downloader(tmp_path):
    downloader = object.__new__(LiveDownloader)
    downloader.config = {"live": {"max_duration_seconds": 12}}
    downloader.progress_reporter = None
    downloader.api_client = SimpleNamespace(
        proxy=None,
        get_live_room_info=AsyncMock(return_value={
            "room": {"status": 2, "stream_url": {
                "hls_pull_url_map": {"HD1": "https://cdn.example/live.m3u8"},
            }},
            "user": {"nickname": "test"},
        }),
    )
    downloader._plan_output_paths = lambda *_args: (tmp_path, "recording")
    downloader._download_headers = lambda: {"User-Agent": "test"}
    return downloader


@pytest.mark.asyncio
async def test_hls_live_uses_media_recorder_instead_of_saving_playlist(tmp_path):
    downloader = _live_downloader(tmp_path)
    downloader._record_hls_stream = AsyncMock(return_value=True)
    downloader._record_stream = AsyncMock(return_value=True)
    result = await downloader.download({"room_id": "42"})
    assert result.success == 1
    downloader._record_hls_stream.assert_awaited_once()
    assert downloader._record_hls_stream.call_args.args[1].suffix == ".ts"
    downloader._record_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_hls_without_ffmpeg_is_failure_with_actionable_progress(tmp_path, monkeypatch):
    downloader = _live_downloader(tmp_path)
    monkeypatch.setattr(live_module, "resolve_ffmpeg_path", lambda: "")
    updates = []
    downloader._progress_update_step = lambda *args: updates.append(args)
    result = await downloader.download({"room_id": "42"})
    assert result.failed == 1
    assert result.success == 0
    assert "ffmpeg" in str(updates)
    assert not list(tmp_path.glob("*.m3u8"))


@pytest.mark.asyncio
async def test_hls_recorder_passes_headers_and_duration_and_promotes_media(tmp_path, monkeypatch):
    downloader = _live_downloader(tmp_path)
    monkeypatch.setattr(live_module, "resolve_ffmpeg_path", lambda: "ffmpeg.exe")
    captured = []

    async def spawn(*args, **kwargs):
        captured.extend(args)
        from pathlib import Path

        Path(args[-1]).write_bytes(b"media")
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(None, None)))

    monkeypatch.setattr(live_module.asyncio, "create_subprocess_exec", spawn)
    output = tmp_path / "recording.ts"
    assert await downloader._record_hls_stream(
        "https://cdn.example/live.m3u8", output, max_duration=12, idle_timeout=30,
    )
    assert captured[captured.index("-t") + 1] == "12"
    assert "https://live.douyin.com/" in captured[captured.index("-headers") + 1]
    assert output.read_bytes() == b"media"
    assert not output.with_suffix(".ts.tmp").exists()


@pytest.mark.asyncio
async def test_flv_recording_uses_configured_proxy(tmp_path):
    downloader = _live_downloader(tmp_path)
    downloader.api_client.proxy = "http://127.0.0.1:7890"
    captured = {}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        @property
        def content(self):
            return self

        async def iter_chunked(self, _size):
            yield b"FLV-media"

    def get(_url, **kwargs):
        captured.update(kwargs)
        return Response()

    downloader.api_client.get_session = AsyncMock(return_value=SimpleNamespace(get=get))
    assert await downloader._record_stream(
        "https://cdn.example/live.flv", tmp_path / "recording.flv",
        max_duration=0, chunk_size=4096, idle_timeout=30,
    )
    assert captured["proxy"] == "http://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_hls_cancellation_stops_process_and_preserves_partial_media(tmp_path, monkeypatch):
    downloader = _live_downloader(tmp_path)
    monkeypatch.setattr(live_module, "resolve_ffmpeg_path", lambda: "ffmpeg.exe")
    process = SimpleNamespace(
        returncode=None,
        communicate=AsyncMock(side_effect=asyncio.CancelledError()),
        wait=AsyncMock(return_value=0),
    )
    terminated = []

    def terminate():
        terminated.append(True)
        process.returncode = 0

    process.terminate = terminate

    async def spawn(*args, **kwargs):
        from pathlib import Path

        Path(args[-1]).write_bytes(b"partial-media")
        return process

    monkeypatch.setattr(live_module.asyncio, "create_subprocess_exec", spawn)
    output = tmp_path / "recording.ts"
    with pytest.raises(asyncio.CancelledError):
        await downloader._record_hls_stream(
            "https://cdn.example/live.m3u8", output, max_duration=12, idle_timeout=30,
        )
    assert terminated == [True]
    process.wait.assert_awaited_once()
    assert output.read_bytes() == b"partial-media"
