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
    def test_refreshes_original_and_quality_metadata_without_requeueing(self):
        store = FeedStore()
        existing = _feed("a", status="done")
        store.add([existing])
        store.clear_new_event()
        refresh = _feed("a", url="https://cdn/fresh.mp4")
        refresh.full_url = "https://cdn/fresh-original.mp4?token=fresh"
        refresh.specs = [{"fileFormat": "hd", "width": 1920, "height": 1080}]
        assert store.add([refresh]) == []
        assert existing.full_url == refresh.full_url
        assert existing.specs == refresh.specs
        assert existing.status == "done" and not store.has_new
        store.add([_feed("a")])
        assert existing.full_url == refresh.full_url and existing.specs == refresh.specs

    def test_sparse_refresh_keeps_high_specs_and_replaces_matching_metadata(self):
        store = FeedStore()
        existing = _feed("a", status="done")
        existing.specs = [
            {"fileFormat": "high", "width": 1920, "height": 1080},
            {"fileFormat": "low", "width": 1280, "height": 720, "bitrate": 100},
        ]
        store.add([existing])
        fresh = _feed("a", url="https://cdn/fresh.mp4?sign=fresh")
        fresh.width, fresh.height = 1280, 720
        fresh.specs = [{"fileFormat": "low", "width": 1280, "height": 720, "bitrate": 200}]
        assert store.add([fresh]) == []
        assert [s["fileFormat"] for s in existing.specs] == ["high", "low"]
        assert existing.specs[1]["bitrate"] == 200
        assert (existing.width, existing.height) == (1280, 720)
        from channels.feed import pick_quality_url
        assert pick_quality_url(existing) == fresh.url + "&X-snsvideoflag=high"

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

    def test_refresh_fills_original_metadata_without_lowering_duration(self):
        store = FeedStore()
        original = ChannelFeed(object_id="a", kind="video", title="a")
        store.add([original])
        full = ChannelFeed(object_id="a", kind="video", title="完整视频", author_name="作者",
                           duration=23, file_size=500000, cover_url="https://cdn/cover.jpg")
        assert store.add([full]) == []
        assert store.get("a") is original
        assert (original.title, original.author_name, original.duration, original.file_size) == (
            "完整视频", "作者", 23, 500000,
        )
        assert original.cover_url == full.cover_url
        store.add([ChannelFeed(object_id="a", kind="video", duration=6)])
        assert original.duration == 23
        assert len(store) == 1

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

    def test_trim_evicts_oldest_first(self):
        # 2.0.1 语义：done 也是可淘汰终态（旧实现永不淘汰 done，导致列表
        # 无界增长）；淘汰按插入序从最旧开始。
        store = FeedStore(max_items=3)
        store.add([_feed("a"), _feed("b"), _feed("c")])
        store.get("nonce_a").status = "done"
        store.add([_feed("d"), _feed("e")])
        assert len(store) == 3
        assert store.get("nonce_a") is None  # done（最旧）先于 pending 淘汰
        assert store.get("nonce_b") is None
        assert {f.object_id for f in store.all()} == {"c", "d", "e"}


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


class TestBoundedCache:
    """P1：max_items 必须是严格上限；所有索引同步清理，不允许 stale index。"""

    def test_done_items_are_evicted_oldest_first_when_over_limit(self):
        store = FeedStore(max_items=3)
        store.add([_feed("a"), _feed("b"), _feed("c")])
        for feed in store.all():
            feed.status = "done"
        store.add([_feed("d")])
        assert len(store) == 3
        # 最旧的 done（a）被淘汰，不是拒绝入库。
        assert store.get("a") is None
        assert {f.object_id for f in store.all()} == {"b", "c", "d"}

    def test_eviction_priority_failed_then_skipped_then_done(self):
        store = FeedStore(max_items=3)
        # 带终态入库，避免 setup 阶段就被当成 pending 淘汰。
        store.add([
            _feed("old_done", status="done"),
            _feed("old_skipped", status="skipped"),
            _feed("old_failed", status="failed"),
        ])
        store.add([_feed("keep"), _feed("new1")])
        # 一次溢出 2 条：先 failed、再 skipped（终态优先于 done/pending）。
        assert store.get("old_failed") is None
        assert store.get("old_skipped") is None
        assert store.get("old_done") is not None
        store.add([_feed("new2")])
        assert store.get("old_done") is None
        assert len(store) == 3

    def test_downloading_never_evicted_while_evictable_exist(self):
        store = FeedStore(max_items=3)
        store.add([_feed("a"), _feed("b"), _feed("c")])
        store.get("a").status = "downloading"
        for key in ("b", "c"):
            store.get(key).status = "done"
        store.add([_feed("d")])
        statuses = {f.object_id: f.status for f in store.all()}
        assert statuses.get("a") == "downloading"
        assert len(store) == 3

    def test_absolute_bound_even_all_downloading(self):
        """极端场景：超过容量的全部是 downloading，也不能无限增长。"""
        store = FeedStore(max_items=2)
        store.add([_feed("a"), _feed("b"), _feed("c"), _feed("d")])
        for feed in store.all():
            feed.status = "downloading"
        store.add([_feed("e")])
        assert len(store) <= 2 + 1  # 允许在飞条目，但绝不累积

    def test_indexes_cleaned_on_eviction(self):
        store = FeedStore(max_items=2)
        store.add([_feed("a"), _feed("b")])
        for feed in store.all():
            feed.status = "done"
        store.add([_feed("c")])
        # feed_id 与 object_id 两个索引都必须清干净，不允许 stale entry。
        assert store.get("nonce_a") is None
        assert store.get("a") is None
        # 淘汰后同 objectId 重新捕获：重新入库且索引可用。
        store.add([_feed("a", status="pending")])
        assert store.get("a") is not None
        assert store.get("nonce_a") is not None

    def test_long_capture_session_stays_bounded(self):
        import random

        store = FeedStore(max_items=50)
        statuses = ["done", "failed", "skipped", "pending"]
        for i in range(500):
            store.add([_feed(f"id{i}", status=random.choice(statuses))])
        assert len(store) <= 50
        # 索引规模与条目数一致。
        assert len(store._id_index) <= len(store) * 2


def _media_flow(url: str = "https://finder.video.qq.com/251/20302/stodownload?encfilekey=abc&token=x&seq=3", *, status: int = 200, range_header: str = "", content_range: str = "", length: int = 1024):
    """构造带 headers 的媒体分片假 flow（_record_media_request 只碰头部）。

    headers 用普通 dict——产品代码走 ``headers.get(name)``，dict 原生 get
    即可满足（缺键返回 None 与 mitmproxy Headers 行为一致）。
    """
    resp_headers = {
        "content-range": content_range,
        "content-type": "video/mp4",
        "content-length": str(length),
    }
    return SimpleNamespace(
        request=SimpleNamespace(
            pretty_host="finder.video.qq.com",
            path=url.split("?", 1)[1] if "?" in url else "/",
            method="GET",
            url=url,
            headers={"range": range_header} if range_header else {},
        ),
        response=SimpleNamespace(
            status_code=status,
            headers=resp_headers,
        ),
    )


class TestSnifferMediaRecording:
    """媒体分片 URL 记录（直播回放全量下载依据）：只记头、不进 feed 通道。"""

    def test_media_url_recorded(self):
        store = FeedStore()
        addon = SnifferAddon(store)
        addon.response(_media_flow(range_header="bytes=0-1023", content_range="bytes 0-1023/9999999"))
        snap = addon.diagnostics.snapshot()
        assert snap["counters"]["media_requests"] == 1
        assert snap["counters"]["media_request_bytes"] == 1024
        assert len(snap["media_urls"]) == 1
        entry = snap["media_urls"][0]
        assert "encfilekey=abc" in entry["url"]
        assert entry["range"] == "bytes=0-1023"
        assert entry["status"] == 200

    def test_media_host_does_not_create_feed(self):
        # 媒体域响应不含 feed 标记，且不在解析白名单——store 必须为空。
        store = FeedStore()
        addon = SnifferAddon(store)
        addon.response(_media_flow())
        assert len(store) == 0

    def test_non_media_flow_without_headers_still_safe(self):
        # 旧式无 headers 属性的假 flow（非媒体域）不应因 _record_media_request 崩溃。
        store = FeedStore()
        addon = SnifferAddon(store)
        addon.response(_flow("channels.weixin.qq.com", json.dumps(_payload(["1"]))))
        assert len(store) == 1
