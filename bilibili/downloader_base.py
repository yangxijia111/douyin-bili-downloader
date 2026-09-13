"""B 站下载器基类。

复用抖音侧的基础设施，但业务语义完全独立：

* **文件落盘与流式下载** —— ``storage.FileManager``（含 256KB 分块、吞吐地板
  判定、原子改名、403 时 httpx 兜底）。
* **限速 / 重试 / 并发** —— ``control`` 三个组件。
* **命名模板** —— ``utils.naming`` 的 ``{date}_{title}_{id}`` 体系，模板变量与
  抖音完全一致（``{id}`` 在 B 站侧是 bvid，分 P 时是 ``{bvid}_p{n}``）。
* **增量判定** —— 抖音按 aweme_id 扫磁盘；这里按 bvid 扫，且额外支持分 P
  粒度：多 P 稿件下到一半时重跑只补缺失的 P。

不共用的是下载动作本身：抖音是 ``play_addr`` 单文件，B 站是 DASH 双流需要
ffmpeg 合并，且 CDN 校验 Referer。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import ConfigLoader
from control import QueueManager, RateLimiter, RetryHandler
from core.downloader_base import DownloadResult
from storage import Database, FileManager
from utils.logger import setup_logger
from utils.naming import (
    DEFAULT_FILE_TEMPLATE,
    DEFAULT_FOLDER_TEMPLATE,
    build_aweme_context,
    render_template,
)

from .api_client import BiliAPIClient, BiliLoginRequiredError
from .merger import FFmpegMissingError, cleanup_temp_files, merge_dash_streams
from .security import is_safe_url
from .streams import (
    describe_audio_stream,
    describe_video_stream,
    resolve_durl_qn,
    select_audio_stream,
    select_video_stream,
    stream_urls,
)

logger = setup_logger("BiliBaseDownloader")

# 单条稿件（含全部分 P）的兜底总时限。分 P 多的稿件会有 N 次「视频+音频」下载
# 加一次合并，没有总时限时一条异常稿件就能把整个队列拖死。
_ITEM_DEADLINE_SECONDS = 1800

# 磁盘增量扫描时认作「主媒体」的后缀。``.m4s`` 刻意不在其中：合并前的临时
# 分片会留在目录里（进程被杀时），把它算成已下载会让该作品永远补不下。
_LOCAL_MEDIA_SUFFIXES = {
    ".mp4",
    ".flv",
    ".mkv",
    ".m4a",
    ".mp3",
    ".flac",
}

# 侧车文件（封面/字幕/弹幕/JSON）带 bvid 但不代表主媒体存在。
_SIDECAR_SUFFIX_HINTS = (
    "_cover",
    "_avatar",
    "_music",
    "_data",
    "_comments",
    "_danmaku",
    "_subtitle",
)

# 文件名里的作品标识：``BV1xx411c7mD`` 或分 P 的 ``BV1xx411c7mD_p12``。
_VIDEO_TOKEN_RE = re.compile(r"(BV[0-9A-Za-z]{10})(?:_p(\d+))?")

# 侧车/临时文件后缀，永不参与命名去重与增量判定。
_TEMP_SUFFIXES = (".m4s", ".part", ".tmp")


class BiliBaseDownloader(ABC):
    def __init__(
        self,
        config: ConfigLoader,
        api_client: BiliAPIClient,
        file_manager: FileManager,
        cookie_manager: Optional[Any] = None,
        database: Optional[Database] = None,
        rate_limiter: Optional[RateLimiter] = None,
        retry_handler: Optional[RetryHandler] = None,
        queue_manager: Optional[QueueManager] = None,
        progress_reporter: Optional[Any] = None,
        job_id: Optional[str] = None,
    ):
        self.config = config
        self.api_client = api_client
        self.file_manager = file_manager
        # 抖音侧的 CookieManager 在这里用不上（B 站凭据走 config.bilibili.cookies
        # 并已注入 api_client），保留参数只为两侧工厂签名一致。
        self.cookie_manager = cookie_manager
        self.database = database
        self.rate_limiter = rate_limiter or RateLimiter()
        self.retry_handler = retry_handler or RetryHandler()
        thread_count = int(self.config.get("thread", 5) or 5)
        self.queue_manager = queue_manager or QueueManager(max_workers=thread_count)
        self.progress_reporter = progress_reporter
        self.job_id = job_id

        self._local_video_ids: Optional[set] = None
        self._local_index_lock: Optional[asyncio.Lock] = None
        self._download_error_log_count = 0
        self._download_error_log_limit = 5

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def bili(self, key: str, default: Any = None) -> Any:
        section = self.config.get("bilibili")
        if isinstance(section, dict) and key in section:
            return section[key]
        return default

    def _number_limit(self, url_type: str) -> int:
        numbers = self.bili("number", {}) or {}
        try:
            return int(numbers.get(url_type, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _increase_enabled(self, url_type: str) -> bool:
        increase = self.bili("increase", {}) or {}
        # 缺键按「未启用」处理：B 站增量只对配置里显式声明的类型生效，
        # 传入抖音侧的类型（如 post）时不做 B 站增量判定，直接下载。
        value = increase.get(url_type, False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def ffmpeg_path(self) -> str:
        return str(self.bili("ffmpeg_path") or "").strip()

    # ------------------------------------------------------------------
    # 进度上报（与抖音侧同名，便于宿主复用同一套 reporter）
    # ------------------------------------------------------------------

    def _progress_update_step(self, step: str, detail: str = "") -> None:
        if not self.progress_reporter:
            return
        try:
            self.progress_reporter.update_step(step, detail)
        except Exception as exc:
            logger.debug("Progress update_step failed: %s", exc)

    def _progress_set_item_total(self, total: int, detail: str = "") -> None:
        if not self.progress_reporter:
            return
        try:
            self.progress_reporter.set_item_total(total, detail)
        except Exception as exc:
            logger.debug("Progress set_item_total failed: %s", exc)

    def _progress_advance_item(self, status: str, detail: str = "") -> None:
        if not self.progress_reporter:
            return
        try:
            self.progress_reporter.advance_item(status, detail)
        except Exception as exc:
            logger.debug("Progress advance_item failed: %s", exc)

    def _progress_report_author(self, name: Optional[str], mid: Optional[str]) -> None:
        if not self.progress_reporter:
            return
        try:
            hook = getattr(self.progress_reporter, "on_author", None)
            if callable(hook):
                hook(nickname=name, sec_uid=mid)
        except Exception as exc:  # pragma: no cover — 防御
            logger.debug("Progress on_author failed: %s", exc)

    def _make_item_progress(self, item_id: Optional[str]):
        """构造单文件下载途中的字节进度回调（没有 reporter / id 时返回 None）。

        与 core 侧同名方法对齐：多 P 教程这类长任务若只在整条稿件处理完后
        才 advance_item，网页任务卡片在 0% 上静默几分钟到几十分钟，用户会
        判定为卡死。字节进度让 job.current 实时反映当前传输。
        """
        reporter = self.progress_reporter
        if not reporter or not item_id:
            return None
        emit = getattr(reporter, "on_item_progress", None)
        if not callable(emit):
            return None

        def _on_progress(bytes_read: int, bytes_total: int) -> None:
            try:
                emit(aweme_id=str(item_id), bytes_read=bytes_read, bytes_total=bytes_total)
            except Exception as exc:
                logger.debug("Progress on_item_progress failed: %s", exc)

        return _on_progress

    def _log_download_error(self, log_fn, message: str) -> None:
        if self._download_error_log_count < self._download_error_log_limit:
            log_fn(message)
        elif self._download_error_log_count == self._download_error_log_limit:
            logger.error("Too many download errors, suppressing further per-file logs...")
        self._download_error_log_count += 1

    @abstractmethod
    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        """执行一次批量下载，返回统计结果。"""

    # ------------------------------------------------------------------
    # 时间范围与数量筛选
    # ------------------------------------------------------------------

    def _time_range_bounds(self) -> Tuple[Optional[int], Optional[int]]:
        start_time = self.config.get("start_time")
        end_time = self.config.get("end_time")
        start_ts = (
            int(datetime.strptime(start_time, "%Y-%m-%d").timestamp()) if start_time else None
        )
        end_ts = None
        if end_time:
            end_date = datetime.strptime(end_time, "%Y-%m-%d") + timedelta(days=1)
            end_ts = int(end_date.timestamp())
        return start_ts, end_ts

    def _filter_by_time(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按稿件发布时间过滤。B 站字段是 ``pubdate``（合集/收藏夹）或 ``created``
        （空间投稿），统一在此归一。"""
        start_ts, end_ts = self._time_range_bounds()
        if start_ts is None and end_ts is None:
            return items

        filtered: List[Dict[str, Any]] = []
        for item in items:
            published = _item_pubdate(item)
            if published is None:
                # 时间未知时不丢：宁可多下一次，也不要静默漏掉用户要的稿件。
                filtered.append(item)
                continue
            if start_ts is not None and published < start_ts:
                continue
            if end_ts is not None and published >= end_ts:
                continue
            filtered.append(item)
        return filtered

    def _apply_limit(self, items: List[Dict[str, Any]], url_type: str) -> List[Dict[str, Any]]:
        limit = self._number_limit(url_type)
        if limit > 0:
            return items[:limit]
        return items

    @staticmethod
    def _published_at(item: Dict[str, Any]) -> Optional[int]:
        """归一化各接口的发布时间字段为 unix 秒。"""
        return _item_pubdate(item)

    async def run_batch(
        self,
        items: List[Dict[str, Any]],
        *,
        author_name: str,
        author_mid: Optional[str],
        mode: str,
        url_type: str,
        result: DownloadResult,
        collection_dir: Optional[str] = None,
    ) -> None:
        """顺序下载一批稿件并累加统计。

        串行而非并发：B 站接口对同一账号的并发请求有风控阈值，而真正的瓶颈是
        CDN 带宽（单条稿件本身就是「视频轨 + 音频轨」两次大文件传输），并发
        只会把风控触发概率放大而不提升总吞吐。
        """
        for item in items:
            bvid = str(item.get("bvid") or "")
            status = await self.download_video_item(
                item,
                author_name=author_name,
                author_mid=author_mid,
                mode=mode,
                url_type=url_type,
                collection_dir=collection_dir,
            )
            if status == "success":
                result.success += 1
            elif status == "skipped":
                result.skipped += 1
            else:
                result.failed += 1
            self._progress_advance_item(status, bvid or str(item.get("aid") or ""))

    # ------------------------------------------------------------------
    # 磁盘增量索引
    # ------------------------------------------------------------------

    async def _ensure_local_index(self) -> None:
        if self._local_video_ids is not None:
            return
        if self._local_index_lock is None:
            self._local_index_lock = asyncio.Lock()
        async with self._local_index_lock:
            if self._local_video_ids is not None:
                return
            await asyncio.to_thread(self._build_local_index)

    def _build_local_index(self) -> None:
        base_path = self.file_manager.base_path
        ids: set = set()
        if base_path.exists():
            for path in base_path.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() in _TEMP_SUFFIXES:
                    continue
                if path.suffix.lower() not in _LOCAL_MEDIA_SUFFIXES:
                    continue
                if path.stem.lower().endswith(_SIDECAR_SUFFIX_HINTS):
                    continue
                try:
                    if path.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                ids.update(_video_tokens(path.name))
        self._local_video_ids = ids

    def _is_locally_downloaded(self, token: str) -> bool:
        if not token:
            return False
        if self._local_video_ids is None:
            self._build_local_index()
        return bool(self._local_video_ids) and token in (self._local_video_ids or set())

    def _mark_local_downloaded(self, token: str) -> None:
        if not token:
            return
        if self._local_video_ids is None:
            self._build_local_index()
        if self._local_video_ids is None:  # pragma: no cover — 防御
            self._local_video_ids = set()
        self._local_video_ids.add(token)

    async def _should_download(self, bvid: str, *, url_type: str, force: bool = False) -> bool:
        """整条稿件的**廉价**增量判定。

        只认「单 P 稿件已落盘」这一种无歧义的完成态。看到 ``{bvid}_p1`` 时
        **不**判定为完成：分 P 文件名只产出 ``bvid_pN`` 而不产出裸 ``bvid``（见
        :func:`_video_tokens`），所以要继续走详情请求、由页级判定
        :meth:`_should_download_page` 逐 P 补齐。代价是增量批次里每条多 P 稿件
        多一次详情请求（无媒体流量），换的是「下到一半的多 P 稿件不会被永久
        判成已完成」。
        """
        if force or not self._increase_enabled(url_type):
            return True

        await self._ensure_local_index()
        if self._is_locally_downloaded(bvid):
            logger.info("Bilibili video %s already exists locally, skipping", bvid)
            return False

        if self.database is None or bool(self.config.get("redownload_missing_files", True)):
            return True
        try:
            if await self.database.is_downloaded(bvid):
                logger.info("Bilibili video %s in history but file missing; skipping", bvid)
                return False
        except Exception as exc:
            logger.warning("Download history lookup failed for %s, downloading: %s", bvid, exc)
        return True

    async def _should_download_page(self, page_token: str) -> bool:
        """分 P 粒度增量判定：多 P 稿件补下缺失的 P。"""
        await self._ensure_local_index()
        return not self._is_locally_downloaded(page_token)

    # ------------------------------------------------------------------
    # 命名与目录
    # ------------------------------------------------------------------

    def _build_item_context(
        self,
        *,
        item_id: str,
        title: str,
        author_name: str,
        author_mid: Optional[str],
        pubdate: Any,
        media_type: str,
        mode: str,
        collection_dir: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        publish_ts, publish_date = _resolve_publish_time(pubdate)
        if not publish_date:
            publish_date = datetime.now().strftime("%Y-%m-%d")
            logger.warning(
                "Bilibili item %s missing/invalid pubdate, fallback to current date %s",
                item_id,
                publish_date,
            )

        context = build_aweme_context(
            aweme_id=str(item_id),
            title=title or "no_title",
            author_name=author_name,
            author_sec_uid=author_mid,
            publish_date=publish_date,
            publish_ts=publish_ts,
            media_type=media_type,
            mode=mode,
        )
        fallback = f"{publish_date}_{item_id}"
        file_stem = render_template(
            self.config.get("filename_template") or DEFAULT_FILE_TEMPLATE,
            context,
            fallback=fallback,
        )
        folder_name = render_template(
            self.config.get("folder_template") or DEFAULT_FOLDER_TEMPLATE,
            context,
            fallback=fallback,
        )

        author_dir_style = self.config.get("author_dir") or "nickname"
        save_dir = self.file_manager.get_save_path(
            author_name=author_name,
            mode=mode,
            aweme_title=title,
            aweme_id=str(item_id),
            folderstyle=bool(self.config.get("folderstyle", True)),
            download_date=publish_date,
            folder_name=folder_name,
            author_sec_uid=author_mid,
            author_dir_style=author_dir_style,
            group_by_mode=bool(self.config.get("group_by_mode", True)),
            collection_dir=collection_dir,
        )
        return {
            "item_id": str(item_id),
            "title": title or "no_title",
            "publish_ts": publish_ts,
            "publish_date": publish_date,
            "file_stem": file_stem,
            "save_dir": save_dir,
        }

    # ------------------------------------------------------------------
    # 媒体下载原语
    # ------------------------------------------------------------------

    async def _download_mirrors(
        self,
        urls: List[str],
        dest: Path,
        session,
        *,
        referer: Optional[str] = None,
        on_progress=None,
    ) -> bool:
        """按候选地址依次尝试下载，全部失败整轮退避重试。

        B 站 DASH 每条轨给 ``baseUrl`` + 若干 ``backupUrl``；单条失败通常只是
        该 CDN 节点的问题，换镜像即可，所以镜像轮扫优先于同址重试。
        """
        if not urls:
            return False

        async def _attempt_round() -> bool:
            for url in urls:
                # 候选地址来自接口响应，是不可信数据：坏候选只跳过本轮该地址，
                # 不中断轮次（轮内其余镜像仍可用）。
                if not is_safe_url(url):
                    self._log_download_error(
                        logger.warning,
                        f"Blocked unsafe stream url candidate for {dest.name}: {url}",
                    )
                    continue
                headers = self.api_client.download_headers(url, referer=referer)
                try:
                    saved = await self.file_manager.download_file(
                        url,
                        dest,
                        session,
                        headers=headers,
                        proxy=self.api_client.proxy or None,
                        on_progress=on_progress,
                    )
                except Exception as exc:  # pragma: no cover — FileManager 内部已吞异常
                    self._log_download_error(
                        logger.warning, f"Stream download error for {dest.name}: {exc}"
                    )
                    saved = False
                if saved:
                    return True
            raise RuntimeError(
                f"All {len(urls)} stream url candidate(s) failed for {dest.name}"
            )

        try:
            return bool(await self.retry_handler.execute_with_retry(_attempt_round))
        except Exception as exc:
            self._log_download_error(
                logger.error, f"Stream download failed for {dest.name}: {exc}"
            )
            return False

    async def _download_plain_url(
        self, url: str, dest: Path, session, *, referer: Optional[str] = None
    ) -> bool:
        return await self._download_mirrors([url], dest, session, referer=referer)

    async def _finalize_media(
        self,
        video_tmp: Optional[Path],
        audio_tmp: Optional[Path],
        target: Path,
    ) -> bool:
        """合并/封装成最终文件，并清理临时分片。

        ``video_tmp`` 为空表示这是一次纯音频下载（``audio_only``），此时用
        音频轨作为唯一输入。

        只有单条轨且系统里没有 ffmpeg 时降级为直接改名——这至少保证文件可播，
        比整条作品失败更符合预期；双轨缺 ffmpeg 则必须失败，静默产出没有声音
        的「成功」视频是更糟的结果。
        """
        primary = video_tmp or audio_tmp
        if primary is None:
            return False
        secondary = audio_tmp if video_tmp is not None else None
        tmps = [path for path in (video_tmp, audio_tmp) if path is not None]

        try:
            ok = await merge_dash_streams(
                primary, secondary, target, ffmpeg_path=self.ffmpeg_path
            )
        except FFmpegMissingError as exc:
            if secondary is None:
                logger.warning(
                    "ffmpeg unavailable (%s); keeping raw stream as %s", exc, target.name
                )
                os.replace(str(primary), str(target))
                return True
            logger.error("ffmpeg is required to merge DASH streams: %s", exc)
            return False
        finally:
            cleanup_temp_files([path for path in tmps if path.exists()])
        return ok

    # ------------------------------------------------------------------
    # 侧车资产
    # ------------------------------------------------------------------

    async def _save_cover(
        self, cover_url: str, save_dir: Path, file_stem: str, session, referer: str
    ) -> Optional[Path]:
        if not cover_url:
            return None
        url = _normalize_bili_url(cover_url)
        target = save_dir / f"{file_stem}_cover.jpg"
        if self.file_manager.file_exists(target):
            return target
        if await self._download_plain_url(url, target, session, referer=referer):
            return target
        return None

    async def _save_subtitles(
        self, bvid: str, cid: int, save_dir: Path, file_stem: str, session, referer: str
    ) -> List[Path]:
        subtitles = await self.api_client.get_subtitles(bvid, cid)
        saved: List[Path] = []
        for entry in subtitles:
            subtitle_url = str(entry.get("subtitle_url") or entry.get("subtitle_url_v2") or "")
            if not subtitle_url:
                continue
            language = str(entry.get("lan") or "unknown")
            target = save_dir / f"{file_stem}.{language}.srt"
            if self.file_manager.file_exists(target):
                saved.append(target)
                continue
            raw = await self._fetch_text(_normalize_bili_url(subtitle_url), session, referer)
            if not raw:
                continue
            body = _parse_subtitle_payload(raw)
            if not body:
                continue
            if _write_text(target, _subtitle_body_to_srt(body)):
                saved.append(target)
        return saved

    async def _save_danmaku(
        self, cid: int, save_dir: Path, file_stem: str
    ) -> Optional[Path]:
        target = save_dir / f"{file_stem}.danmaku.xml"
        if self.file_manager.file_exists(target):
            return target
        xml = await self.api_client.get_danmaku_xml(cid)
        if not xml:
            return None
        return target if _write_text(target, xml) else None

    async def _fetch_text(self, url: str, session, referer: str) -> str:
        if not is_safe_url(url):
            logger.warning("Blocked unsafe text fetch url: %s", url)
            return ""
        headers = self.api_client.download_headers(url, referer=referer)
        try:
            async with session.get(
                url,
                headers=headers,
                proxy=self.api_client.proxy or None,
            ) as response:
                if response.status != 200:
                    logger.debug("Text fetch failed: status=%s url=%s", response.status, url)
                    return ""
                return await response.text()
        except Exception as exc:
            logger.debug("Text fetch error for %s: %s", url, exc)
            return ""

    # ------------------------------------------------------------------
    # 单条稿件下载（含分 P）
    # ------------------------------------------------------------------

    async def download_video_item(
        self,
        video_info: Dict[str, Any],
        *,
        author_name: str,
        author_mid: Optional[str],
        mode: str,
        url_type: str,
        collection_dir: Optional[str] = None,
        page_filter: Optional[int] = None,
        force: bool = False,
    ) -> str:
        """下载一条稿件（含全部分 P），返回 ``"success"`` / ``"failed"`` /
        ``"skipped"``。

        ``force=True`` 跳过整条稿件的增量判定——用户在链接里显式写了 ``?p=N``
        时必须这样，否则「第 1 P 已在盘上」会让整条稿件被跳过，用户要的那一 P
        永远下不到（页级判定仍会跳过真正已存在的 P）。
        """
        try:
            return await asyncio.wait_for(
                self._download_video_item_inner(
                    video_info,
                    author_name=author_name,
                    author_mid=author_mid,
                    mode=mode,
                    url_type=url_type,
                    collection_dir=collection_dir,
                    page_filter=page_filter,
                    force=force,
                ),
                timeout=_ITEM_DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            self._log_download_error(
                logger.warning,
                f"Item deadline ({_ITEM_DEADLINE_SECONDS}s) exceeded for "
                f"{video_info.get('bvid') or video_info.get('aid')}",
            )
            return "failed"
        except BiliLoginRequiredError:
            raise
        except Exception as exc:
            logger.error(
                "Unexpected failure downloading %s: %s",
                video_info.get("bvid") or video_info.get("aid"),
                exc,
            )
            return "failed"

    async def _download_video_item_inner(
        self,
        video_info: Dict[str, Any],
        *,
        author_name: str,
        author_mid: Optional[str],
        mode: str,
        url_type: str,
        collection_dir: Optional[str],
        page_filter: Optional[int],
        force: bool,
    ) -> str:
        bvid = str(video_info.get("bvid") or "").strip()
        aid = video_info.get("aid")

        await self.rate_limiter.acquire()
        detail = await self.api_client.get_video_detail(bvid=bvid or None, aid=aid or None)
        if not detail:
            logger.error("Failed to fetch bilibili video detail: %s", bvid or aid)
            return "failed"

        bvid = str(detail.get("bvid") or bvid).strip()
        if not bvid:
            return "failed"

        if not await self._should_download(bvid, url_type=url_type, force=force):
            return "skipped"

        pages = _video_pages(detail, page_filter)
        if not pages:
            logger.error("No playable page (cid) found for bilibili video %s", bvid)
            return "failed"

        title = str(detail.get("title") or "").strip() or "no_title"
        pubdate = detail.get("pubdate")
        cover_url = str(detail.get("pic") or "").strip()
        owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
        resolved_author = str(owner.get("name") or author_name or "unknown")
        resolved_mid = str(owner.get("mid") or author_mid or "") or None
        self._progress_report_author(resolved_author, resolved_mid)

        session = await self.api_client.get_session()
        referer = f"{self.api_client.BASE_URL}/video/{bvid}"
        # 多 P 判定必须看稿件的**全部**页数：page_filter 过滤后只剩一页时，
        # 「过滤后 1 页」不代表这是单 P 稿件，文件名仍要带 _pN 后缀，
        # 否则会与第 1 P 的单 P 命名冲突。
        multi_page = len(_video_pages(detail, None)) > 1
        downloaded_files: List[Path] = []
        page_failures = 0
        page_skips = 0

        for page in pages:
            page_no = _to_positive_int(page.get("page"), 1)
            part = str(page.get("part") or "").strip()
            if multi_page and part and part != title:
                display_title = f"{title}_{part}"
            else:
                display_title = title
            page_id = f"{bvid}_p{page_no}" if multi_page else bvid

            if not await self._should_download_page(page_id):
                page_skips += 1
                continue

            context = self._build_item_context(
                item_id=page_id,
                title=display_title,
                author_name=resolved_author,
                author_mid=resolved_mid,
                pubdate=pubdate,
                media_type="video",
                mode=mode,
                collection_dir=collection_dir,
            )
            if context is None:
                page_failures += 1
                continue

            # 分 P 级步骤文案：长教程动辄十几分钟一 P，进度条若停在
            # 「拉取稿件详情」会被误读为卡死。
            self._progress_update_step(
                "下载视频",
                f"分P {page_no}/{len(pages)}：{display_title}" if multi_page else display_title,
            )

            page_files = await self._download_single_page(
                bvid=bvid,
                cid=_to_positive_int(page.get("cid"), 0),
                context=context,
                session=session,
                referer=referer,
                detail=detail,
                is_multi_page=multi_page,
                page_index=page_no,
                on_progress=self._make_item_progress(page_id),
            )
            if page_files:
                downloaded_files.extend(page_files)
            else:
                page_failures += 1

        if not downloaded_files:
            if page_skips and not page_failures:
                return "skipped"
            return "failed"

        # 封面是稿件级资产，与分 P 无关，只存一份。
        first_context = self._build_item_context(
            item_id=bvid,
            title=title,
            author_name=resolved_author,
            author_mid=resolved_mid,
            pubdate=pubdate,
            media_type="video",
            mode=mode,
            collection_dir=collection_dir,
        )
        if first_context and bool(self.bili("download_cover", False)):
            cover = await self._save_cover(
                cover_url, first_context["save_dir"], first_context["file_stem"], session, referer
            )
            if cover:
                downloaded_files.append(cover)

        await self._record_video(
            bvid=bvid,
            detail=detail,
            author_name=resolved_author,
            author_mid=resolved_mid,
            mode=mode,
            files=downloaded_files,
            context=first_context,
        )
        self._mark_local_downloaded(bvid)
        logger.info(
            "Downloaded bilibili video %s (%s), %d file(s)",
            bvid,
            title,
            len(downloaded_files),
        )
        return "success"

    async def _download_single_page(
        self,
        *,
        bvid: str,
        cid: int,
        context: Dict[str, Any],
        session,
        referer: str,
        detail: Dict[str, Any],
        is_multi_page: bool,
        page_index: int,
        on_progress=None,
    ) -> List[Path]:
        """下载一个分 P 的视频轨 + 音频轨并合并；返回落盘的文件列表。"""
        if not cid:
            return []

        save_dir: Path = context["save_dir"]
        file_stem: str = context["file_stem"]
        quality = str(self.bili("quality") or "highest")
        audio_quality = str(self.bili("audio_quality") or "highest")
        codec = str(self.bili("codec") or "auto")
        audio_only = bool(self.bili("audio_only", False))

        play_info = await self.api_client.get_playurl(
            bvid, cid, qn=resolve_durl_qn(quality)
        )
        dash = play_info.get("dash") if isinstance(play_info.get("dash"), dict) else None

        files: List[Path] = []
        if dash and dash.get("video"):
            files = await self._download_dash(
                dash=dash,
                save_dir=save_dir,
                file_stem=file_stem,
                session=session,
                referer=referer,
                quality=quality,
                audio_quality=audio_quality,
                codec=codec,
                audio_only=audio_only,
                on_progress=on_progress,
            )
        if not files:
            files = await self._download_durl_fallback(
                bvid=bvid,
                cid=cid,
                save_dir=save_dir,
                file_stem=file_stem,
                session=session,
                referer=referer,
                quality=quality,
                audio_only=audio_only,
                on_progress=on_progress,
            )
        if not files:
            return []

        if bool(self.bili("download_subtitle", False)):
            files.extend(
                await self._save_subtitles(bvid, cid, save_dir, file_stem, session, referer)
            )
        if bool(self.bili("download_danmaku", False)):
            danmaku = await self._save_danmaku(cid, save_dir, file_stem)
            if danmaku:
                files.append(danmaku)
        if bool(self.bili("download_json", False)):
            payload = dict(detail)
            payload["_page"] = {"cid": cid, "page": page_index, "multi_page": is_multi_page}
            payload["_playurl_summary"] = _summarize_playurl(play_info)
            json_path = save_dir / f"{file_stem}_data.json"
            if _write_json(json_path, payload):
                files.append(json_path)

        self._mark_local_downloaded(f"{bvid}_p{page_index}" if is_multi_page else bvid)
        return files

    async def _download_dash(
        self,
        *,
        dash: Dict[str, Any],
        save_dir: Path,
        file_stem: str,
        session,
        referer: str,
        quality: str,
        audio_quality: str,
        codec: str,
        audio_only: bool,
        on_progress=None,
    ) -> List[Path]:
        video_streams = dash.get("video")
        video_streams = video_streams if isinstance(video_streams, list) else []
        video_stream = select_video_stream(video_streams, quality, codec)
        audio_stream = select_audio_stream(dash, audio_quality)

        if audio_only:
            if not audio_stream:
                logger.warning("No audio stream available for %s", file_stem)
                return []
            audio_tmp = save_dir / f"{file_stem}.audio.m4s"
            target = save_dir / f"{file_stem}.m4a"
            if self.file_manager.file_exists(target):
                return [target]
            if not await self._download_mirrors(
                stream_urls(audio_stream), audio_tmp, session, referer=referer,
                on_progress=on_progress,
            ):
                return []
            if await self._finalize_media(None, audio_tmp, target):
                logger.info("Saved audio-only track %s (%s)", target.name, describe_audio_stream(audio_stream))
                return [target]
            return []

        if not video_stream:
            logger.warning("No video stream matched quality=%s for %s", quality, file_stem)
            return []

        target = save_dir / f"{file_stem}.mp4"
        if self.file_manager.file_exists(target):
            return [target]

        video_tmp = save_dir / f"{file_stem}.video.m4s"
        audio_tmp = save_dir / f"{file_stem}.audio.m4s"
        cleanup_temp_files([video_tmp, audio_tmp])

        if not await self._download_mirrors(
            stream_urls(video_stream), video_tmp, session, referer=referer,
            on_progress=on_progress,
        ):
            logger.error("Failed to download video stream for %s", file_stem)
            cleanup_temp_files([video_tmp])
            return []

        if audio_stream and not await self._download_mirrors(
            stream_urls(audio_stream), audio_tmp, session, referer=referer,
            on_progress=on_progress,
        ):
            # 音频失败就退化成无声视频，比整条失败更接近「拿到了内容」；
            # 用户可从日志看到降级原因。
            logger.warning("Failed to download audio stream for %s; keeping silent video", file_stem)
            audio_tmp = None

        logger.info(
            "Merging %s: video=%s audio=%s",
            file_stem,
            describe_video_stream(video_stream),
            describe_audio_stream(audio_stream),
        )
        if await self._finalize_media(video_tmp, audio_tmp, target):
            return [target]
        return []

    async def _download_durl_fallback(
        self,
        *,
        bvid: str,
        cid: int,
        save_dir: Path,
        file_stem: str,
        session,
        referer: str,
        quality: str,
        audio_only: bool,
        on_progress=None,
    ) -> List[Path]:
        """没有 DASH 时的兜底：``fnval=1`` 的单文件流。

        老稿件、部分番剧以及 DASH 被风控降级时会走到这里。格式可能是 mp4 或
        flv，按接口返回的 ``format`` 决定扩展名。
        """
        if audio_only:
            return []
        try:
            play_info = await self.api_client.get_playurl(
                bvid, cid, qn=resolve_durl_qn(quality), fnval=1
            )
        except Exception as exc:
            logger.error("durl fallback request failed for %s: %s", bvid, exc)
            return []

        durl = play_info.get("durl")
        if not isinstance(durl, list) or not durl:
            logger.error("No playable stream (dash/durl) for %s cid=%s", bvid, cid)
            return []

        fmt = str(play_info.get("format") or "mp4").lower()
        suffix = ".flv" if fmt == "flv" else ".mp4"
        target = save_dir / f"{file_stem}{suffix}"
        if self.file_manager.file_exists(target):
            return [target]

        entry = durl[0] if isinstance(durl[0], dict) else {}
        urls: List[str] = []
        for candidate in (entry.get("url"),):
            if isinstance(candidate, str) and candidate:
                urls.append(candidate)
        backups = entry.get("backup_url")
        if isinstance(backups, list):
            urls.extend(item for item in backups if isinstance(item, str) and item)
        if not urls:
            return []

        if await self._download_mirrors(urls, target, session, referer=referer, on_progress=on_progress):
            logger.info("Saved single-file stream %s (%s)", target.name, fmt)
            return [target]
        return []

    # ------------------------------------------------------------------
    # 落库与清单
    # ------------------------------------------------------------------

    async def _record_video(
        self,
        *,
        bvid: str,
        detail: Dict[str, Any],
        author_name: str,
        author_mid: Optional[str],
        mode: str,
        files: List[Path],
        context: Optional[Dict[str, Any]],
    ) -> None:
        if not files:
            return

        save_dir = context["save_dir"] if context else files[0].parent
        title = context["title"] if context else str(detail.get("title") or "")
        publish_date = context["publish_date"] if context else ""
        publish_ts = context["publish_ts"] if context else None
        owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
        mid = str(owner.get("mid") or author_mid or "")

        if self.database:
            try:
                await self.database.add_aweme(
                    {
                        # 复用 aweme 表承载 B 站记录：aweme_id 存 bvid、author_sec_uid
                        # 存 mid。类型前缀 ``bili_`` 让历史视图能把两个平台分开，
                        # 无需额外建表与迁移。
                        "aweme_id": bvid,
                        "aweme_type": "bili_video",
                        "title": title,
                        "author_id": mid,
                        "author_name": author_name,
                        "author_sec_uid": mid,
                        "create_time": publish_ts,
                        "file_path": str(save_dir),
                        "metadata": json.dumps(detail, ensure_ascii=False),
                        "cover_urls": json.dumps(_cover_mirrors(detail)),
                        "job_id": self.job_id or "",
                    },
                    author_sec_uid=mid,
                )
            except Exception as exc:
                logger.warning("Failed to record bilibili video %s into database: %s", bvid, exc)

        manifest_record = {
            "platform": "bilibili",
            "date": publish_date,
            "aweme_id": bvid,
            "bvid": bvid,
            "aid": detail.get("aid"),
            "author_name": author_name,
            "author_sec_uid": mid,
            "author_url": f"{self.api_client.SPACE_URL}/{mid}/video" if mid else "",
            "desc": title,
            "media_type": "video",
            "mode": mode,
            "tags": _extract_tags(detail),
            "file_names": [path.name for path in files],
            "file_paths": [self._to_manifest_path(path) for path in files],
        }
        if publish_ts:
            manifest_record["publish_timestamp"] = publish_ts

        try:
            await _append_manifest(self.file_manager.base_path, manifest_record)
        except OSError as exc:
            logger.warning("Failed to append download manifest for %s: %s", bvid, exc)

    def _to_manifest_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.file_manager.base_path))
        except ValueError:
            return str(path)


# ----------------------------------------------------------------------
# 模块级工具
# ----------------------------------------------------------------------


async def _append_manifest(base_path: Path, record: Dict[str, Any]) -> None:
    """追加一行下载清单。

    与抖音侧写同一个 ``download_manifest.jsonl``：靠 ``platform`` 字段区分，
    这样「本次任务下载了什么」只需看一个文件。
    """
    import aiofiles

    base_path.mkdir(parents=True, exist_ok=True)
    manifest_path = base_path / "download_manifest.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    async with aiofiles.open(manifest_path, "a", encoding="utf-8") as handle:
        await handle.write(line)


def _video_tokens(filename: str) -> List[str]:
    """从文件名里抽出增量标识。

    带 ``_pN`` 的分 P 只产出 ``bvid_pN``，**不**额外产出裸 ``bvid``：一旦产出，
    ``_should_download`` 会把「只下到第 1 P」的多 P 稿件判成整条已完成，缺失的
    分 P 永远补不回来。只下到部分 P 时靠裸 ``bvid`` 未命中、``bvid_p1`` 命中
    让整条进入「已存在」分支，再由页级判定逐 P 补齐。
    """
    tokens: List[str] = []
    for match in _VIDEO_TOKEN_RE.finditer(filename or ""):
        bvid = match.group(1)
        page = match.group(2)
        token = f"{bvid}_p{page}" if page else bvid
        if token not in tokens:
            tokens.append(token)
    return tokens


def _video_pages(detail: Dict[str, Any], page_filter: Optional[int]) -> List[Dict[str, Any]]:
    pages = detail.get("pages")
    pages = [item for item in pages if isinstance(item, dict)] if isinstance(pages, list) else []
    pages = [item for item in pages if item.get("cid")]
    if not pages:
        cid = detail.get("cid")
        if not cid:
            return []
        pages = [{"cid": cid, "page": 1, "part": detail.get("title")}]
    if page_filter:
        pages = [item for item in pages if _to_positive_int(item.get("page"), 1) == page_filter]
    return pages


def _item_pubdate(item: Dict[str, Any]) -> Optional[int]:
    """归一化各接口的发布时间字段。"""
    for key in ("pubdate", "created", "pubtime", "ctime"):
        value = item.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _resolve_publish_time(value: Any) -> Tuple[Optional[int], str]:
    if value in (None, ""):
        return None, ""
    try:
        publish_ts = int(value)
    except (TypeError, ValueError):
        return None, ""
    if publish_ts <= 0:
        return None, ""
    try:
        return publish_ts, datetime.fromtimestamp(publish_ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return None, ""


def _to_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalize_bili_url(url: str) -> str:
    """把接口返回的 ``//i0.hdslb.com/...`` 补成 https。"""
    stripped = str(url or "").strip()
    if stripped.startswith("//"):
        return f"https:{stripped}"
    if stripped.startswith("http://"):
        # B 站图片域名走 http 会被 301 到 https，直接升级省一次往返。
        return f"https://{stripped[len('http://'):]}"
    return stripped


def _cover_mirrors(detail: Dict[str, Any]) -> List[str]:
    cover = str(detail.get("pic") or "").strip()
    return [_normalize_bili_url(cover)] if cover else []


def _extract_tags(detail: Dict[str, Any]) -> List[str]:
    tags: List[str] = []
    for key in ("tname", "dynamic"):
        value = detail.get(key)
        if isinstance(value, str):
            normalized = value.strip().lstrip("#")
            if normalized and normalized not in tags:
                tags.append(normalized)
    for entry in detail.get("pages") or []:
        if isinstance(entry, dict):
            part = str(entry.get("part") or "").strip()
            if part and part not in tags:
                tags.append(part)
    return tags


def _summarize_playurl(play_info: Dict[str, Any]) -> Dict[str, Any]:
    """JSON 侧车里只留可读的播放信息，避免把带签名的直链与无用的超大字段写盘。"""
    dash = play_info.get("dash") if isinstance(play_info.get("dash"), dict) else {}
    return {
        "quality": play_info.get("quality"),
        "format": play_info.get("format"),
        "accept_quality": play_info.get("accept_quality"),
        "accept_description": play_info.get("accept_description"),
        "timelength_ms": play_info.get("timelength"),
        "dash_duration": dash.get("duration"),
    }


def _parse_subtitle_payload(raw: str) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    body = payload.get("body")
    if not isinstance(body, list):
        return []
    return [item for item in body if isinstance(item, dict)]


def _subtitle_body_to_srt(body: List[Dict[str, Any]]) -> str:
    """把 B 站字幕 JSON 的 ``body`` 转成 SRT。"""
    blocks: List[str] = []
    for index, item in enumerate(body, start=1):
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        start = _seconds_to_timestamp(item.get("from"))
        end = _seconds_to_timestamp(item.get("to"))
        blocks.append(f"{index}\n{start} --> {end}\n{content}\n")
    return "\n".join(blocks)


def _seconds_to_timestamp(value: Any) -> str:
    try:
        total = float(value)
    except (TypeError, ValueError):
        total = 0.0
    if total < 0:
        total = 0.0
    milliseconds = int(round(total * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _write_text(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return True
    except OSError as exc:
        logger.warning("Failed to write %s: %s", path, exc)
        return False


def _write_json(path: Path, payload: Dict[str, Any]) -> bool:
    return _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
