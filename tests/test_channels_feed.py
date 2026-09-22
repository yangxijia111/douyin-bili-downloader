"""channels.feed —— 视频号响应 JSON 的 feed 提取与画质选择测试。"""

from __future__ import annotations

from channels.feed import (
    ChannelFeed,
    clean_media_url,
    extract_feeds,
    pick_quality_url,
)


def _video_feed_payload(**overrides) -> dict:
    media = {
        "url": "https://finder.video.qq.com/302/20304/10004/1f0004dbgmaca4abmiwjnvxfrkgidykw2fy.f100040.mp4?dis_k=8be4f&dis_t=1760000000&src=3&vsig=xxx",
        "urlToken": "&encfilekey=abc123xyzdef456&token=qqeyJhbGciOi",
        "decodeKey": "18446744073709551615",
        "fileSize": 52428800,
        "videoPlayLen": 95,
        "coverUrl": "https://wx.qlogo.cn/cover/1.jpg",
        "spec": [
            {"fileFormat": "1", "width": 1080, "height": 1920},
            {"fileFormat": "3", "width": 540, "height": 960},
        ],
    }
    media.update(overrides.pop("media", {}))
    node = {
        "objectId": "obj_1001",
        "objectNonceId": "nonce_abc",
        "createtime": 1760000100,
        "contact": {
            "nickname": "测试作者",
            "username": "wxid_test",
            "headUrl": "https://wx.qlogo.cn/avatar/1.jpg",
        },
        "objectDesc": {
            "mediaType": 4,
            "description": "这是\n视频标题\n#话题",
            "media": [media],
        },
    }
    node.update(overrides)
    return node


class TestExtractFeeds:
    def test_video_fields(self):
        payload = {"data": {"object": _video_feed_payload()}}
        feeds = extract_feeds(payload, source_api="finderPcFlow")
        assert len(feeds) == 1
        feed = feeds[0]
        assert feed.kind == "video"
        assert feed.object_id == "obj_1001"
        assert feed.nonce_id == "nonce_abc"
        # decodeKey 以字符串下发（超出 JS 安全整数），必须正确转 int。
        assert feed.decode_key == 18446744073709551615
        assert feed.url.startswith("https://finder.video.qq.com/")
        assert feed.url.endswith("&encfilekey=abc123xyzdef456&token=qqeyJhbGciOi")
        assert feed.title == "这是 视频标题 #话题"  # 换行压缩
        assert feed.author_name == "测试作者"
        assert feed.author_id == "wxid_test"
        assert feed.duration == 95
        assert feed.file_size == 52428800
        assert feed.create_time == 1760000100
        assert len(feed.specs) == 2
        assert feed.source_api == "finderPcFlow"
        assert feed.token == "channels_obj_1001"

    def test_recursive_and_dedup(self):
        """嵌套层（关联推荐）里的 feed 也要提取；同 objectId 只保留一条。"""
        node = _video_feed_payload()
        related = _video_feed_payload(objectId="obj_1002", objectNonceId="nonce_b")
        dup = _video_feed_payload(objectNonceId="nonce_diff_same_object")
        payload = {"data": {"object_list": [node, dup], "extra": {"related": [related]}}}
        feeds = extract_feeds(payload)
        assert [f.object_id for f in feeds] == ["obj_1001", "obj_1002"]

    def test_image_feed(self):
        node = _video_feed_payload()
        node["objectDesc"] = {
            "mediaType": 2,
            "description": "图文动态",
            "media": [
                {"url": "https://img.qq.com/a.jpg", "urlToken": "?token=i1"},
                {"url": "https://img.qq.com/b.jpg"},
            ],
            "followPostInfo": {"musicInfo": {"mediaStreamingUrl": "https://music.qq.com/x.mp3"}},
        }
        feeds = extract_feeds({"data": node})
        feed = feeds[0]
        assert feed.kind == "image"
        assert feed.images == ["https://img.qq.com/a.jpg?token=i1", "https://img.qq.com/b.jpg"]
        assert feed.bgm_url == "https://music.qq.com/x.mp3"

    def test_live_feed(self):
        node = _video_feed_payload()
        node["liveInfo"] = {"streamUrl": "https://live.qq.com/flv/xxx.flv"}
        node["anchorContact"] = {"liveCoverImgUrl": "https://cover/live.jpg"}
        node["objectDesc"]["mediaType"] = 9
        feeds = extract_feeds({"data": node})
        feed = feeds[0]
        assert feed.kind == "live"
        assert feed.url == "https://live.qq.com/flv/xxx.flv"
        assert feed.cover_url == "https://cover/live.jpg"

    def test_live_without_mediatype_marker(self):
        """mediaType 未标 9 但带 streamUrl 的（回放列表常见）也按直播处理。"""
        node = _video_feed_payload()
        node["liveInfo"] = {"streamUrl": "https://live.qq.com/flv/y.flv"}
        feeds = extract_feeds({"data": node})
        assert feeds[0].kind == "live"

    def test_no_media_video_skipped(self):
        node = _video_feed_payload()
        node["objectDesc"]["media"] = []
        assert extract_feeds({"data": node}) == []

    def test_missing_ids_skipped(self):
        node = _video_feed_payload(objectId="", objectNonceId="")
        assert extract_feeds({"data": node}) == []

    def test_malformed_sibling_node_ignored(self):
        """结构异常的节点（objectDesc 非 dict 等）不影响其余提取。"""
        payload = {
            "data": [
                {"objectDesc": "broken"},
                _video_feed_payload(),
                None,
                42,
            ]
        }
        feeds = extract_feeds(payload)
        assert len(feeds) == 1

    def test_decode_key_missing_is_none(self):
        node = _video_feed_payload(media={"decodeKey": ""})
        feeds = extract_feeds({"data": node})
        assert feeds[0].decode_key is None


class TestQualityUrl:
    def _feed(self) -> ChannelFeed:
        return extract_feeds({"data": _video_feed_payload()})[0]

    def test_highest_returns_clean_url(self):
        feed = self._feed()
        url = pick_quality_url(feed, "highest")
        assert url == (
            "https://finder.video.qq.com/302/20304/10004/1f0004dbgmaca4abmiwjnvxfrkgidykw2fy.f100040.mp4"
            "?encfilekey=abc123xyzdef456&token=qqeyJhbGciOi"
        )

    def test_quality_appends_snsvideoflag(self):
        feed = self._feed()
        url = pick_quality_url(feed, "540p")
        assert url.endswith("&X-snsvideoflag=3")  # 540p 高度 960 → fileFormat=3

    def test_lowest(self):
        feed = self._feed()
        assert pick_quality_url(feed, "lowest").endswith("&X-snsvideoflag=3")

    def test_unknown_quality_falls_back(self):
        feed = self._feed()
        assert pick_quality_url(feed, "weird") == pick_quality_url(feed, "highest")

    def test_no_specs_falls_back(self):
        feed = self._feed()
        feed.specs = []
        assert pick_quality_url(feed, "720p") == pick_quality_url(feed, "highest")

    def test_live_url_passthrough(self):
        feed = self._feed()
        feed.kind = "live"
        feed.url = "https://live.qq.com/x.flv"
        assert pick_quality_url(feed, "720p") == "https://live.qq.com/x.flv"


class TestCleanMediaUrl:
    def test_strips_volatile_params(self):
        url = "https://cdn/v.mp4?dis_k=1&dis_t=2&encfilekey=k&idx=1&token=t"
        assert clean_media_url(url) == "https://cdn/v.mp4?encfilekey=k&token=t"

    def test_keeps_url_without_core_params(self):
        url = "https://cdn/v.mp4?dis_k=1"
        assert clean_media_url(url) == url

    def test_empty(self):
        assert clean_media_url("") == ""
