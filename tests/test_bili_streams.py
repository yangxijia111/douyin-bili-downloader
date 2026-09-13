"""DASH 轨选择测试。

选错轨的后果不是报错而是「下到了东西但不对」：画质档位不匹配、编码是老的 AVC
而用户要 AV1、或者音频永远停在 192K AAC 拿不到无损。所以这里逐条锁死选择规则。
"""

from bilibili.streams import (
    describe_audio_stream,
    describe_video_stream,
    normalize_quality,
    resolve_durl_qn,
    select_audio_stream,
    select_video_stream,
    stream_urls,
)


def _video(quality_id, codecs="avc1.640028", bandwidth=1000, width=1920, height=1080):
    return {
        "id": quality_id,
        "codecs": codecs,
        "bandwidth": bandwidth,
        "width": width,
        "height": height,
        "baseUrl": f"https://cdn.example.com/video-{quality_id}-{codecs}.m4s",
        "backupUrl": [f"https://backup.example.com/video-{quality_id}.m4s"],
    }


def _audio(quality_id, bandwidth=1000, base="https://cdn.example.com/audio.m4s"):
    return {"id": quality_id, "bandwidth": bandwidth, "baseUrl": base}


VIDEO_SET = [
    _video(16, width=640, height=360),
    _video(32, width=854, height=480),
    _video(64, width=1280, height=720),
    _video(80, width=1920, height=1080),
    _video(120, width=3840, height=2160, codecs="hev1.1.6.L150"),
]


def test_normalize_quality_defaults_to_highest():
    assert normalize_quality(None) == "highest"
    assert normalize_quality("") == "highest"
    assert normalize_quality("  1080P ") == "1080p"


def test_select_highest_picks_largest_quality_id():
    chosen = select_video_stream(VIDEO_SET, "highest")
    assert chosen["id"] == 120


def test_select_lowest_picks_smallest():
    chosen = select_video_stream(VIDEO_SET, "lowest")
    assert chosen["id"] == 16


def test_select_exact_quality():
    assert select_video_stream(VIDEO_SET, "720p")["id"] == 64
    assert select_video_stream(VIDEO_SET, "1080p")["id"] == 80


def test_select_quality_degrades_to_nearest_available():
    """要 1080P60 但只有 1080P 与 4K 时，应降级到 1080P（降级优于超配）。"""
    candidates = [_video(80), _video(120)]
    assert select_video_stream(candidates, "1080p60")["id"] == 80


def test_select_quality_upgrades_when_only_higher_available():
    """确实没有更低档时才升到最近的高档。"""
    candidates = [_video(120), _video(125)]
    assert select_video_stream(candidates, "1080p")["id"] == 120


def test_highest_is_not_capped_by_codec_preference():
    """编码偏好不是过滤器：4K 档只有 HEVC 时，highest 不能退到 AVC 的 1080P。"""
    candidates = [
        _video(80, codecs="avc1.640028"),
        _video(120, codecs="hev1.1.6.L150", width=3840, height=2160),
    ]
    assert select_video_stream(candidates, "highest", "auto")["id"] == 120


def test_unknown_quality_falls_back_to_highest():
    assert select_video_stream(VIDEO_SET, "original")["id"] == 120


def test_codec_preference_avc():
    candidates = [
        _video(80, codecs="hev1.1.6.L150"),
        _video(80, codecs="avc1.640028", bandwidth=2000),
        _video(80, codecs="av01.0.08M.08", bandwidth=3000),
    ]
    chosen = select_video_stream(candidates, "1080p", "avc")
    assert chosen["codecs"].startswith("avc")


def test_codec_preference_falls_back_when_missing():
    """指定 avc 但该稿件只转码了 AV1 时，不能失败，要退回可用编码。"""
    candidates = [_video(80, codecs="av01.0.08M.08")]
    chosen = select_video_stream(candidates, "1080p", "avc")
    assert chosen is not None
    assert chosen["codecs"].startswith("av01")


def test_auto_codec_prefers_avc_over_hevc_and_av1():
    candidates = [
        _video(80, codecs="av01.0.08M.08"),
        _video(80, codecs="hev1.1.6.L150"),
        _video(80, codecs="avc1.640028"),
    ]
    assert select_video_stream(candidates, "1080p", "auto")["codecs"].startswith("avc")


def test_codecid_only_response_is_classified():
    entry = {"id": 80, "codecid": 12, "baseUrl": "https://cdn/x.m4s", "bandwidth": 1}
    chosen = select_video_stream([entry], "1080p", "hevc")
    assert chosen is entry


def test_select_video_returns_none_without_playable_url():
    assert select_video_stream([{"id": 80, "codecs": "avc1"}], "highest") is None
    assert select_video_stream([], "highest") is None
    assert select_video_stream(None, "highest") is None


def test_higher_bandwidth_wins_within_same_quality_id():
    candidates = [
        _video(80, bandwidth=1000),
        _video(80, bandwidth=5000),
    ]
    assert select_video_stream(candidates, "1080p")["bandwidth"] == 5000


# ----------------------------------------------------------------------
# 音频
# ----------------------------------------------------------------------


def test_select_audio_highest_prefers_flac_then_dolby_then_192k():
    dash = {
        "audio": [_audio(30216), _audio(30232), _audio(30280)],
        "dolby": {"audio": [_audio(30250)]},
        "flac": {"audio": _audio(30258)},
    }
    assert select_audio_stream(dash, "highest")["id"] == 30258

    dash.pop("flac")
    assert select_audio_stream(dash, "highest")["id"] == 30250

    dash.pop("dolby")
    assert select_audio_stream(dash, "highest")["id"] == 30280


def test_select_audio_lowest_picks_smallest_rank():
    dash = {"audio": [_audio(30280), _audio(30216), _audio(30232)]}
    assert select_audio_stream(dash, "lowest")["id"] == 30216


def test_select_audio_returns_none_when_absent():
    assert select_audio_stream({}, "highest") is None
    assert select_audio_stream({"audio": []}, "highest") is None


def test_audio_rank_is_not_quality_id_order():
    """30250/30258 的 id 比 30280 小但质量更高，不能按 id 排序。"""
    dash = {"audio": [_audio(30280)], "dolby": {"audio": [_audio(30250)]}}
    assert select_audio_stream(dash, "highest")["id"] == 30250


# ----------------------------------------------------------------------
# 其它
# ----------------------------------------------------------------------


def test_stream_urls_lists_base_then_backups_without_duplicates():
    stream = {
        "baseUrl": "https://a/x.m4s",
        "backupUrl": ["https://b/x.m4s", "https://a/x.m4s"],
    }
    assert stream_urls(stream) == ["https://a/x.m4s", "https://b/x.m4s"]


def test_stream_urls_supports_snake_case_and_missing_fields():
    assert stream_urls({"base_url": "https://a/x.m4s"}) == ["https://a/x.m4s"]
    assert stream_urls({}) == []


def test_describe_video_and_audio_streams():
    assert "1080P" in describe_video_stream(_video(80))
    assert "AVC" in describe_video_stream(_video(80))
    assert describe_video_stream(None) == "无"
    assert describe_audio_stream(_audio(30280)) == "192K"
    assert describe_audio_stream(None) == "无"


def test_resolve_durl_qn_mapping():
    assert resolve_durl_qn("highest") == 127
    assert resolve_durl_qn("lowest") == 16
    assert resolve_durl_qn("1080p") == 80
    assert resolve_durl_qn("unknown-quality") == 80
