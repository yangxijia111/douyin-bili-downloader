"""FeedStore 与 SnifferAddon 测试（全部离线，flow 用假对象代替 mitmproxy）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from channels.feed import ChannelFeed
from channels.feed_store import FeedStore
from channels.interceptor import SnifferAddon


def _feed(object_id: str, *, url: str = "https://cdn/v.mp4", status: str = "pending") -> ChannelFeed:
    return ChannelFeed(
        object_id=object_id,
        nonce_id=f"nonce_{object_id}",
        kind="video",
        url=url,
        title=f"标题{object_id}",
        status=status,
    )


class TestFeedStore:
    def test_add_returns_only_fresh(self):
        store = FeedStore()
        fresh = store.add([_feed("a"), _feed("b")])
        assert [f.object_id for f in fresh] == ["a", "b"]
        assert len(store) == 2

        # 同 objectId 再来（推荐流重复下发）不算新捕获。
        again = store.add([_feed("a", url="https://cdn/v2.mp4"), _feed("c")])
        assert [f.object_id for f in again] == ["c"]
        assert len(store) == 3
        # 但旧条目的直链被刷新（时效参数更新鲜）。
        assert store.get("nonce_a").url == "https://cdn/v2.mp4"

    def test_add_preserves_status_on_refresh(self):
        store = FeedStore()
        store.add([_feed("a")])
        store.get("nonce_a").status = "done"
        store.add([_feed("a")])
        assert store.get("nonce_a").status == "done"

    def test_get_by_id_variants(self):
        store = FeedStore()
        store.add([_feed("abc")])
        assert store.get("nonce_abc") is not None
        assert store.get("abc") is not None      # objectId 也可查（= dedup_key 兜底）
        assert store.get("missing") is None

    def test_new_event(self):
        store = FeedStore()
        assert not store.has_new
        store.add([_feed("a")])
        assert store.has_new
        store.clear_new_event()
        assert not store.has_new

    @pytest.mark.asyncio
    async def test_wait_for_new_timeout(self):
        store = FeedStore()
        assert await store.wait_for_new(timeout=0.01) is False

    @pytest.mark.asyncio
    async def test_wait_for_new_immediate(self):
        store = FeedStore()
        store.add([_feed("a")])
        assert await store.wait_for_new(timeout=0.1) is True

    def test_trim_drops_pending_keeps_done(self):
        store = FeedStore(max_items=3)
        store.add([_feed("a"), _feed("b"), _feed("c")])
        store.get("nonce_a").status = "done"
        store.add([_feed("d"), _feed("e")])
        assert len(store) == 3
        assert store.get("nonce_a") is not None  # done 保留
        assert store.get("nonce_b") is None      # 最旧的 pending 被淘汰


def _flow(host: str, body: str, path: str = "/web/runtime/finderPcFlow"):
    return SimpleNamespace(
        request=SimpleNamespace(pretty_host=host, path=path),
        response=SimpleNamespace(get_text=lambda strict=False: body),
    )


def _payload(object_ids):
    return {
        "data": [
            {
                "objectId": oid,
                "objectNonceId": f"n_{oid}",
                "contact": {"nickname": "作者"},
                "objectDesc": {
                    "mediaType": 4,
                    "description": f"视频{oid}",
                    "media": [{"url": f"https://cdn/{oid}.mp4", "decodeKey": "123"}],
                },
            }
            for oid in object_ids
        ]
    }


class TestSnifferAddon:
    def test_extracts_from_weixin_channels_host(self):
        store = FeedStore()
        addon = SnifferAddon(store)
        addon.response(_flow("channels.weixin.qq.com", json.dumps(_payload(["1", "2"]))))
        assert len(store) == 2

    def test_ignores_other_hosts(self):
        store = FeedStore()
        addon = SnifferAddon(store)
        addon.response(_flow("www.douyin.com", json.dumps(_payload(["1"]))))
        addon.response(_flow("example.weixin.qq.com.evil.com", json.dumps(_payload(["2"]))))
        assert len(store) == 0

    def test_ignores_body_without_marker(self):
        store = FeedStore()
        addon = SnifferAddon(store)
        addon.response(_flow("channels.weixin.qq.com", '{"data": {"foo": 1}}'))
        addon.response(_flow("channels.weixin.qq.com", "not json at all"))
        assert len(store) == 0

    def test_ignores_huge_body(self):
        store = FeedStore()
        addon = SnifferAddon(store)
        huge = "x" * (SnifferAddon.MAX_BODY_BYTES + 1)
        addon.response(_flow("channels.weixin.qq.com", huge))
        assert len(store) == 0

    def test_on_capture_callback(self):
        captured = []
        store = FeedStore()
        addon = SnifferAddon(store, on_capture=captured.append)
        addon.response(_flow("channels.weixin.qq.com", json.dumps(_payload(["9"]))))
        assert len(captured) == 1 and captured[0][0].object_id == "9"

    def test_malformed_json_does_not_raise(self):
        store = FeedStore()
        addon = SnifferAddon(store)
        addon.response(_flow("channels.weixin.qq.com", '{"objectDesc": broken'))
        assert len(store) == 0
