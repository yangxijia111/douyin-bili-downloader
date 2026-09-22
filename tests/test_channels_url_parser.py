"""channels.url_parser —— 视频号 URL 识别测试。"""

from channels.url_parser import CHANNELS_URL_HINT, ChannelsURLParser, is_channels_url


class TestIsChannelsUrl:
    def test_feed_page(self):
        assert is_channels_url("https://channels.weixin.qq.com/web/pages/feed/abc123") is True

    def test_profile_page(self):
        assert is_channels_url("https://channels.weixin.qq.com/web/pages/profile/xyz") is True

    def test_other_weixin_hosts(self):
        assert is_channels_url("https://weixin.qq.com/x") is False
        assert is_channels_url("https://mp.weixin.qq.com/x") is False
        assert is_channels_url("https://www.douyin.com/video/1") is False
        assert is_channels_url("https://www.bilibili.com/video/BV1") is False

    def test_bad_input(self):
        assert is_channels_url("") is False
        assert is_channels_url(None) is False
        assert is_channels_url("not a url at all") is False

    def test_parse(self):
        parsed = ChannelsURLParser.parse("https://channels.weixin.qq.com/web/pages/feed/abc")
        assert parsed == {
            "original_url": "https://channels.weixin.qq.com/web/pages/feed/abc",
            "type": "sniff_required",
            "platform": "channels",
        }
        assert ChannelsURLParser.parse("https://www.douyin.com/video/1") is None

    def test_hint_actionable(self):
        assert "--channels" in CHANNELS_URL_HINT
