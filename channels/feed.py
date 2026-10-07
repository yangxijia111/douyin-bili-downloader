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
版即失效（其 issue #558）；本项目不注入页面，显著降低前端改版导致的失效
概率——但仍依赖 ``objectDesc`` / ``media`` / ``decodeKey`` / ``mediaType`` /
``liveInfo`` 这些 API 字段结构与 ISAAC64 / CDN 行为，协议层改动仍需适配。

字段路径参考 wx_channels_download 的 ``inject/channels.utils.js``
``format_feed``（MIT + Commons Clause，允许借鉴）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

__all__ = [
    "MEDIA_TYPE_IMAGE",
    "MEDIA_TYPE_LIVE",
    "MEDIA_TYPE_VIDEO",
    "ChannelFeed",
    "clean_media_url",
    "extract_feeds",
    "extract_preview_feeds",
    "pick_quality_url",
    "quality_options",
    "resolve_quality",
]

# objectDesc.mediaType 的取值。
MEDIA_TYPE_VIDEO = 4
MEDIA_TYPE_IMAGE = 2
MEDIA_TYPE_LIVE = 9

# 画质档位里提取目标高度的模型（"1080p" / "1080p60" / "720p"…）。
_QUALITY_RE = re.compile(r"(\d{3,4})p(?:\d{1,3})?")
_FORMAT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_IMAGE_PATH_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp|avif|svg)$", re.IGNORECASE)


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


def _original_media_url(media: Dict[str, Any]) -> str:
    """只认视频 media 的明确 fullUrl，不从封面或播放地址猜测原画链接。"""
    value = media.get("fullUrl") or media.get("fullURL")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    if host != "finder.video.qq.com" and not host.endswith(".finder.video.qq.com"):
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        return ""
    if _IMAGE_PATH_RE.search(unquote(parts.path)):
        return ""
    return value


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
    full_url: str = ""  # 仅来自视频 media.fullUrl 的明确原文件地址。
    decode_key: Optional[int] = None
    specs: List[Dict[str, Any]] = field(default_factory=list)
    width: int = 0  # 当前播放源几何；不代表所有档位中的最高画质。
    height: int = 0
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
    downloaded_quality: Dict[str, Any] = field(default_factory=dict)
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

    @property
    def has_original(self) -> bool:
        return self.kind == "video" and bool(_original_media_url({"fullUrl": self.full_url}))

    def to_dict(self) -> Dict[str, Any]:
        """完整内部视图：含直链、decodeKey 等下载必需的敏感字段。

        **只允许进程内使用**（下载器、CLI 会话）。对外（REST / Web）必须走
        :meth:`to_public_dict`，避免把临时 CDN 直链与解密密钥泄漏给浏览器端。
        """
        return {
            "feed_id": self.feed_id,
            "object_id": self.object_id,
            "nonce_id": self.nonce_id,
            "kind": self.kind,
            "media_type": self.media_type,
            "url": self.url,
            "full_url": self.full_url,
            "decode_key": self.decode_key,
            "specs": self.specs,
            "width": self.width,
            "height": self.height,
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
            "downloaded_quality": self.downloaded_quality,
            "error": self.error,
        }

    def to_public_dict(self) -> Dict[str, Any]:
        """对外展示视图（REST / Web 唯一允许的序列化）。

        刻意排除：``url``（带一次性 CDN token 的直链）、``decode_key``（ISAAC64
        解密种子）、``bgm_url`` / ``images``（同样是 CDN 直链）、``specs``（含
        各档转码 URL 参数）、``source_api``（内部接口路径）、``object_id`` /
        ``nonce_id``（``feed_id`` 已够前端定位条目）。网页控制台用不到这些
        字段，就不外泄——内部下载走进程内对象，不需要 public model 之外的
        通道。
        """
        return {
            "feed_id": self.feed_id,
            "kind": self.kind,
            "media_type": self.media_type,
            "title": self.title,
            "author_name": self.author_name,
            "author_id": self.author_id,
            "cover_url": self.cover_url,
            "create_time": self.create_time,
            "duration": self.duration,
            "file_size": self.file_size,
            "quality_options": quality_options(self),
            "has_original": self.has_original,
            "status": self.status,
            "downloaded_paths": self.downloaded_paths,
            "downloaded_quality": _public_downloaded_quality(self.downloaded_quality),
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
    desc = feed.get("objectDesc")
    if not isinstance(desc, dict):
        return None
    # 内嵌视频号详情/播放列表以 id 下发同一稳定标识；只有已经确认的
    # objectDesc 节点才使用这个别名，避免把普通响应的通用 id 当作视频。
    # _as_str 不经浮点转换，保留 uint64 字符串的每一位。
    object_id = _as_str(feed.get("objectId")) or _as_str(feed.get("id"))
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
    item.full_url = _original_media_url(first)
    item.url = _media_url(first) or item.full_url
    if not item.url:
        return None
    decode_key = _as_int(first.get("decodeKey"), 0)
    item.decode_key = decode_key or None
    specs = first.get("spec")
    item.specs = [s for s in specs if isinstance(s, dict)] if isinstance(specs, list) else []
    item.width = max(0, _as_int(first.get("width")))
    item.height = max(0, _as_int(first.get("height")))
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
# 分享链接预览页（finder-preview）的 sceneInfo 模式
# ----------------------------------------------------------------------

def _collect_preview_nodes(node: Any, out: List[Dict[str, Any]], depth: int) -> None:
    """递归收集 preview 页 sceneInfo 形态的 feed 节点。

    预览页接口（getFeedInfo）返回的对象特征是含 ``videoUrl`` / ``picInfo``
    字段（而不是 ``objectDesc``）——字段名来自对官方 preview bundle 的观察
    （videoUrl / coverUrl / mediaType / description / nickname / id）。
    """
    if depth > 32 or node is None or not isinstance(node, (dict, list)):
        return
    if isinstance(node, list):
        for item in node:
            _collect_preview_nodes(item, out, depth + 1)
        return
    if isinstance(node.get("videoUrl"), str) or isinstance(node.get("picInfo"), list):
        out.append(node)
    for value in node.values():
        _collect_preview_nodes(value, out, depth + 1)


def _preview_feed_from_node(node: Dict[str, Any], source_api: str) -> Optional[ChannelFeed]:
    video_url = _as_str(node.get("videoUrl"))
    images = node.get("picInfo")
    images = [i for i in images if isinstance(i, dict)] if isinstance(images, list) else []
    if not video_url and not images:
        return None
    object_id = _as_str(node.get("id")) or _as_str(node.get("dynamicExportId"))
    if not object_id:
        return None
    media_type = _as_int(node.get("mediaType"), MEDIA_TYPE_VIDEO)
    item = ChannelFeed(
        object_id=object_id,
        nonce_id=_as_str(node.get("dynamicExportId")) or object_id,
        media_type=media_type,
        title=_as_str(node.get("description")) or object_id,
        author_name=_as_str(node.get("nickname")),
        author_id=_as_str(node.get("username")),
        author_avatar=_as_str(node.get("headUrl") or node.get("avatar")),
        create_time=_as_int(node.get("createTime")),
        source_api=source_api,
    )
    if video_url:
        item.kind = "video"
        item.url = video_url
        # 预览页不下发 decodeKey（页面自行解密）；下载时若头部校验失败，
        # 由调用方按「可能未加密」重试（见 downloader 的宽容路径）。
        item.decode_key = None
        item.cover_url = _as_str(node.get("coverUrl"))
        item.duration = _as_int(node.get("videoDuration") or node.get("duration"))
        item.file_size = _as_int(node.get("fileSize") or node.get("videoSize"))
        return item
    # 图文：picInfo 是图片列表。
    item.kind = "image"
    item.images = [_as_str(i.get("url")) for i in images if _as_str(i.get("url"))]
    item.cover_url = _as_str(node.get("coverUrl")) or (item.images[0] if item.images else "")
    return item


def extract_preview_feeds(payload: Any, *, source_api: str = "") -> List[ChannelFeed]:
    """从 preview 页（分享链接）响应中提取 feed（sceneInfo 模式）。"""
    nodes: List[Dict[str, Any]] = []
    _collect_preview_nodes(payload, nodes, 0)
    feeds: List[ChannelFeed] = []
    seen: set = set()
    for node in nodes:
        try:
            feed = _preview_feed_from_node(node, source_api)
        except Exception:  # noqa: BLE001 —— 单节点失败不影响其余
            continue
        if feed is None or feed.dedup_key in seen:
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


def _spec_format(spec: Dict[str, Any]) -> str:
    value = _as_str(spec.get("fileFormat") or spec.get("file_format"))
    return value if _FORMAT_RE.fullmatch(value) else ""


def _spec_dimensions(spec: Dict[str, Any]) -> Tuple[int, int]:
    return max(0, _as_int(spec.get("width"))), max(0, _as_int(spec.get("height")))


def _spec_resolution(spec: Dict[str, Any]) -> int:
    width, height = _spec_dimensions(spec)
    # 单轴无法判断短边；不能把竖屏高度 1920 冒称为 1920p。
    return min(width, height) if width and height else 0


def _spec_rank(spec: Dict[str, Any]) -> Tuple[int, int, int, int]:
    width, height = _spec_dimensions(spec)
    bitrate = _as_int(
        spec.get("bitrate") or spec.get("bitRate") or spec.get("bit_rate")
        or spec.get("videoBitrate") or spec.get("video_bitrate")
    )
    size = _as_int(spec.get("fileSize") or spec.get("file_size"))
    area = width * height if width and height else width or height
    return _spec_resolution(spec), area, max(0, bitrate), max(0, size)


def _usable_specs(feed: ChannelFeed) -> List[Dict[str, Any]]:
    formats: Dict[str, Dict[str, Any]] = {}
    for spec in feed.specs:
        if not isinstance(spec, dict):
            continue
        # dynamicRangeType 非零是 HDR 规格；通用下载只选择兼容的 SDR 档。
        if _as_int(spec.get("dynamicRangeType") or spec.get("dynamic_range_type")):
            continue
        format_id = _spec_format(spec)
        if not format_id:
            continue
        old = formats.get(format_id)
        if old is None or _spec_rank(spec) > _spec_rank(old):
            formats[format_id] = spec
    return sorted(formats.values(), key=_spec_rank, reverse=True)


def _spec_label(spec: Dict[str, Any]) -> str:
    width, height = _spec_dimensions(spec)
    resolution = _spec_resolution(spec)
    if width and height:
        return f"{resolution}p · {width}×{height}"
    if width:
        return f"宽度{width}（高度未知）"
    if height:
        return f"高度{height}（宽度未知）"
    return "已捕获档位（分辨率未知）"


def _current_source_label(feed: ChannelFeed) -> str:
    if feed.width or feed.height:
        geometry = _spec_label({"width": feed.width, "height": feed.height})
        return f"当前源 · {geometry}（未捕获其他画质档位）"
    return "当前源（未捕获画质档位）"


def _replace_quality_flag(url: str, format_id: str) -> str:
    """只替换画质参数；其余签名的编码、大小写、顺序原样保留。"""
    before_fragment, hash_sep, fragment = url.partition("#")
    path, query_sep, query = before_fragment.partition("?")
    output: List[str] = []
    replaced = False
    for item in query.split("&") if query_sep else []:
        key = unquote(item.split("=", 1)[0]).lower()
        if key == "x-snsvideoflag":
            if not replaced:
                output.append("X-snsvideoflag=" + format_id)
                replaced = True
        else:
            output.append(item)
    if not replaced:
        output.append("X-snsvideoflag=" + format_id)
    result = path + "?" + "&".join(output)
    return result + hash_sep + fragment


def quality_options(feed: ChannelFeed) -> List[Dict[str, Any]]:
    """公开的安全画质菜单；不携带 URL、密钥或原始 spec。"""
    if feed.kind != "video":
        return []
    usable = _usable_specs(feed)
    options: List[Dict[str, Any]] = []
    highest = usable[0] if usable else None
    width, height = _spec_dimensions(highest) if highest is not None else (feed.width, feed.height)
    label = "最高可用 · " + _spec_label(highest) if highest is not None else _current_source_label(feed)
    options.append({"value": "highest", "label": label, "width": width, "height": height})
    if feed.has_original:
        options.append({"value": "original", "label": "原画（已捕获源文件）", "width": 0, "height": 0})
    lowest = usable[-1] if usable else None
    width, height = _spec_dimensions(lowest) if lowest is not None else (feed.width, feed.height)
    label = "最低可用 · " + _spec_label(lowest) if lowest is not None else _current_source_label(feed)
    options.append({"value": "lowest", "label": label, "width": width, "height": height})
    labels: Dict[str, int] = {}
    for spec in usable:
        label = _spec_label(spec)
        labels[label] = labels.get(label, 0) + 1
    label_indices: Dict[str, int] = {}
    for spec in usable:
        width, height = _spec_dimensions(spec)
        label = _spec_label(spec)
        if labels[label] > 1:
            label_indices[label] = label_indices.get(label, 0) + 1
            label = f"{label} · 档位 {label_indices[label]}"
        options.append({"value": "format:" + _spec_format(spec), "label": label,
                        "width": width, "height": height})
    return options


def resolve_quality(feed: ChannelFeed, quality: str = "highest") -> Dict[str, Any]:
    """返回同一次选择的下载 URL 与安全规格，供下载、完整性校验共用。"""
    requested = _as_str(quality).strip() or "highest"
    wanted = requested.lower()
    current = {"url": feed.url, "value": "highest", "label": _current_source_label(feed),
               "width": feed.width, "height": feed.height}
    if feed.kind != "video":
        return current
    if wanted == "original" and feed.has_original:
        return {"url": feed.full_url, "value": "original", "label": "原画（已捕获源文件）",
                "width": 0, "height": 0}
    if not feed.url:
        return current
    usable = _usable_specs(feed)
    if not usable:
        return current
    target = usable[0]
    value = "highest"
    label_prefix = "最高可用 · "
    if wanted == "lowest":
        target = usable[-1]
        value = "lowest"
        label_prefix = "最低可用 · "
    elif wanted.startswith("format:"):
        # fileFormat 区分大小写，不能 lower() 后发送给 CDN。
        format_id = requested[len("format:"):]
        exact = next((s for s in usable if _spec_format(s) == format_id), None)
        if exact is not None:
            target = exact
            value = "format:" + format_id
            label_prefix = ""
    else:
        match = _QUALITY_RE.fullmatch(wanted)
        if match:
            goal = int(match.group(1))
            # usable 按画质降序，同距离和同分辨率自动保留更好的规格。
            known = [s for s in usable if _spec_resolution(s)]
            if known:
                target = min(known, key=lambda s: abs(_spec_resolution(s) - goal))
                value = "format:" + _spec_format(target)
                label_prefix = ""
    width, height = _spec_dimensions(target)
    return {"url": _replace_quality_flag(feed.url, _spec_format(target)), "value": value,
            "label": label_prefix + _spec_label(target), "width": width, "height": height}


def pick_quality_url(feed: ChannelFeed, quality: str = "highest") -> str:
    """最高档按短边/面积/码率/大小选择；原画只使用明确的 media.fullUrl。"""
    return resolve_quality(feed, quality)["url"]


def _public_downloaded_quality(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    width, height = _spec_dimensions(value)
    safe: Dict[str, Any] = {"width": width, "height": height,
                           "label": _spec_label(value) if width or height else "画质未识别"}
    for field_name in ("requested", "selected"):
        text = _as_str(value.get(field_name))
        if text in ("highest", "lowest", "original") or _QUALITY_RE.fullmatch(text):
            safe[field_name] = text
        elif text.startswith("format:") and _FORMAT_RE.fullmatch(text[len("format:"):]):
            safe[field_name] = text
        else:
            safe[field_name] = ""
    return safe
