"""视频号 URL 识别与引导。

视频号没有免登录的网页版可抓：``channels.weixin.qq.com/web/pages/feed/<id>``
这类链接在普通浏览器打开需要微信扫码登录，本项目也没有微信登录态，所以
**不支持直链下载**。识别出视频号链接的目的是在 CLI / Server 的分流处给出
明确引导（改用 ``--channels`` 嗅探模式），而不是掉进抖音兜底逻辑报
「不支持的链接」。
"""

from __future__ import annotations

from typing import Dict, Optional
from urllib.parse import urlsplit

__all__ = [
    "CHANNELS_URL_HINT",
    "ChannelsURLParser",
    "is_channels_url",
]

CHANNELS_HOSTS = ("channels.weixin.qq.com",)

CHANNELS_URL_HINT = (
    "微信视频号链接不支持直接下载（网页版需要微信登录态）。"
    '请使用嗅探模式：运行 `python run.py --channels`（或在网页控制台「视频号」页'
    "启动嗅探），然后在本机微信里打开视频号播放视频，工具会自动捕获并下载。"
)


def is_channels_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        return False
    return host.lower() in CHANNELS_HOSTS


class ChannelsURLParser:
    """与其他平台解析器同构的静态解析器。

    解析结果永远标记 ``type=sniff_required``——分流处据此展示
    :data:`CHANNELS_URL_HINT`，不会真正进入下载链路。
    """

    @staticmethod
    def parse(url: str) -> Optional[Dict[str, str]]:
        if not is_channels_url(url):
            return None
        return {"original_url": url, "type": "sniff_required", "platform": "channels"}
