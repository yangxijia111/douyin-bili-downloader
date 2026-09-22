"""通用视频平台（yt-dlp 引擎）的链接识别。

抖音与 B 站各自有独立实现的 API 客户端；除此之外的知名平台（爱奇艺、腾讯视频、
优酷、芒果 TV、快手、西瓜视频、今日头条、微博、小红书）统一交给 yt-dlp 引擎
解析与下载。本模块只做**纯函数、不依赖网络**的域名判定，供 CLI / Server 在建
任何客户端之前完成平台分流。

路由顺序（见 ``cli.main.download_url`` / ``server.app._execute_download``）：
B 站 → yt-dlp 平台 → 抖音兜底。三组域名互不重叠，顺序只影响裸 ID 这类没有
host 的输入的归属，不影响正确性。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse


class PlatformSpec:
    """一个 yt-dlp 平台的静态描述。

    ``key`` 是配置与历史库里的稳定标识（``ytdlp.platforms.<key>``、
    ``url_type="ytdlp:<key>:video"``），``name`` 是给用户看的中文名，
    ``hosts`` 是命中该平台的域名后缀（``www.`` / ``m.`` 等子域同样命中），
    ``cookie_domain`` 是写 Netscape Cookie 文件时的作用域。
    """

    __slots__ = ("key", "name", "hosts", "cookie_domain")

    def __init__(self, key: str, name: str, hosts: Tuple[str, ...], cookie_domain: str):
        self.key = key
        self.name = name
        self.hosts = hosts
        self.cookie_domain = cookie_domain


# 有序：判定时按声明顺序匹配，先声明的平台优先。
SUPPORTED_PLATFORMS: Tuple[PlatformSpec, ...] = (
    PlatformSpec("iqiyi", "爱奇艺", ("iqiyi.com", "iq.com"), ".iqiyi.com"),
    PlatformSpec("tencent", "腾讯视频", ("v.qq.com", "video.qq.com"), ".qq.com"),
    PlatformSpec("youku", "优酷", ("youku.com",), ".youku.com"),
    PlatformSpec("mgtv", "芒果TV", ("mgtv.com",), ".mgtv.com"),
    PlatformSpec(
        "kuaishou",
        "快手",
        ("kuaishou.com", "gifshow.com", "chenzhongtech.com"),
        ".kuaishou.com",
    ),
    PlatformSpec("xigua", "西瓜视频", ("ixigua.com",), ".ixigua.com"),
    PlatformSpec("toutiao", "今日头条", ("toutiao.com",), ".toutiao.com"),
    PlatformSpec("weibo", "微博", ("weibo.com", "weibo.cn"), ".weibo.com"),
    PlatformSpec("xiaohongshu", "小红书", ("xiaohongshu.com", "xhslink.com"), ".xiaohongshu.com"),
)

PLATFORM_BY_KEY: Dict[str, PlatformSpec] = {spec.key: spec for spec in SUPPORTED_PLATFORMS}


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def normalize_url(url: str) -> str:
    """裸域名链接（分享文案里常见的 ``v.kuaishou.com/xxx``）补上 https。"""
    stripped = (url or "").strip()
    if not stripped:
        return ""
    if stripped.lower().startswith(("http://", "https://")):
        return stripped
    return f"https://{stripped}"


def detect_ytdlp_platform(url: str) -> Optional[str]:
    """返回命中的平台 ``key``（如 ``"iqiyi"``），非 yt-dlp 平台返回 ``None``。"""
    candidate = normalize_url(url)
    if not candidate:
        return None
    host = (urlparse(candidate).hostname or "").lower()
    if not host:
        return None
    for spec in SUPPORTED_PLATFORMS:
        if any(_host_matches(host, suffix) for suffix in spec.hosts):
            return spec.key
    return None


def is_ytdlp_url(url: str) -> bool:
    return detect_ytdlp_platform(url) is not None


def platform_display_name(key: Optional[str]) -> str:
    """平台中文名；未知 key 原样返回，避免日志里出现空串。"""
    spec = PLATFORM_BY_KEY.get(str(key or ""))
    return spec.name if spec else str(key or "")


class YtdlpURLParser:
    """把链接归一成下载器需要的最小描述。

    yt-dlp 自己会识别站内的具体形态（单集 / 剧集页 / 用户页），这里不重复解析
    路径，只给出 ``platform`` 与归一化后的 ``url``；``type`` 固定为 ``"video"``，
    与其他平台的 ``url_type`` 命名空间对齐（配置里的 ``number.video`` /
    ``increase.video`` 也按这个键取值）。
    """

    @staticmethod
    def parse(url: str) -> Optional[Dict[str, Any]]:
        raw = (url or "").strip()
        if not raw:
            return None
        platform = detect_ytdlp_platform(raw)
        if platform is None:
            return None
        return {
            "type": "video",
            "platform": platform,
            "platform_name": platform_display_name(platform),
            "url": normalize_url(raw),
            "original_url": raw,
        }
