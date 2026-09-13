"""B 站链接解析与平台路由测试。

路由判定在建 API 客户端之前就要定下来（抖音与 B 站客户端不可互换），所以
「这个链接属于哪个平台」必须是纯函数且有明确答案——判定错了不会报错，只会
用错误的客户端去请求，表现为一堆莫名其妙的解析失败。
"""

import pytest

from bilibili.url_parser import (
    BiliURLParser,
    detect_platform,
    is_bili_short_url,
    is_bilibili_url,
    is_douyin_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bilibili.com/video/BV1GJ411x7h7",
        "https://m.bilibili.com/video/BV1GJ411x7h7?p=3",
        "https://www.bilibili.com/video/av170001",
        "https://space.bilibili.com/271779326",
        "https://space.bilibili.com/271779326/video",
        "https://space.bilibili.com/271779326/upload/video",
        "https://space.bilibili.com/271779326/channel/collectiondetail?sid=12345",
        "https://space.bilibili.com/271779326/channel/seriesdetail?sid=6789",
        "https://space.bilibili.com/271779326/favlist?fid=999",
        "https://www.bilibili.com/list/271779326?sid=12345&type=season",
        "https://www.bilibili.com/list/271779326?sid=6789&type=series",
        "https://www.bilibili.com/list/ml999",
        "https://www.bilibili.com/medialist/play/271779326?business=space_collection&business_id=12345",
        "https://b23.tv/abcdefg",
        "BV1GJ411x7h7",
        "av170001",
    ],
)
def test_is_bilibili_url_accepts_supported_forms(url):
    assert is_bilibili_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/video/7301234567890123456",
        "https://v.douyin.com/abc123/",
        "https://github.com/foo/bar",
        "https://example.com/video/BV1GJ411x7h7",
        "",
    ],
)
def test_is_bilibili_url_rejects_other_hosts(url):
    assert is_bilibili_url(url) is False


def test_detect_platform_prefers_bilibili_for_bare_ids():
    assert detect_platform("BV1GJ411x7h7") == "bilibili"
    assert detect_platform("https://www.bilibili.com/video/BV1GJ411x7h7") == "bilibili"


def test_detect_platform_identifies_douyin():
    assert detect_platform("https://www.douyin.com/video/7301234567890123456") == "douyin"
    assert detect_platform("https://v.douyin.com/abc/") == "douyin"
    assert detect_platform("https://live.douyin.com/123456") == "douyin"


def test_detect_platform_returns_none_for_unknown():
    assert detect_platform("https://example.com/whatever") is None
    assert detect_platform("not a url") is None


def test_is_douyin_url_helper():
    assert is_douyin_url("https://www.douyin.com/user/MS4wLjABAAAA") is True
    assert is_douyin_url("https://www.bilibili.com/video/BV1GJ411x7h7") is False


def test_short_url_detection_and_normalization():
    assert is_bili_short_url("https://b23.tv/abc") is True
    assert is_bili_short_url("b23.tv/abc") is True
    assert is_bili_short_url("https://www.bilibili.com/video/BV1GJ411x7h7") is False


def test_parse_video_with_page():
    parsed = BiliURLParser.parse("https://www.bilibili.com/video/BV1GJ411x7h7?p=3")
    assert parsed["type"] == "video"
    assert parsed["bvid"] == "BV1GJ411x7h7"
    assert parsed["page"] == 3


def test_parse_video_without_page_has_no_page_key():
    parsed = BiliURLParser.parse("https://www.bilibili.com/video/BV1GJ411x7h7")
    assert parsed["type"] == "video"
    assert parsed.get("page") is None


def test_parse_av_number_video():
    parsed = BiliURLParser.parse("https://www.bilibili.com/video/av170001")
    assert parsed["type"] == "video"
    assert parsed["aid"] == "170001"


def test_parse_bare_bvid_and_avid():
    assert BiliURLParser.parse("BV1GJ411x7h7") == {
        "original_url": "BV1GJ411x7h7",
        "type": "video",
        "bvid": "BV1GJ411x7h7",
    }
    parsed = BiliURLParser.parse("av170001")
    assert parsed["type"] == "video"
    assert parsed["aid"] == "170001"


def test_parse_user_space_variants():
    for url in (
        "https://space.bilibili.com/271779326",
        "https://space.bilibili.com/271779326/video",
        "https://space.bilibili.com/271779326/upload/video",
    ):
        parsed = BiliURLParser.parse(url)
        assert parsed["type"] == "user"
        assert parsed["mid"] == "271779326"


def test_parse_collection_and_series():
    collection = BiliURLParser.parse(
        "https://space.bilibili.com/271779326/channel/collectiondetail?sid=12345"
    )
    assert collection["type"] == "collection"
    assert collection["season_id"] == "12345"
    assert collection["mid"] == "271779326"

    series = BiliURLParser.parse(
        "https://space.bilibili.com/271779326/channel/seriesdetail?sid=6789"
    )
    assert series["type"] == "series"
    assert series["series_id"] == "6789"
    assert series["mid"] == "271779326"


def test_parse_list_pages_by_type_param():
    collection = BiliURLParser.parse(
        "https://www.bilibili.com/list/271779326?sid=12345&type=season"
    )
    assert collection["type"] == "collection"
    assert collection["season_id"] == "12345"

    series = BiliURLParser.parse("https://www.bilibili.com/list/271779326?sid=6789&type=series")
    assert series["type"] == "series"
    assert series["series_id"] == "6789"


def test_parse_favlist_variants():
    by_fid = BiliURLParser.parse("https://space.bilibili.com/271779326/favlist?fid=999")
    assert by_fid["type"] == "favlist"
    assert by_fid["media_id"] == "999"

    by_ml = BiliURLParser.parse("https://www.bilibili.com/list/ml888")
    assert by_ml["type"] == "favlist"
    assert by_ml["media_id"] == "888"

    by_medialist = BiliURLParser.parse("https://www.bilibili.com/medialist/play/ml888")
    assert by_medialist["type"] == "favlist"
    assert by_medialist["media_id"] == "888"


def test_parse_legacy_medialist_collection_and_series():
    collection = BiliURLParser.parse(
        "https://www.bilibili.com/medialist/play/271779326"
        "?business=space_collection&business_id=12345"
    )
    assert collection["type"] == "collection"
    assert collection["season_id"] == "12345"

    series = BiliURLParser.parse(
        "https://www.bilibili.com/medialist/play/271779326?business=space_series&business_id=6789"
    )
    assert series["type"] == "series"
    assert series["series_id"] == "6789"


def test_parse_short_url_marks_type_short():
    parsed = BiliURLParser.parse("https://b23.tv/abcdefg")
    assert parsed["type"] == "short"


def test_parse_gated_bangumi_yields_bangumi_type():
    """番剧解析得出但被工厂门禁拒绝——必须给出可操作的解释而非「无法解析」。"""
    parsed = BiliURLParser.parse("https://www.bilibili.com/bangumi/play/ss12345")
    assert parsed is not None
    assert parsed["type"] == "bangumi"


def test_parse_empty_and_other_host_return_none():
    assert BiliURLParser.parse("") is None
    assert BiliURLParser.parse("https://example.com/video/BV1GJ411x7h7") is None


def test_build_url_round_trips_each_type():
    cases = [
        "https://www.bilibili.com/video/BV1GJ411x7h7?p=3",
        "https://space.bilibili.com/271779326/video",
        "https://space.bilibili.com/271779326/channel/collectiondetail?sid=12345",
        "https://space.bilibili.com/271779326/channel/seriesdetail?sid=6789",
    ]
    for url in cases:
        parsed = BiliURLParser.parse(url)
        rebuilt = BiliURLParser.build_url(parsed)
        assert rebuilt.startswith("https://")
        # 反推链接必须能再解析回同一类型，保证日志/增量记录里的链接可用。
        assert BiliURLParser.parse(rebuilt)["type"] == parsed["type"]
