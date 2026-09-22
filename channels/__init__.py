"""微信视频号（Channels）下载支持。

接入方式与 bilibili / ytdlp 平台并列的第四条链路，但形态不同：视频号没有
免登录 Web API，登录态只存在于本机微信客户端的内嵌浏览器里，因此本平台是
**嗅探式**下载——启动本机 MITM 代理被动读取微信流量中的 feed 数据（含 MP4
直链与 ISAAC64 解密种子），捕获后自动或手动下载。

模块导航：

* :mod:`channels.url_parser` —— 视频号 URL 识别（直链不可下，引导嗅探模式）
* :mod:`channels.interceptor` —— mitmproxy 嗅探引擎 / 根证书 / 系统代理
* :mod:`channels.feed` —— feed 数据模型、响应提取、画质选择
* :mod:`channels.feed_store` —— 捕获存储与去重
* :mod:`channels.isaac64` —— 头部流密码解密（算法移植）
* :mod:`channels.downloader` —— 下载器（流式边下边解密）
* :mod:`channels.live` —— 直播 ffmpeg 录制

方案参考 ltaoo/wx_channels_download（MIT + Commons Clause，允许借鉴修改、
禁止出售；本项目为免费开源工具）与其解密算法来源 Hanson/WechatSphDecrypt
（MIT）。与其差异见各模块 docstring：不注入 / 不改写微信前端 JS，纯被动
嗅探，对微信改版零依赖。
"""

from channels.downloader import ChannelsDownloader
from channels.feed import ChannelFeed, extract_feeds, pick_quality_url
from channels.feed_store import FeedStore
from channels.interceptor import (
    CertificateManager,
    ChannelsInterceptor,
    SystemProxyManager,
    mitmproxy_available,
)
from channels.isaac64 import Isaac64Cipher
from channels.url_parser import CHANNELS_URL_HINT, ChannelsURLParser, is_channels_url

__all__ = [
    "CHANNELS_URL_HINT",
    "CertificateManager",
    "ChannelFeed",
    "ChannelsDownloader",
    "ChannelsInterceptor",
    "ChannelsURLParser",
    "FeedStore",
    "Isaac64Cipher",
    "SystemProxyManager",
    "extract_feeds",
    "is_channels_url",
    "mitmproxy_available",
    "pick_quality_url",
]
