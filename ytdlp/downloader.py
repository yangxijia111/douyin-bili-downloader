"""yt-dlp 引擎下载器。

抖音与 B 站各自逆向了站方接口；爱奇艺 / 腾讯视频 / 优酷这类站点的接口签名与
风控变化频繁，自行维护成本极高，且 VIP 内容有 DRM，逆向也拿不到。这里改为把
解析与下载整体委托给 yt-dlp（公有领域许可、社区持续维护各站解析器），本项目
只负责三件事：

* **接线** —— 平台识别、配置读取、Cookie 注入、ffmpeg 定位、进度桥接；
* **落盘规则** —— 目录与文件名走 ``storage.FileManager`` + ``utils.naming`` 的
  同一套模板，和抖音 / B 站产物并排放；
* **增量与历史** —— 复用 ``aweme`` 表与 ``download_manifest.jsonl``，靠
  ``aweme_type="ytdlp_<platform>"`` 与 ``platform`` 字段区分来源。

yt-dlp 是同步阻塞的，所有调用都放进线程池（``asyncio.to_thread``），进度回调再
通过 ``call_soon_threadsafe`` 回到事件循环。

能力边界：VIP 专享内容（ChinaDRM / Widevine）任何工具都无法直接下载；免费内容
与登录后可看的非 DRM 内容可下载。yt-dlp 抛出的错误会被归类为
``drm`` / ``login`` / ``geo`` / ``unsupported`` / ``phantomjs`` / ``generic``，由
CLI / Server 给出对应的可操作提示。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

from .options_policy import UNSAFE_TOGGLE_KEY, apply_extra_options
from .url_parser import PLATFORM_BY_KEY, SUPPORTED_PLATFORMS, platform_display_name

logger = setup_logger("YtdlpDownloader")

# 磁盘增量扫描时认作「主媒体」的后缀。yt-dlp 的中间产物（``.part`` / ``.ytdl`` /
# 合并前的 ``.fXXX.mp4``）不在其中，避免把下到一半的作品判成已完成。
_LOCAL_MEDIA_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".webm",
    ".flv",
    ".mov",
    ".ts",
    ".m4a",
    ".mp3",
    ".opus",
    ".aac",
}

# yt-dlp 临时文件后缀，永不参与增量判定与产物收集。
_TEMP_SUFFIXES = (".part", ".ytdl", ".tmp")

# 侧车文件（封面 / 字幕 / info.json）带作品标识但不代表主媒体存在。
_SIDECAR_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".srt", ".vtt", ".ass", ".json")

# 文件名里的作品标识：``<platform>_<safe_id>``。``safe_id`` 只含字母、数字与
# 连字符（见 :func:`safe_video_id`），所以下划线天然充当分隔符，模板里
# ``{id}`` 前后接什么都不会误吞。
_PLATFORM_KEYS_PATTERN = "|".join(re.escape(spec.key) for spec in SUPPORTED_PLATFORMS)
_VIDEO_TOKEN_RE = re.compile(rf"(?<![0-9A-Za-z])({_PLATFORM_KEYS_PATTERN})_([0-9A-Za-z][0-9A-Za-z\-]*)")

# 画质档位 → yt-dlp format 选择器里的最大高度。
_QUALITY_HEIGHTS = {
    "8k": 4320,
    "4k": 2160,
    "2k": 1440,
    "1080p60": 1080,
    "1080p+": 1080,
    "1080p": 1080,
    "720p60": 720,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
}

# 数据库 metadata 只留这些字段：完整 info dict 里的 ``formats`` 动辄几十项，
# 全量入库既占空间又没有回看价值。
_METADATA_KEYS = (
    "id",
    "title",
    "description",
    "uploader",
    "uploader_id",
    "uploader_url",
    "channel",
    "channel_id",
    "channel_url",
    "duration",
    "upload_date",
    "timestamp",
    "release_timestamp",
    "webpage_url",
    "original_url",
    "thumbnail",
    "extractor",
    "extractor_key",
    "view_count",
    "like_count",
    "comment_count",
    "tags",
    "categories",
    "width",
    "height",
    "ext",
    "format_id",
    "resolution",
    "series",
    "season_number",
    "episode",
    "episode_number",
    "playlist_title",
    "playlist_index",
)


class YtdlpMissingError(RuntimeError):
    """未安装 yt-dlp。"""


class YtdlpDownloadError(RuntimeError):
    """yt-dlp 解析或下载失败。``kind`` 见模块文档。"""

    def __init__(self, message: str, *, kind: str = "generic"):
        super().__init__(message)
        self.message = message
        self.kind = kind


def import_ytdlp():
    """延迟导入 yt-dlp：它是可选依赖，只有真正遇到第三方平台链接时才需要。"""
    try:
        import yt_dlp  # noqa: WPS433 — 运行时导入
    except ImportError as exc:
        raise YtdlpMissingError(
            "未安装 yt-dlp。执行 `pip install yt-dlp`（或 `pip install -r requirements.txt`）后重试。"
        ) from exc
    return yt_dlp


def resolve_ffmpeg_path(configured: Optional[str] = None) -> str:
    """ffmpeg 路径：显式配置 > 打包内置 > PATH。找不到返回空串，由 yt-dlp 自行搜索。"""
    candidate = str(configured or "").strip()
    if candidate:
        return candidate
    try:
        from core.ffmpeg import resolve_ffmpeg_path as resolve_bundled

        return resolve_bundled()
    except Exception as exc:  # pragma: no cover — 仅在异常环境触发
        logger.debug("Failed to resolve bundled ffmpeg: %s", exc)
        return ""


def safe_video_id(video_id: Any) -> str:
    """把站方的视频 ID 归一成只含 ``[0-9A-Za-z-]`` 的文件名安全片段。

    微博 / 小红书这类 ID 可能带冒号、下划线或中文，直接进文件名会被清洗得
    面目全非，增量扫描也就对不上号。统一映射成连字符后，构造 token 与扫描
    文件名两侧看到的是同一个字符串。
    """
    text = str(video_id or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"[^0-9A-Za-z-]+", "-", text).strip("-")
    return cleaned or ""


def build_video_token(platform: str, video_id: Any) -> str:
    """``<platform>_<safe_id>``，作为文件名 ``{id}``、数据库 ``aweme_id`` 与增量标识。"""
    safe = safe_video_id(video_id)
    if not safe:
        return ""
    return f"{platform}_{safe}"


def video_tokens_in_filename(filename: str) -> List[str]:
    tokens: List[str] = []
    for match in _VIDEO_TOKEN_RE.finditer(filename or ""):
        token = f"{match.group(1)}_{match.group(2)}"
        if token not in tokens:
            tokens.append(token)
    return tokens


def format_selector(quality: Any, *, audio_only: bool = False) -> str:
    """把配置里的画质档位翻译成 yt-dlp 的 format 表达式。

    * ``highest`` / 空：最佳视频轨 + 最佳音频轨，退回单文件最佳；
    * ``lowest``：最差，适合只想快速预览的场景；
    * ``1080p`` 等：限制视频高度，仍优先分离轨合并；
    * ``audio_only``：只要音频轨。

    yt-dlp 在指定档位不可用时会自动降到最接近的可用档，行为与 B 站侧一致。
    """
    if audio_only:
        return "ba/b"
    key = str(quality or "highest").strip().lower()
    if key in ("", "highest", "best"):
        return "bv*+ba/b"
    if key in ("lowest", "worst"):
        return "wv*+wa/w"
    height = _QUALITY_HEIGHTS.get(key)
    if height is None:
        digits = re.match(r"^(\d{3,4})p?$", key)
        if digits:
            height = int(digits.group(1))
    if height is None:
        logger.warning("Unknown ytdlp.quality %r, falling back to highest", quality)
        return "bv*+ba/b"
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"


def classify_download_error(message: str) -> str:
    """把 yt-dlp 的报错文案归类，让上层给出可操作的提示而不是原样甩给用户。"""
    text = (message or "").lower()
    # iq.com（爱奇艺国际站）等站点的 JS 挑战需要 PhantomJS；这是可安装解决的
    # 环境缺失，必须与「站方改版」区分开。
    if "phantomjs" in text:
        return "phantomjs"
    if "drm" in text or "widevine" in text or "protected" in text:
        return "drm"
    if any(
        hint in text
        for hint in (
            "login",
            "log in",
            "sign in",
            "cookies",
            "premium",
            "vip",
            "member",
            "subscri",
            "purchase",
            "paid",
            "付费",
            "会员",
            "登录",
        )
    ):
        return "login"
    if any(hint in text for hint in ("geo", "not available in your country", "region", "地区")):
        return "geo"
    if any(
        hint in text
        for hint in (
            "unsupported url",
            "no video formats",
            "can't find any video",
            "unable to extract",
            "please report this issue",
        )
    ):
        return "unsupported"
    return "generic"


def write_netscape_cookies(cookies: Dict[str, str], domain: str, target: Path) -> None:
    """把 ``name=value`` 字典写成 yt-dlp 能读的 Netscape Cookie 文件。

    只写平台主域；``domain`` 以 ``.`` 开头时 flag 列为 ``TRUE``（包含子域）。
    过期时间统一给一年——站方真正的过期由服务端判定，这里只需保证 yt-dlp 不
    把它当过期 Cookie 丢掉。
    """
    expiry = int(time.time()) + 365 * 86400
    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
    lines = [
        "# Netscape HTTP Cookie File",
        "# 由 douyin-downloader 根据 config.yml 的 ytdlp.cookies 自动生成，勿手改。",
        "",
    ]
    for name, value in cookies.items():
        key = str(name or "").strip()
        if not key:
            continue
        lines.append(
            "\t".join([domain, include_subdomains, "/", "FALSE", str(expiry), key, str(value or "")])
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _YtdlpLoggerAdapter:
    """把 yt-dlp 的日志接到项目 logger。

    yt-dlp 把 ``[download]`` 一类的进度信息也发到 ``debug``（带 ``[debug]`` 前缀
    的才是真正的调试信息），全都降到 DEBUG 级别，避免刷屏。
    """

    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.error("yt-dlp: %s", message)


class YtdlpDownloader:
    """单条链接（可展开为剧集 / 列表）的 yt-dlp 下载器。

    构造签名与 :class:`bilibili.downloader_base.BiliBaseDownloader` 对齐，只是
    没有 ``api_client``——HTTP 由 yt-dlp 自己管。
    """

    def __init__(
        self,
        config: ConfigLoader,
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
        self.file_manager = file_manager
        # 抖音侧的 CookieManager 用不上（凭据走 config.ytdlp.cookies），保留参数
        # 只为与其他平台的工厂签名一致。
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
        self._temp_cookie_path: Optional[Path] = None
        # 最后一次失败的分类与文案：整条链接全部失败时由 CLI / Server 用来
        # 决定提示措辞（DRM / 登录 / 地区限制各不相同）。
        self.last_error: Optional[YtdlpDownloadError] = None

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def ytd(self, key: str, default: Any = None) -> Any:
        section = self.config.get("ytdlp")
        if isinstance(section, dict) and key in section:
            return section[key]
        return default

    def _number_limit(self, url_type: str) -> int:
        numbers = self.ytd("number", {}) or {}
        try:
            return int(numbers.get(url_type, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _increase_enabled(self, url_type: str) -> bool:
        increase = self.ytd("increase", {}) or {}
        value = increase.get(url_type, True)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _as_bool(self, key: str, default: bool = False) -> bool:
        value = self.ytd(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def ffmpeg_path(self) -> str:
        return str(self.ytd("ffmpeg_path") or "").strip()

    # ------------------------------------------------------------------
    # 进度上报（与抖音 / B 站侧同名，便于宿主复用同一套 reporter）
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

    def _progress_report_author(self, name: Optional[str], author_id: Optional[str]) -> None:
        if not self.progress_reporter:
            return
        try:
            hook = getattr(self.progress_reporter, "on_author", None)
            if callable(hook):
                hook(nickname=name, sec_uid=author_id)
        except Exception as exc:  # pragma: no cover — 防御
            logger.debug("Progress on_author failed: %s", exc)

    def _make_progress_hook(
        self, token: str, loop: asyncio.AbstractEventLoop
    ) -> Optional[Callable[[Dict[str, Any]], None]]:
        """构造 yt-dlp 的 ``progress_hooks`` 回调，把字节进度桥回事件循环。

        回调在 yt-dlp 的工作线程里触发，reporter（rich 进度条 / 网页任务对象）
        都只在事件循环线程里安全，所以经 ``call_soon_threadsafe`` 中转。
        """
        reporter = self.progress_reporter
        if not reporter or not token:
            return None
        emit = getattr(reporter, "on_item_progress", None)
        if not callable(emit):
            return None

        def _emit_safely(bytes_read: int, bytes_total: int) -> None:
            try:
                emit(aweme_id=token, bytes_read=bytes_read, bytes_total=bytes_total)
            except Exception as exc:
                logger.debug("Progress on_item_progress failed: %s", exc)

        def _hook(payload: Dict[str, Any]) -> None:
            if payload.get("status") != "downloading":
                return
            done = int(payload.get("downloaded_bytes") or 0)
            total = int(payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0)
            try:
                loop.call_soon_threadsafe(_emit_safely, done, total)
            except RuntimeError:
                # 事件循环已关闭（任务被取消时的收尾阶段），进度丢掉即可。
                pass

        return _hook

    def _log_download_error(self, log_fn, message: str) -> None:
        if self._download_error_log_count < self._download_error_log_limit:
            log_fn(message)
        elif self._download_error_log_count == self._download_error_log_limit:
            logger.error("Too many download errors, suppressing further per-file logs...")
        self._download_error_log_count += 1

    # ------------------------------------------------------------------
    # yt-dlp 选项
    # ------------------------------------------------------------------

    def _prepare_cookie_file(self, platform: str) -> Optional[Path]:
        """得到传给 yt-dlp 的 Cookie 文件路径。

        优先级：``ytdlp.cookie_file``（浏览器导出的 Netscape 文件，直接用）>
        ``ytdlp.cookies.<platform>``（``name=value`` 字符串或字典，写临时文件）。
        返回的临时文件由调用方在任务结束后删除。
        """
        explicit = str(self.ytd("cookie_file") or "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            if path.is_file():
                return path
            logger.warning("ytdlp.cookie_file 不存在，忽略：%s", explicit)

        getter = getattr(self.config, "get_ytdlp_cookies", None)
        cookies = getter(platform) if callable(getter) else {}
        if not cookies:
            return None

        spec = PLATFORM_BY_KEY.get(platform)
        domain = spec.cookie_domain if spec else f".{platform}.com"
        handle = tempfile.NamedTemporaryFile(
            "w", prefix=f"ytdlp_{platform}_", suffix=".txt", delete=False, encoding="utf-8"
        )
        handle.close()
        target = Path(handle.name)
        write_netscape_cookies(cookies, domain, target)
        self._temp_cookie_path = target
        return target

    def _base_options(self, platform: str, cookie_path: Optional[Path]) -> Dict[str, Any]:
        audio_only = self._as_bool("audio_only", False)
        retries = int(self.config.get("retry_times", 3) or 3)
        thread_count = int(self.config.get("thread", 5) or 5)

        options: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "logger": _YtdlpLoggerAdapter(),
            "format": format_selector(self.ytd("quality", "highest"), audio_only=audio_only),
            "retries": retries,
            "fragment_retries": retries,
            "socket_timeout": 30,
            "ignoreerrors": False,
            # 允许剧集页 / 列表页展开；条数上限由 playlistend 控制。
            "noplaylist": False,
            # HLS / DASH 分片并发：复用抖音侧的 thread 配置，语义都是「同时几路」。
            "concurrent_fragment_downloads": max(1, thread_count),
            # 文件名由本项目模板渲染并清洗过，这里只让 yt-dlp 处理它自己的
            # ``.fXXX`` 中间名与扩展名。
            "windowsfilenames": os.name == "nt",
            "overwrites": False,
            "continuedl": True,
        }
        if not audio_only:
            options["merge_output_format"] = "mp4"
        else:
            options["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}
            ]

        limit = self._number_limit("video")
        if limit > 0:
            options["playlistend"] = limit

        proxy = self.config.get("proxy")
        if proxy:
            options["proxy"] = str(proxy)

        if cookie_path is not None:
            options["cookiefile"] = str(cookie_path)

        ffmpeg = resolve_ffmpeg_path(self.ffmpeg_path)
        if ffmpeg:
            options["ffmpeg_location"] = ffmpeg

        if self._as_bool("download_subtitle", False):
            options["writesubtitles"] = True
            options["subtitleslangs"] = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW"]
        if self._as_bool("download_cover", False):
            options["writethumbnail"] = True
        if self._as_bool("download_json", False):
            options["writeinfojson"] = True

        extra = self.ytd("extra_options")
        if isinstance(extra, dict) and extra:
            # 逃生舱走白名单分级（见 ytdlp.options_policy）：默认只放行无本地
            # 副作用的参数；outtmpl / exec_cmd 等危险参数需显式
            # ytdlp.unsafe_extra_options: true，未知键一律报错。
            apply_extra_options(
                options,
                extra,
                unsafe_enabled=self._as_bool(UNSAFE_TOGGLE_KEY, False),
            )
        return options

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        result = DownloadResult()
        platform = str(parsed_url.get("platform") or "")
        url = str(parsed_url.get("url") or parsed_url.get("original_url") or "").strip()
        url_type = str(parsed_url.get("type") or "video")
        if not platform or not url:
            logger.error("ytdlp download called without platform/url: %r", parsed_url)
            return result

        ydl_module = import_ytdlp()
        name = platform_display_name(platform)
        cookie_path = self._prepare_cookie_file(platform)

        try:
            base_options = self._base_options(platform, cookie_path)
            self._progress_update_step("解析链接", f"{name} · 提取视频信息")
            await self.rate_limiter.acquire()
            info = await asyncio.to_thread(self._extract_info, ydl_module, base_options, url)
            entries = self._flatten_entries(info)
            entries = self._apply_limit(entries, url_type)
            if not entries:
                logger.warning("yt-dlp returned no downloadable entries for %s", url)
                self._progress_update_step("解析链接", "未解析到可下载的视频")
                return result

            result.total = len(entries)
            first = entries[0]
            author_name, author_id = self._author_of(first, platform)
            self._progress_report_author(author_name, author_id)
            self._progress_set_item_total(len(entries), f"{name} · 共 {len(entries)} 条")

            for entry in entries:
                token = build_video_token(platform, entry.get("id"))
                status = await self._download_entry(
                    ydl_module,
                    base_options,
                    entry,
                    platform=platform,
                    url_type=url_type,
                )
                if status == "success":
                    result.success += 1
                elif status == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1
                self._progress_advance_item(status, token or str(entry.get("id") or ""))
        finally:
            self._cleanup_cookie_file()
        return result

    def _extract_info(self, ydl_module, options: Dict[str, Any], url: str) -> Dict[str, Any]:
        """在工作线程里跑 yt-dlp 的信息提取（不下载）。"""
        with ydl_module.YoutubeDL(dict(options)) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except ydl_module.utils.DownloadError as exc:
                message = str(exc)
                raise YtdlpDownloadError(message, kind=classify_download_error(message)) from exc
            except ydl_module.utils.ExtractorError as exc:
                message = str(exc)
                raise YtdlpDownloadError(message, kind=classify_download_error(message)) from exc
        return info or {}

    def _run_download(self, ydl_module, options: Dict[str, Any], url: str) -> None:
        """在工作线程里跑 yt-dlp 的实际下载。"""
        with ydl_module.YoutubeDL(dict(options)) as ydl:
            try:
                retcode = ydl.download([url])
            except ydl_module.utils.DownloadError as exc:
                message = str(exc)
                raise YtdlpDownloadError(message, kind=classify_download_error(message)) from exc
        if retcode not in (0, None):
            raise YtdlpDownloadError(f"yt-dlp exited with code {retcode}")

    @staticmethod
    def _flatten_entries(info: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """单条返回 ``[info]``；剧集 / 列表页展开一层嵌套后返回条目列表。"""
        if not info:
            return []
        if info.get("_type") != "playlist":
            return [info]
        flat: List[Dict[str, Any]] = []
        for entry in info.get("entries") or []:
            if not entry:
                continue
            if entry.get("_type") == "playlist":
                flat.extend(item for item in (entry.get("entries") or []) if item)
            else:
                flat.append(entry)
        return flat

    def _apply_limit(self, items: List[Dict[str, Any]], url_type: str) -> List[Dict[str, Any]]:
        limit = self._number_limit(url_type)
        if limit > 0:
            return items[:limit]
        return items

    async def _download_entry(
        self,
        ydl_module,
        base_options: Dict[str, Any],
        entry: Dict[str, Any],
        *,
        platform: str,
        url_type: str,
    ) -> str:
        video_id = entry.get("id")
        token = build_video_token(platform, video_id)
        if not token:
            logger.error("yt-dlp entry without id, skipping: %r", entry.get("title"))
            return "failed"

        if not await self._should_download(token, url_type=url_type):
            return "skipped"

        entry_url = str(
            entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or ""
        ).strip()
        if not entry_url:
            logger.error("yt-dlp entry %s has no downloadable URL", token)
            return "failed"

        context = self._build_item_context(entry, platform=platform, token=token)
        options = dict(base_options)
        options["outtmpl"] = {"default": str(context["save_dir"] / f"{context['file_stem']}.%(ext)s")}
        # 单条下载时不要再把它当列表展开：剧集页里的一集本身也可能带列表信息。
        options["noplaylist"] = True
        options.pop("playlistend", None)
        hook = self._make_progress_hook(token, asyncio.get_running_loop())
        if hook is not None:
            options["progress_hooks"] = [hook]

        self._progress_update_step("执行下载", f"{context['title']}")
        await self.rate_limiter.acquire()
        try:
            await asyncio.to_thread(self._run_download, ydl_module, options, entry_url)
        except YtdlpDownloadError as exc:
            self.last_error = exc
            self._log_download_error(
                logger.error, f"yt-dlp download failed for {token} ({exc.kind}): {exc.message}"
            )
            return "failed"
        except Exception as exc:
            self.last_error = YtdlpDownloadError(str(exc))
            self._log_download_error(logger.error, f"yt-dlp download crashed for {token}: {exc}")
            return "failed"

        files = self._collect_output_files(context["save_dir"], context["file_stem"])
        if not files:
            self._log_download_error(
                logger.error, f"yt-dlp reported success but no media found for {token}"
            )
            return "failed"

        self._mark_local_downloaded(token)
        await self._record_video(
            token=token,
            entry=entry,
            platform=platform,
            files=files,
            context=context,
        )
        logger.info("Saved %s → %s", token, files[0].name)
        return "success"

    # ------------------------------------------------------------------
    # 磁盘增量索引（与 B 站侧同构）
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
                suffix = path.suffix.lower()
                if suffix in _TEMP_SUFFIXES or suffix not in _LOCAL_MEDIA_SUFFIXES:
                    continue
                try:
                    if path.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                ids.update(video_tokens_in_filename(path.name))
        self._local_video_ids = ids

    def _is_locally_downloaded(self, token: str) -> bool:
        if not token:
            return False
        if self._local_video_ids is None:
            self._build_local_index()
        return token in (self._local_video_ids or set())

    def _mark_local_downloaded(self, token: str) -> None:
        if not token:
            return
        if self._local_video_ids is None:
            self._build_local_index()
        if self._local_video_ids is None:  # pragma: no cover — 防御
            self._local_video_ids = set()
        self._local_video_ids.add(token)

    async def _should_download(self, token: str, *, url_type: str) -> bool:
        if not self._increase_enabled(url_type):
            return True
        await self._ensure_local_index()
        if self._is_locally_downloaded(token):
            logger.info("%s already exists locally, skipping", token)
            return False
        if self.database is None or bool(self.config.get("redownload_missing_files", True)):
            return True
        try:
            if await self.database.is_downloaded(token):
                logger.info("%s in history but file missing; skipping", token)
                return False
        except Exception as exc:
            logger.warning("Download history lookup failed for %s, downloading: %s", token, exc)
        return True

    # ------------------------------------------------------------------
    # 命名、目录与产物收集
    # ------------------------------------------------------------------

    @staticmethod
    def _author_of(entry: Dict[str, Any], platform: str) -> Tuple[str, str]:
        """作者名与作者 ID。影视类站点常没有 uploader，用平台中文名兜底。"""
        name = str(
            entry.get("uploader")
            or entry.get("channel")
            or entry.get("creator")
            or entry.get("uploader_id")
            or ""
        ).strip()
        author_id = str(entry.get("uploader_id") or entry.get("channel_id") or "").strip()
        if not name:
            name = platform_display_name(platform)
        return name, author_id

    @staticmethod
    def _publish_time(entry: Dict[str, Any]) -> Tuple[Optional[int], str]:
        ts = entry.get("timestamp") or entry.get("release_timestamp")
        publish_ts: Optional[int] = None
        if ts:
            try:
                publish_ts = int(ts)
            except (TypeError, ValueError):
                publish_ts = None
        upload_date = str(entry.get("upload_date") or entry.get("release_date") or "").strip()
        publish_date = ""
        if re.fullmatch(r"\d{8}", upload_date):
            publish_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        elif publish_ts:
            try:
                publish_date = datetime.fromtimestamp(publish_ts).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError):
                publish_date = ""
        return publish_ts, publish_date

    def _build_item_context(
        self, entry: Dict[str, Any], *, platform: str, token: str
    ) -> Dict[str, Any]:
        author_name, author_id = self._author_of(entry, platform)
        title = str(entry.get("title") or "").strip() or "no_title"
        publish_ts, publish_date = self._publish_time(entry)
        if not publish_date:
            publish_date = datetime.now().strftime("%Y-%m-%d")
            logger.debug("%s missing upload_date, fallback to %s", token, publish_date)

        media_type = "audio" if self._as_bool("audio_only", False) else "video"
        context = build_aweme_context(
            aweme_id=token,
            title=title,
            author_name=author_name,
            author_sec_uid=author_id or None,
            publish_date=publish_date,
            publish_ts=publish_ts,
            media_type=media_type,
            mode=platform,
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
        author_dir_style = self.config.get("author_dir") or "nickname"
        save_dir = self.file_manager.get_save_path(
            author_name=author_name,
            mode=platform,
            aweme_title=title,
            aweme_id=token,
            folderstyle=bool(self.config.get("folderstyle", True)),
            download_date=publish_date,
            folder_name=folder_name,
            author_sec_uid=author_id or None,
            author_dir_style=author_dir_style,
            group_by_mode=bool(self.config.get("group_by_mode", True)),
        )
        return {
            "token": token,
            "title": title,
            "author_name": author_name,
            "author_id": author_id,
            "publish_ts": publish_ts,
            "publish_date": publish_date,
            "file_stem": file_stem,
            "save_dir": save_dir,
            "media_type": media_type,
        }

    @staticmethod
    def _collect_output_files(save_dir: Path, file_stem: str) -> List[Path]:
        """收集 yt-dlp 落在目录里的主媒体文件（排除临时与侧车文件）。"""
        if not save_dir.exists():
            return []
        files: List[Path] = []
        for path in sorted(save_dir.iterdir()):
            if not path.is_file() or not path.name.startswith(file_stem):
                continue
            suffix = path.suffix.lower()
            if suffix in _TEMP_SUFFIXES or suffix in _SIDECAR_SUFFIXES:
                continue
            if suffix not in _LOCAL_MEDIA_SUFFIXES:
                continue
            # 合并前的分轨中间文件形如 ``<stem>.f137.mp4``，合并成功后 yt-dlp 会删掉；
            # 若残留说明合并失败，不能当成品。
            if re.search(r"\.f\d+\.[A-Za-z0-9]+$", path.name):
                continue
            try:
                if path.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            files.append(path)
        return files

    # ------------------------------------------------------------------
    # 落库与清单
    # ------------------------------------------------------------------

    @staticmethod
    def _slim_metadata(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {key: entry.get(key) for key in _METADATA_KEYS if entry.get(key) is not None}

    async def _record_video(
        self,
        *,
        token: str,
        entry: Dict[str, Any],
        platform: str,
        files: List[Path],
        context: Dict[str, Any],
    ) -> None:
        if not files:
            return
        save_dir: Path = context["save_dir"]
        author_id = str(context.get("author_id") or "")
        thumbnail = str(entry.get("thumbnail") or "")

        if self.database:
            try:
                await self.database.add_aweme(
                    {
                        # 复用 aweme 表：aweme_id 存 ``<platform>_<id>`` token，类型前缀
                        # ``ytdlp_`` 让历史视图能把来源分开，无需建表与迁移。
                        "aweme_id": token,
                        "aweme_type": f"ytdlp_{platform}",
                        "title": context["title"],
                        "author_id": author_id,
                        "author_name": context["author_name"],
                        "author_sec_uid": author_id,
                        "create_time": context.get("publish_ts"),
                        "file_path": str(save_dir),
                        "metadata": json.dumps(self._slim_metadata(entry), ensure_ascii=False),
                        "cover_urls": json.dumps([thumbnail] if thumbnail else []),
                        "job_id": self.job_id or "",
                    },
                    author_sec_uid=author_id,
                )
            except Exception as exc:
                logger.warning("Failed to record %s into database: %s", token, exc)

        manifest_record: Dict[str, Any] = {
            "platform": platform,
            "engine": "yt-dlp",
            "date": context.get("publish_date", ""),
            "aweme_id": token,
            "source_id": str(entry.get("id") or ""),
            "author_name": context["author_name"],
            "author_sec_uid": author_id,
            "author_url": str(entry.get("uploader_url") or entry.get("channel_url") or ""),
            "desc": context["title"],
            "media_type": context.get("media_type", "video"),
            "mode": platform,
            "tags": [str(tag) for tag in (entry.get("tags") or []) if tag][:50],
            "webpage_url": str(entry.get("webpage_url") or ""),
            "file_names": [path.name for path in files],
            "file_paths": [self._to_manifest_path(path) for path in files],
        }
        if context.get("publish_ts"):
            manifest_record["publish_timestamp"] = context["publish_ts"]

        try:
            await _append_manifest(self.file_manager.base_path, manifest_record)
        except OSError as exc:
            logger.warning("Failed to append download manifest for %s: %s", token, exc)

    def _to_manifest_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.file_manager.base_path))
        except ValueError:
            return str(path)

    def _cleanup_cookie_file(self) -> None:
        path = getattr(self, "_temp_cookie_path", None)
        if path is None:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Failed to remove temp cookie file %s: %s", path, exc)
        self._temp_cookie_path = None


# ----------------------------------------------------------------------
# 模块级工具
# ----------------------------------------------------------------------


async def _append_manifest(base_path: Path, record: Dict[str, Any]) -> None:
    """追加一行下载清单。与抖音 / B 站写同一个 ``download_manifest.jsonl``。"""
    import aiofiles

    base_path.mkdir(parents=True, exist_ok=True)
    manifest_path = base_path / "download_manifest.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    async with aiofiles.open(manifest_path, "a", encoding="utf-8") as handle:
        await handle.write(line)
