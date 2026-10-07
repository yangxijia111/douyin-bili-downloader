"""MSE 分片旁路捕获测试（离线）：拼装存储 + ``/__cuin/mse`` 虚拟端点。

覆盖：三段式协议（begin/chunk/end/abort）、MP4 头校验、落盘命名（含重名
避让）、FeedStore/诊断登记、上限与非法输入防护、路由校验（同源/方法/503）。
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from channels.diagnostics import ChannelsDiagnostics
from channels.feed import ChannelFeed
from channels.feed_store import FeedStore
from channels.mse_capture import (
    MAX_OPEN_STREAMS,
    MseCaptureError,
    MseCaptureStore,
    validate_stream_id,
)
from channels.pipeline import FeedCapturePipeline
from channels.virtual_host import VirtualHostAddon

mitmproxy_http = pytest.importorskip("mitmproxy.http")
Request = mitmproxy_http.Request

_HOST = "channels.weixin.qq.com"

# Tests use playable media, not a magic header followed by garbage.
_FTYP_HEAD = b""


@pytest.fixture(autouse=True)
def _real_payload(channels_real_mp4):
    global _FTYP_HEAD
    _FTYP_HEAD = channels_real_mp4.read_bytes()


def _finish(mse, sid, meta=None):
    meta = meta or {}
    if sid in mse._streams:
        mse.feed_store.add([ChannelFeed(
            object_id=sid, kind="video", duration=2,
            title=meta.get("title") or "原视频标题",
            author_name=meta.get("author") or "原作者",
        )])
    return mse.finalize(sid, {"feed_id": sid, "complete": True, "duration": 2, **meta})


def _planner(feed):
    """假命名上下文（生产里由 ChannelsDownloader.build_item_context 提供）。"""
    return {"save_dir": str(_planner.out_dir), "file_stem": _planner.stem}


class _StubDownloader:
    """只提供 MSE 需要的两个接口（不触碰真实配置/网络）。"""

    def __init__(self, out_dir, stem="myvideo"):
        _planner.out_dir = out_dir
        _planner.stem = stem

    def build_item_context(self, feed):
        return _planner(feed)


def _store(tmp_path, feed_store=None, diagnostics=None, downloader=None):
    # 注意：不能写 `feed_store or FeedStore()`——FeedStore 定义了 __len__，
    # 空 store 是 falsy，会悄悄新建一个（与 test_channels_virtual_host 同坑）。
    if feed_store is None:
        feed_store = FeedStore()
    if diagnostics is None:
        diagnostics = ChannelsDiagnostics()
    return MseCaptureStore(
        feed_store,
        diagnostics,
        downloader=downloader,
        temp_dir=tmp_path / "parts",
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _mse_flow(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    request = Request.make(
        "POST",
        f"https://{_HOST}/__cuin/mse",
        body,
        {"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
    )
    return SimpleNamespace(request=request, response=None)


class TestStreamIdValidation:
    def test_accepts_alnum(self):
        assert validate_stream_id("abc123XYZ012") == "abc123XYZ012"

    def test_rejects_short(self):
        with pytest.raises(MseCaptureError):
            validate_stream_id("ab1")

    def test_rejects_symbols(self):
        with pytest.raises(MseCaptureError):
            validate_stream_id("abc/../etc")

    def test_rejects_non_string(self):
        with pytest.raises(MseCaptureError):
            validate_stream_id(12345)


class TestMseCaptureStore:
    def test_happy_path_with_planner(self, tmp_path):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        downloader = _StubDownloader(tmp_path / "out", stem="我的视频")
        mse = _store(tmp_path, feed_store=store, diagnostics=diagnostics, downloader=downloader)

        assert mse.begin("streamAaa001", mime='video/mp4; codecs="avc1"') > 0
        mse.chunk("streamAaa001", _b64(_FTYP_HEAD[:-100]))
        total = mse.chunk("streamAaa001", _b64(_FTYP_HEAD[-100:]))
        assert total == len(_FTYP_HEAD)

        result = _finish(mse, "streamAaa001", {"title": "测试视频", "author": "作者"})
        final = tmp_path / "out" / "我的视频.mp4"
        assert final.exists()
        assert final.read_bytes()[4:8] == b"ftyp"
        assert result["path"] == str(final)
        assert result["bytes"] > 0

        feed = store.get(result["feed_id"])
        assert feed is not None
        assert feed.status == "done"
        assert feed.title == "测试视频"
        assert feed.author_name == "作者"
        assert feed.kind == "video"
        assert feed.downloaded_paths == [str(final)]
        assert len(store) == 1
        assert feed.object_id == "streamAaa001"
        assert diagnostics.strategy_stats() == {}
        assert diagnostics.get("parsed_feeds") == 0  # existing Feed is not captured twice
        assert diagnostics.get("mse_capture_streams") == 1
        assert diagnostics.get("mse_capture_bytes") == len(_FTYP_HEAD)

    def test_finalize_without_planner_uses_fallback_dir(self, tmp_path):
        mse = _store(tmp_path)
        mse.begin("streamBbb002")
        mse.chunk("streamBbb002", _b64(_FTYP_HEAD))
        result = _finish(mse, "streamBbb002")
        final = tmp_path / "parts" / "saved" / "channels_streamBbb002.mp4"
        assert final.exists()
        assert result["path"] == str(final)

    def test_duplicate_name_gets_sequence_suffix(self, tmp_path):
        downloader = _StubDownloader(tmp_path / "out", stem="same")
        mse = _store(tmp_path, downloader=downloader)
        for sid in ("streamCcc003", "streamDdd004"):
            mse.begin(sid)
            mse.chunk(sid, _b64(_FTYP_HEAD))
            _finish(mse, sid)
        assert (tmp_path / "out" / "same.mp4").exists()
        assert (tmp_path / "out" / "same_1.mp4").exists()

    def test_rejects_non_mp4_payload(self, tmp_path):
        store = FeedStore()
        mse = _store(tmp_path, feed_store=store)
        mse.begin("streamEee005")
        mse.chunk("streamEee005", _b64(b"not an mp4 at all" * 8))
        with pytest.raises(MseCaptureError, match="ftyp"):
            _finish(mse, "streamEee005")
        assert len(store) == 1
        assert store.get("streamEee005").status == "pending"
        assert not (tmp_path / "parts" / "mse_streamEee005.part").exists()

    def test_rejects_tiny_stream(self, tmp_path):
        mse = _store(tmp_path)
        mse.begin("streamFff006")
        mse.chunk("streamFff006", _b64(b"ftyp"))
        with pytest.raises(MseCaptureError, match="太少"):
            _finish(mse, "streamFff006")

    def test_chunk_unknown_stream_raises_keyerror(self, tmp_path):
        mse = _store(tmp_path)
        with pytest.raises(KeyError):
            mse.chunk("streamZzz999", _b64(b"data"))

    def test_finalize_unknown_stream_raises_keyerror(self, tmp_path):
        mse = _store(tmp_path)
        with pytest.raises(KeyError):
            _finish(mse, "streamZzz999")

    def test_bad_base64_rejected(self, tmp_path):
        mse = _store(tmp_path)
        mse.begin("streamGgg007")
        with pytest.raises(MseCaptureError, match="base64"):
            mse.chunk("streamGgg007", "!!!not-base64!!!")

    def test_empty_chunk_rejected(self, tmp_path):
        mse = _store(tmp_path)
        mse.begin("streamHhh008")
        with pytest.raises(MseCaptureError):
            mse.chunk("streamHhh008", _b64(b""))

    def test_duplicate_begin_rejected(self, tmp_path):
        mse = _store(tmp_path)
        mse.begin("streamIii009")
        with pytest.raises(MseCaptureError, match="已存在"):
            mse.begin("streamIii009")

    def test_stream_byte_cap(self, tmp_path, monkeypatch):
        import channels.mse_capture as mod

        monkeypatch.setattr(mod, "MAX_STREAM_BYTES", 16)
        mse = _store(tmp_path)
        mse.begin("streamJjj010")
        with pytest.raises(MseCaptureError, match="上限"):
            mse.chunk("streamJjj010", _b64(b"x" * 32))

    def test_open_stream_cap(self, tmp_path):
        mse = _store(tmp_path)
        for i in range(MAX_OPEN_STREAMS):
            mse.begin(f"streamKk{i:03d}x")
        with pytest.raises(MseCaptureError, match="过多"):
            mse.begin("streamLll999")

    def test_abort_discards_part_file(self, tmp_path):
        mse = _store(tmp_path)
        mse.begin("streamMmm011")
        mse.chunk("streamMmm011", _b64(_FTYP_HEAD))
        mse.abort("streamMmm011")
        assert not (tmp_path / "parts" / "mse_streamMmm011.part").exists()
        # abort 后可重新 begin 同名 id（幂等清理）。
        mse.begin("streamMmm011")
        mse.abort("streamMmm011")

    def test_original_title_is_used_when_upload_meta_empty(self, tmp_path):
        store = FeedStore()
        mse = _store(tmp_path, feed_store=store)
        mse.begin("streamNnn012")
        mse.chunk("streamNnn012", _b64(_FTYP_HEAD))
        result = _finish(mse, "streamNnn012", meta=None)
        feed = store.get(result["feed_id"])
        assert feed.title == "原视频标题"
        assert feed.author_name == "原作者"


class TestVirtualHostMseRoute:
    def _addon(self, tmp_path, feed_store=None, diagnostics=None, **kwargs):
        store = feed_store or FeedStore()
        diagnostics = diagnostics or ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        mse = MseCaptureStore(
            store, diagnostics, temp_dir=tmp_path / "parts", **kwargs
        )
        addon = VirtualHostAddon(
            pipeline,
            diagnostics,
            allowed_suffixes=("weixin.qq.com",),
            mse_store=mse,
        )
        return addon, mse, store, diagnostics

    def test_roundtrip_via_route(self, tmp_path):
        addon, mse, store, diagnostics = self._addon(tmp_path)
        sid = "streamOoo013"
        store.add([ChannelFeed(object_id="route-video", kind="video", duration=2,
                               title="路由保存")])

        flow = _mse_flow({"op": "begin", "stream_id": sid, "mime": "video/mp4"})
        addon.request(flow)
        assert flow.response.status_code == 200
        assert flow.response.json()["cap"] > 0

        flow = _mse_flow({"op": "chunk", "stream_id": sid, "b64": _b64(_FTYP_HEAD)})
        addon.request(flow)
        assert flow.response.status_code == 200

        flow = _mse_flow({"op": "end", "stream_id": sid, "meta": {
            "feed_id": "route-video", "complete": True, "duration": 2,
        }})
        addon.request(flow)
        assert flow.response.status_code == 200
        payload = flow.response.json()
        assert payload["ok"] is True
        assert store.get(payload["feed_id"]) is not None
        assert len(store) == 1
        assert diagnostics.strategy_stats() == {}

    def test_route_without_store_returns_503(self, tmp_path):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        addon = VirtualHostAddon(
            pipeline, diagnostics, allowed_suffixes=("weixin.qq.com",)
        )
        flow = _mse_flow({"op": "begin", "stream_id": "streamPpp014"})
        addon.request(flow)
        assert flow.response.status_code == 503

    def test_bad_op_returns_400(self, tmp_path):
        addon, *_ = self._addon(tmp_path)
        flow = _mse_flow({"op": "nope", "stream_id": "streamQqq015"})
        addon.request(flow)
        assert flow.response.status_code == 400

    def test_bad_stream_id_returns_400(self, tmp_path):
        addon, *_ = self._addon(tmp_path)
        flow = _mse_flow({"op": "begin", "stream_id": "bad/id!"})
        addon.request(flow)
        assert flow.response.status_code == 400

    def test_chunk_unknown_stream_returns_404(self, tmp_path):
        addon, *_ = self._addon(tmp_path)
        flow = _mse_flow({"op": "chunk", "stream_id": "streamRrr016", "b64": _b64(b"abc")})
        addon.request(flow)
        assert flow.response.status_code == 404

    def test_origin_mismatch_returns_403(self, tmp_path):
        addon, *_ = self._addon(tmp_path)
        body = json.dumps({"op": "begin", "stream_id": "streamSss017"}).encode()
        request = Request.make(
            "POST",
            f"https://{_HOST}/__cuin/mse",
            body,
            {
                "Content-Type": "application/json",
                "Origin": "https://evil.example.com",
            },
        )
        flow = SimpleNamespace(request=request, response=None)
        addon.request(flow)
        assert flow.response.status_code == 403

    def test_wrong_method_returns_405(self, tmp_path):
        addon, *_ = self._addon(tmp_path)
        request = Request.make("GET", f"https://{_HOST}/__cuin/mse", b"", {})
        flow = SimpleNamespace(request=request, response=None)
        addon.request(flow)
        assert flow.response.status_code == 405


class TestMseAutoSave:
    """v2.0.4 自动保存：开关属性 + finalize 成功计入会话统计。"""

    def test_auto_save_default_on(self, tmp_path):
        mse = _store(tmp_path)
        assert mse.auto_save is True
        mse.auto_save = False
        assert mse.auto_save is False

    def test_finalize_counts_session_stats(self, tmp_path):
        from core.downloader_base import DownloadResult

        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        stats = DownloadResult()
        mse = MseCaptureStore(
            store,
            diagnostics,
            temp_dir=tmp_path / "parts",
            stats=stats,
        )
        mse.begin("streamTtt018", mime="video/mp4")
        mse.chunk("streamTtt018", _b64(_FTYP_HEAD))

        result = _finish(mse, "streamTtt018", {})
        assert result["bytes"] > 0
        # 分享链接模式据此判断「目标已下载」并结束会话。
        assert stats.success == 1
        assert stats.total == 1
        assert diagnostics.get("parsed_feeds") == 0

    def test_finalize_stats_survives_failure(self, tmp_path):
        from core.downloader_base import DownloadResult

        stats = DownloadResult()
        mse = MseCaptureStore(
            FeedStore(), ChannelsDiagnostics(),
            temp_dir=tmp_path / "parts", stats=stats,
        )
        mse.begin("streamUuu019", mime="video/mp4")
        # 不含 ftyp 头：finalize 拒绝，统计不得增加。
        mse.chunk("streamUuu019", _b64(b"\x00" * 200))
        with pytest.raises(MseCaptureError):
            _finish(mse, "streamUuu019", {})
        assert stats.success == 0
