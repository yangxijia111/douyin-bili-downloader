"""通用视频平台下载（yt-dlp 引擎）。

与 ``core/``（抖音）、``bilibili/`` 平级：只复用基础设施层（``storage`` /
``control`` / ``utils.naming`` / 进度上报），解析与下载整体委托给 yt-dlp。
平台路由在 ``cli.main.download_url`` / ``server.app._execute_download`` 里完成，
判定函数见 :mod:`ytdlp.url_parser`。
"""

from .downloader import (
    YtdlpDownloader,
    YtdlpDownloadError,
    YtdlpMissingError,
    build_video_token,
    classify_download_error,
    format_selector,
    import_ytdlp,
    safe_video_id,
    video_tokens_in_filename,
    write_netscape_cookies,
)
from .url_parser import (
    PLATFORM_BY_KEY,
    SUPPORTED_PLATFORMS,
    PlatformSpec,
    YtdlpURLParser,
    detect_ytdlp_platform,
    is_ytdlp_url,
    normalize_url,
    platform_display_name,
)

__all__ = [
    "PLATFORM_BY_KEY",
    "SUPPORTED_PLATFORMS",
    "PlatformSpec",
    "YtdlpDownloadError",
    "YtdlpDownloader",
    "YtdlpMissingError",
    "YtdlpURLParser",
    "build_video_token",
    "classify_download_error",
    "detect_ytdlp_platform",
    "format_selector",
    "import_ytdlp",
    "is_ytdlp_url",
    "normalize_url",
    "platform_display_name",
    "safe_video_id",
    "video_tokens_in_filename",
    "write_netscape_cookies",
]
