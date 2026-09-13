"""合集 / 系列批量下载。

两者在 URL 上都表现为 ``?sid=N``，但归属两套完全不同的接口（``season_id``
vs ``series_id``），而且 id 空间独立、无法从数值本身判断类型。因此以链接类型
为第一猜测，再用「该接口是否返回内容」做一次自动纠偏——用户从地址栏复制
``collectiondetail`` 与 ``seriesdetail`` 时经常混淆两者。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.downloader_base import DownloadResult
from utils.logger import setup_logger

from .api_client import BiliAPIError, BiliLoginRequiredError, BiliRiskControlError
from .downloader_base import BiliBaseDownloader

logger = setup_logger("BiliCollectionDownloader")

PAGE_SIZE = 30
MAX_PAGES = 200

# 链接类型 -> 增量配置键。合集与系列虽然有各自接口，但在「是否增量下载」这类
# 策略上用户是按业务类型区分的，所以保持分开配置。
URL_TYPE_BY_KIND = {"collection": "collection", "series": "series"}


class BiliCollectionDownloader(BiliBaseDownloader):
    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        result = DownloadResult()

        declared = str(parsed_url.get("type") or "collection")
        raw_sid = (
            parsed_url.get("season_id") if declared == "collection" else parsed_url.get("series_id")
        )
        sid = str(raw_sid or "").strip()
        mid = str(parsed_url.get("mid") or "").strip()
        if not sid:
            logger.error("No sid found in parsed bilibili collection/series URL")
            return result
        if not mid:
            logger.error("No mid found in parsed bilibili collection/series URL")
            return result

        self._progress_update_step("解析合集", f"sid={sid}")
        kind, first_page = await self._resolve_kind(mid, sid, declared)
        url_type = URL_TYPE_BY_KIND.get(kind, "collection")

        collected, meta = await self._collect_items(mid, sid, kind, first_page)
        if not collected:
            logger.warning("No videos found in %s sid=%s (mid=%s)", kind, sid, mid)
            self._progress_set_item_total(0, "无待下载稿件")
            return result

        card = await self.api_client.get_user_card(mid)
        author_name = (card or {}).get("name") or f"mid_{mid}"
        author_mid = (card or {}).get("mid") or mid
        self._progress_report_author(author_name, author_mid)

        collection_name = str(meta.get("name") or "").strip() or f"{kind}_{sid}"
        result.total = len(collected)
        self._progress_set_item_total(len(collected), collection_name)
        self._progress_update_step("下载合集", f"{collection_name} · {len(collected)} 条")
        logger.info(
            "%s %s (%s): %d video(s) selected",
            kind,
            collection_name,
            sid,
            len(collected),
        )

        await self.run_batch(
            collected,
            author_name=author_name,
            author_mid=author_mid,
            mode=kind,
            url_type=url_type,
            result=result,
            collection_dir=collection_name,
        )
        return result

    # ------------------------------------------------------------------

    async def _resolve_kind(self, mid: str, sid: str, declared: str) -> tuple:
        """确认 sid 属于合集还是系列，并顺带返回第 1 页（避免重复请求）。"""
        candidates = [declared] + [kind for kind in ("collection", "series") if kind != declared]
        first_page: Dict[str, Any] = {"items": [], "total": 0, "has_more": False, "meta": {}}
        for kind in candidates:
            page = await self._fetch_page(mid, sid, kind, 1)
            if page["items"] or page["total"]:
                if kind != declared:
                    logger.info(
                        "sid=%s is actually a %s (link said %s); using %s",
                        sid,
                        kind,
                        declared,
                        kind,
                    )
                return kind, page
            first_page = page
        return declared, first_page

    async def _fetch_page(
        self, mid: str, sid: str, kind: str, page: int
    ) -> Dict[str, Any]:
        await self.rate_limiter.acquire()
        try:
            if kind == "series":
                return await self.api_client.get_series_archives(
                    mid, sid, page=page, page_size=PAGE_SIZE
                )
            return await self.api_client.get_season_archives(
                mid, sid, page=page, page_size=PAGE_SIZE
            )
        except (BiliLoginRequiredError, BiliRiskControlError):
            # 两者都是 BiliAPIError 的子类，必须先于基类拦截并原样冒泡，
            # 否则「未登录 / 被风控」会被吞成「合集没有内容」。
            raise
        except BiliAPIError as exc:
            logger.debug("Fetch %s sid=%s page=%d failed: %s", kind, sid, page, exc)
            return {"items": [], "total": 0, "has_more": False, "meta": {}}

    async def _collect_items(
        self, mid: str, sid: str, kind: str, first_page: Optional[Dict[str, Any]] = None
    ) -> tuple:
        url_type = URL_TYPE_BY_KIND.get(kind, "collection")
        limit = self._number_limit(url_type)
        start_ts, end_ts = self._time_range_bounds()
        has_time_filter = start_ts is not None or end_ts is not None

        collected: List[Dict[str, Any]] = []
        seen: set = set()
        meta: Dict[str, Any] = {}
        page = 1
        while page <= MAX_PAGES:
            data = first_page if page == 1 and first_page else await self._fetch_page(
                mid, sid, kind, page
            )
            batch = data.get("items") or []
            if batch and not meta:
                meta = data.get("meta") or {}
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
                logger.debug("Pagination stalled for %s sid=%s at page %d", kind, sid, page)
                break

            collected.extend(fresh)
            if limit > 0 and not has_time_filter and len(collected) >= limit:
                break
            if not data.get("has_more"):
                break
            page += 1

        items = self._filter_by_time(collected)
        return self._apply_limit(items, url_type), meta
