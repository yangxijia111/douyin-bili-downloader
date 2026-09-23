"""视频号页面注入包（前端资源与路径约定）。

目录结构（v2.0.2 页面注入方案）::

    channels/inject/assets/
        cuin_core.js       纯逻辑核心（Node 可单测，无 DOM 依赖）
        bootstrap.js       浏览器胶水层（hooks / 心跳 / 按钮 / 模块加载）
        channels_home.js   首页推荐流页面模块
        channels_feed.js   视频详情页 / 作者主页页面模块
        channels_live.js   直播页页面模块
        channels.css       注入 UI 样式（视觉融入微信）

注入与虚拟资源服务见 :mod:`channels.injector` 与
:mod:`channels.virtual_host`；前端与后端的边界约定：

* 前端只负责：当前播放内容识别、Feed 捕获（fetch/XHR/运行时 hook）、
  用户交互、下载按钮；
* 后端负责：任务、下载、ISAAC64 解密、落盘、数据库——前端不重新实现
  下载器（参考 ltaoo/wx_channels_download 的行为分工，但为独立实现）。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["ASSETS_DIR", "asset_path", "ASSET_FILES"]

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# 注入 HTML 引用的三个资源 + 页面模块（顺序即加载顺序）。
ASSET_FILES = (
    "channels.css",
    "cuin_core.js",
    "bootstrap.js",
    "channels_home.js",
    "channels_feed.js",
    "channels_live.js",
)


def asset_path(name: str) -> Path:
    """注入资源文件的绝对路径（虚拟端点按名读取）。"""
    return ASSETS_DIR / name
