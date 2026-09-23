"""微信视频号分享链接解析（v2.0.3 链接粘贴下载入口）。

用户在微信里「分享 → 复制链接」得到的视频号链接有两种形态::

    短链   https://weixin.qq.com/sph/<id>
    全链   https://channels.weixin.qq.com/finder-preview/pages/sph?id=<id>

两者都指向同一个「视频号预览页」。该页面在**微信内置浏览器**里会调用
``getFeedInfo`` 接口取回视频数据（普通浏览器打开只显示二维码）——因此
本项目的链接下载流程是::

    粘贴链接 → 识别 id → 启动/复用嗅探会话（注入已覆盖预览页路径）
             → 用户在微信里打开该链接 → 预览页的 API 响应经注入脚本
               的 fetch/XHR hook 捕获 → FeedStore → 自动下载

预览页路径（``/finder-preview/pages/``）与普通页面（``/web/pages/``）
不同，注入白名单与前端页面类型识别都已覆盖（见 channels.injector 与
cuin_core.js 的 detectPageType）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = ["ChannelsShareLink", "parse_share_link", "is_share_link"]

# 短链：weixin.qq.com/sph/<id>（容忍 from=singlemessage 等尾缀）
_SHORT_RE = re.compile(
    r"^https?://weixin\.qq\.com/sph/([A-Za-z0-9_-]{4,64})(?:[/?#&].*)?$", re.IGNORECASE
)
# 全链：channels.weixin.qq.com/finder-preview/pages/sph?id=<id>
_FULL_HOSTS = ("channels.weixin.qq.com", "channels.weixin.qq.com.")
_FULL_PATH_RE = re.compile(r"^/finder-preview/pages/sph/?$", re.IGNORECASE)


@dataclass(frozen=True)
class ChannelsShareLink:
    """解析后的视频号分享链接。"""

    share_id: str
    # 规范化后的全链（微信内打开时实际访问的地址）。
    full_url: str
    # 用户粘贴的原始链接。
    original: str

    @property
    def preview_path(self) -> str:
        """预览页路径（注入白名单按此前缀匹配）。"""
        return f"/finder-preview/pages/sph?id={self.share_id}"


def parse_share_link(url: str) -> Optional[ChannelsShareLink]:
    """解析视频号分享链接；不是视频号链接返回 None。

    容忍常见「脏」输入：前后空白、微信分享文案包裹（``<url>`` 之外还有
    文字时由调用方先抽取）、URL 编码。
    """
    text = (url or "").strip().strip("<>\"'")
    if not text:
        return None

    # 分享文案里混排文字时，抽取其中的 http(s) 链接。
    if " " in text or "\n" in text:
        match = re.search(r"https?://\S+", text)
        if match:
            text = match.group(0).rstrip("，。,)】")
        else:
            return None

    try:
        text = unquote(text)
    except Exception:  # noqa: BLE001 —— 编码异常按原样尝试
        pass

    short = _SHORT_RE.match(text)
    if short:
        share_id = short.group(1)
        return ChannelsShareLink(
            share_id=share_id,
            full_url=f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={share_id}",
            original=url,
        )

    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host in _FULL_HOSTS and _FULL_PATH_RE.match(parts.path or ""):
        query = parse_qs(parts.query or "")
        share_id = (query.get("id") or [""])[0]
        if share_id:
            return ChannelsShareLink(
                share_id=share_id,
                full_url=f"https://channels.weixin.qq.com{parts.path}?id={share_id}",
                original=url,
            )
    return None


def is_share_link(url: str) -> bool:
    return parse_share_link(url) is not None
