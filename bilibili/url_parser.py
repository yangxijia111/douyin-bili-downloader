"""B 站链接识别与解析，以及跨平台路由判定。

CLI 需要在建任何 API 客户端之前就知道链接属于哪个平台（抖音客户端与 B 站
客户端互不通用），所以平台判定必须是一个不依赖网络的纯函数。

支持识别的链接形态：

======================  ==========================================================
形态                     说明
======================  ==========================================================
``/video/BV...``         单稿件；``?p=N`` 指定分 P，缺省下全部
``/video/av123``         老 av 号稿件
``BV1xx411c7mD``         裸 bvid（用户直接粘贴 ID 的常见情形）
``space.bilibili.com/<mid>``  UP 主空间（``/video`` 与 ``/upload/video`` 同上）
``.../channel/collectiondetail?sid=N``  合集
``.../channel/seriesdetail?sid=N``      系列
``/list/<mid>?sid=N&type=season``       合集播放页
``/list/<mid>?sid=N&type=series``       系列播放页
``.../favlist?fid=N``     收藏夹
``/list/ml<N>``           收藏夹播放页
``/medialist/play/<mid>?business=...``  旧版合集/收藏夹播放页
``b23.tv/xxxx``           站内短链，需先展开
======================  ==========================================================
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from utils.logger import setup_logger

logger = setup_logger("BiliURLParser")

# 站内短链域名，需要先请求一次拿到落点。
SHORT_URL_HOSTS = ("b23.tv", "acg.tv")

# 内容域名后缀（space. / www. / m. 等都命中）。
_CONTENT_HOST_SUFFIXES = ("bilibili.com",)

# 裸 ID 形态：bvid 固定 12 字符（BV + 10 位 Base58），av 号为纯数字。
_BARE_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_BARE_AVID_RE = re.compile(r"^av(\d+)$", re.IGNORECASE)
_VIDEO_PATH_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10}|av(\d+))", re.IGNORECASE)
_FAV_ML_PATH_RE = re.compile(r"/list/ml(\d+)")
_LIST_PATH_RE = re.compile(r"/list/(\d+)")
_MEDIALIST_PATH_RE = re.compile(r"/medialist/play/(ml\d+|\d+)")
_SPACE_MID_RE = re.compile(r"^/(?:space/)?(\d+)(?:/|$)")

# 解析得出但永不支持的版权内容路径：番剧 / 影视（bangumi）、付费课程（cheese）
# 与活动页（festival）。识别成 type="bangumi" 后由工厂门禁给出明确解释，
# 而不是落到「无法解析」让用户以为链接写错了。
_GATED_PATH_RES = (
    re.compile(r"^/(?:bangumi|cheese|festival)/", re.IGNORECASE),
)

# 抖音侧域名，仅用于「非 B 站链接一律按抖音处理」的兜底判定。
_DOUYIN_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com", "amemv.com")


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def is_bili_short_url(url: str) -> bool:
    if not url:
        return False
    candidate = (url or "").strip()
    lowered = candidate.lower()
    for scheme in ("https://", "http://"):
        if lowered.startswith(scheme):
            lowered = lowered[len(scheme):]
            break
    for host in SHORT_URL_HOSTS:
        if lowered.startswith(f"{host}/") or lowered == host:
            return True
    return False


def normalize_short_url(url: str) -> str:
    stripped = (url or "").strip()
    if stripped.lower().startswith(("http://", "https://")):
        return stripped
    return f"https://{stripped}"


def is_bilibili_url(url: str) -> bool:
    """判断链接（或裸 ID）是否属于 B 站。"""
    if not url:
        return False
    candidate = url.strip()
    if not candidate:
        return False
    if _BARE_BVID_RE.match(candidate) or _BARE_AVID_RE.match(candidate):
        return True

    host = (urlparse(candidate).hostname or "").lower()
    if not host:
        return False
    return any(_host_matches(host, suffix) for suffix in _CONTENT_HOST_SUFFIXES) or any(
        _host_matches(host, suffix) for suffix in SHORT_URL_HOSTS
    )


def is_douyin_url(url: str) -> bool:
    if not url:
        return False
    host = (urlparse(url.strip()).hostname or "").lower()
    if not host:
        return False
    return any(_host_matches(host, suffix) for suffix in _DOUYIN_HOST_SUFFIXES)


def detect_platform(url: str) -> Optional[str]:
    """返回 ``"bilibili"`` / ``"douyin"`` / ``None``。

    B 站判定优先：``b23.tv`` 之类的短链域名与抖音无交集，顺序不影响正确性，
    但先判 B 站能让「裸 bvid」这种没有 host 的输入也走到正确分支。
    """
    if is_bilibili_url(url):
        return "bilibili"
    if is_douyin_url(url):
        return "douyin"
    return None


class BiliURLParser:
    @staticmethod
    def parse(url: str) -> Optional[Dict[str, Any]]:
        raw = (url or "").strip()
        if not raw:
            return None

        result: Dict[str, Any] = {"original_url": raw}

        # 裸 ID：没有 host，直接归一化成一个可复现的规范链接。
        if _BARE_BVID_RE.match(raw):
            result.update(type="video", bvid=raw)
            return result
        bare_av = _BARE_AVID_RE.match(raw)
        if bare_av:
            result.update(type="video", aid=bare_av.group(1))
            return result

        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        query = parse_qs(parsed.query)

        if is_bili_short_url(raw):
            result["type"] = "short"
            return result

        if not any(_host_matches(host, suffix) for suffix in _CONTENT_HOST_SUFFIXES):
            return None

        # 版权内容（番剧 / 影视 / 付费课程 / 活动页）先于普通稿件判定：它们的
        # path 里也可能出现 BV/av 形态的 token，但不能按普通稿件下载。
        if any(pattern.search(path) for pattern in _GATED_PATH_RES):
            result["type"] = "bangumi"
            return result

        video_match = _VIDEO_PATH_RE.search(path)
        if video_match:
            result["type"] = "video"
            token = video_match.group(1)
            if token.lower().startswith("av"):
                result["aid"] = video_match.group(2)
            else:
                result["bvid"] = token
            page = _first_int(query, "p") or _first_int(query, "page")
            if page is not None:
                result["page"] = page
            return result

        # 合集 / 系列的规范跳转页：/list/{mid}?sid=N&type=season|series
        list_match = _LIST_PATH_RE.fullmatch(path)
        if list_match:
            list_type = (_first_str(query, "type") or "").lower()
            sid = _first_str(query, "sid")
            mid = list_match.group(1)
            if list_type == "season" and sid:
                result.update(type="collection", season_id=sid, mid=mid)
                return result
            if list_type == "series" and sid:
                result.update(type="series", series_id=sid, mid=mid)
                return result

        fav_ml_match = _FAV_ML_PATH_RE.search(path)
        if fav_ml_match:
            result.update(type="favlist", media_id=fav_ml_match.group(1))
            return result

        # 旧版播放页：/medialist/play/{mid}?business=space_collection&business_id=N
        medialist_match = _MEDIALIST_PATH_RE.search(path)
        if medialist_match:
            business = (_first_str(query, "business") or "").lower()
            business_id = _first_str(query, "business_id")
            owner = medialist_match.group(1)
            if owner.lower().startswith("ml"):
                result.update(type="favlist", media_id=owner[2:])
                return result
            if business_id and business == "space_collection":
                result.update(type="collection", season_id=business_id, mid=owner)
                return result
            if business_id and business == "space_series":
                result.update(type="series", series_id=business_id, mid=owner)
                return result

        space_mid = _SPACE_MID_RE.match(path)
        if space_mid:
            mid = space_mid.group(1)
            if "/channel/collectiondetail" in path:
                sid = _first_str(query, "sid")
                if sid:
                    result.update(type="collection", season_id=sid, mid=mid)
                    return result
            if "/channel/seriesdetail" in path:
                sid = _first_str(query, "sid")
                if sid:
                    result.update(type="series", series_id=sid, mid=mid)
                    return result
            if "/favlist" in path:
                fid = _first_str(query, "fid")
                if fid:
                    result.update(type="favlist", media_id=fid, mid=mid)
                    return result
            if "/channel/" in path or "/lists" in path:
                logger.warning("Unsupported bilibili channel page: %s", raw)
                return None
            # 纯空间首页 / /video / /upload/video 都按 UP 主投稿处理。
            result.update(type="user", mid=mid)
            return result

        # www.bilibili.com/space/{mid} 变体
        space_match = re.match(r"^/space/(\d+)", path)
        if space_match:
            result.update(type="user", mid=space_match.group(1))
            return result

        logger.warning("Unsupported bilibili URL: %s", raw)
        return None

    @staticmethod
    def build_url(parsed: Dict[str, Any]) -> str:
        """由解析结果反推一个规范链接，用于日志与增量记录。"""
        url_type = parsed.get("type")
        if url_type == "video":
            if parsed.get("bvid"):
                base = f"https://www.bilibili.com/video/{parsed['bvid']}"
                page = parsed.get("page")
                return f"{base}?p={page}" if page else base
            if parsed.get("aid"):
                return f"https://www.bilibili.com/video/av{parsed['aid']}"
            return ""
        if url_type == "user":
            return f"https://space.bilibili.com/{parsed.get('mid')}/video"
        if url_type == "collection":
            return (
                f"https://space.bilibili.com/{parsed.get('mid')}"
                f"/channel/collectiondetail?sid={parsed.get('season_id')}"
            )
        if url_type == "series":
            return (
                f"https://space.bilibili.com/{parsed.get('mid')}"
                f"/channel/seriesdetail?sid={parsed.get('series_id')}"
            )
        if url_type == "favlist":
            return (
                f"https://space.bilibili.com/{parsed.get('mid', '')}"
                f"/favlist?fid={parsed.get('media_id')}"
            )
        return parsed.get("original_url") or ""


def _first_str(query: Dict[str, list], key: str) -> Optional[str]:
    values = query.get(key)
    if not values:
        return None
    value = str(values[0]).strip()
    return value or None


def _first_int(query: Dict[str, list], key: str) -> Optional[int]:
    value = _first_str(query, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
