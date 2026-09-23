"""FastAPI REST 服务入口 + 网页可视化控制台。

HTTP 层薄封装：
- 接收 URL，创建 job，返回 job_id
- 实际下载委托给 cli.main.download_url 的简化复用
- ``/`` 提供单文件网页控制台（``web/index.html``），覆盖下载 / 任务 /
  数据发现 / 档案 / 配置等全部能力

fastapi/uvicorn 是**可选**依赖。若未安装，导入本模块会 ImportError。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from collections import Counter, deque
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from auth import CookieManager
from bilibili import (
    BiliAPIClient,
    BiliDownloaderFactory,
    BiliLoginRequiredError,
    BiliRiskControlError,
    BiliURLParser,
    detect_platform,
)
from bilibili.factory import UNSUPPORTED_URL_TYPE_DETAIL as BILI_UNSUPPORTED_URL_TYPE_DETAIL
from bilibili.url_parser import is_bili_short_url, normalize_short_url
from config import ConfigLoader
from control import QueueManager, RateLimiter, RetryHandler
from core import UNSUPPORTED_URL_TYPE_DETAIL, DouyinAPIClient, DownloaderFactory, URLParser
from core.discovery import dump_hot_board, search_and_dump
from server.auth import (
    AuthPolicy,
    extract_presented_token,
    resolve_auth_token,
)
from server.jobs import CURRENT_JOB, DownloadJob, JobManager
from server.progress import JobProgressReporter
from storage import Database, FileManager
from utils.logger import setup_logger
from utils.validators import is_short_url
from utils.validators import normalize_short_url as normalize_douyin_short_url
from ytdlp import (
    YtdlpDownloader,
    YtdlpDownloadError,
    YtdlpMissingError,
    YtdlpURLParser,
    detect_ytdlp_platform,
    platform_display_name,
)

logger = setup_logger("REST")

try:  # 版本号在项目根的 __init__.py 里，缺失时退默认值
    from __init__ import __version__ as _VERSION
except ImportError:  # pragma: no cover - 打包/独立运行场景
    _VERSION = "2.0.3"

# 网页控制台根目录（项目根 /web）
_WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

# 允许通过网页控制台写入 config.yml 的键。刻意不含 cookies /
# transcript.api_key 等凭据，避免把密钥暴露在浏览器侧或 HTTP 日志里。
_EDITABLE_CONFIG_KEYS = (
    "path",
    "thread",
    "retry_times",
    "rate_limit",
    "proxy",
    "video",
    "cover",
    "music",
    "avatar",
    "json",
    "folderstyle",
    "filename_template",
    "folder_template",
    "author_dir",
    "group_by_mode",
    "download_pinned",
    "author_url",
    "homepage_screenshot",
    "start_time",
    "end_time",
    "mode",
    "number",
    "increase",
    "redownload_missing_files",
    "video_quality",
    "comments",
    "live",
    "transcript",
    "notifications",
    "server",
    "browser_fallback",
    "auto_cookie",
)

# 单次提交允许覆盖的配置键（不落盘，只作用于该 job）。
# ``bilibili`` / ``ytdlp`` 允许整段传入：ConfigLoader.update 对 dict 深合并，网页端
# 可以只覆盖 bilibili.number / ytdlp.quality 等子键而无需重发整段配置。
_OVERRIDE_KEYS = frozenset(
    {
        "mode",
        "number",
        "increase",
        "video",
        "cover",
        "music",
        "avatar",
        "json",
        "folderstyle",
        "download_pinned",
        "author_url",
        "homepage_screenshot",
        "start_time",
        "end_time",
        "video_quality",
        "redownload_missing_files",
        "comments",
        "live",
        "transcript",
        "bilibili",
        "ytdlp",
    }
)

# HTTP override 里 ytdlp 段不允许出现的键：extra_options 是 yt-dlp Python API
# 逃生舱（含 outtmpl / exec_cmd 等可写盘、可执行命令的参数），unsafe 开关
# 也不得经 HTTP 打开。两者只能由本机 config.yml 设置（见
# ytdlp.options_policy，即便本机也默认只放行安全参数）。
_HTTP_FORBIDDEN_YTDLP_KEYS = ("extra_options", "unsafe_extra_options")

# 抖音 App「复制链接」拿到的其实是整条分享文案，例如：
#   长按复制此条消息，打开抖音搜索，查看TA的更多作品。 https://v.douyin.com/xxxx/
# 直接把这一整行当 URL 解析必然失败（落成 Unsupported URL）。下面几个正则
# 从任意文本里抠出真正的链接：先找带 scheme 的，再找裸域名（App 里也常出现
# 不带 https:// 的 v.douyin.com/xxx），最后是 B 站裸 BV 号。
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'）)】\]】，。、；！？]+", re.IGNORECASE)
_BARE_DOUYIN_RE = re.compile(
    r"(?<![0-9A-Za-z._-])"
    r"((?:v\.|www\.|live\.)?(?:douyin|iesdouyin)\.com/[^\s<>\"'）)】\]】，。、；！？]*"
    r"|webcast\.amemv\.com/[^\s<>\"'）)】\]】，。、；！？]*)",
    re.IGNORECASE,
)
_BARE_BILI_RE = re.compile(
    r"(?<![0-9A-Za-z._-])"
    r"((?:www\.|space\.|m\.)?bilibili\.com/[^\s<>\"'）)】\]】，。、；！？]*"
    r"|b23\.tv/[^\s<>\"'）)】\]】，。、；！？]*)",
    re.IGNORECASE,
)
_BARE_BVID_RE = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")
# 快手 / 小红书等平台的分享文案同样常带不含 scheme 的裸短链。
_BARE_YTDLP_RE = re.compile(
    r"(?<![0-9A-Za-z._-])"
    r"((?:[a-z0-9-]+\.)*(?:iqiyi\.com|iq\.com|v\.qq\.com|video\.qq\.com|youku\.com|mgtv\.com"
    r"|kuaishou\.com|gifshow\.com|chenzhongtech\.com|ixigua\.com|toutiao\.com"
    r"|weibo\.com|weibo\.cn|xiaohongshu\.com|xhslink\.com)/[^\s<>\"'）)】\]】，。、；！？]*)",
    re.IGNORECASE,
)
_URL_TRAILING_JUNK = ".,;:!?)]}>）】、，。；：！？"


def extract_url_from_text(raw: str) -> str:
    """从分享文案里提取真正的链接；提取不到时原样返回，让下游给出可读错误。"""
    text = (raw or "").strip()
    if not text:
        return ""
    match = _URL_IN_TEXT_RE.search(text)
    if match:
        return match.group(0).rstrip(_URL_TRAILING_JUNK)
    match = _BARE_DOUYIN_RE.search(text)
    if match:
        return "https://" + match.group(1).rstrip(_URL_TRAILING_JUNK)
    match = _BARE_BILI_RE.search(text)
    if match:
        return "https://" + match.group(1).rstrip(_URL_TRAILING_JUNK)
    match = _BARE_YTDLP_RE.search(text)
    if match:
        return "https://" + match.group(1).rstrip(_URL_TRAILING_JUNK)
    match = _BARE_BVID_RE.search(text)
    if match:
        return match.group(1)
    return text


class DownloadRequest(BaseModel):
    url: str
    overrides: Optional[Dict[str, Any]] = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    url: str


class ConfigUpdateRequest(BaseModel):
    updates: Dict[str, Any]


class OpenFolderRequest(BaseModel):
    path: Optional[str] = None


def _launch_folder(path: str) -> None:
    """用系统文件管理器打开一个目录。

    浏览器出于安全原因无法直接打开本地文件夹，所以由本地服务端代劳——
    桌面版也是同样的做法。
    """
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _resolve_inside_root(root: Path, candidate: Optional[str]) -> Path:
    """把请求路径解析成绝对路径，并确保它落在下载根目录内。

    这个接口等价于「在用户机器上打开一个文件夹」，所以必须限制范围，
    否则它会变成一个打开任意路径的后门。
    """
    root_resolved = root.resolve()
    raw = (candidate or "").strip()
    target = root_resolved if not raw else Path(raw)
    if raw and not target.is_absolute():
        target = root_resolved / target
    target = target.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise HTTPException(status_code=403, detail="只能打开下载目录内的路径")
    return target


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _redacted_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the config safe to hand to the browser."""
    out = deepcopy(config)
    for key in ("cookies", "cookie"):
        if key not in out:
            continue
        value = out[key]
        if isinstance(value, dict):
            out[key] = {k: ("***" if v else "") for k, v in value.items()}
        elif value:
            out[key] = "***"
    # bilibili 段里的 cookie/cookies 同样是账号凭据（SESSDATA），一并脱敏。
    bilibili = out.get("bilibili")
    if isinstance(bilibili, dict):
        for key in ("cookies", "cookie"):
            value = bilibili.get(key)
            if isinstance(value, dict):
                bilibili[key] = {k: ("***" if v else "") for k, v in value.items()}
            elif value:
                bilibili[key] = "***"
    # ytdlp.cookies 是「平台 → Cookie」两层字典，逐平台脱敏；cookie_file 是本机
    # 路径，也不该暴露给浏览器。
    ytdlp = out.get("ytdlp")
    if isinstance(ytdlp, dict):
        cookies = ytdlp.get("cookies")
        if isinstance(cookies, dict):
            redacted: Dict[str, Any] = {}
            for platform, value in cookies.items():
                if isinstance(value, dict):
                    redacted[platform] = {k: ("***" if v else "") for k, v in value.items()}
                else:
                    redacted[platform] = "***" if value else ""
            ytdlp["cookies"] = redacted
        elif cookies:
            ytdlp["cookies"] = "***"
        if ytdlp.get("cookie_file"):
            ytdlp["cookie_file"] = "***"
    transcript = out.get("transcript")
    if isinstance(transcript, dict) and transcript.get("api_key"):
        transcript["api_key"] = "***"
    # server.auth_token 是 REST 认证凭据，绝不回显给浏览器。
    server_section = out.get("server")
    if isinstance(server_section, dict) and server_section.get("auth_token"):
        server_section["auth_token"] = "***"
    return out


def _write_config_file(path: str, config: Dict[str, Any], keys: tuple) -> bool:
    """Merge the given keys from ``config`` into the YAML file at ``path``.

    Mirrors ``ConfigLoader.save()`` semantics: keys the user wrote by hand are
    preserved, only the whitelisted keys are overwritten. Kept local to the
    server module so the shared ``config/config_loader.py`` stays in sync with
    the desktop sibling.
    """
    if not path:
        return False
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create config directory %s: %s", target.parent, exc)
        return False

    existing: Dict[str, Any] = {}
    if target.exists():
        try:
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Failed to read %s for merge: %s", target, exc)

    for key in keys:
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, dict):
            value = deepcopy(value)
        elif isinstance(value, list):
            value = list(value)
        existing[key] = value

    try:
        target.write_text(
            yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Failed to write config %s: %s", target, exc)
        return False
    return True


def _fork_config(base: ConfigLoader, overrides: Optional[Dict[str, Any]]) -> ConfigLoader:
    """Build a per-job config view without mutating the shared instance."""
    if not overrides:
        return base
    forked = ConfigLoader(None)
    forked.config = deepcopy(base.config)
    forked.config_path = base.config_path
    safe = {k: v for k, v in overrides.items() if k in _OVERRIDE_KEYS}
    ytdlp_section = safe.get("ytdlp")
    if isinstance(ytdlp_section, dict) and _HTTP_FORBIDDEN_YTDLP_KEYS:
        # Web API 用户不得借 overrides 间接构造任意 yt-dlp 参数（含 outtmpl /
        # exec_cmd 等可写盘、可执行命令的能力）——见 ytdlp.options_policy。
        sanitized = {k: v for k, v in ytdlp_section.items() if k not in _HTTP_FORBIDDEN_YTDLP_KEYS}
        if sanitized:
            safe["ytdlp"] = sanitized
        else:
            safe.pop("ytdlp", None)
    if safe:
        forked.update(**safe)
    return forked


class _ServerDeps:
    """跨请求复用的重量级依赖。

    REST 服务在进程生命周期内只需要一份 FileManager / RateLimiter / RetryHandler /
    QueueManager / CookieManager；每个请求重新构造既浪费又会触发文件系统 mkdir。
    DouyinAPIClient 由于持有 aiohttp.ClientSession，依旧按请求创建，避免跨请求泄漏
    连接状态或触发 "Session is closed" 错误。
    """

    def __init__(self, config: ConfigLoader):
        self.config = config
        # Resolve the cookie file path relative to the config file's directory
        # so the sidecar can find it regardless of its working directory (which
        # on macOS is often '/' when launched by Electron).
        if config.config_path:
            config_dir = Path(config.config_path).resolve().parent
            cookie_file = str(config_dir / ".cookies.json")
        else:
            config_dir = Path.cwd()
            cookie_file = ".cookies.json"
        self.cookie_manager = CookieManager(cookie_file=cookie_file)
        # Load cookies from the config (env var / YAML cookie key) first, then
        # fall back to whatever is already on disk in the cookie file. This
        # ensures that cookies saved by a previous session are picked up on
        # restart even when the config doesn't embed them inline.
        initial_cookies = config.get_cookies()
        if initial_cookies:
            self.cookie_manager.set_cookies(initial_cookies)
        else:
            # Trigger a load from disk so get_cookies() returns the persisted
            # session without requiring a fresh login on every app restart.
            self.cookie_manager.get_cookies()
        self.file_manager = FileManager(config.get("path"))
        self.rate_limiter = RateLimiter(max_per_second=float(config.get("rate_limit", 2) or 2))
        self.retry_handler = RetryHandler(max_retries=int(config.get("retry_times", 3) or 3))
        self.queue_manager = QueueManager(max_workers=int(config.get("thread", 5) or 5))

        # 可视化控制台的档案/统计页要读 SQLite。数据库默认开启，但若用户显式关掉
        # 就沿用 CLI 语义（不主动创建库文件），此时相关页面按"未启用"处理。
        self.database_enabled = bool(config.get("database"))
        raw_db_path = config.get("database_path") or "dy_downloader.db"
        db_path = Path(raw_db_path)
        if not db_path.is_absolute():
            db_path = config_dir / db_path
        self.db = Database(db_path=str(db_path))
        self._db_ready = False

    async def ensure_db(self) -> Optional[Database]:
        """Lazily open the history DB. Returns None when disabled."""
        if not self.database_enabled:
            return None
        if not self._db_ready:
            await self.db.initialize()
            self._db_ready = True
        return self.db

    def reload_runtime(self, updates: Dict[str, Any]) -> None:
        """Rebuild the shared primitives whose config key changed."""
        if "path" in updates:
            self.file_manager = FileManager(self.config.get("path"))
        if "rate_limit" in updates:
            self.rate_limiter = RateLimiter(
                max_per_second=float(self.config.get("rate_limit", 2) or 2)
            )
        if "retry_times" in updates:
            self.retry_handler = RetryHandler(
                max_retries=int(self.config.get("retry_times", 3) or 3)
            )
        if "thread" in updates:
            workers = int(self.config.get("thread", 5) or 5)
            self.queue_manager = QueueManager(max_workers=workers)

    def cookie_status(self) -> Dict[str, Any]:
        try:
            cookies = self.cookie_manager.get_cookies() or {}
        except Exception as exc:  # noqa: BLE001 - 状态查询不应 500
            logger.warning("cookie status read failed: %s", exc)
            cookies = {}
        required = ("ttwid", "odin_tt", "passport_csrf_token")
        missing = [k for k in required if not cookies.get(k)]
        try:
            valid = bool(self.cookie_manager.validate_cookies())
        except Exception:  # noqa: BLE001
            valid = False
        return {
            "configured": bool(cookies),
            "count": len(cookies),
            "keys": sorted(cookies.keys()),
            "missing_required": missing,
            "valid": valid,
        }


class _PausableJobLimiter:
    """按 job 可暂停的限速器包装。

    下载循环在每次 API 请求前都会 ``await rate_limiter.acquire()``，所以这里
    是最合适的协作式暂停点：先等本 job 的恢复闸门，再走全局限速器。全局
    限速仍由共享的 RateLimiter 兜底，不会因为每个并发 job 各持一个限速器
    而把请求速率放大成 N 倍。
    """

    def __init__(self, shared: RateLimiter, job: "DownloadJob"):
        self._shared = shared
        self._job = job

    async def acquire(self) -> None:
        await self._job.wait_if_paused()
        await self._shared.acquire()


async def _execute_download(
    url: str,
    deps: "_ServerDeps",
    overrides: Optional[Dict[str, Any]] = None,
    job: Optional["DownloadJob"] = None,
) -> Dict[str, int]:
    """简化版 download_url：只负责执行并返回成功/失败计数。

    有意不复用 cli.main.download_url —— 后者绑定了 progress_display 的 rich 状态。
    API client 仍按请求创建（aiohttp session 不跨请求复用）；其余重量级依赖从
    _ServerDeps 共享。``overrides`` 给网页控制台用来给单个链接挂独立下载范围；
    ``job`` 用来把实时进度、暂停闸门与 job_id 接到本次执行上。
    """
    config = _fork_config(deps.config, overrides)
    database = await deps.ensure_db()
    reporter = JobProgressReporter(job) if job is not None else None
    limiter: Any = deps.rate_limiter if job is None else _PausableJobLimiter(deps.rate_limiter, job)

    # 平台分流：B 站的 API 客户端、URL 解析与下载器都和抖音不通用，必须在建
    # 任何客户端之前决定走哪条链路（与 cli.main.download_url 的分流一致）。
    if detect_platform(url) == "bilibili":
        return await _execute_bilibili_download(
            url, deps, config, database, limiter, reporter, job
        )
    # 爱奇艺 / 腾讯视频 / 优酷等其他平台走 yt-dlp 引擎。
    if detect_ytdlp_platform(url) is not None:
        return await _execute_ytdlp_download(url, deps, config, database, limiter, reporter, job)

    # 视频号链接不可直链下载（网页版要微信登录态）：给出嗅探模式引导。
    from channels.url_parser import CHANNELS_URL_HINT, is_channels_url

    if is_channels_url(url):
        raise RuntimeError(CHANNELS_URL_HINT)

    # proxy 与 cli.main.download_url 对齐:API 请求、短链解析和 CDN 媒体
    # 下载(downloader_base 读 api_client.proxy)统一走配置代理。
    async with DouyinAPIClient(
        deps.cookie_manager.get_cookies(),
        proxy=config.get("proxy"),
    ) as api_client:
        if is_short_url(url):
            resolved = await api_client.resolve_short_url(normalize_douyin_short_url(url))
            if not resolved:
                raise RuntimeError(f"Failed to resolve short URL: {url}")
            url = resolved

        parsed = URLParser.parse(url)
        if not parsed:
            raise RuntimeError(f"Unsupported URL: {url}")
        # 能力门禁：解析得出来但永远不会有下载器的类型，给出真实原因。
        gated_detail = UNSUPPORTED_URL_TYPE_DETAIL.get(str(parsed.get("type") or ""))
        if gated_detail:
            raise RuntimeError(gated_detail)

        downloader = DownloaderFactory.create(
            parsed["type"],
            config,
            api_client,
            deps.file_manager,
            deps.cookie_manager,
            database,
            limiter,
            deps.retry_handler,
            deps.queue_manager,
            progress_reporter=reporter,
            job_id=job.job_id if job is not None else None,
        )
        if downloader is None:
            raise RuntimeError(f"No downloader for url_type={parsed['type']}")

        result = await downloader.download(parsed)
        return {
            "total": result.total,
            "success": result.success,
            "failed": result.failed,
            "skipped": result.skipped,
        }


_SNAPSHOT_EXCLUDED_KEYS = ("cookies", "cookie", "transcript")
_PLATFORM_SECRET_KEYS = {
    "bilibili": ("cookie", "cookies"),
    "ytdlp": ("cookie", "cookies", "cookie_file"),
}


def _config_snapshot(config: ConfigLoader) -> str:
    """历史库配置快照：所有平台的账号凭据绝不入库（与 cli.main._config_snapshot 对齐）。"""
    safe_config = {
        k: v
        for k, v in config.config.items()
        if k not in _SNAPSHOT_EXCLUDED_KEYS and k not in _PLATFORM_SECRET_KEYS
    }
    for section_name, secret_keys in _PLATFORM_SECRET_KEYS.items():
        section = deepcopy(config.get(section_name))
        snapshot = section if isinstance(section, dict) else {}
        for key in secret_keys:
            snapshot.pop(key, None)
        safe_config[section_name] = snapshot
    return json.dumps(safe_config, ensure_ascii=False)


def _bili_config_snapshot(config: ConfigLoader) -> str:
    """B 站任务的历史库配置快照：凭据（cookie/cookies）绝不入库。"""
    return _config_snapshot(config)


async def _execute_bilibili_download(
    url: str,
    deps: "_ServerDeps",
    config: ConfigLoader,
    database: Optional[Database],
    limiter: Any,
    reporter: Optional[JobProgressReporter],
    job: Optional["DownloadJob"],
) -> Dict[str, int]:
    """B 站链路：短链展开 → 解析 → 门禁 → 下载器 → 历史落库。

    与 cli.main.download_bilibili_url 对齐；差别在报错方式——CLI 打印到终端，
    这里抛 RuntimeError 落到 job.error，网页任务卡片才能显示可操作的原因。
    """
    if not config.get_bilibili_enabled():
        raise RuntimeError(
            "哔哩哔哩下载已在配置中关闭（bilibili.enabled: false）。"
            "改为 true 或删除该配置项后重试。"
        )

    original_url = url
    section = config.get("bilibili") if isinstance(config.get("bilibili"), dict) else {}
    cookies = config.get_bilibili_cookies()

    async with BiliAPIClient(
        cookies,
        proxy=config.get("proxy"),
        request_interval=float(section.get("request_interval", 0.5) or 0),
    ) as api_client:
        if reporter:
            reporter.update_step("解析链接", "检查 B 站短链并解析 URL")
        if is_bili_short_url(url):
            resolved = await api_client.resolve_short_url(normalize_short_url(url))
            if not resolved:
                raise RuntimeError(f"B 站短链展开失败：{url}")
            url = resolved

        parsed = BiliURLParser.parse(url)
        if not parsed:
            raise RuntimeError(f"无法解析为受支持的 B 站链接：{url}")

        gated_detail = BILI_UNSUPPORTED_URL_TYPE_DETAIL.get(str(parsed.get("type") or ""))
        if gated_detail:
            raise RuntimeError(gated_detail)

        # 登录态探测：只在「配了 Cookie」或「该类型必须要登录」时做一次，
        # 把「拿不到高清 / 收藏夹不可用」在下载开始前讲清楚。
        needs_login = parsed.get("type") == "favlist"
        if cookies or needs_login:
            await api_client.ensure_wbi_keys()
        if needs_login and api_client.is_login is not True:
            raise RuntimeError(
                "收藏夹接口需要登录：请在 config.yml 的 bilibili.cookies 中填入 SESSDATA。"
            )
        if not cookies:
            logger.info("bilibili cookies not configured; quality capped at 480P")

        if reporter:
            reporter.update_step("创建下载器", f"B 站 · {parsed['type']}")
        downloader = BiliDownloaderFactory.create(
            parsed["type"],
            config=config,
            api_client=api_client,
            file_manager=deps.file_manager,
            database=database,
            rate_limiter=limiter,
            retry_handler=deps.retry_handler,
            queue_manager=deps.queue_manager,
            progress_reporter=reporter,
            job_id=job.job_id if job is not None else None,
        )
        if downloader is None:
            raise RuntimeError(f"No downloader for url_type=bilibili:{parsed['type']}")

        try:
            result = await downloader.download(parsed)
        except BiliLoginRequiredError as exc:
            raise RuntimeError(
                f"该链接需要登录态（{exc.message or '账号未登录'}）。"
                "请在 config.yml 的 bilibili.cookies 中填入 SESSDATA 后重试。"
            ) from exc
        except BiliRiskControlError as exc:
            raise RuntimeError(
                f"被 B 站风控拦截（{exc.message or exc.code}）。"
                "可尝试调大 bilibili.request_interval、调小 thread，或稍后重试。"
            ) from exc

    if database:
        try:
            await database.add_history(
                {
                    "url": original_url,
                    "url_type": f"bilibili:{parsed['type']}",
                    "total_count": result.total,
                    "success_count": result.success,
                    "config": _bili_config_snapshot(config),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败不吞下载结果
            logger.warning("Failed to record bilibili history for %s: %s", original_url, exc)

    return {
        "total": result.total,
        "success": result.success,
        "failed": result.failed,
        "skipped": result.skipped,
    }


def _ytdlp_error_message(error: YtdlpDownloadError, platform_name: str) -> str:
    """按错误类别组织网页任务卡片上的可操作文案（与 cli.main.ytdlp_error_hint 对齐）。"""
    hints = {
        "drm": (
            f"该内容受 {platform_name} 的 DRM 保护（通常是 VIP 专享影视），"
            "任何下载工具都无法直接获取，配置会员 Cookie 也不行。"
        ),
        "login": (
            f"该内容需要登录态或会员权限。请在 config.yml 的 ytdlp.cookies 中"
            f"填入 {platform_name} 的 Cookie 后重试；若已是会员仍失败，说明内容受 DRM 保护。"
        ),
        "geo": "该内容有地区限制。可尝试配置 proxy，或在 ytdlp.extra_options 里开启 geo_bypass。",
        "unsupported": (
            f"yt-dlp 暂不支持该 {platform_name} 链接形态，或站方近期改版导致解析器失效。"
            "请先 `pip install -U yt-dlp` 更新到最新版再试；若仍失败说明上游尚未修复。"
        ),
        "phantomjs": (
            f"{platform_name} 的这个站点需要 PhantomJS 执行页面脚本。"
            "从 https://phantomjs.org/download.html 下载后把可执行文件放到 PATH 再重试。"
        ),
    }
    hint = hints.get(error.kind, "下载失败，可先 `pip install -U yt-dlp` 更新解析器后重试。")
    return f"{hint}（yt-dlp: {error.message}）"


async def _execute_ytdlp_download(
    url: str,
    deps: "_ServerDeps",
    config: ConfigLoader,
    database: Optional[Database],
    limiter: Any,
    reporter: Optional[JobProgressReporter],
    job: Optional["DownloadJob"],
) -> Dict[str, int]:
    """yt-dlp 平台链路：平台门禁 → 解析 → 下载器 → 历史落库。

    与 cli.main.download_ytdlp_url 对齐；报错抛 RuntimeError 落到 job.error，
    网页任务卡片才能显示可操作的原因。
    """
    parsed = YtdlpURLParser.parse(url)
    if not parsed:
        raise RuntimeError(f"无法识别为受支持的平台链接：{url}")
    platform = parsed["platform"]
    name = parsed.get("platform_name") or platform_display_name(platform)

    if not config.get_ytdlp_enabled():
        raise RuntimeError(
            "其他平台下载已在配置中关闭（ytdlp.enabled: false）。改为 true 或删除该配置项后重试。"
        )
    if not config.get_ytdlp_platform_enabled(platform):
        raise RuntimeError(
            f"{name} 下载已在配置中关闭（ytdlp.platforms.{platform}: false）。改为 true 后重试。"
        )

    original_url = url
    if reporter:
        reporter.update_step("解析链接", f"{name} · yt-dlp 引擎")
    if not config.get_ytdlp_cookies(platform):
        logger.info("%s cookies not configured; only free content is downloadable", platform)

    downloader = YtdlpDownloader(
        config=config,
        file_manager=deps.file_manager,
        database=database,
        rate_limiter=limiter,
        retry_handler=deps.retry_handler,
        queue_manager=deps.queue_manager,
        progress_reporter=reporter,
        job_id=job.job_id if job is not None else None,
    )

    try:
        result = await downloader.download(parsed)
    except YtdlpMissingError as exc:
        raise RuntimeError(str(exc)) from exc
    except YtdlpDownloadError as exc:
        raise RuntimeError(_ytdlp_error_message(exc, name)) from exc

    # 整条链接全部失败时把分类提示抛成 job.error：逐条报错只在日志里，网页
    # 任务卡片上需要一句能行动的解释。
    if result.total and result.success == 0 and downloader.last_error is not None:
        raise RuntimeError(_ytdlp_error_message(downloader.last_error, name))

    if database:
        try:
            await database.add_history(
                {
                    "url": original_url,
                    "url_type": f"ytdlp:{platform}:{parsed['type']}",
                    "total_count": result.total,
                    "success_count": result.success,
                    "config": _config_snapshot(config),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败不吞下载结果
            logger.warning("Failed to record ytdlp history for %s: %s", original_url, exc)

    return {
        "total": result.total,
        "success": result.success,
        "failed": result.failed,
        "skipped": result.skipped,
    }


def _read_manifest_tail(path: Path, limit: int) -> List[Dict[str, Any]]:
    """Read the last ``limit`` JSON lines of a manifest file (sync helper)."""
    tail: deque = deque(maxlen=limit)
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                tail.append(line)
    items: List[Dict[str, Any]] = []
    for line in reversed(tail):
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    return items


def build_app(config: ConfigLoader) -> FastAPI:
    deps = _ServerDeps(config)

    async def executor(url: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
        # CURRENT_JOB 由 JobManager._run 在调度本次执行前 set；进度上报、
        # 暂停闸门与 job_id 都从这里取的 job 拿。测试里把 executor 换成假的
        # 单参函数时不会走到这里。
        return await _execute_download(url, deps, overrides, CURRENT_JOB.get())

    server_cfg = config.get("server") or {}
    if not isinstance(server_cfg, dict):
        server_cfg = {}
    manager = JobManager(
        executor=executor,
        max_concurrency=int(config.get("thread", 2) or 2),
        max_jobs=int(server_cfg.get("max_jobs") or JobManager.DEFAULT_MAX_JOBS),
        job_ttl_seconds=float(
            server_cfg.get("job_ttl_seconds") or JobManager.DEFAULT_JOB_TTL_SECONDS
        ),
    )

    from server.channels import ChannelsSessionError, ChannelsSessionManager

    channels_sessions = ChannelsSessionManager(config, deps.file_manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await manager.shutdown()
        # 视频号嗅探会话兜底停止（还原系统代理；未运行时幂等）。
        try:
            await channels_sessions.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止视频号嗅探会话异常: %s", exc)
        await deps.db.close()

    app = FastAPI(
        title="Douyin Bili Downloader API",
        version=_VERSION,
        description=(
            "REST API for the multi-platform video downloader "
            "(Douyin / Bilibili / WeChat Channels / yt-dlp engines)."
        ),
        lifespan=lifespan,
    )
    app.state.job_manager = manager
    app.state.deps = deps

    # ------------------------------------------------------------------
    # 远程访问边界（server/auth.py）
    # ------------------------------------------------------------------
    # 环回客户端不受影响；非环回客户端必须携带 token（未配置 token 的
    # 服务端对非环回一律 403）。覆盖全部 /api/*（含 channels、config、
    # download、任务控制），仅 health 保持公开。
    auth_policy = AuthPolicy(resolve_auth_token(config))
    app.state.auth_policy = auth_policy
    _AUTH_PUBLIC_PATHS = frozenset({"/api/v1/health"})

    @app.middleware("http")
    async def api_auth_boundary(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in _AUTH_PUBLIC_PATHS:
            client_host = request.client.host if request.client else ""
            ok, status, detail = auth_policy.check(
                client_host, extract_presented_token(request.headers)
            )
            if not ok:
                return JSONResponse({"detail": detail}, status_code=status)
        return await call_next(request)

    # ------------------------------------------------------------------
    # 网页可视化控制台
    # ------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def dashboard() -> Response:
        index = _WEB_ROOT / "index.html"
        if not index.exists():
            raise HTTPException(
                status_code=404,
                detail=f"web/index.html not found at {index}. 请在项目根目录运行 --serve。",
            )
        return FileResponse(str(index), media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # 健康检查 / 任务
    # ------------------------------------------------------------------

    @app.get("/api/v1/health")
    async def health() -> Dict[str, Any]:
        # 唯一公开的 API 端点：只含非敏感的策略摘要（是否要求 token）。
        return {"status": "ok", "version": _VERSION, **auth_policy.describe()}

    @app.post("/api/v1/download", response_model=JobResponse)
    async def create_job(req: DownloadRequest) -> JobResponse:
        if not req.url:
            raise HTTPException(status_code=400, detail="url is required")
        # 允许直接粘贴 App 分享文案：先把真正的链接抠出来再进下载流程。
        clean_url = extract_url_from_text(req.url)
        job = await manager.submit(clean_url, overrides=req.overrides)
        return JobResponse(job_id=job.job_id, status=job.status, url=clean_url)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> Dict[str, Any]:
        job = await manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    @app.get("/api/v1/jobs")
    async def list_jobs() -> Dict[str, List[Dict[str, Any]]]:
        jobs = await manager.list_jobs()
        return {"jobs": [j.to_dict() for j in jobs]}

    @app.post("/api/v1/jobs/{job_id}/retry")
    async def retry_job(job_id: str) -> JobResponse:
        job = await manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        new_job = await manager.submit(job.url, overrides=job.overrides)
        return JobResponse(job_id=new_job.job_id, status=new_job.status, url=new_job.url)

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> Dict[str, Any]:
        """取消排队中或下载中的任务（已下载的文件保留）。"""
        job = await manager.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    @app.post("/api/v1/jobs/{job_id}/pause")
    async def pause_job(job_id: str) -> Dict[str, Any]:
        """暂停任务：当前作品下载完后、下一次请求之前停下。"""
        job = await manager.set_paused(job_id, True)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    @app.post("/api/v1/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> Dict[str, Any]:
        job = await manager.set_paused(job_id, False)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.to_dict()

    @app.delete("/api/v1/jobs/{job_id}")
    async def delete_job(job_id: str) -> Dict[str, Any]:
        """删除一条任务记录（活跃的先取消）。磁盘上已下载的文件不受影响。"""
        if not await manager.remove(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        return {"removed": job_id}

    @app.delete("/api/v1/jobs")
    async def clear_jobs(
        include_active: bool = Query(False, description="是否连排队中/下载中的任务一起清理"),
    ) -> Dict[str, int]:
        """批量清理任务。默认只清理终态（成功/失败/已取消）的记录。"""
        return {"removed": await manager.clear_finished(include_active=include_active)}

    # ------------------------------------------------------------------
    # 配置中心
    # ------------------------------------------------------------------

    @app.get("/api/v1/config")
    async def read_config() -> Dict[str, Any]:
        return {
            "config": _redacted_config(deps.config.config),
            "config_path": deps.config.config_path,
            "editable_keys": list(_EDITABLE_CONFIG_KEYS),
            "override_keys": sorted(_OVERRIDE_KEYS),
            "cookies_redacted": True,
        }

    @app.put("/api/v1/config")
    async def update_config(req: ConfigUpdateRequest) -> Dict[str, Any]:
        updates = {k: v for k, v in (req.updates or {}).items() if k in _EDITABLE_CONFIG_KEYS}
        # auth_token 是 REST 认证凭据：不得经 HTTP 写入，否则远程匿名请求
        # 可以先自设 token 再通过认证（自签发后门）。
        server_section = updates.get("server")
        if isinstance(server_section, dict) and "auth_token" in server_section:
            server_section.pop("auth_token", None)
            if not server_section:
                updates.pop("server", None)
        if not updates:
            raise HTTPException(
                status_code=400,
                detail="no editable keys provided (credentials are never writable via HTTP)",
            )
        deps.config.update(**updates)
        deps.reload_runtime(updates)
        if "thread" in updates:
            manager.set_max_concurrency(int(deps.config.get("thread", 2) or 2))
        saved = _write_config_file(
            deps.config.config_path or "", deps.config.config, _EDITABLE_CONFIG_KEYS
        )
        return {
            "saved": saved,
            "config_path": deps.config.config_path,
            "applied_keys": sorted(updates.keys()),
            "note": None if saved else "未写入磁盘（未指定 config_path 或写入失败），仅本次进程内生效",
        }

    @app.get("/api/v1/cookies/status")
    async def cookies_status() -> Dict[str, Any]:
        return deps.cookie_status()

    # ------------------------------------------------------------------
    # 总览 / 统计
    # ------------------------------------------------------------------

    @app.get("/api/v1/stats")
    async def stats() -> Dict[str, Any]:
        jobs = await manager.list_jobs()
        by_status = Counter(j.status for j in jobs)
        db = await deps.ensure_db()

        archive: Dict[str, Any] = {"enabled": db is not None, "total": 0}
        top_authors: List[Dict[str, Any]] = []
        if db is not None:
            try:
                history = await db.get_aweme_history(page=1, size=1)
                archive["total"] = int(history.get("total", 0))
            except Exception as exc:  # noqa: BLE001
                logger.warning("stats archive count failed: %s", exc)
            try:
                top_authors = await db.get_top_authors(days=30, limit=8)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stats top authors failed: %s", exc)

        return {
            "version": _VERSION,
            "download_dir": str(deps.config.get("path") or "./Downloaded/"),
            "download_dir_abs": str(Path(deps.config.get("path") or "./Downloaded/").resolve()),
            "config_path": deps.config.config_path,
            "cookies": deps.cookie_status(),
            "jobs": {
                "total": len(jobs),
                "pending": by_status.get("pending", 0),
                "running": by_status.get("running", 0),
                "success": by_status.get("success", 0),
                "failed": by_status.get("failed", 0),
            },
            "archive": archive,
            "top_authors": top_authors,
            "runtime": {
                "thread": int(deps.config.get("thread", 5) or 5),
                "rate_limit": float(deps.config.get("rate_limit", 2) or 2),
                "retry_times": int(deps.config.get("retry_times", 3) or 3),
                "video_quality": deps.config.get("video_quality", "highest"),
                "mode": deps.config.get("mode") or [],
                "proxy": bool(deps.config.get("proxy")),
            },
        }

    # ------------------------------------------------------------------
    # 下载档案 / 清单
    # ------------------------------------------------------------------

    @app.get("/api/v1/history")
    async def history(
        page: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=200),
        author: str = "",
        title: str = "",
        aweme_type: str = "",
    ) -> Dict[str, Any]:
        db = await deps.ensure_db()
        if db is None:
            return {
                "enabled": False,
                "total": 0,
                "page": page,
                "size": size,
                "items": [],
                "note": "数据库未启用（config 中 database: false）",
            }
        result = await db.get_aweme_history(
            page=page,
            size=size,
            author=author or None,
            title=title or None,
            aweme_type=aweme_type or None,
        )
        return {"enabled": True, **result}

    @app.get("/api/v1/manifest")
    async def manifest(limit: int = Query(50, ge=1, le=1000)) -> Dict[str, Any]:
        base = Path(deps.config.get("path") or "./Downloaded/")
        manifest_path = base / "download_manifest.jsonl"
        if not manifest_path.exists():
            return {"found": False, "path": str(manifest_path), "items": []}
        loop = asyncio.get_running_loop()
        try:
            items = await loop.run_in_executor(None, _read_manifest_tail, manifest_path, limit)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"读取清单失败：{exc}") from exc
        return {"found": True, "path": str(manifest_path), "items": items}

    @app.get("/api/v1/download-dir")
    async def download_dir() -> Dict[str, Any]:
        """返回下载根目录的绝对路径与是否存在（供前端「打开/复制」用）。"""
        root = Path(deps.config.get("path") or "./Downloaded/")
        resolved = root.resolve()
        return {
            "download_dir": str(resolved),
            "configured": str(deps.config.get("path") or "./Downloaded/"),
            "exists": resolved.exists(),
            "platform": sys.platform,
        }

    @app.post("/api/v1/open-folder")
    async def open_folder(req: OpenFolderRequest) -> Dict[str, Any]:
        """在系统文件管理器里打开下载目录（或它下面的某个子目录）。

        只允许下载根目录及其子目录；目录尚未创建时退到最近的已存在祖先，
        避免因为「任务失败所以没生成目录」而报错打断用户。
        """
        root = Path(deps.config.get("path") or "./Downloaded/")
        try:
            target = _resolve_inside_root(root, req.path)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"路径无效：{exc}") from exc

        if not target.exists():
            fallback = target
            while not fallback.exists() and fallback.parent != fallback:
                fallback = fallback.parent
            target = fallback

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _launch_folder, str(target))
        except Exception as exc:  # noqa: BLE001 - 无 GUI/无 explorer 时降级提示
            raise HTTPException(
                status_code=501,
                detail=(
                    f"当前环境无法自动打开文件夹（{type(exc).__name__}）：{exc}。"
                    "请改用「复制路径」。"
                ),
            ) from exc
        return {"opened": str(target)}

    # ------------------------------------------------------------------
    # 数据发现：热搜榜 / 关键词搜索
    # ------------------------------------------------------------------

    @app.get("/api/v1/discovery/hot-board")
    async def hot_board(limit: int = Query(30, ge=0, le=200)) -> Dict[str, Any]:
        output_dir = Path(deps.config.get("path") or "./Downloaded/")
        try:
            async with DouyinAPIClient(
                deps.cookie_manager.get_cookies(), proxy=deps.config.get("proxy")
            ) as api_client:
                result = await dump_hot_board(api_client, output_dir, limit=int(limit))
        except Exception as exc:  # noqa: BLE001 - 网络/风控失败统一转 502
            raise HTTPException(
                status_code=502, detail=f"热搜榜获取失败：{type(exc).__name__}: {exc}"
            ) from exc
        return {"count": result["count"], "path": result["path"], "items": result["items"]}

    @app.get("/api/v1/discovery/search")
    async def discovery_search(
        keyword: str = Query(..., min_length=1),
        max_items: int = Query(50, ge=1, le=500),
    ) -> Dict[str, Any]:
        output_dir = Path(deps.config.get("path") or "./Downloaded/")
        try:
            async with DouyinAPIClient(
                deps.cookie_manager.get_cookies(), proxy=deps.config.get("proxy")
            ) as api_client:
                result = await search_and_dump(
                    api_client, keyword, output_dir, max_items=int(max_items)
                )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"搜索失败：{type(exc).__name__}: {exc}"
            ) from exc
        return {
            "keyword": result["keyword"],
            "count": result["count"],
            "path": result["path"],
            "items": result["items"],
        }

    # ------------------------------------------------------------------
    # 微信视频号嗅探会话（channels/ 包；网页控制台「视频号」页使用）
    # ------------------------------------------------------------------

    @app.get("/api/v1/channels/status")
    async def channels_status() -> Dict[str, Any]:
        return channels_sessions.status()

    @app.get("/api/v1/channels/certificate")
    async def channels_certificate() -> Dict[str, Any]:
        return channels_sessions.certificate()

    @app.post("/api/v1/channels/certificate/install")
    async def channels_certificate_install() -> Dict[str, Any]:
        try:
            return await channels_sessions.install_certificate()
        except ChannelsSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/channels/certificate/uninstall")
    async def channels_certificate_uninstall() -> Dict[str, Any]:
        """卸载嗅探根证书（仅删当前用户 Root 存储中本机 CA 指纹的那张）。"""
        try:
            return await channels_sessions.uninstall_certificate()
        except ChannelsSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/channels/start")
    async def channels_start(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        try:
            return await channels_sessions.start(
                port=body.get("port"),
                auto_download=body.get("auto_download"),
                database=await deps.ensure_db(),
            )
        except ChannelsSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/channels/stop")
    async def channels_stop() -> Dict[str, Any]:
        return await channels_sessions.stop()

    @app.get("/api/v1/channels/feeds")
    async def channels_feeds(limit: int = Query(200, ge=1, le=2000)) -> Dict[str, Any]:
        return channels_sessions.feeds(limit=limit)

    @app.post("/api/v1/channels/feeds/{feed_id}/download")
    async def channels_feed_download(feed_id: str) -> Dict[str, Any]:
        try:
            return await channels_sessions.download_feed(feed_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="feed not found") from None
        except ChannelsSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/channels/auto-download")
    async def channels_auto_download(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled (bool) is required")
        try:
            return await channels_sessions.set_auto_download(enabled)
        except ChannelsSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/channels/network/repair")
    async def channels_network_repair() -> Dict[str, Any]:
        """恢复上次异常退出残留的系统代理（幂等；不动用户手动改过的设置）。"""
        return channels_sessions.repair_network()

    @app.post("/api/v1/channels/link/parse")
    async def channels_link_parse(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """解析视频号分享链接（不启动会话；返回识别结果供前端确认）。"""
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=400, detail="url (str) is required")
        try:
            return channels_sessions.parse_link(url)
        except ChannelsSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/channels/link")
    async def channels_link_start(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """分享链接下载：识别链接并按需启动嗅探会话（强制自动下载）。

        用户随后在微信里打开该链接；预览页数据经注入脚本捕获后自动下载。
        """
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=400, detail="url (str) is required")
        try:
            return await channels_sessions.start_link_session(url)
        except ChannelsSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/v1/channels/link")
    async def channels_link_clear() -> Dict[str, Any]:
        """清除待处理的分享链接（停止链接模式引导）。"""
        return channels_sessions.clear_link()

    return app


async def run_server(config: ConfigLoader, *, host: str, port: int) -> None:
    import uvicorn

    app = build_app(config)
    logger.info("网页控制台: http://%s:%s/", host, port)
    if not (host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")):
        token = resolve_auth_token(config)
        if token:
            logger.warning(
                "REST 服务监听在非环回地址 %s：远程请求必须携带 X-Auth-Token 认证头。", host
            )
        else:
            logger.warning(
                "REST 服务监听在非环回地址 %s 且未配置认证 token："
                "非本机客户端对 /api/* 的请求将被一律拒绝（403）。"
                "如需远程访问，请设置 server.auth_token 或环境变量 DOWNLOADER_API_TOKEN。"
                "不建议把 REST 服务暴露到公网。",
                host,
            )
    uv_config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(uv_config)
    await server.serve()
