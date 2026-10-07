"""Offline link feedback and validation before mutable server state changes."""

import math
import re
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from bilibili import BiliURLParser, detect_platform
from bilibili.factory import UNSUPPORTED_URL_TYPE_DETAIL as BILI_UNSUPPORTED
from channels.url_parser import CHANNELS_URL_HINT, is_channels_url
from config.default_config import DEFAULT_CONFIG
from core import UNSUPPORTED_URL_TYPE_DETAIL, URLParser
from core.user_mode_registry import UserModeRegistry
from ytdlp import detect_ytdlp_platform, platform_display_name


def validate_channels_quality(value: Any) -> str:
    """Accept a quality policy or captured format ID, never a client-supplied URL."""
    allowed = {"highest", "lowest", "original", "2160p", "1440p", "1080p", "720p",
               "540p", "480p", "360p"}
    if not isinstance(value, str) or (value not in allowed and
            re.fullmatch(r"format:[A-Za-z0-9_.-]{1,128}", value) is None):
        raise ValueError("视频号画质必须是最高可用、原始源、受支持的分辨率或已捕获档位")
    return value


def describe_link(url: str) -> Dict[str, Any]:
    """Classify syntax/support only; this does not claim the website is reachable."""
    result = {
        "url": url, "platform": "unknown", "platform_name": "未知平台",
        "supported": False, "type": None, "message": "请粘贴受支持的视频链接或 B 站 BV/av 号",
    }
    try:
        parts = urlsplit(url)
        _ = parts.port  # Access validates malformed/out-of-range port numbers.
        if parts.scheme and parts.scheme.lower() not in {"http", "https"}:
            result["message"] = "链接必须使用 http:// 或 https://"
            return result
        if parts.username is not None or parts.password is not None:
            result["message"] = "视频链接不能包含用户名或密码"
            return result
        if is_channels_url(url):
            result.update(platform="channels", platform_name="微信视频号",
                          type="sniff_required", message=CHANNELS_URL_HINT)
            return result
        parsed: Optional[Dict[str, Any]] = None
        if detect_platform(url) == "bilibili":
            result.update(platform="bilibili", platform_name="哔哩哔哩")
            parsed = BiliURLParser.parse(url)
            gated = BILI_UNSUPPORTED
        else:
            platform = detect_ytdlp_platform(url)
            if platform:
                result.update(platform=platform, platform_name=platform_display_name(platform),
                              supported=True, type="video",
                              message="由 yt-dlp 解析；实际可用性取决于站点、登录权限和内容保护")
                return result
            if detect_platform(url) != "douyin":
                return result
            result.update(platform="douyin", platform_name="抖音")
            parsed = URLParser.parse(url)
            gated = UNSUPPORTED_URL_TYPE_DETAIL
        if not parsed:
            result["message"] = "无法识别此链接，请复制视频、作者主页、合集或收藏夹的完整链接"
            return result
        kind = str(parsed.get("type") or "")
        result["type"] = kind
        if kind in gated:
            result["message"] = gated[kind]
            return result
        if kind == "short" and not parts.path.strip("/"):
            result["message"] = "短链接缺少内容编号，请重新复制完整分享链接"
            return result
        if result["platform"] == "douyin":
            required_id = {
                "video": "aweme_id", "user": "sec_uid", "collection": "mix_id",
                "gallery": "aweme_id", "music": "music_id", "live": "room_id",
                "live_replay": "episode_id",
            }.get(kind)
            if required_id and not parsed.get(required_id):
                result["message"] = "链接缺少有效内容编号，请重新复制完整链接"
                return result
        result.update(supported=True, message="链接格式已识别；提交后会检查实际可用性")
    except (ValueError, TypeError):
        result["message"] = "链接格式无效，请重新复制完整链接"
    return result


def validate_config_updates(updates: Dict[str, Any]) -> None:
    """Reject malformed HTTP values before they replace shared config objects."""
    def check(values: Dict[str, Any], defaults: Dict[str, Any], prefix: str = "") -> None:
        for key, value in values.items():
            name = prefix + key
            expected = defaults.get(key)
            if key in {"number", "increase"}:
                if not isinstance(value, dict):
                    raise ValueError(f"{name} 必须是按模式/类型填写的对象")
                for mode, setting in value.items():
                    if key == "number" and (type(setting) is not int or setting < 0):
                        raise ValueError(f"{name}.{mode} 必须是大于或等于 0 的整数")
                    if key == "increase" and type(setting) is not bool:
                        raise ValueError(f"{name}.{mode} 必须是 true 或 false")
            elif isinstance(expected, dict):
                if not isinstance(value, dict):
                    raise ValueError(f"{name} 必须是对象")
                check(value, expected, name + ".")
            elif type(expected) is bool and type(value) is not bool:
                raise ValueError(f"{name} 必须是 true 或 false")
            elif key == "rate_limit":
                if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"{name} 必须是有效的正数")
            elif type(expected) is int:
                minimum = 1 if key in {"thread", "max_jobs", "page_size", "chunk_size"} else 0
                if type(value) is not int or value < minimum:
                    raise ValueError(f"{name} 必须是大于或等于 {minimum} 的整数")
            elif isinstance(expected, float):
                minimum = 0
                if (type(value) not in {int, float} or not math.isfinite(value)
                        or value < minimum):
                    raise ValueError(f"{name} 必须是有效的非负数")
            elif isinstance(expected, str) and not isinstance(value, str):
                raise ValueError(f"{name} 必须是文本")
            elif isinstance(expected, list) and not isinstance(value, list):
                raise ValueError(f"{name} 必须是列表")

    check(updates, DEFAULT_CONFIG)
    channels = updates.get("channels")
    if isinstance(channels, dict) and "quality" in channels:
        validate_channels_quality(channels["quality"])
    if "path" in updates and not updates["path"].strip():
        raise ValueError("下载目录不能为空")
    if "mode" in updates:
        modes = updates["mode"]
        allowed = set(UserModeRegistry().all_modes()) | {"allmix"}
        if not modes or any(not isinstance(mode, str) or mode not in allowed for mode in modes):
            raise ValueError("mode 必须包含有效下载模式：" + "、".join(sorted(allowed)))
    for key in ("start_time", "end_time"):
        value = updates.get(key)
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(f"{key} 请输入有效日期（YYYY-MM-DD）") from exc
    if updates.get("start_time") and updates.get("end_time"):
        if updates["start_time"] > updates["end_time"]:
            raise ValueError("开始日期不能晚于结束日期")
