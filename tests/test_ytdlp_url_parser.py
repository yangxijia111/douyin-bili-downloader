"""yt-dlp 平台链接识别测试（纯函数，不依赖网络）。

平台判定是 CLI / Server 分流的第一道门：判错会把爱奇艺链接送进抖音解析器、
或把抖音链接送进 yt-dlp。这里逐平台验证正反例，并确认三组域名互不重叠。
"""

import pytest

from bilibili.url_parser import detect_platform
from ytdlp.url_parser import (
    PLATFORM_BY_KEY,
    SUPPORTED_PLATFORMS,
    YtdlpURLParser,
    detect_ytdlp_platform,
    is_ytdlp_url,
    normalize_url,
    platform_display_name,
)

# ----------------------------------------------------------------------
# 平台识别
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.iqiyi.com/v_19rr7xxxx.html", "iqiyi"),
        ("https://m.iqiyi.com/v_abc.html", "iqiyi"),
        ("https://www.iq.com/play/xxx", "iqiyi"),
        ("https://v.qq.com/x/cover/abc/def.html", "tencent"),
        ("https://m.v.qq.com/x/m/play?cid=abc", "tencent"),
        ("https://video.qq.com/x/page/abc.html", "tencent"),
        ("https://v.youku.com/v_show/id_XNDU2.html", "youku"),
        ("https://www.mgtv.com/b/12345/67890.html", "mgtv"),
        ("https://www.kuaishou.com/short-video/3xabc", "kuaishou"),
        ("https://v.kuaishou.com/AbCd12", "kuaishou"),
        ("https://www.ixigua.com/7123456789", "xigua"),
        ("https://www.toutiao.com/video/7123456789/", "toutiao"),
        ("https://weibo.com/tv/show/1034:4xxxx", "weibo"),
        ("https://m.weibo.cn/detail/4xxxx", "weibo"),
        ("https://www.xiaohongshu.com/explore/64abc", "xiaohongshu"),
        ("https://xhslink.com/a/xyz", "xiaohongshu"),
    ],
)
def test_detect_ytdlp_platform_positive(url, expected):
    assert detect_ytdlp_platform(url) == expected
    assert is_ytdlp_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/video/7123456789",
        "https://v.douyin.com/abc/",
        "https://www.bilibili.com/video/BV1GJ411x7h7",
        "https://b23.tv/abc",
        "BV1GJ411x7h7",
        "https://www.qq.com/",  # 腾讯主站不是视频站
        "https://mail.qq.com/",
        "https://example.com/iqiyi.com/fake",  # 路径里出现域名不算
        "https://notiqiyi.com/x",  # 前缀相似但不是子域
        "",
        None,
        "not a url at all",
    ],
)
def test_detect_ytdlp_platform_negative(url):
    assert detect_ytdlp_platform(url) is None
    assert not is_ytdlp_url(url)


def test_bare_domain_without_scheme_is_recognized():
    """分享文案里常见的裸短链（无 https://）也要能识别。"""
    assert detect_ytdlp_platform("v.kuaishou.com/AbCd12") == "kuaishou"
    assert detect_ytdlp_platform("xhslink.com/a/xyz") == "xiaohongshu"
    assert detect_ytdlp_platform("  www.iqiyi.com/v_abc.html  ") == "iqiyi"


def test_platform_domains_do_not_overlap_with_douyin_or_bilibili():
    """三条链路的域名集合必须互斥，否则分流顺序会影响结果。"""
    samples = {
        "iqiyi": "https://www.iqiyi.com/v_x.html",
        "tencent": "https://v.qq.com/x/cover/a/b.html",
        "youku": "https://v.youku.com/v_show/id_x.html",
        "mgtv": "https://www.mgtv.com/b/1/2.html",
        "kuaishou": "https://www.kuaishou.com/short-video/x",
        "xigua": "https://www.ixigua.com/1",
        "toutiao": "https://www.toutiao.com/video/1/",
        "weibo": "https://weibo.com/tv/show/x",
        "xiaohongshu": "https://www.xiaohongshu.com/explore/x",
    }
    assert set(samples) == set(PLATFORM_BY_KEY)
    for key, url in samples.items():
        assert detect_ytdlp_platform(url) == key
        assert detect_platform(url) is None, f"{key} 链接不应被判成抖音/B站"


def test_supported_platforms_registry_is_consistent():
    keys = [spec.key for spec in SUPPORTED_PLATFORMS]
    assert len(keys) == len(set(keys)), "平台 key 不能重复"
    for spec in SUPPORTED_PLATFORMS:
        assert spec.name, f"{spec.key} 缺少中文名"
        assert spec.hosts, f"{spec.key} 缺少域名"
        assert spec.cookie_domain.startswith("."), f"{spec.key} cookie_domain 应以 . 开头"


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def test_platform_display_name():
    assert platform_display_name("iqiyi") == "爱奇艺"
    assert platform_display_name("tencent") == "腾讯视频"
    assert platform_display_name("unknown") == "unknown"
    assert platform_display_name(None) == ""


def test_normalize_url():
    assert normalize_url("v.kuaishou.com/x") == "https://v.kuaishou.com/x"
    assert normalize_url("https://a.com/x") == "https://a.com/x"
    assert normalize_url("HTTP://a.com/x") == "HTTP://a.com/x"
    assert normalize_url("  ") == ""
    assert normalize_url(None) == ""


# ----------------------------------------------------------------------
# YtdlpURLParser
# ----------------------------------------------------------------------


def test_parser_returns_video_descriptor():
    parsed = YtdlpURLParser.parse("m.iqiyi.com/v_abc.html")
    assert parsed == {
        "type": "video",
        "platform": "iqiyi",
        "platform_name": "爱奇艺",
        "url": "https://m.iqiyi.com/v_abc.html",
        "original_url": "m.iqiyi.com/v_abc.html",
    }


def test_parser_rejects_foreign_and_empty():
    assert YtdlpURLParser.parse("https://www.douyin.com/video/1") is None
    assert YtdlpURLParser.parse("") is None
    assert YtdlpURLParser.parse(None) is None
