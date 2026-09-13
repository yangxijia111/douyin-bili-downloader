"""收藏夹批量下载（``space.bilibili.com/{mid}/favlist?fid=N``）。

收藏夹与其它批量来源的本质差别：**夹内稿件的 UP 主各不相同**。因此这里不把
收藏夹名当作作者名（那会让 ``{author}`` 模板变量撒谎），而是每个稿件用自己
的真实 UP 主建目录，再把收藏夹名作为 ``collection_dir`` 插入一层：

``Downloaded/{UP主}/favlist/{收藏夹名}/{date}_{title}_{bvid}/``

收藏夹接口必须登录，未登录时 ``-101`` 会一路冒泡成
:class:`BiliLoginRequiredError`，由 CLI 给出可操作提示。
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.downloader_base import DownloadResult
from utils.logger import setup_logger

from .api_client import BiliAPIError, BiliLoginRequiredError, BiliRiskControlError
from .downloader_base import BiliBaseDownloader

logger = setup_logger("BiliFavDownloader")

PAGE_SIZE = 20
MAX_PAGES = 200

# 收藏夹资源的 type 字段：只处理视频。音频（12）与「视频合集」条目（21）
# 需要另一套展开逻辑，直接跳过并计数，避免静默漏下。
_FAV_TYPE_VIDEO = 2
_FAV_ATTR_INVALID = 1


class BiliFavDownloader(BiliBaseDownloader):
    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        result = DownloadResult()

        media_id = str(parsed_url.get("media_id") or "").strip()
        if not media_id:
            logger.error("No fid found in parsed bilibili favlist URL")
            return result

        self._progress_update_step("拉取收藏夹", f"fid={media_id}")
        items, info, skipped_kinds = await self._collect_items(media_id)
        if skipped_kinds:
            logger.info(
                "Skipped %d non-video favlist entr(ies) (audio / ugc season)",
                skipped_kinds,
            )
        if not items:
            logger.warning("No downloadable videos found in bilibili favlist %s", media_id)
            self._progress_set_item_total(0, "无待下载稿件")
            return result

        fav_name = str(info.get("title") or "").strip() or f"favlist_{media_id}"
        result.total = len(items)
        self._progress_set_item_total(len(items), fav_name)
        self._progress_update_step("下载收藏夹", f"{fav_name} · {len(items)} 条")
        logger.info("Favlist %s (%s): %d video(s) selected", fav_name, media_id, len(items))

        await self.run_batch(
            items,
            author_name=fav_name,
            author_mid=None,
            mode="favlist",
            url_type="favlist",
            result=result,
            collection_dir=fav_name,
        )
        return result

    async def _collect_items(self, media_id: str) -> tuple:
        limit = self._number_limit("favlist")
        start_ts, end_ts = self._time_range_bounds()
        has_time_filter = start_ts is not None or end_ts is not None

        collected: List[Dict[str, Any]] = []
        seen: set = set()
        info: Dict[str, Any] = {}
        skipped_kinds = 0
        empty_streak = 0
        page = 1
        while page <= MAX_PAGES:
            await self.rate_limiter.acquire()
            try:
                data = await self.api_client.get_fav_resources(
                    media_id, page=page, page_size=PAGE_SIZE
                )
            except (BiliLoginRequiredError, BiliRiskControlError):
                # 收藏夹接口未登录必然 -101，必须冒泡给 CLI 提示补 SESSDATA。
                raise
            except BiliAPIError as exc:
                logger.error("Failed to fetch favlist %s page %d: %s", media_id, page, exc)
                break

            batch = data.get("items") or []
            if not info and isinstance(data.get("info"), dict):
                info = data["info"]
            if not batch:
                break

            fresh = []
            for item in batch:
                if _is_invalid(item):
                    continue
                if int(item.get("type") or 0) != _FAV_TYPE_VIDEO:
                    skipped_kinds += 1
                    continue
                bvid = str(item.get("bvid") or "")
                if not bvid:
                    continue
                if bvid in seen:
                    continue
                seen.add(bvid)
                # 收藏夹用 ``pubtime`` 作为稿件发布时间，用 ``pubdate`` 统一字段名。
                fresh.append({"bvid": bvid, "aid": item.get("id"), "pubdate": item.get("pubtime")})

            collected.extend(fresh)
            if limit > 0 and not has_time_filter and len(collected) >= limit:
                break

            # 本页可能整页都是失效 / 音频 / 合集条目，此时 fresh 为空但 has_more
            # 仍为真——继续翻页，但连续多页都没有可用条目说明接口在空转，收手。
            empty_streak = empty_streak + 1 if not fresh else 0
            if empty_streak >= 3:
                logger.debug("Favlist %s produced no usable entry for 3 pages, stop", media_id)
                break
            if not data.get("has_more"):
                break
            page += 1

        items = self._filter_by_time(collected)
        return self._apply_limit(items, "favlist"), info, skipped_kinds


def _is_invalid(item: Dict[str, Any]) -> bool:
    """``attr`` 最低位为 1 表示稿件已失效（被删 / 仅自己可见）。"""
    try:
        return bool(int(item.get("attr") or 0) & _FAV_ATTR_INVALID)
    except (TypeError, ValueError):
        return False
