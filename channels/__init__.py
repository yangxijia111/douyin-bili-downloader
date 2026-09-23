"""微信视频号（Channels）下载支持。

接入方式与 bilibili / ytdlp 平台并列的第四条链路，但形态不同：视频号没有
免登录 Web API，登录态只存在于本机微信客户端的内嵌浏览器里，因此本平台是
**嗅探式**下载——v2.0.2 起以「页面注入」为主要捕获方案（向视频号页面注入
下载按钮，页面内 hook fetch/XHR 与 finder 运行时函数，经同源虚拟接口
``/__cuin/*`` 回传 Python 侧），被动响应嗅探作为兜底策略并行保留。

模块导航：

* :mod:`channels.url_parser` —— 视频号 URL 识别（直链不可下，引导嗅探模式）
* :mod:`channels.interceptor` —— mitmproxy 嗅探引擎 / 根证书 / 系统代理
* :mod:`channels.injector` —— HTML bootstrap 注入（CSP / 压缩 / 幂等）
* :mod:`channels.inject` —— 前端注入包（JS / CSS 资源与路径约定）
* :mod:`channels.virtual_host` —— ``/__cuin/*`` 同源虚拟接口（feed / task）
* :mod:`channels.pipeline` —— 四策略捕获流水线（A 被动 / B fetch / C 运行时 / D 补丁）
* :mod:`channels.patches` —— Strategy D 兼容补丁框架（默认无补丁）
* :mod:`channels.task_hub` —— 微信页面按钮的后端任务中心
* :mod:`channels.feed` —— feed 数据模型、响应提取、画质选择
* :mod:`channels.feed_store` —— 捕获存储与去重
* :mod:`channels.diagnostics` —— 链路诊断计数器与分级状态
* :mod:`channels.isaac64` —— 头部流密码解密（算法移植）
* :mod:`channels.downloader` —— 下载器（流式边下边解密）
* :mod:`channels.live` —— 直播 ffmpeg 录制

方案参考 ltaoo/wx_channels_download（MIT + Commons Clause，允许借鉴修改、
禁止出售；本项目为免费开源工具，前端注入为独立实现，不复制其源码）与其
解密算法来源 Hanson/WechatSphDecrypt（MIT）。
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
