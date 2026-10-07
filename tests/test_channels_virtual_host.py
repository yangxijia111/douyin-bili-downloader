"""``/__cuin/*`` 虚拟端点测试（离线；请求用真实 mitmproxy Request）。

覆盖：Feed 桥接校验（大小/类型/schema/同源）、心跳、任务提交与状态、
虚拟静态资源（含路径穿越防护）、非 __cuin 请求零影响、永不转发上游。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from channels.diagnostics import ChannelsDiagnostics
from channels.feed_store import FeedStore
from channels.pipeline import FeedCapturePipeline
from channels.virtual_host import (
    MAX_FEED_BODY_BYTES,
    MAX_FEED_NODES,
    VIRTUAL_ASSETS_DIR,
    VirtualHostAddon,
    validate_feed_payload,
)

mitmproxy_http = pytest.importorskip("mitmproxy.http")
Request = mitmproxy_http.Request

_HOST = "channels.weixin.qq.com"


def _node(object_id: str) -> dict:
    return {
        "objectId": object_id,
        "objectNonceId": f"n_{object_id}",
        "contact": {"nickname": "作者"},
        "objectDesc": {
            "mediaType": 4,
            "description": f"视频{object_id}",
            "media": [{"url": f"https://cdn/{object_id}.mp4", "decodeKey": "123"}],
        },
    }


def _flow(method: str, path: str, *, body: bytes = b"", headers: dict | None = None):
    """构造带真实 mitmproxy Request 的假 flow。"""
    request = Request.make(
        method, f"https://{_HOST}{path}", body, headers or {}
    )
    return SimpleNamespace(request=request, response=None)


def _feed_flow(nodes, *, strategy: str = "page_network_hook", page: str = "home",
               headers: dict | None = None, raw_body: bytes | None = None) -> SimpleNamespace:
    if raw_body is None:
        raw_body = json.dumps(
            {"strategy": strategy, "page": page, "feeds": nodes}
        ).encode("utf-8")
    base_headers = {
        "Content-Type": "application/json",
        "Origin": f"https://{_HOST}",
    }
    base_headers.update(headers or {})
    return _flow("POST", "/__cuin/feed", body=raw_body, headers=base_headers)


def _addon(store: FeedStore | None = None, diagnostics: ChannelsDiagnostics | None = None,
           task_hub=None, mse_store=None) -> VirtualHostAddon:
    # 注意：不能用 `store or FeedStore()`——FeedStore 定义了 __len__，
    # 空 store 是 falsy，会每次都新建一个。
    if store is None:
        store = FeedStore()
    if diagnostics is None:
        diagnostics = ChannelsDiagnostics()
    pipeline = FeedCapturePipeline(store, diagnostics)
    return VirtualHostAddon(
        pipeline, diagnostics,
        allowed_suffixes=("weixin.qq.com",), task_hub=task_hub,
        mse_store=mse_store,
    )


class TestFeedBridge:
    def test_bridge_has_safe_quality_options_and_original_capability(self):
        addon = _addon()
        node = _node("a")
        media = node["objectDesc"]["media"][0]
        media["fullUrl"] = "https://finder.video.qq.com/original.mp4?token=secret"
        media["spec"] = [
            {"fileFormat": "hd", "width": 1920, "height": 1080, "url": "https://cdn/hd?token=secret"},
            {"fileFormat": "bad&token=secret", "width": 3840, "height": 2160},
        ]
        flow = _feed_flow([node])
        addon.request(flow)
        view = flow.response.json()["feeds"][0]
        assert view["has_original"] is True
        assert "format:hd" in [o["value"] for o in view["quality_options"]]
        assert "hd" in view["qualities"]
        assert all(set(o) == {"value", "label", "width", "height"} for o in view["quality_options"])
        assert "secret" not in json.dumps(view)

    def test_replay_page_receives_existing_capability_without_requeueing_profile_capture(self):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        on_capture = Mock()
        pipeline = FeedCapturePipeline(store, diagnostics, on_capture=on_capture)
        addon = VirtualHostAddon(pipeline, diagnostics)
        profile = _feed_flow([_node("replay")], page="profile")
        addon.request(profile)
        assert profile.response.json()["accepted"] == 1
        store.clear_new_event()

        duplicate = _node("replay")
        duplicate["id"] = duplicate.pop("objectId")
        duplicate.pop("objectNonceId")
        duplicate["objectDesc"]["media"][0]["url"] = "https://cdn/replay.mp4?token=secret"
        replay = _feed_flow([duplicate, duplicate], strategy="page_runtime_hook", page="feed")
        addon.request(replay)
        payload = replay.response.json()

        assert payload["accepted"] == 0
        assert len(payload["feeds"]) == 1
        view = payload["feeds"][0]
        assert view["object_id"] == "replay"
        assert view["has_url"] is True
        assert view["has_decode_key"] is True
        assert "url" not in view and "decode_key" not in view
        assert "token=secret" not in json.dumps(payload)
        assert len(store) == 1
        assert store.has_new is False
        assert diagnostics.get("parsed_feeds") == 1
        on_capture.assert_called_once()

    def test_embedded_id_only_returns_downloadable_view(self):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        addon = _addon(store, diagnostics)
        node = _node("14990527163746486645")
        node["id"] = node.pop("objectId")
        node.pop("objectNonceId")
        flow = _feed_flow([node], strategy="page_runtime_hook")
        addon.request(flow)
        payload = flow.response.json()
        assert payload["accepted"] == 1
        assert payload["feeds"][0]["object_id"] == "14990527163746486645"
        assert payload["feeds"][0]["has_url"] is True
        assert store.get("14990527163746486645").decode_key == 123
        assert diagnostics.strategy_stats() == {"page_runtime_hook": 1}

    def test_generic_id_has_no_downloadable_view(self):
        addon = _addon()
        flow = _feed_flow([{"id": "14990527163746486645", "url": "https://cdn/a.mp4"}])
        addon.request(flow)
        assert flow.response.json()["accepted"] == 0
        assert flow.response.json()["feeds"] == []

    def test_unparsed_node_cannot_fetch_capability_of_an_existing_feed_by_id(self):
        store = FeedStore()
        addon = _addon(store)
        addon.request(_feed_flow([_node("existing")], page="profile"))
        flow = _feed_flow([{"id": "existing", "url": "https://cdn/arbitrary.mp4"}], page="feed")
        addon.request(flow)
        payload = flow.response.json()
        assert payload["accepted"] == 0
        assert payload["feeds"] == []
        assert len(store) == 1

    def test_duplicate_valid_preview_node_keeps_its_existing_capability_view(self):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        addon = _addon(store, diagnostics)
        node = {"id": "preview", "videoUrl": "https://cdn/preview.mp4?token=secret"}
        first = _feed_flow([node], page="preview")
        addon.request(first)
        assert first.response.json()["accepted"] == 1
        repeated = _feed_flow([node], page="feed")
        addon.request(repeated)
        payload = repeated.response.json()
        assert payload["accepted"] == 0
        assert len(payload["feeds"]) == 1
        assert payload["feeds"][0]["object_id"] == "preview"
        assert payload["feeds"][0]["has_url"] is True
        assert "token=secret" not in json.dumps(payload)
        assert diagnostics.get("parsed_feeds") == 1

    def test_happy_path(self):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        addon = _addon(store, diagnostics)
        flow = _feed_flow([_node("a"), _node("b")])
        addon.request(flow)
        assert flow.response is not None
        assert flow.response.status_code == 200
        payload = flow.response.json()
        assert payload["ok"] is True
        assert payload["accepted"] == 2
        assert len(store) == 2
        assert diagnostics.get("parsed_feeds") == 2
        assert diagnostics.strategy_stats() == {"page_network_hook": 2}

    def test_dedup_second_post(self):
        store = FeedStore()
        addon = _addon(store)
        addon.request(_feed_flow([_node("a")]))
        flow = _feed_flow([_node("a")])
        addon.request(flow)
        assert flow.response.json()["accepted"] == 0
        assert len(store) == 1

    def test_view_has_no_sensitive_fields(self):
        """桥接响应不得包含直链 / decodeKey（能力视图即可驱动按钮）。"""
        addon = _addon()
        flow = _feed_flow([_node("a")])
        addon.request(flow)
        view = flow.response.json()["feeds"][0]
        assert view["kind"] == "video"
        assert view["has_url"] is True
        assert view["has_decode_key"] is True
        assert "url" not in view
        assert "decode_key" not in view
        assert "highest" in view["qualities"] and "lowest" in view["qualities"]

    def test_rejects_wrong_content_type(self):
        diagnostics = ChannelsDiagnostics()
        addon = _addon(diagnostics=diagnostics)
        flow = _feed_flow([_node("a")], headers={"Content-Type": "text/plain"})
        addon.request(flow)
        assert flow.response.status_code == 400
        assert diagnostics.get("feed_bridge_rejected") == 1

    def test_rejects_oversized_body(self):
        diagnostics = ChannelsDiagnostics()
        addon = _addon(diagnostics=diagnostics)
        huge = b'{"feeds":[' + b'{"objectDesc":{}},' * 1 + b"]}"
        huge = b"x" * (MAX_FEED_BODY_BYTES + 10)
        flow = _feed_flow([], raw_body=huge)
        addon.request(flow)
        assert flow.response.status_code == 400
        assert "上限" in flow.response.json()["error"]

    def test_rejects_invalid_schema(self):
        diagnostics = ChannelsDiagnostics()
        addon = _addon(diagnostics=diagnostics)
        cases = [
            b'{"feeds": "not-a-list"}',
            b'{"feeds": []}',
            b'{"feeds": ["string-node"]}',
            b'{"feeds": [null]}',
            b'[]',
        ]
        for raw in cases:
            flow = _feed_flow([], raw_body=raw)
            addon.request(flow)
            assert flow.response.status_code == 400, raw
        assert diagnostics.get("feed_bridge_rejected") == len(cases)

    def test_rejects_too_many_nodes(self):
        addon = _addon()
        nodes = [_node(str(i)) for i in range(MAX_FEED_NODES + 1)]
        flow = _feed_flow(nodes)
        addon.request(flow)
        assert flow.response.status_code == 400

    def test_rejects_origin_mismatch(self):
        diagnostics = ChannelsDiagnostics()
        addon = _addon(diagnostics=diagnostics)
        flow = _feed_flow(
            [_node("a")], headers={"Origin": "https://evil.example.com"}
        )
        addon.request(flow)
        assert flow.response.status_code == 403
        assert diagnostics.get("feed_bridge_rejected") == 1

    def test_accepts_referer_when_no_origin(self):
        addon = _addon()
        flow = _feed_flow([_node("a")], headers={"Origin": ""})
        # Origin 显式为空时按缺失处理，Referer 同源即可。
        flow.request.headers["Referer"] = f"https://{_HOST}/web/pages/home"
        addon.request(flow)
        assert flow.response.status_code == 200

    def test_rejects_missing_origin_and_referer(self):
        addon = _addon()
        flow = _feed_flow([_node("a")], headers={"Origin": ""})
        flow.request.headers.pop("Origin", None)
        addon.request(flow)
        assert flow.response.status_code == 403


class TestHeartbeat:
    def test_heartbeat_updates_diagnostics(self):
        diagnostics = ChannelsDiagnostics()
        addon = _addon(diagnostics=diagnostics)
        body = json.dumps(
            {"page": "home", "url": "/web/pages/home", "buttons": 2}
        ).encode()
        flow = _flow(
            "POST", "/__cuin/heartbeat", body=body,
            headers={"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
        )
        addon.request(flow)
        assert flow.response.status_code == 200
        assert diagnostics.get("frontend_heartbeat") == 1
        assert diagnostics.get("buttons_created") == 2
        snapshot = diagnostics.snapshot()
        assert snapshot["page_type"] == "home"

    def test_heartbeat_tolerates_bad_json(self):
        diagnostics = ChannelsDiagnostics()
        addon = _addon(diagnostics=diagnostics)
        flow = _flow(
            "POST", "/__cuin/heartbeat", body=b"not-json",
            headers={"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
        )
        addon.request(flow)
        assert flow.response.status_code == 400

    def test_heartbeat_returns_mse_auto_save_flag(self):
        """v2.0.4：心跳响应下发 MSE 自动保存开关（页面据此决定自动上传）。"""
        from types import SimpleNamespace as NS

        body = json.dumps({"page": "feed"}).encode()
        headers = {
            "Content-Type": "application/json",
            "Origin": f"https://{_HOST}",
        }

        # 有 mse_store：透传其 auto_save 属性（True 与 False 两种状态）。
        for flag in (True, False):
            addon = _addon(mse_store=NS(auto_save=flag))
            flow = _flow("POST", "/__cuin/heartbeat", body=body, headers=headers)
            addon.request(flow)
            assert flow.response.status_code == 200
            assert flow.response.json()["mse_auto_save"] is flag

        # 无 mse_store：开关为 False（页面不应自动上传）。
        addon = _addon()
        flow = _flow("POST", "/__cuin/heartbeat", body=body, headers=headers)
        addon.request(flow)
        assert flow.response.status_code == 200
        assert flow.response.json()["mse_auto_save"] is False


class TestTaskEndpoints:
    @pytest.mark.parametrize("quality", ["highest", "lowest", "original", "1080p", "720p", "format:" + "x" * 128])
    def test_submit_accepts_safe_quality_and_full_format_length(self, quality):
        hub = Mock()
        hub.submit.return_value = "task123"
        addon = _addon(task_hub=hub)
        flow = _flow(
            "POST", "/__cuin/task",
            body=json.dumps({"feed_id": "x", "action": "download", "quality": quality}).encode(),
            headers={"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
        )
        addon.request(flow)
        assert flow.response.status_code == 200
        hub.submit.assert_called_once_with("x", action="download", quality=quality)

    @pytest.mark.parametrize("quality", [
        "https://finder.video.qq.com/v.mp4", "format:hd&token=secret", "format:",
        "format:" + "x" * 129, "highest?token=secret", "1080p/../", "format:中文", 1080,
    ])
    def test_submit_rejects_unsafe_quality_without_queueing(self, quality):
        hub = Mock()
        addon = _addon(task_hub=hub)
        flow = _flow(
            "POST", "/__cuin/task",
            body=json.dumps({"feed_id": "x", "action": "download", "quality": quality}).encode(),
            headers={"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
        )
        addon.request(flow)
        assert flow.response.status_code == 400
        assert flow.response.json()["error"] == "bad quality"
        hub.submit.assert_not_called()

    def test_submit_without_hub_returns_503(self):
        addon = _addon()
        flow = _flow(
            "POST", "/__cuin/task",
            body=json.dumps({"feed_id": "x", "action": "download"}).encode(),
            headers={"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
        )
        addon.request(flow)
        assert flow.response.status_code == 503

    def test_submit_and_status(self):
        store = FeedStore()
        store.add([_feed_like(_node("a"))])

        class _Hub:
            def submit(self, feed_id, *, action="download", quality=None):
                return "task123"

            def status(self, task_id):
                return {"task_id": task_id, "status": "done"}

        addon = _addon(store, task_hub=_Hub())
        flow = _flow(
            "POST", "/__cuin/task",
            body=json.dumps(
                {"feed_id": store.all()[0].feed_id, "action": "download", "quality": "highest"}
            ).encode(),
            headers={"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
        )
        addon.request(flow)
        assert flow.response.status_code == 200
        assert flow.response.json()["task_id"] == "task123"

        status_flow = _flow("GET", "/__cuin/task/task123")
        addon.request(status_flow)
        assert status_flow.response.json()["task"]["status"] == "done"

    def test_submit_bad_action(self):
        class _Hub:
            def submit(self, feed_id, *, action="download", quality=None):
                return "x"

            def status(self, task_id):
                return None

        addon = _addon(task_hub=_Hub())
        flow = _flow(
            "POST", "/__cuin/task",
            body=json.dumps({"feed_id": "x", "action": "destroy"}).encode(),
            headers={"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
        )
        addon.request(flow)
        assert flow.response.status_code == 400

    def test_submit_unknown_feed_returns_404(self):
        class _Hub:
            def submit(self, feed_id, *, action="download", quality=None):
                raise KeyError(feed_id)

            def status(self, task_id):
                return None

        addon = _addon(task_hub=_Hub())
        flow = _flow(
            "POST", "/__cuin/task",
            body=json.dumps({"feed_id": "nope", "action": "download"}).encode(),
            headers={"Content-Type": "application/json", "Origin": f"https://{_HOST}"},
        )
        addon.request(flow)
        assert flow.response.status_code == 404

    def test_status_unknown_task_returns_404(self):
        class _Hub:
            def submit(self, feed_id, *, action="download", quality=None):
                return "x"

            def status(self, task_id):
                return None

        addon = _addon(task_hub=_Hub())
        flow = _flow("GET", "/__cuin/task/missing")
        addon.request(flow)
        assert flow.response.status_code == 404

    def test_wrong_method_rejected(self):
        addon = _addon()
        flow = _flow("GET", "/__cuin/feed")
        addon.request(flow)
        assert flow.response.status_code == 405


class TestVirtualAssets:
    def test_serve_js_asset(self):
        addon = _addon()
        flow = _flow("GET", "/__cuin/assets/bootstrap.js")
        addon.request(flow)
        assert flow.response.status_code == 200
        assert "javascript" in flow.response.headers["content-type"]
        assert flow.response.raw_content == (VIRTUAL_ASSETS_DIR / "bootstrap.js").read_bytes()

    def test_serve_css_asset(self):
        addon = _addon()
        flow = _flow("GET", "/__cuin/assets/channels.css")
        addon.request(flow)
        assert flow.response.status_code == 200
        assert "text/css" in flow.response.headers["content-type"]

    def test_path_traversal_rejected(self):
        addon = _addon()
        for bad in (
            "/__cuin/assets/..%2f..%2fpyproject.toml",
            "/__cuin/assets/sub/dir/file.js",
        ):
            flow = _flow("GET", bad)
            addon.request(flow)
            assert flow.response.status_code == 400, bad

    def test_unknown_extension_404(self):
        addon = _addon()
        flow = _flow("GET", "/__cuin/assets/secret.txt")
        addon.request(flow)
        assert flow.response.status_code == 404

    def test_missing_asset_404(self):
        addon = _addon()
        flow = _flow("GET", "/__cuin/assets/nonexistent.js")
        addon.request(flow)
        assert flow.response.status_code == 404


class TestIsolation:
    def test_non_cuin_path_untouched(self):
        addon = _addon()
        flow = _flow("GET", "/web/pages/home")
        addon.request(flow)
        assert flow.response is None  # 未被虚拟端点接管

    def test_cuin_path_on_other_host_untouched(self):
        addon = _addon()
        request = Request.make(
            "POST", "https://www.douyin.com/__cuin/feed", b"{}",
            {"Content-Type": "application/json"},
        )
        flow = SimpleNamespace(request=request, response=None)
        addon.request(flow)
        assert flow.response is None  # 非白名单域：完全不干预

    def test_unknown_cuin_endpoint_404(self):
        addon = _addon()
        flow = _flow("GET", "/__cuin/unknown")
        addon.request(flow)
        assert flow.response.status_code == 404

    def test_internal_error_returns_500_not_raise(self):
        """虚拟端点内部异常必须转 500，绝不向上抛（不破坏页面）。"""

        class _BrokenPipeline:
            def ingest_page_nodes(self, *args, **kwargs):
                raise RuntimeError("pipeline boom")

        addon = VirtualHostAddon(_BrokenPipeline(), ChannelsDiagnostics())
        flow = _feed_flow([_node("a")])
        addon.request(flow)  # 不抛
        assert flow.response.status_code == 500
        assert flow.response.json()["ok"] is False


class TestValidatePayload:
    def test_valid_payload(self):
        nodes, strategy, page = validate_feed_payload(
            {"feeds": [_node("a")], "strategy": "page_runtime_hook", "page": "live"}
        )
        assert len(nodes) == 1
        assert strategy == "page_runtime_hook"
        assert page == "live"

    def test_bad_strategy_and_page_sanitized(self):
        nodes, strategy, page = validate_feed_payload(
            {"feeds": [_node("a")], "strategy": 123, "page": None}
        )
        assert strategy == ""
        assert page == ""

    def test_build_view_for_image_feed(self):
        addon = _addon()
        image_node = {
            "objectId": "img1",
            "objectNonceId": "n_img1",
            "objectDesc": {
                "mediaType": 2,
                "media": [{"url": "https://cdn/a.jpg", "coverUrl": "https://cdn/c.jpg"}],
            },
        }
        flow = _feed_flow([image_node])
        addon.request(flow)
        view = flow.response.json()["feeds"][0]
        assert view["kind"] == "image"
        assert view["has_cover"] is True


def _feed_like(node: dict):
    """把节点经 pipeline 转成 ChannelFeed（构造 store 条目用）。"""
    from channels.feed import extract_feeds

    feeds = extract_feeds([node])
    assert feeds, node
    return feeds[0]
