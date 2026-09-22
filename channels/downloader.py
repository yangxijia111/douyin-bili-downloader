"""视频号下载器：直链流式下载 + ISAAC64 头部解密 + 落盘落库。

下载本身很普通（finderVideo CDN 的 MP4 直链，带 ``Referer`` 即可），特殊的
只有两点：

1. **头部解密**：文件前 131072 字节用 ISAAC64 密钥流异或（见
   :mod:`channels.isaac64`）。下载时边收边解——分块的文件偏移直接作为密钥
   流下标，不需要整文件缓冲；
2. **MP4 魔数自校验**：解密正确则文件头必然出现 ``ftyp`` box；校验失败说明
   decodeKey 与文件不匹配（过期重签 URL 等），立即报废重下，不把坏文件
   留在磁盘与历史库里。

落盘 / 增量 / 历史与抖音、B 站、yt-dlp 平台共用同一套基础设施：
``storage.FileManager`` + ``utils.naming`` 模板、``aweme`` 表（token 为
``channels_<objectId>``，``aweme_type`` 前缀区分 video/image/live）、
``download_manifest.jsonl`` 与磁盘文件名扫描判重。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlsplit

import aiofiles
import aiohttp

from channels.feed import ChannelFeed, pick_quality_url
from channels.isaac64 import ENCRYPTED_HEADER_SIZE, Isaac64Cipher, is_mp4_header
from channels.live import LiveRecorder
from config import ConfigLoader
from core.downloader_base import DownloadResult
from storage import Database, FileManager
from utils.logger import setup_logger
from utils.naming import (
    DEFAULT_FILE_TEMPLATE,
    DEFAULT_FOLDER_TEMPLATE,
    build_aweme_context,
    render_template,
)

logger = setup_logger("ChannelsDownloader")

__all__ = ["ChannelsDownloadError", "ChannelsDownloader"]

# 视频号页面（微信内嵌 Chromium）的常规 UA；CDN 只做宽松校验。
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://channels.weixin.qq.com/",
}

# 直链里允许保留的媒体扩展名（防 CDN 返回怪后缀）。
_ALLOWED_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp3"}

# channels 段配置缺省值（与 config/default_config.py 保持一致）。
_SECTION_DEFAULTS: Dict[str, Any] = {
    "quality": "highest",
    "increase": True,
    "download_cover": False,
    "live_record": False,
}


class ChannelsDownloadError(RuntimeError):
    pass


class ChannelsDownloader:
    """嗅探捕获结果 → 本地文件。

    构造签名与 ``ytdlp.downloader.YtdlpDownloader`` 对齐（视频号无登录凭据，
    无 cookie_manager）。一个实例服务一个嗅探会话，会话结束调
    :meth:`aclose` 释放连接池。
    """

    # 磁盘增量扫描：文件名里的 ``channels_<objectId>`` token。
    _VIDEO_TOKEN_RE = re.compile(
        r"(?<![0-9A-Za-z])channels_([0-9A-Za-z][0-9A-Za-z\-]*)"
    )

    def __init__(
        self,
        config: ConfigLoader,
        file_manager: FileManager,
        *,
        database: Optional[Database] = None,
        rate_limiter: Any = None,
        retry_handler: Any = None,
        progress_reporter: Any = None,
        job_id: Optional[str] = None,
    ):
        self.config = config
        self.file_manager = file_manager
        self.database = database
        self.rate_limiter = rate_limiter
        self.retry_handler = retry_handler
        self.progress_reporter = progress_reporter
        self.job_id = job_id
        self._session: Optional[aiohttp.ClientSession] = None
        self._local_tokens: Optional[Set[str]] = None
        self._live_recorder: Optional[LiveRecorder] = None

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def download(self, parsed: Dict[str, Any]) -> DownloadResult:
        """统一契约入口。嗅探会话传入 ``{"feeds": [ChannelFeed, ...]}``。"""
        result = DownloadResult()
        feeds: List[ChannelFeed] = list(parsed.get("feeds") or [])
        if self.progress_reporter:
            self.progress_reporter.set_item_total(len(feeds))
        for feed in feeds:
            result.total += 1
            status = await self.download_feed(feed)
            if status == "done":
                result.success += 1
            elif status == "skipped":
                result.skipped += 1
            else:
                result.failed += 1
            if self.progress_reporter:
                self.progress_reporter.advance_item(status)
        return result

    async def download_feed(self, feed: ChannelFeed, *, manual: bool = False) -> str:
        """下载单条捕获，返回 ``done`` / ``skipped`` / ``failed``。

        ``manual`` 为 True 时（用户在列表里点下载）跳过增量判重。
        """
        try:
            if feed.kind == "live":
                return await self._download_live(feed, manual=manual)
            if not manual:
                if not await self._should_download(feed.token):
                    logger.info("%s 已存在，跳过", feed.token)
                    return "skipped"
            if feed.kind == "image":
                files = await self._download_images(feed)
                await self._record(feed, files, "channels_image")
            else:
                files = await self._download_video(feed)
                await self._record(feed, files, "channels_video")
            return "done"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单条失败不影响会话
            logger.error("下载失败 %s: %s", feed.token, exc)
            feed.error = str(exc)
            return "failed"

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    # 增量判重
    # ------------------------------------------------------------------

    def _opt(self, key: str, default: Any = None) -> Any:
        section = self.config.get("channels")
        if not isinstance(section, dict):
            section = {}
        return section.get(key, _SECTION_DEFAULTS.get(key, default))

    def _build_local_index(self) -> None:
        tokens: Set[str] = set()
        base = self.file_manager.base_path
        if base.exists():
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                match = self._VIDEO_TOKEN_RE.search(path.name)
                if match:
                    tokens.add(f"channels_{match.group(1)}")
        self._local_tokens = tokens

    def _is_locally_downloaded(self, token: str) -> bool:
        if self._local_tokens is None:
            self._build_local_index()
        assert self._local_tokens is not None
        return token in self._local_tokens

    async def _should_download(self, token: str) -> bool:
        if not bool(self._opt("increase", True)):
            return True
        if self._is_locally_downloaded(token):
            return False
        if self.database is None or bool(self.config.get("redownload_missing_files", True)):
            return True
        try:
            if await self.database.is_downloaded(token):
                logger.info("%s 历史有记录但文件缺失，按配置跳过", token)
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询下载历史失败，按需下载 %s: %s", token, exc)
        return True

    def _remember_token(self, token: str) -> None:
        if self._local_tokens is None:
            self._build_local_index()
        assert self._local_tokens is not None
        self._local_tokens.add(token)

    # ------------------------------------------------------------------
    # 命名与目录
    # ------------------------------------------------------------------

    def _build_item_context(self, feed: ChannelFeed) -> Dict[str, Any]:
        author_name = feed.author_name or "视频号作者"
        title = feed.title or feed.object_id or "no_title"
        publish_ts = feed.create_time or None
        try:
            publish_date = (
                datetime.fromtimestamp(publish_ts).strftime("%Y-%m-%d")
                if publish_ts
                else datetime.now().strftime("%Y-%m-%d")
            )
        except (OSError, OverflowError, ValueError):
            publish_date = datetime.now().strftime("%Y-%m-%d")
            publish_ts = None

        token = feed.token
        context = build_aweme_context(
            aweme_id=token,
            title=title,
            author_name=author_name,
            author_sec_uid=feed.author_id or None,
            publish_date=publish_date,
            publish_ts=publish_ts,
            media_type=feed.kind,
            mode="channels",
        )
        fallback = f"{publish_date}_{token}"
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
        save_dir = self.file_manager.get_save_path(
            author_name=author_name,
            mode="channels",
            aweme_title=title,
            aweme_id=token,
            folderstyle=bool(self.config.get("folderstyle", True)),
            download_date=publish_date,
            folder_name=folder_name,
            author_sec_uid=feed.author_id or None,
            author_dir_style=self.config.get("author_dir") or "nickname",
            group_by_mode=bool(self.config.get("group_by_mode", True)),
        )
        return {
            "token": token,
            "title": title,
            "author_name": author_name,
            "author_id": feed.author_id,
            "publish_ts": publish_ts,
            "publish_date": publish_date,
            "file_stem": file_stem,
            "save_dir": save_dir,
        }

    # ------------------------------------------------------------------
    # 下载实现
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=8),
                headers=_HEADERS,
            )
        return self._session

    @staticmethod
    def _ext_from_url(url: str, default: str) -> str:
        ext = Path(urlsplit(url).path).suffix.lower()
        return ext if ext in _ALLOWED_EXTS else default

    async def _download_video(self, feed: ChannelFeed) -> List[Path]:
        url = pick_quality_url(feed, str(self._opt("quality", "highest")))
        if not url:
            raise ChannelsDownloadError("视频缺少可下载直链")
        context = self._build_item_context(feed)
        save_dir: Path = context["save_dir"]
        target = save_dir / f"{context['file_stem']}{self._ext_from_url(url, '.mp4')}"
        part = target.with_name(target.name + ".part")
        cipher = Isaac64Cipher(feed.decode_key) if feed.decode_key else None

        session = await self._ensure_session()
        timeout = aiohttp.ClientTimeout(total=15 * 60, sock_read=90)
        last_exc: Optional[Exception] = None
        retries = max(1, int(self.config.get("retry_times", 3) or 3))
        for attempt in range(retries):
            try:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise ChannelsDownloadError(f"CDN 返回 HTTP {resp.status}")
                    save_dir.mkdir(parents=True, exist_ok=True)
                    offset = 0
                    async with aiofiles.open(part, "wb") as fh:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            if cipher and offset < ENCRYPTED_HEADER_SIZE:
                                chunk = cipher.xor_header(chunk, offset)
                            await fh.write(chunk)
                            offset += len(chunk)
                break
            except asyncio.CancelledError:
                part.unlink(missing_ok=True)
                raise
            except Exception as exc:  # noqa: BLE001 —— 重试后仍失败才报错
                last_exc = exc
                part.unlink(missing_ok=True)
                if attempt < retries - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
        else:
            raise ChannelsDownloadError(f"下载失败（已重试 {retries} 次）: {last_exc}")

        # 解密与文件完整性自校验：正确解码则头部必为合法 MP4 box。
        async with aiofiles.open(part, "rb") as fh:
            head = await fh.read(64)
        if not is_mp4_header(head):
            part.unlink(missing_ok=True)
            raise ChannelsDownloadError(
                "解密校验失败：文件头不是合法 MP4（decodeKey 与文件不匹配）"
            )
        part.replace(target)
        self._remember_token(feed.token)
        logger.info("已下载 %s（%s）", target.name, feed.title[:40])

        files = [target]
        if bool(self._opt("download_cover", False)) and feed.cover_url:
            cover = save_dir / f"{context['file_stem']}_cover{self._ext_from_url(feed.cover_url, '.jpg')}"
            if await self._fetch_simple(feed.cover_url, cover):
                files.append(cover)
        return files

    async def _download_images(self, feed: ChannelFeed) -> List[Path]:
        if not feed.images:
            raise ChannelsDownloadError("图文动态没有可下载图片")
        context = self._build_item_context(feed)
        save_dir: Path = context["save_dir"]
        save_dir.mkdir(parents=True, exist_ok=True)
        files: List[Path] = []
        for idx, image_url in enumerate(feed.images, 1):
            target = save_dir / f"{context['file_stem']}_{idx:02d}{self._ext_from_url(image_url, '.jpg')}"
            if await self._fetch_simple(image_url, target):
                files.append(target)
        if feed.bgm_url:
            bgm = save_dir / f"{context['file_stem']}_bgm.mp3"
            if await self._fetch_simple(feed.bgm_url, bgm):
                files.append(bgm)
        if not files:
            raise ChannelsDownloadError("图文动态全部图片下载失败")
        self._remember_token(feed.token)
        return files

    async def _fetch_simple(self, url: str, target: Path) -> bool:
        """小文件（图片 / 封面 / BGM）直下，失败返回 False 不重试。"""
        session = await self._ensure_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    logger.warning("下载 %s 失败: HTTP %d", target.name, resp.status)
                    return False
                async with aiofiles.open(target, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        await fh.write(chunk)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载 %s 失败: %s", target.name, exc)
            target.unlink(missing_ok=True)
            return False

    async def _download_live(self, feed: ChannelFeed, *, manual: bool = False) -> str:
        if not feed.url:
            feed.error = "直播缺少拉流地址"
            return "failed"
        # 自动模式默认不录制直播：录制时长无上界，必须用户显式开启或手动触发。
        if not manual and not bool(self._opt("live_record", False)):
            return "skipped"
        if self._live_recorder is None:
            try:
                self._live_recorder = LiveRecorder(
                    ffmpeg_path=str(self.config.get("ffmpeg_path") or "")
                )
            except Exception as exc:  # noqa: BLE001
                feed.error = str(exc)
                logger.error("直播录制不可用: %s", exc)
                return "failed"
        context = self._build_item_context(feed)
        output = context["save_dir"] / f"{context['file_stem']}_live.flv"
        try:
            await self._live_recorder.record(feed.url, output)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            feed.error = str(exc)
            return "failed"
        await self._record(feed, [output], "channels_live")
        self._remember_token(feed.token)
        return "done"

    # ------------------------------------------------------------------
    # 落库与清单
    # ------------------------------------------------------------------

    async def _record(
        self, feed: ChannelFeed, files: List[Path], aweme_type: str
    ) -> None:
        if not files:
            return
        context = self._build_item_context(feed)
        save_dir: Path = context["save_dir"]
        feed.downloaded_paths = [str(p) for p in files]

        if self.database:
            try:
                await self.database.add_aweme(
                    {
                        # 与 ytdlp 平台同一复用策略：aweme_id 存平台前缀 token。
                        "aweme_id": feed.token,
                        "aweme_type": aweme_type,
                        "title": context["title"],
                        "author_id": feed.author_id,
                        "author_name": context["author_name"],
                        "author_sec_uid": feed.author_id,
                        "create_time": feed.create_time or None,
                        "file_path": str(save_dir),
                        "metadata": json.dumps(
                            {
                                "kind": feed.kind,
                                "duration": feed.duration,
                                "file_size": feed.file_size,
                                "specs": feed.specs[:8],
                                "decode_key": feed.decode_key,
                                "source_api": feed.source_api,
                            },
                            ensure_ascii=False,
                        ),
                        "cover_urls": json.dumps([feed.cover_url] if feed.cover_url else []),
                        "job_id": self.job_id or "",
                    },
                    author_sec_uid=feed.author_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("写入历史库失败 %s: %s", feed.token, exc)

        manifest_record: Dict[str, Any] = {
            "platform": "channels",
            "engine": "sniffer",
            "date": context.get("publish_date", ""),
            "aweme_id": feed.token,
            "source_id": feed.object_id,
            "author_name": context["author_name"],
            "author_sec_uid": feed.author_id,
            "desc": context["title"],
            "media_type": feed.kind,
            "mode": "channels",
            "webpage_url": "",
            "file_names": [p.name for p in files],
            "file_paths": [str(p) for p in files],
        }
        if context.get("publish_ts"):
            manifest_record["publish_timestamp"] = context["publish_ts"]
        try:
            await _append_manifest(self.file_manager.base_path, manifest_record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入下载清单失败 %s: %s", feed.token, exc)


async def _append_manifest(base_path: Path, record: Dict[str, Any]) -> None:
    """追加一行下载清单。与抖音 / B 站 / yt-dlp 写同一个文件。"""
    base_path.mkdir(parents=True, exist_ok=True)
    manifest_path = base_path / "download_manifest.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    async with aiofiles.open(manifest_path, "a", encoding="utf-8") as handle:
        await handle.write(line)
