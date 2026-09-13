"""DASH 音视频轨选择。

Bilibili 的 ``/x/player/playurl`` 在 ``fnval=4048`` 下返回 DASH 清单：视频与
音频是两条独立流（``dash.video[]`` / ``dash.audio[]``），需要分别下载再合并。
同一档清晰度会有多个编码版本（AVC / HEVC / AV1），码率与体积差异明显，所以
「选哪一轨」必须同时受画质档位与编码偏好两个维度控制。

档位 ID 与含义（``id`` 字段，官方定义）：

===== ===========
 id    清晰度
===== ===========
 127   8K
 126   杜比视界
 125   HDR
 120   4K
 116   1080P 60帧
 112   1080P 高码率
 80    1080P
 74    720P 60帧
 64    720P
 32    480P
 16    360P
 6     240P
===== ===========

登录态决定可用上限：未登录通常只有 360P/480P；登录后到 1080P；1080P+/4K/8K/
HDR/杜比需要大会员。这里不做权限判断——接口只会下发当前账号有权的档位，
按配置挑最接近的即可。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# 视频档位 ID -> 展示名。顺序即「由低到高」，供 nearest 计算使用。
VIDEO_QUALITY_LADDER = (
    (6, "240P"),
    (16, "360P"),
    (32, "480P"),
    (64, "720P"),
    (74, "720P60"),
    (80, "1080P"),
    (112, "1080P+"),
    (116, "1080P60"),
    (120, "4K"),
    (125, "HDR"),
    (126, "杜比视界"),
    (127, "8K"),
)

VIDEO_QUALITY_LABELS: Dict[int, str] = dict(VIDEO_QUALITY_LADDER)

# 用户可写的画质别名 -> 目标档位 ID。"highest" / "lowest" 走专用分支。
VIDEO_QUALITY_ALIASES: Dict[str, int] = {
    "240p": 6,
    "360p": 16,
    "480p": 32,
    "720p": 64,
    "720p60": 74,
    "1080p": 80,
    "1080p+": 112,
    "1080p60": 116,
    "1440p": 120,
    "2k": 120,
    "4k": 120,
    "2160p": 120,
    "hdr": 125,
    "dolby": 126,
    "dolby_vision": 126,
    "8k": 127,
    "4320p": 127,
}

# 音频档位权重：FLAC(30258) > 杜比(30250) > 192K AAC(30280) > 132K(30232) > 64K(30216)。
# 不能按 id 大小排序——30250/30258 的 id 比 30280 小，但质量更高得多。
AUDIO_QUALITY_RANK: Dict[int, int] = {
    30216: 1,
    30232: 2,
    30280: 3,
    30250: 4,
    30258: 5,
}

AUDIO_QUALITY_LABELS: Dict[int, str] = {
    30216: "64K",
    30232: "132K",
    30280: "192K",
    30250: "杜比全景声",
    30258: "无损 FLAC",
}

# 编码标识（接口返回 ``codecs`` 字段的前缀）-> 编码偏好别名。
CODEC_FAMILY_ALIASES = {
    "avc": "avc",
    "h264": "avc",
    "hevc": "hevc",
    "h265": "hevc",
    "hev": "hevc",
    "av1": "av1",
}

# 默认编码偏好顺序：AVC 兼容性最好（浏览器 / 剪辑软件 / 老播放器都能解），
# HEVC 次之（同码率画质更好，Windows 需 HEVC 扩展才能预览），AV1 最后。
DEFAULT_CODEC_PREFERENCE = ("avc", "hevc", "av1")

# durl 兜底（fnval=1 的单文件流）没有档位列表，只能给出 qn 请求值。
DEFAULT_DURL_QN = 80


def normalize_quality(quality: Optional[str]) -> str:
    """把用户配置画质归一化为小写去空串；空值按 ``highest`` 处理。"""
    normalized = str(quality or "").strip().lower()
    return normalized or "highest"


def _codec_preference(codec: Optional[str]) -> Sequence[str]:
    """把配置的编码偏好展开成有序偏好列表。

    ``auto`` / 空值 → 默认顺序；单个编码（``avc``/``hevc``/``av1``）→ 只偏好
    该编码，但它不存在时调用方会退回全量列表，不至于一个也挑不出来。
    """
    normalized = str(codec or "").strip().lower()
    if not normalized or normalized == "auto":
        return DEFAULT_CODEC_PREFERENCE
    family = CODEC_FAMILY_ALIASES.get(normalized, normalized)
    rest = [item for item in DEFAULT_CODEC_PREFERENCE if item != family]
    return (family,) + tuple(rest)


def _codec_family(stream: Dict[str, Any]) -> str:
    codecs = str(stream.get("codecs") or "").lower()
    if codecs.startswith(("avc", "h264")):
        return "avc"
    if codecs.startswith(("hev", "h265")):
        return "hevc"
    if codecs.startswith(("av01", "av1")):
        return "av1"
    # 少数响应只给 ``codecid`` 数字：7=AVC, 12=HEVC, 13=AV1。
    codecid = stream.get("codecid")
    return {7: "avc", 12: "hevc", 13: "av1"}.get(codecid, "")


def _stream_quality_id(stream: Dict[str, Any]) -> int:
    try:
        return int(stream.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _stream_bandwidth(stream: Dict[str, Any]) -> int:
    try:
        return int(stream.get("bandwidth") or 0)
    except (TypeError, ValueError):
        return 0


def _nearest_quality_id(target: int, available: Sequence[int]) -> Optional[int]:
    """挑最接近 ``target`` 的可用档位，**降级优先**。

    有低于目标的档位时取其中最高的一个；只有全部高于目标时才升到最低的一个。
    不能直接用 id 距离取最近：用户要 ``1080p60``（116）而接口给了 ``1080P``（80）
    与 ``4K``（120）时，id 距离指向 4K，但用户想要的是 1080P 那一档——超配之外
    还平白多几倍体积。档位 id 沿清晰度单调递增（6→127），所以可以直接比较。
    """
    if not available:
        return None
    at_or_below = [value for value in available if value <= target]
    if at_or_below:
        return max(at_or_below)
    return min(available)


def select_video_stream(
    video_streams: Sequence[Dict[str, Any]],
    quality: str = "highest",
    codec: str = "auto",
) -> Optional[Dict[str, Any]]:
    """从 ``dash.video`` 里挑一条视频轨。

    两步走，顺序不能颠倒：

    1. **先按画质档位收敛**。编码偏好不是过滤器——同一档清晰度常有 AVC/HEVC/AV1
       三个版本，但如果先按编码筛再取最高档，``highest`` 会被 AVC 那一批锁死
       （实测 4K 档只有 HEVC/AV1 版本，先筛 AVC 会让「最高画质」退到 1080P）。
    2. **再在同档位内按编码偏好挑**，偏好编码该档位没有时退回同档位其它编码，
       而不是直接判失败（例如配置 ``codec: avc`` 但 4K 只有 HEVC）。
    """
    entries = [item for item in (video_streams or []) if isinstance(item, dict)]
    entries = [item for item in entries if item.get("baseUrl") or item.get("base_url")]
    if not entries:
        return None

    normalized = normalize_quality(quality)
    if normalized == "lowest":
        tier = [item for item in entries if _stream_quality_id(item) == min(
            _stream_quality_id(entry) for entry in entries
        )]
    elif normalized in ("highest", ""):
        tier = [item for item in entries if _stream_quality_id(item) == max(
            _stream_quality_id(entry) for entry in entries
        )]
    else:
        target = VIDEO_QUALITY_ALIASES.get(normalized)
        if target is None:
            # 未知画质名（含 ``original`` 之类抖音遗留值）按最高档处理，调用方
            # 通过返回值自行记录实际档位，不静默失败。
            target = max(_stream_quality_id(entry) for entry in entries)
        available = sorted({_stream_quality_id(item) for item in entries})
        chosen_id = _nearest_quality_id(target, available)
        tier = [item for item in entries if _stream_quality_id(item) == chosen_id]

    if not tier:
        return None

    for family in _codec_preference(codec):
        matching = [item for item in tier if _codec_family(item) == family]
        if matching:
            return max(matching, key=_stream_bandwidth)
    return max(tier, key=_stream_bandwidth)


def select_audio_stream(
    dash: Dict[str, Any],
    quality: str = "highest",
) -> Optional[Dict[str, Any]]:
    """从 DASH 清单里挑一条音频轨。

    无损（``dash.flac.audio``）与杜比（``dash.dolby.audio``）是 ``dash.audio``
    之外的两个可选字段，只有大会员稿件才下发；挑选时必须把它们一起纳入候选，
    否则「最高音质」实际只会拿到 192K AAC。
    """
    dash = dash or {}
    candidates: List[Dict[str, Any]] = []

    def _extend(value: Any) -> None:
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))

    _extend(dash.get("audio"))
    dolby = dash.get("dolby")
    if isinstance(dolby, dict):
        _extend(dolby.get("audio"))
    flac = dash.get("flac")
    if isinstance(flac, dict):
        _extend(flac.get("audio"))

    candidates = [item for item in candidates if item.get("baseUrl") or item.get("base_url")]
    if not candidates:
        return None

    def _rank(item: Dict[str, Any]) -> int:
        return AUDIO_QUALITY_RANK.get(_stream_quality_id(item), 0)

    if normalize_quality(quality) == "lowest":
        return min(candidates, key=lambda item: (_rank(item), _stream_bandwidth(item)))
    return max(candidates, key=lambda item: (_rank(item), _stream_bandwidth(item)))


def stream_urls(stream: Dict[str, Any]) -> List[str]:
    """取一条轨的全部候选地址（``baseUrl`` + ``backupUrl``），保持原序。"""
    urls: List[str] = []
    for key in ("baseUrl", "base_url"):
        value = stream.get(key)
        if isinstance(value, str) and value and value not in urls:
            urls.append(value)
    for key in ("backupUrl", "backup_url"):
        value = stream.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item and item not in urls:
                    urls.append(item)
    return urls


def describe_video_stream(stream: Optional[Dict[str, Any]]) -> str:
    if not stream:
        return "无"
    quality_id = _stream_quality_id(stream)
    label = VIDEO_QUALITY_LABELS.get(quality_id, str(quality_id))
    family = _codec_family(stream) or "unknown"
    width = stream.get("width") or "?"
    height = stream.get("height") or "?"
    return f"{label} {family.upper()} {width}x{height}"


def describe_audio_stream(stream: Optional[Dict[str, Any]]) -> str:
    if not stream:
        return "无"
    quality_id = _stream_quality_id(stream)
    return AUDIO_QUALITY_LABELS.get(quality_id, str(quality_id))


def resolve_durl_qn(quality: str) -> int:
    """DASH 不可用时给 durl 请求用的 ``qn``。"""
    normalized = normalize_quality(quality)
    if normalized == "lowest":
        return 16
    if normalized in ("highest", ""):
        return 127
    return VIDEO_QUALITY_ALIASES.get(normalized, DEFAULT_DURL_QN)
