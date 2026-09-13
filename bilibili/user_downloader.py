"""UP 主空间投稿批量下载（``space.bilibili.com/{mid}/video``）。

分页约定：``x/space/wbi/arc/search`` 按 ``order`` 倒序返回，默认 ``pubdate``。
在 ``pubdate`` 顺序下可以做时间下界提前收敛——一旦某页出现早于 ``start_time``
的稿件，后续页只会更早，无需继续翻页。这是大批量增量下载能把请求数从「全部
页数」压到「新稿件所在页数」的关键。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.downloader_base import DownloadResult
from utils.logger import setup_logger

from .api_client import BiliAPIError, BiliLoginRequiredError, BiliRiskControlError
from .downloader_base import BiliBaseDownloader

logger = setup_logger("BiliUserDownloader")

PAGE_SIZE = 30

# 分页上限。接口在 total 缺失时会一直回 ``has_more``，没有硬上限就会无限翻页。
MAX_PAGES = 200


class BiliUserDownloader(BiliBaseDownloader):
    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        result = DownloadResult()

        mid = str(parsed_url.get("mid") or "").strip()
        if not mid:
            logger.error("No mid found in parsed bilibili space URL")
            return result

        self._progress_update_step("拉取主页", f"UP 主 mid={mid}")
        card = await self.api_client.get_user_card(mid)
        author_name = (card or {}).get("name") or f"mid_{mid}"
        author_mid = (card or {}).get("mid") or mid
        self._progress_report_author(author_name, author_mid)

        items = await self._collect_items(mid)
        if not items:
            logger.warning("No bilibili uploads found for UP %s (%s)", author_name, mid)
            self._progress_set_item_total(0, "无待下载投稿")
            return result

        result.total = len(items)
        self._progress_set_item_total(len(items), f"{author_name} 投稿")
        self._progress_update_step("下载投稿", f"{len(items)} 条待处理")
        logger.info("UP %s: %d upload(s) selected for download", author_name, len(items))

        await self.run_batch(
            items,
            author_name=author_name,
            author_mid=author_mid,
            mode="post",
            url_type="user",
            result=result,
        )
        return result

    async def _collect_items(self, mid: str) -> List[Dict[str, Any]]:
        order = str(self.bili("user_order") or "pubdate")
        limit = self._number_limit("user")
        start_ts, end_ts = self._time_range_bounds()
        has_time_filter = start_ts is not None or end_ts is not None
        # 只有按发布时间倒序时，「看到旧稿件」才等价于「后面都更旧」。
        can_stop_early = order == "pubdate" and start_ts is not None

        collected: List[Dict[str, Any]] = []
        seen: set = set()
        page = 1
        while page <= MAX_PAGES:
            await self.rate_limiter.acquire()
            try:
                data = await self.api_client.get_user_videos(
                    mid, page=page, page_size=PAGE_SIZE, order=order
                )
            except (BiliLoginRequiredError, BiliRiskControlError):
                # 这两类是「需要用户动作」的错误（补 Cookie / 降频重试），必须
                # 原样冒泡给 CLI 给出可执行提示；吞掉只会变成「没有投稿」。
                raise
            except BiliAPIError as exc:
                logger.error("Failed to fetch UP %s uploads (page %d): %s", mid, page, exc)
                break

            batch = data.get("items") or []
            if not batch:
                break

            fresh = []
            for item in batch:
                bvid = str(item.get("bvid") or "")
                if bvid and bvid in seen:
                    continue
                if bvid:
                    seen.add(bvid)
                fresh.append(item)
            if not fresh:
                # 分页没有推进（接口在最后一页重复返回同一批），及时收手。
                logger.debug("UP %s pagination stalled at page %d", mid, page)
                break

            collected.extend(fresh)

            if limit > 0 and not has_time_filter and len(collected) >= limit:
                # 有时间筛选时不能按原始条数提前收敛：前面若干条可能全被时间
                # 窗口滤掉，收早了会少下。此时交给下游 _apply_limit 收口。
                break
            if can_stop_early and self._page_crosses_start(fresh, start_ts):
                break
            if not data.get("has_more"):
                break
            page += 1

        items = self._filter_by_time(collected)
        return self._apply_limit(items, "user")

    def _page_crosses_start(self, batch: List[Dict[str, Any]], start_ts: Optional[int]) -> bool:
        if not start_ts:
            return False
        for item in batch:
            published = self._published_at(item)
            if published is not None and published < start_ts:
                return True
        return False
