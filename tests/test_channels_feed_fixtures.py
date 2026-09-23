"""视频号 feed 解析的脱敏 fixture 回归测试（P2 能力边界锚点）。

fixture 是按真实响应**结构**构造的脱敏样本（ID / 域名 / 签名均为假值），
覆盖：普通视频 / 图文 / 直播 / 直播回放 / 多清晰度 / 缺失字段 / 字段类型
异常 / 重复 feed。微信协议字段变化时，先更新 fixture 再改 parser，
让「结构变化」在 code review 里可见。
"""

from __future__ import annotations

import json
from pathlib import Path

from channels.feed import (
    MEDIA_TYPE_IMAGE,
    MEDIA_TYPE_LIVE,
    extract_feeds,
    pick_quality_url,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _one(name: str):
    feeds = extract_feeds(_load(name), source_api=f"fixture:{name}")
    assert len(feeds) == 1, f"{name}: 期望恰好 1 条，得到 {len(feeds)}"
    return feeds[0]


class TestFixtures:
    def test_video_normal(self):
        feed = _one("video_normal.json")
        assert feed.kind == "video"
        assert feed.media_type == 4
        assert feed.object_id == "14059479562123456789"
        assert feed.decode_key == 7106243968264845312
        assert feed.duration == 32
        assert feed.file_size == 5242880
        assert feed.author_name == "示例作者"
        # 标题压缩了换行、保留了话题文本。
        assert "示例视频" in feed.title and "\n" not in feed.title
        # 干净直链只保留 encfilekey / token 参数。
        assert feed.url.startswith("https://finder.video.qq.com/")
        assert "dis_k=demo" in feed.url

    def test_image_post(self):
        feed = _one("image_post.json")
        assert feed.kind == "image"
        assert feed.media_type == MEDIA_TYPE_IMAGE
        assert len(feed.images) == 3
        assert feed.bgm_url.endswith("bgm_demo.m4a")
        assert feed.cover_url.endswith("img_1.jpg")

    def test_live(self):
        feed = _one("live.json")
        assert feed.kind == "live"
        assert feed.media_type == MEDIA_TYPE_LIVE
        assert feed.url.endswith(".flv?txSecret=demo")
        assert feed.cover_url.endswith("live_cover.jpg")

    def test_live_replay_is_encrypted_video(self):
        feed = _one("live_replay.json")
        assert feed.kind == "video"
        assert feed.decode_key is not None
        assert feed.duration > 3600

    def test_multi_quality(self):
        feed = _one("multi_quality.json")
        assert len(feed.specs) == 4
        # 指定档位 → 追加 X-snsvideoflag 切换转码档。
        url_720 = pick_quality_url(feed, "720p")
        assert "X-snsvideoflag=bd" in url_720
        url_low = pick_quality_url(feed, "lowest")
        assert "X-snsvideoflag=sd" in url_low
        highest = pick_quality_url(feed, "highest")
        assert "X-snsvideoflag" not in highest

    def test_missing_fields_fallbacks(self):
        feed = _one("missing_fields.json")
        assert feed.kind == "video"
        assert feed.title == feed.object_id  # 标题回退 objectId
        assert feed.cover_url == ""
        assert feed.specs == []
        assert feed.duration == 0
        # nonce 缺失时 feed_id 回退 object_id。
        assert feed.feed_id == feed.object_id

    def test_weird_types_do_not_crash(self):
        feed = _one("weird_types.json")
        assert feed.kind == "video"
        # mediaType 以字符串 "4" 下发也能识别；列表里混入非 dict 被跳过。
        assert feed.media_type == 4
        # decodeKey 是 int 下发：同样转成 int。
        assert feed.decode_key == 7106243968264845316
        # spec 不是列表：按空处理，不抛异常。
        assert feed.specs == []

    def test_duplicate_feeds_dedup_in_extraction(self):
        feeds = extract_feeds(_load("duplicate_feeds.json"))
        assert len(feeds) == 1
        assert feeds[0].object_id == "14059479562123456796"
        # 同一响应内重复：保留先出现的（直链刷新语义由 FeedStore 负责）。

    def test_duplicate_across_responses_refreshes_url_in_store(self):
        from channels.feed import ChannelFeed
        from channels.feed_store import FeedStore

        store = FeedStore()
        feeds = extract_feeds(_load("duplicate_feeds.json"))
        store.add(feeds)
        # 模拟推荐流再次下发同一 objectId（带更新的签名参数）。
        fresh = ChannelFeed(
            object_id="14059479562123456796",
            nonce_id="nonce_dup_b",
            url="https://finder.video.qq.com/dup.mp4?sig=fresher&idx=3",
            decode_key=7106243968264845317,
        )
        assert len(store.add([fresh])) == 0  # 不算新捕获
        assert store.get("nonce_dup_a").url.endswith("idx=3")  # 直链已刷新
