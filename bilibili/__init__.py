"""哔哩哔哩（Bilibili）批量下载子包。

与 ``core/``（抖音）平级：抖音的域模型是 aweme / sec_uid / msToken 签名，B 站的
域模型是 bvid·aid·cid / mid / WBI 签名 / DASH 分离音视频，两者没有可复用的业务
语义。这里只复用基础设施层（``storage.FileManager`` 的流式下载与吞吐保护、
``control`` 的限速与重试、``cli.ProgressDisplay`` 的进度上报、``utils.naming``
的命名模板），业务逻辑全部独立实现。

支持的链接类型：单稿件（含分 P）、UP 主空间投稿、合集 / 系列、收藏夹。
"""

from .api_client import (
    BiliAPIClient,
    BiliAPIError,
    BiliLoginRequiredError,
    BiliRiskControlError,
)
from .collection_downloader import BiliCollectionDownloader
from .downloader_base import BiliBaseDownloader
from .factory import UNSUPPORTED_URL_TYPE_DETAIL, BiliDownloaderFactory
from .fav_downloader import BiliFavDownloader
from .url_parser import BiliURLParser, detect_platform, is_bilibili_url
from .user_downloader import BiliUserDownloader
from .video_downloader import BiliVideoDownloader

__all__ = [
    "BiliAPIClient",
    "BiliAPIError",
    "BiliBaseDownloader",
    "BiliCollectionDownloader",
    "BiliDownloaderFactory",
    "BiliFavDownloader",
    "BiliLoginRequiredError",
    "BiliRiskControlError",
    "BiliURLParser",
    "BiliUserDownloader",
    "BiliVideoDownloader",
    "UNSUPPORTED_URL_TYPE_DETAIL",
    "detect_platform",
    "is_bilibili_url",
]
