"""单稿件下载器（``/video/BV...``，含分 P）。

用户显式写了 ``?p=N`` 时只下第 N P 并强制跳过整条稿件的增量判定——否则
「第 1 P 已经在盘上」会让整条被跳过，用户要的那一 P 永远下不到。
"""

from __future__ import annotations

from typing import Any, Dict

from core.downloader_base import DownloadResult
from utils.logger import setup_logger

from .downloader_base import BiliBaseDownloader

logger = setup_logger("BiliVideoDownloader")


class BiliVideoDownloader(BiliBaseDownloader):
    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        result = DownloadResult()

        bvid = str(parsed_url.get("bvid") or "").strip()
        aid = parsed_url.get("aid")
        if not bvid and not aid:
            logger.error("No bvid/aid found in parsed bilibili URL")
            return result

        page_filter = parsed_url.get("page")
        result.total = 1
        self._progress_set_item_total(1, "单视频下载")
        self._progress_update_step("下载视频", "拉取稿件详情")

        # 已知 bvid 且没有指定分 P 时，先做一次便宜的增量判定，命中就完全不
        # 请求接口。指定分 P 的情形交给页级判定处理。
        if bvid and not page_filter:
            if not await self._should_download(bvid, url_type="video"):
                result.skipped = 1
                self._progress_advance_item("skipped", bvid)
                return result

        status = await self.download_video_item(
            {"bvid": bvid or None, "aid": aid},
            author_name="unknown",
            author_mid=None,
            mode="video",
            url_type="video",
            page_filter=page_filter,
            force=bool(page_filter),
        )
        if status == "success":
            result.success += 1
        elif status == "skipped":
            result.skipped += 1
        else:
            result.failed += 1
        self._progress_advance_item(status, bvid or str(aid or ""))
        return result
