"""B 站下载器工厂。

与抖音侧 ``core.downloader_factory.py`` 同构（URL 类型 -> 下载器实例），但
构造参数不同：B 站的凭据从 config 注入 API 客户端，不需要 CookieManager。
"""

from __future__ import annotations

from typing import Any, Optional

from utils.logger import setup_logger

from .api_client import BiliAPIClient
from .collection_downloader import BiliCollectionDownloader
from .downloader_base import BiliBaseDownloader
from .fav_downloader import BiliFavDownloader
from .user_downloader import BiliUserDownloader
from .video_downloader import BiliVideoDownloader

logger = setup_logger("BiliDownloaderFactory")

# 这些类型解析得出来但当前没有下载器，给用户一句可执行的解释，而不是
# 「Unsupported URL type」这种无从下手的报错。
UNSUPPORTED_URL_TYPE_DETAIL = {
    "short": "B 站短链需要先展开，请检查网络或改用完整链接",
    "bangumi": "番剧 / 影视 / 付费课程受版权保护且需要大会员流媒体鉴权，本工具不支持下载",
}


class BiliDownloaderFactory:
    @staticmethod
    def create(
        url_type: str,
        *,
        config: Any,
        api_client: BiliAPIClient,
        file_manager: Any,
        cookie_manager: Optional[Any] = None,
        database: Optional[Any] = None,
        rate_limiter: Optional[Any] = None,
        retry_handler: Optional[Any] = None,
        queue_manager: Optional[Any] = None,
        progress_reporter: Optional[Any] = None,
        job_id: Optional[str] = None,
    ) -> Optional[BiliBaseDownloader]:
        common: dict = {
            "config": config,
            "api_client": api_client,
            "file_manager": file_manager,
            "cookie_manager": cookie_manager,
            "database": database,
            "rate_limiter": rate_limiter,
            "retry_handler": retry_handler,
            "queue_manager": queue_manager,
            "progress_reporter": progress_reporter,
            "job_id": job_id,
        }

        if url_type == "video":
            return BiliVideoDownloader(**common)
        if url_type == "user":
            return BiliUserDownloader(**common)
        if url_type in ("collection", "series"):
            return BiliCollectionDownloader(**common)
        if url_type == "favlist":
            return BiliFavDownloader(**common)

        detail = UNSUPPORTED_URL_TYPE_DETAIL.get(url_type)
        if detail:
            logger.error("Capability-gated bilibili URL type: %s (%s)", url_type, detail)
        else:
            logger.error("Unsupported bilibili URL type: %s", url_type)
        return None
