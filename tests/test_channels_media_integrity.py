"""Regression coverage for short/corrupt Channels downloads using real MP4s."""

import base64
import subprocess
from pathlib import Path
from types import SimpleNamespace

import imageio_ffmpeg
import pytest

from channels.feed import ChannelFeed
from channels.feed_store import FeedStore
from channels.mse_capture import MseCaptureError, MseCaptureStore
from channels.replay_downloader import ReplayDownloadManager, _PendingReplay


@pytest.fixture
def real_video(channels_real_mp4):
    return channels_real_mp4


def _known_meta(mse, sid, duration=2, **extra):
    mse.feed_store.add([ChannelFeed(object_id=sid, kind="video", duration=duration,
                                   title="原视频", author_name="原作者")])
    return {"feed_id": sid, "duration": duration, "complete": True, **extra}


def test_mse_rejects_header_only_garbage(tmp_path):
    store = FeedStore()
    mse = MseCaptureStore(store, temp_dir=tmp_path / "parts")
    mse.begin("garbage123")
    mse.chunk("garbage123", base64.b64encode(b"\0\0\0\x18ftypisom" + b"x" * 200).decode())
    meta = _known_meta(mse, "garbage123")
    with pytest.raises(MseCaptureError):
        mse.finalize("garbage123", meta)
    assert store.get("garbage123").status == "pending"


def test_mse_rejects_two_seconds_of_a_long_video(tmp_path, real_video):
    store = FeedStore()
    mse = MseCaptureStore(store, temp_dir=tmp_path / "parts")
    mse.begin("partial123", mime="video/mp4")
    mse.chunk("partial123", base64.b64encode(real_video.read_bytes()).decode())
    meta = _known_meta(mse, "partial123", duration=60)
    with pytest.raises(MseCaptureError, match="完整|时长"):
        mse.finalize("partial123", meta)
    assert store.get("partial123").status == "pending"


def test_replay_rejects_unrelated_but_valid_mp4(tmp_path, real_video):
    # The old XOR expression always reconstructed the supplied plain prefix;
    # checking its ftyp could never establish that it belongs to the CDN file.
    cipher = tmp_path / "cipher.part"
    cipher.write_bytes(b"\xa5" * 200000)
    manager = ReplayDownloadManager(FeedStore(), temp_dir=tmp_path / "replay")
    pending = _PendingReplay("target123", "https://finder.video.qq.com/video", 200000, cipher)
    manager._pending[pending.key] = pending
    assert manager._finalize_with(pending, real_video) is False
    assert cipher.exists()
    assert len(manager.feed_store) == 0


def _upload(mse, sid, path, mime="video/mp4"):
    mse.begin(sid, mime=mime)
    data = path.read_bytes()
    for start in range(0, len(data), 4096):
        mse.chunk(sid, base64.b64encode(data[start:start + 4096]).decode())


def _assert_playable(path):
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-xerror",
        "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-",
    ], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_complete_capture_is_seekable_and_decodes(tmp_path, real_video):
    store = FeedStore()
    mse = MseCaptureStore(store, temp_dir=tmp_path / "parts")
    _upload(mse, "complete123", real_video)
    result = mse.finalize("complete123", _known_meta(mse, "complete123"))
    path = Path(result["path"])
    data = path.read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")
    assert result["duration"] == pytest.approx(2, abs=0.15)
    assert len(store) == 1
    assert result["feed_id"] == "complete123"
    assert store.get("complete123").title == "原视频"
    _assert_playable(path)


def test_separate_video_audio_are_merged(tmp_path, real_video):
    mse = MseCaptureStore(FeedStore(), temp_dir=tmp_path / "parts")
    for kind in ("v", "a"):
        path = tmp_path / (kind + ".mp4")
        subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(real_video),
            "-map", "0:" + kind, "-c", "copy", "-movflags",
            "frag_keyframe+empty_moov+default_base_moof", str(path),
        ], check=True, capture_output=True)
        _upload(mse, "separate" + kind, path, "video/mp4" if kind == "v" else "audio/mp4")
    result = mse.finalize("separatev", _known_meta(
        mse, "separatev", companions=["separatea"],
    ))
    _assert_playable(result["path"])
    assert not list((tmp_path / "parts").glob("*.part"))


def test_truncated_media_never_enters_success_history(tmp_path, real_video):
    store = FeedStore()
    mse = MseCaptureStore(store, temp_dir=tmp_path / "parts")
    mse.begin("truncated123")
    mse.chunk("truncated123", base64.b64encode(real_video.read_bytes()[:-6000]).decode())
    meta = _known_meta(mse, "truncated123")
    with pytest.raises(MseCaptureError):
        mse.finalize("truncated123", meta)
    assert store.get("truncated123").status == "pending"
    assert not list((tmp_path / "parts").glob("*.mp4"))


def test_hevc_capture_is_converted_to_h264(tmp_path, real_video):
    hevc = tmp_path / "hevc.mp4"
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(real_video),
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "pools=1:log-level=error",
        "-c:a", "copy", "-movflags", "frag_keyframe+empty_moov+default_base_moof", str(hevc),
    ], check=True, capture_output=True)
    mse = MseCaptureStore(FeedStore(), temp_dir=tmp_path / "parts")
    _upload(mse, "hevc12345", hevc)
    result = mse.finalize("hevc12345", _known_meta(mse, "hevc12345", duration=2.3))
    _assert_playable(result["path"])
    info = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", result["path"],
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "Video: h264" in info.stderr
    assert "Audio: aac" in info.stderr


async def test_proxy_finalization_keeps_event_loop_free(tmp_path, real_video, monkeypatch):
    import asyncio
    import json
    import threading

    from mitmproxy.http import Request

    from channels.pipeline import FeedCapturePipeline
    from channels.virtual_host import VirtualHostAddon

    store = FeedStore()
    mse = MseCaptureStore(store, temp_dir=tmp_path / "parts")
    _upload(mse, "async1234", real_video)
    meta = _known_meta(mse, "async1234")
    entered = threading.Event()
    release = threading.Event()
    original = mse._prepare

    def blocked_prepare(streams, meta):
        entered.set()
        assert release.wait(5)
        return original(streams, meta)

    monkeypatch.setattr(mse, "_prepare", blocked_prepare)
    addon = VirtualHostAddon(FeedCapturePipeline(store, mse.diagnostics), mse_store=mse)
    actions = []
    flow = SimpleNamespace(
        request=Request.make("POST", "https://channels.weixin.qq.com/__cuin/mse",
            json.dumps({"op": "end", "stream_id": "async1234", "meta": meta}).encode(),
            {"Content-Type": "application/json", "Origin": "https://channels.weixin.qq.com"}),
        response=None, intercept=lambda: actions.append("intercept"),
        resume=lambda: actions.append("resume"),
    )
    addon.request(flow)
    closing = asyncio.create_task(addon.aclose())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert flow.response is None
        assert store.get("async1234").status == "pending"
        assert not closing.done(), "shutdown must wait for in-flight media verification"
    finally:
        release.set()
    await closing
    assert actions == ["intercept", "resume"]
    assert flow.response.status_code == 200
    assert len(store) == 1


@pytest.fixture
def six_second_video(tmp_path, real_video):
    path = tmp_path / "six-seconds.mp4"
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-stream_loop", "2",
        "-i", str(real_video), "-t", "6", "-c", "copy", "-movflags",
        "frag_keyframe+empty_moov+default_base_moof", str(path),
    ], check=True, capture_output=True)
    return path


def test_six_second_mse_without_original_feed_never_counts_as_success(tmp_path, six_second_video):
    from core.downloader_base import DownloadResult

    stats = DownloadResult()
    store = FeedStore()
    mse = MseCaptureStore(store, temp_dir=tmp_path / "parts", stats=stats)
    _upload(mse, "anonymous123", six_second_video)
    with pytest.raises(MseCaptureError, match="原视频"):
        mse.finalize("anonymous123", {"complete": True, "duration": 6})
    assert len(store) == 0
    assert stats.success == stats.total == 0
    assert not list((tmp_path / "parts").glob("*.part"))
    assert not list((tmp_path / "parts").glob("*.mp4"))


def test_six_second_window_cannot_lower_known_23_second_duration(tmp_path, six_second_video):
    from core.downloader_base import DownloadResult

    stats = DownloadResult()
    mse = MseCaptureStore(FeedStore(), temp_dir=tmp_path / "parts", stats=stats)
    _upload(mse, "window123", six_second_video)
    meta = _known_meta(mse, "original123", duration=23)
    meta["duration"] = 6
    with pytest.raises(MseCaptureError, match="目标 23"):
        mse.finalize("window123", meta)
    assert mse.feed_store.get("original123").status == "pending"
    assert stats.success == stats.total == 0
    assert len(mse.feed_store) == 1


def test_original_missing_duration_cannot_use_mse_window_duration(tmp_path, six_second_video):
    mse = MseCaptureStore(FeedStore(), temp_dir=tmp_path / "parts")
    _upload(mse, "missing123", six_second_video)
    meta = _known_meta(mse, "original123", duration=0)
    meta["duration"] = 6
    with pytest.raises(MseCaptureError, match="完整时长"):
        mse.finalize("missing123", meta)
    assert mse.feed_store.get("original123").status == "pending"


def test_known_complete_six_second_capture_uses_original_identity(tmp_path, six_second_video):
    from core.downloader_base import DownloadResult

    stats = DownloadResult()
    mse = MseCaptureStore(FeedStore(), temp_dir=tmp_path / "parts", stats=stats)
    _upload(mse, "fullsix123", six_second_video)
    meta = _known_meta(mse, "original123", duration=6)
    result = mse.finalize("fullsix123", meta)
    assert result["feed_id"] == "original123"
    assert result["duration"] == pytest.approx(6, abs=0.25)
    assert len(mse.feed_store) == 1
    assert mse.feed_store.get("original123").status == "done"
    assert stats.success == stats.total == 1


def test_mse_does_not_duplicate_direct_download_completed_during_validation(
    tmp_path, real_video, monkeypatch,
):
    from core.downloader_base import DownloadResult

    stats = DownloadResult()
    mse = MseCaptureStore(FeedStore(), temp_dir=tmp_path / "parts", stats=stats)
    _upload(mse, "racing123", real_video)
    meta = _known_meta(mse, "original123")
    original_prepare = mse._prepare

    def finish_direct_download(streams, meta):
        duration = original_prepare(streams, meta)
        feed = mse.feed_store.get("original123")
        feed.status = "done"
        feed.downloaded_paths = [str(real_video)]
        return duration

    monkeypatch.setattr(mse, "_prepare", finish_direct_download)
    result = mse.finalize("racing123", meta)
    assert result["path"] == str(real_video)
    assert stats.success == stats.total == 0
    assert not list((tmp_path / "parts").glob("*.mp4"))


def test_mse_does_not_publish_while_direct_download_is_running(tmp_path, real_video):
    mse = MseCaptureStore(FeedStore(), temp_dir=tmp_path / "parts")
    _upload(mse, "racing123", real_video)
    meta = _known_meta(mse, "original123")
    mse.feed_store.get("original123").status = "downloading"
    with pytest.raises(MseCaptureError, match="正在下载"):
        mse.finalize("racing123", meta)
    assert mse.feed_store.get("original123").status == "downloading"
    assert not list((tmp_path / "parts").glob("*.mp4"))


def test_late_full_duration_refresh_cannot_certify_short_window(tmp_path, real_video, monkeypatch):
    mse = MseCaptureStore(FeedStore(), temp_dir=tmp_path / "parts")
    _upload(mse, "latefeed123", real_video)
    meta = _known_meta(mse, "original123")
    original_prepare = mse._prepare

    def full_duration_arrives(streams, meta):
        duration = original_prepare(streams, meta)
        mse.feed_store.add([ChannelFeed(object_id="original123", kind="video", duration=23)])
        return duration

    monkeypatch.setattr(mse, "_prepare", full_duration_arrives)
    with pytest.raises(MseCaptureError, match="目标 23"):
        mse.finalize("latefeed123", meta)
    assert mse.feed_store.get("original123").status == "pending"
    assert not list((tmp_path / "parts").glob("*.mp4"))
