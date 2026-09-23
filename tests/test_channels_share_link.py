"""分享链接解析测试（v2.0.3 链接粘贴下载入口）。"""

from __future__ import annotations

import pytest

from channels.share_link import is_share_link, parse_share_link


class TestParseShareLink:
    @pytest.mark.parametrize(
        "url",
        [
            "https://weixin.qq.com/sph/AseYzCvBg3",
            "http://weixin.qq.com/sph/AseYzCvBg3",
            "https://weixin.qq.com/sph/AseYzCvBg3/",
            "https://weixin.qq.com/sph/AseYzCvBg3?from=singlemessage",
            "https://weixin.qq.com/sph/AseYzCvBg3?from=singlemessage&isappinstalled=0",
            "  https://weixin.qq.com/sph/AseYzCvBg3  ",
            "<https://weixin.qq.com/sph/AseYzCvBg3>",
            "【分享】一个小视频 https://weixin.qq.com/sph/AseYzCvBg3 看看",
            "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AseYzCvBg3",
            "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AseYzCvBg3&scene=1",
        ],
    )
    def test_valid_links(self, url: str):
        link = parse_share_link(url)
        assert link is not None, url
        assert link.share_id == "AseYzCvBg3"
        assert link.full_url == (
            "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AseYzCvBg3"
        )
        assert link.preview_path == "/finder-preview/pages/sph?id=AseYzCvBg3"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "not a url",
            "https://www.douyin.com/video/123",
            "https://weixin.qq.com/sph/abc",  # id 太短（<4）
            "https://weixin.qq.com/sph/",  # 无 id
            "https://evil.com/sph/AseYzCvBg3",  # 域名不对
            "https://weixin.qq.com.evil.com/sph/AseYzCvBg3",  # 仿冒域名
            "https://channels.weixin.qq.com/web/pages/home",  # 非分享链接
            "https://channels.weixin.qq.com/finder-preview/pages/sph",  # 无 id
        ],
    )
    def test_invalid_links(self, url: str):
        assert parse_share_link(url) is None, url
        assert is_share_link(url) is False

    def test_is_share_link(self):
        assert is_share_link("https://weixin.qq.com/sph/AseYzCvBg3") is True


class TestInjectionPathCoverage:
    """注入白名单必须覆盖分享链接预览页（链接下载的捕获点）。"""

    def test_preview_path_matches_injector_regex(self):
        from channels.injector import CHANNELS_PAGE_PATH_RE

        for path in (
            "/finder-preview/pages/sph",
            "/finder-preview/pages/sph?id=AseYzCvBg3",
            "/finder-preview/pages/feed/xxx",
            "/web/pages/home",
            "/web/pages/feed/abc",
            "/web/pages/live",
            "/web/pages/profile",
        ):
            assert CHANNELS_PAGE_PATH_RE.match(path), path

    def test_non_page_paths_rejected(self):
        from channels.injector import CHANNELS_PAGE_PATH_RE

        for path in ("/web/other", "/api/feed", "/", "/finder-preview/other"):
            assert not CHANNELS_PAGE_PATH_RE.match(path), path


class TestPreviewFeedExtraction:
    """preview 页 sceneInfo 模式提取（videoUrl/picInfo 而非 objectDesc）。"""

    def test_extract_preview_video_feed(self):
        from channels.feed import extract_preview_feeds

        payload = {
            "data": {
                "sceneInfo": {
                    "id": "feed_obj_1",
                    "dynamicExportId": "AseYzCvBg3",
                    "videoUrl": "https://finder.video.qq.com/251/20302/stodownload?encfilekey=abc",
                    "coverUrl": "https://finder.video.qq.com/cover.jpg",
                    "mediaType": 4,
                    "description": "测试视频标题",
                    "nickname": "测试作者",
                    "createTime": 1750000000,
                }
            }
        }
        feeds = extract_preview_feeds(payload, source_api="page:preview")
        assert len(feeds) == 1
        feed = feeds[0]
        assert feed.object_id == "feed_obj_1"
        assert feed.kind == "video"
        assert feed.url == "https://finder.video.qq.com/251/20302/stodownload?encfilekey=abc"
        assert feed.title == "测试视频标题"
        assert feed.author_name == "测试作者"
        assert feed.cover_url == "https://finder.video.qq.com/cover.jpg"
        assert feed.decode_key is None  # 预览页不下发 decodeKey

    def test_extract_preview_image_feed(self):
        from channels.feed import extract_preview_feeds

        payload = {
            "picInfo": [{"url": "https://cdn/1.jpg"}, {"url": "https://cdn/2.jpg"}],
            "id": "img_1",
            "mediaType": 2,
            "description": "图文",
        }
        feeds = extract_preview_feeds(payload)
        assert len(feeds) == 1
        assert feeds[0].kind == "image"
        assert feeds[0].images == ["https://cdn/1.jpg", "https://cdn/2.jpg"]

    def test_object_desc_payload_not_matched_by_preview_extractor(self):
        """objectDesc 模式的响应不该被 preview 提取器误判（两模式互不干扰）。"""
        from channels.feed import extract_preview_feeds

        payload = {"data": [{"objectId": "a", "objectDesc": {"media": [{"url": "u"}]}}]}
        assert extract_preview_feeds(payload) == []

    def test_pipeline_falls_back_to_preview_extractor(self):
        """pipeline：objectDesc 提不到时自动尝试 preview 模式。"""
        from channels.diagnostics import ChannelsDiagnostics
        from channels.feed_store import FeedStore
        from channels.pipeline import FeedCapturePipeline

        store = FeedStore()
        pipeline = FeedCapturePipeline(store, ChannelsDiagnostics())
        payload = {
            "data": {
                "sceneInfo": {
                    "id": "feed_obj_2",
                    "videoUrl": "https://finder.video.qq.com/x.mp4?encfilekey=k",
                    "mediaType": 4,
                    "description": "pipeline 回退测试",
                }
            }
        }
        fresh = pipeline.ingest_page_nodes([payload], strategy="page_network_hook", page="feed")
        assert len(fresh) == 1
        assert fresh[0].object_id == "feed_obj_2"
        assert len(store) == 1
