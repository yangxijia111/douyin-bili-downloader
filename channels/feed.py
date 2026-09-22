"""微信视频号 feed 数据模型与提取。

视频号没有对外的 Web API：``channels.weixin.qq.com`` 的接口（finderFeed /
finderPcFlow / finderUserPage / live_replay_list 等）只在微信内嵌浏览器
（``WeChatAppEx.exe``，Chromium 内核）里带着登录态调用。响应 JSON 中的 feed
对象（特征是含 ``objectDesc`` 键）**明文携带**下载所需的一切：

* ``objectDesc.media[0].url + urlToken`` —— MP4 直链（finderVideo CDN）；
* ``objectDesc.media[0].decodeKey`` —— ISAAC64 解密种子（见
  :mod:`channels.isaac64`）；微信客户端自己播放也要用它，所以必然下发；
* ``objectDesc.mediaType`` —— 4=视频、2=图文、9=直播；
* 标题 / 作者 / 封面 / 时长 / 画质规格列表等元数据。

提取策略是**递归宽松扫描**：不按具体接口路径过滤（路径随微信版本频繁变
化），而是遍历响应 JSON 的每个层级，凡出现 ``objectDesc`` 结构就尝试解析。
原项目 wx_channels_download 依赖 30 个正则改写微信前端 JS 源码，微信一改
版即失效（其 issue #558）；本项目不注入页面，对这些改版零依赖。

字段路径参考 wx_channels_download 的 ``inject/channels.utils.js``
``format_feed``（MIT + Commons Clause，允许借鉴）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

__all__ = [
    "MEDIA_TYPE_IMAGE",
    "MEDIA_TYPE_LIVE",
    "MEDIA_TYPE_VIDEO",
    "ChannelFeed",
    "clean_media_url",
    "extract_feeds",
    "pick_quality_url",
]

# objectDesc.mediaType 的取值。
MEDIA_TYPE_VIDEO = 4
MEDIA_TYPE_IMAGE = 2
MEDIA_TYPE_LIVE = 9

# 画质档位里提取目标高度的模型（"1080p" / "1080p60" / "720p"…）。
_QUALITY_RE = re.compile(r"(\d{3,4})p")


def _as_int(value: Any, default: int = 0) -> int:
    """微信 JSON 的数字字段可能以 int 或 str 下发（decodeKey 超 JS 安全整数，
    必然是字符串），统一转 int；失败回退 default。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _media_url(media: Dict[str, Any]) -> str:
    """media 对象的完整下载地址 = ``url + urlToken``（token 可为空）。"""
    return (_as_str(media.get("url")) + _as_str(media.get("urlToken"))).strip()


@dataclass
class ChannelFeed:
    """一条捕获到的视频号动态。

    ``feed_id`` 用 ``objectNonceId``（一次性随机 id，同一条动态在推荐流里
    每次下发都不同）优先去重会误伤——**去重必须用 ``object_id``**
    （``objectId`` 稳定标识一条动态）；``feed_id`` 仅作展示与索引。
    """

    object_id: str = ""
    nonce_id: str = ""
    media_type: int = MEDIA_TYPE_VIDEO
    # video / image / live —— 由 media_type 与可用字段共同决定。
    kind: str = "video"
    url: str = ""
    decode_key: Optional[int] = None
    specs: List[Dict[str, Any]] = field(default_factory=list)
    file_size: int = 0
    duration: int = 0  # 秒
    title: str = ""
    author_name: str = ""
    author_id: str = ""
    author_avatar: str = ""
    cover_url: str = ""
    create_time: int = 0
    images: List[str] = field(default_factory=list)
    bgm_url: str = ""
    source_api: str = ""

    # 运行时状态（FeedStore / 下载器维护，序列化给前端用）。
    status: str = "pending"  # pending / downloading / done / failed / skipped
    downloaded_paths: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def feed_id(self) -> str:
        return self.nonce_id or self.object_id

    @property
    def dedup_key(self) -> str:
        return self.object_id or self.nonce_id

    @property
    def token(self) -> str:
        """增量判重 token，与 aweme 表 / 磁盘扫描的 ``channels_<id>`` 对齐。"""
        return f"channels_{self.object_id or self.nonce_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feed_id": self.feed_id,
            "object_id": self.object_id,
            "nonce_id": self.nonce_id,
            "kind": self.kind,
            "media_type": self.media_type,
            "url": self.url,
            "decode_key": self.decode_key,
            "specs": self.specs,
            "file_size": self.file_size,
            "duration": self.duration,
            "title": self.title,
            "author_name": self.author_name,
            "author_id": self.author_id,
            "author_avatar": self.author_avatar,
            "cover_url": self.cover_url,
            "create_time": self.create_time,
            "images": self.images,
            "bgm_url": self.bgm_url,
            "source_api": self.source_api,
            "status": self.status,
            "downloaded_paths": self.downloaded_paths,
            "error": self.error,
        }


# ----------------------------------------------------------------------
# 提取
# ----------------------------------------------------------------------

def _collect_feed_nodes(node: Any, out: List[Dict[str, Any]]) -> None:
    """递归收集所有含 ``objectDesc`` 的 dict 节点（含嵌套的关联推荐）。"""
    if isinstance(node, dict):
        if isinstance(node.get("objectDesc"), dict):
            out.append(node)
        for value in node.values():
            _collect_feed_nodes(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_feed_nodes(item, out)


def _feed_title(feed: Dict[str, Any], desc: Dict[str, Any]) -> str:
    """标题兜底链，与 wx_channels_download 的 ``get_feed_title`` 一致。"""
    flow_card = desc.get("flowCardDesc") or {}
    newlife = desc.get("finderNewlifeDesc") or {}
    title = (
        _as_str(desc.get("description"))
        or _as_str(flow_card.get("description"))
        or _as_str(newlife.get("richTextTitle"))
        or _as_str(feed.get("description"))
    ).strip()
    # 描述常含大量换行（话题标签排版），压缩成单行便于做文件名。
    return re.sub(r"\s*\n+\s*", " ", title).strip()


def _feed_from_node(feed: Dict[str, Any], source_api: str) -> Optional[ChannelFeed]:
    desc = feed.get("objectDesc") or {}
    if not isinstance(desc, dict):
        return None
    object_id = _as_str(feed.get("objectId"))
    nonce_id = _as_str(feed.get("objectNonceId"))
    if not object_id and not nonce_id:
        return None  # 结构不完整（改版 / 半截响应），交给下一条

    media_list = desc.get("media") or []
    media_list = [m for m in media_list if isinstance(m, dict)]
    first = media_list[0] if media_list else {}
    contact = feed.get("contact") or {}
    if not isinstance(contact, dict):
        contact = {}

    media_type = _as_int(desc.get("mediaType"), MEDIA_TYPE_VIDEO)
    live_info = feed.get("liveInfo") or {}
    if not isinstance(live_info, dict):
        live_info = {}

    item = ChannelFeed(
        object_id=object_id,
        nonce_id=nonce_id,
        media_type=media_type,
        title=_feed_title(feed, desc) or object_id,
        author_name=_as_str(contact.get("nickname")) or _as_str(contact.get("username")),
        author_id=_as_str(contact.get("username")),
        author_avatar=_as_str(contact.get("headUrl")),
        create_time=_as_int(feed.get("createtime")),
        source_api=source_api,
    )

    if media_type == MEDIA_TYPE_LIVE or _as_str(live_info.get("streamUrl")):
        # 直播：FLV 流地址，不加密，由 ffmpeg 拉流录制。
        item.kind = "live"
        item.url = _as_str(live_info.get("streamUrl"))
        anchor = feed.get("anchorContact") or {}
        if isinstance(anchor, dict):
            item.cover_url = _as_str(anchor.get("liveCoverImgUrl"))
        if not item.cover_url:
            item.cover_url = _as_str(first.get("coverUrl"))
        return item

    if media_type == MEDIA_TYPE_IMAGE:
        # 图文：media 是图片列表；BGM 在 followPostInfo.musicInfo。
        item.kind = "image"
        item.images = [_media_url(m) for m in media_list if _media_url(m)]
        follow_post = desc.get("followPostInfo") or {}
        if isinstance(follow_post, dict):
            music = follow_post.get("musicInfo") or {}
            if isinstance(music, dict):
                item.bgm_url = _as_str(music.get("mediaStreamingUrl"))
        cover = next(
            (
                _as_str(m.get(k))
                for m in media_list
                for k in ("coverUrl", "thumbUrl", "fullThumbUrl", "fullUrl")
                if _as_str(m.get(k))
            ),
            "",
        )
        item.cover_url = cover or (item.images[0] if item.images else "")
        return item

    # 普通视频（mediaType=4 或未知类型但有可下载 media 的，宽松归入视频）。
    item.kind = "video"
    item.url = _media_url(first)
    if not item.url:
        return None
    decode_key = _as_int(first.get("decodeKey"), 0)
    item.decode_key = decode_key or None
    specs = first.get("spec")
    item.specs = [s for s in specs if isinstance(s, dict)] if isinstance(specs, list) else []
    item.file_size = _as_int(first.get("fileSize"))
    item.duration = _as_int(first.get("videoPlayLen"))
    item.cover_url = _as_str(first.get("coverUrl"))
    return item


def extract_feeds(payload: Any, *, source_api: str = "") -> List[ChannelFeed]:
    """从任意 API 响应 JSON 中提取全部 feed（递归、按 objectId 去重）。"""
    nodes: List[Dict[str, Any]] = []
    _collect_feed_nodes(payload, nodes)
    feeds: List[ChannelFeed] = []
    seen: set = set()
    for node in nodes:
        try:
            feed = _feed_from_node(node, source_api)
        except Exception:  # noqa: BLE001 —— 单节点解析失败不影响其余捕获
            continue
        if feed is None or not feed.dedup_key or feed.dedup_key in seen:
            continue
        seen.add(feed.dedup_key)
        feeds.append(feed)
    return feeds


# ----------------------------------------------------------------------
# 画质选择
# ----------------------------------------------------------------------

def clean_media_url(url: str) -> str:
    """重建只含 ``encfilekey`` / ``token`` 的干净 URL。

    微信下发的 media.url 挂着一堆一次性参数（dis_k / dis_t / idx …），部分
    参数过期后会导致重试失败；这两个是 CDN 定位与鉴权的核心，其余可丢。若
    URL 里两者都没有则原样返回。
    """
    if not url:
        return url
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k in ("encfilekey", "token")]
    if not kept:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _spec_height(spec: Dict[str, Any]) -> int:
    # 部分转码档只有 width（竖屏视频），用 width 近似排序足够。
    return _as_int(spec.get("height"), 0) or _as_int(spec.get("width"), 0)


def pick_quality_url(feed: ChannelFeed, quality: str = "highest") -> str:
    """按画质档位构造视频下载 URL。

    - ``highest`` / ``original`` / 空：微信下发的 media.url 即当前最佳版本
      （客户端播放用的就是它），重建干净 URL 直接下载；
    - ``lowest``：spec 里高度最低的档；
    - 具体档位（``1080p`` / ``720p`` …）：取高度最接近的 spec 档；
    - 指定档时把该档 ``fileFormat`` 作为 ``X-snsvideoflag`` 参数追加——微信
      CDN 以此切换转码档（与 wx_channels_download 一致）。

    spec 缺失或档位匹配失败时一律回退干净 URL，不失败。
    """
    if feed.kind != "video" or not feed.url:
        return feed.url
    wanted = (quality or "highest").strip().lower()
    base = clean_media_url(feed.url)
    if wanted in ("", "highest", "original"):
        return base

    usable = [s for s in feed.specs if _as_str(s.get("fileFormat"))]
    if not usable:
        return base
    if wanted == "lowest":
        target = min(usable, key=_spec_height)
    else:
        match = _QUALITY_RE.search(wanted)
        if not match:
            return base
        goal = int(match.group(1))
        target = min(usable, key=lambda s: abs(_spec_height(s) - goal))

    flag = quote(_as_str(target.get("fileFormat")))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}X-snsvideoflag={flag}"
