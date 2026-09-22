import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

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
from bilibili.url_parser import (
    is_bili_short_url,
)
from bilibili.url_parser import (
    normalize_short_url as normalize_bili_short_url,
)
from cli.login_flow import can_interactive_login, interactive_relogin
from cli.progress_display import ProgressDisplay
from config import ConfigLoader
from control import QueueManager, RateLimiter, RetryHandler
from core import (
    UNSUPPORTED_URL_TYPE_DETAIL,
    DouyinAPIClient,
    DownloaderFactory,
    LoginRequiredError,
    URLParser,
)
from storage import Database, FileManager
from utils.logger import set_console_log_level, setup_logger
from utils.notifier import build_notifier
from utils.validators import is_short_url, normalize_short_url
from ytdlp import (
    YtdlpDownloader,
    YtdlpDownloadError,
    YtdlpMissingError,
    YtdlpURLParser,
    detect_ytdlp_platform,
    platform_display_name,
)

logger = setup_logger("CLI")
display = ProgressDisplay()

# 历史库配置快照里绝不能出现的顶层键（抖音凭据与转写密钥）。
_SNAPSHOT_EXCLUDED_KEYS = ("cookies", "cookie", "transcript")
# 各平台段落里属于账号凭据的键：段落其余部分（画质 / 数量上限等）保留，便于
# 回看当时的下载参数。
_PLATFORM_SECRET_KEYS = {
    "bilibili": ("cookie", "cookies"),
    "ytdlp": ("cookie", "cookies", "cookie_file"),
}


def _config_snapshot(config: ConfigLoader) -> str:
    """历史库配置快照：所有平台的账号凭据绝不入库。

    三条下载链路（抖音 / B 站 / yt-dlp）共用：哪怕当前任务只是抖音链接，配置
    里其他平台的 Cookie 也不能跟着快照进数据库。
    """
    safe_config = {
        k: v
        for k, v in config.config.items()
        if k not in _SNAPSHOT_EXCLUDED_KEYS and k not in _PLATFORM_SECRET_KEYS
    }
    for section_name, secret_keys in _PLATFORM_SECRET_KEYS.items():
        section = json.loads(json.dumps(config.get(section_name) or {}))
        if isinstance(section, dict):
            for key in secret_keys:
                section.pop(key, None)
        safe_config[section_name] = section
    return json.dumps(safe_config, ensure_ascii=False)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


async def _run_with_relogin(make_coro, cookie_manager, *, serve=False):
    """Run make_coro(); on LoginRequiredError, relogin once and retry.

    make_coro is a zero-arg callable returning a fresh coroutine each call,
    so the retry re-creates its own DouyinAPIClient with refreshed cookies.
    Refreshed cookies propagate through ``cookie_manager`` as a clean replace
    (not a merge), and both call sites read their cookies from it on retry.
    """
    for attempt in range(2):
        try:
            return await make_coro()
        except LoginRequiredError as exc:
            interactive = can_interactive_login(serve=serve)
            if attempt == 1 or not interactive:
                display.print_error(
                    f"登录态失效，需要重新登录（status {exc.status_code}）："
                    f"{exc.status_msg or '请先登录'}。"
                )
                if not interactive:
                    display.print_warning(
                        "当前为非交互环境，未自动打开浏览器。请手动更新 "
                        "config/cookies.json（或运行 python tools/cookie_fetcher.py 登录）。"
                    )
                raise
            display.print_warning(
                f"检测到未登录（status {exc.status_code}），开始重新登录…"
            )
            new_cookies = await interactive_relogin()
            if not new_cookies:
                display.print_error("重新登录未完成，已中止。")
                raise
            cookie_manager.set_cookies(new_cookies)
            display.print_success("已更新登录态，正在重试…")


async def download_url(
    url: str,
    config: ConfigLoader,
    cookie_manager: CookieManager,
    database: Database = None,
    progress_reporter: ProgressDisplay = None,
):
    if progress_reporter:
        progress_reporter.advance_step("初始化", "创建下载组件")
    file_manager = FileManager(config.get("path"))
    rate_limiter = RateLimiter(max_per_second=float(config.get("rate_limit", 2) or 2))
    retry_handler = RetryHandler(max_retries=config.get("retry_times", 3))
    queue_manager = QueueManager(max_workers=int(config.get("thread", 5) or 5))

    # 平台分流：抖音与 B 站的 API 客户端、URL 解析、下载器三者都不可互换，
    # 必须在建任何客户端之前决定走哪条链路。识别不出平台时按抖音处理，
    # 让抖音侧的解析器给出「不支持的链接」而不是在这里静默丢弃。
    if detect_platform(url) == "bilibili":
        return await download_bilibili_url(
            url,
            config,
            file_manager,
            rate_limiter,
            retry_handler,
            queue_manager,
            database=database,
            progress_reporter=progress_reporter,
        )

    # 视频号链接不可直链下载（网页版要微信登录态）：给出嗅探模式引导，
    # 而不是掉进抖音兜底报「不支持的链接」。
    from channels.url_parser import CHANNELS_URL_HINT, is_channels_url

    if is_channels_url(url):
        if progress_reporter:
            progress_reporter.update_step("解析链接", "视频号链接需嗅探模式")
        display.print_warning(CHANNELS_URL_HINT)
        return None

    # 爱奇艺 / 腾讯视频 / 优酷等其他平台走 yt-dlp 引擎；域名与前两者互不重叠。
    if detect_ytdlp_platform(url) is not None:
        return await download_ytdlp_url(
            url,
            config,
            file_manager,
            rate_limiter,
            retry_handler,
            queue_manager,
            database=database,
            progress_reporter=progress_reporter,
        )

    original_url = url

    async with DouyinAPIClient(
        cookie_manager.get_cookies(),
        proxy=config.get("proxy"),
    ) as api_client:
        if progress_reporter:
            progress_reporter.advance_step("解析链接", "检查短链并解析 URL")
        # 支持多种短链变体：v.douyin.com / v.iesdouyin.com / 无 scheme 的裸链接
        if is_short_url(url):
            resolved_url = await api_client.resolve_short_url(normalize_short_url(url))
            if resolved_url:
                url = resolved_url
            else:
                if progress_reporter:
                    progress_reporter.update_step("解析链接", "短链解析失败")
                display.print_error(f"Failed to resolve short URL: {url}")
                return None

        parsed = URLParser.parse(url)
        if not parsed:
            if progress_reporter:
                progress_reporter.update_step("解析链接", "URL 解析失败")
            display.print_error(f"Failed to parse URL: {url}")
            return None

        # 能力门禁：这些类型解析得出来，但永远不会有下载器（见
        # core.downloader_factory.UNSUPPORTED_URL_TYPE_DETAIL）。在建下载器之前
        # 拦，用户才能看到真实原因而不是 "No downloader found for type: ..."。
        gated_detail = UNSUPPORTED_URL_TYPE_DETAIL.get(str(parsed.get("type") or ""))
        if gated_detail:
            if progress_reporter:
                progress_reporter.update_step("解析链接", gated_detail)
            display.print_error(gated_detail)
            return None

        if not progress_reporter:
            display.print_info(f"URL type: {parsed['type']}")
        if progress_reporter:
            progress_reporter.advance_step("创建下载器", f"URL 类型: {parsed['type']}")

        downloader = DownloaderFactory.create(
            parsed["type"],
            config,
            api_client,
            file_manager,
            cookie_manager,
            database,
            rate_limiter,
            retry_handler,
            queue_manager,
            progress_reporter=progress_reporter,
        )

        if not downloader:
            if progress_reporter:
                progress_reporter.update_step("创建下载器", "未找到匹配下载器")
            display.print_error(f"No downloader found for type: {parsed['type']}")
            return None

        if progress_reporter:
            progress_reporter.advance_step("执行下载", "开始拉取与下载资源")
        try:
            result = await downloader.download(parsed)
        except Exception as exc:
            # Surface fatal downloader errors (e.g. user_info fetch failed
            # because cookies are invalid) as a per-URL failure instead of
            # crashing the whole batch. Keeps multi-URL CLI runs robust while
            # still telling the user why the URL was skipped.
            if progress_reporter:
                progress_reporter.update_step("执行下载", f"失败：{exc}")
            display.print_error(f"Download failed for {url}: {exc}")
            return None

        if progress_reporter:
            progress_reporter.advance_step(
                "记录历史",
                "写入数据库历史" if (result and database) else "数据库未启用，跳过",
            )
        if result and database:
            await database.add_history(
                {
                    "url": original_url,
                    "url_type": parsed["type"],
                    "total_count": result.total,
                    "success_count": result.success,
                    "config": _config_snapshot(config),
                }
            )

        if progress_reporter:
            if result:
                progress_reporter.advance_step(
                    "收尾",
                    f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}",
                )
            else:
                progress_reporter.advance_step("收尾", "无可统计结果")

        return result


async def download_bilibili_url(
    url: str,
    config: ConfigLoader,
    file_manager: FileManager,
    rate_limiter: RateLimiter,
    retry_handler: RetryHandler,
    queue_manager: QueueManager,
    database: Database = None,
    progress_reporter: ProgressDisplay = None,
):
    """B 站链路：短链展开 → URL 解析 → 能力门禁 → 下载器 → 历史落库。

    与抖音链路的差别只在客户端与解析器：限速/重试/并发/文件管理/进度上报/
    数据库全部复用同一套实例，由 :func:`download_url` 建好传进来。
    """
    if not config.get_bilibili_enabled():
        if progress_reporter:
            progress_reporter.update_step("解析链接", "B 站下载已在配置中关闭")
        display.print_error(
            "哔哩哔哩下载已在配置中关闭（bilibili.enabled: false）。"
            "改为 true 或删除该配置项后重试。"
        )
        return None

    original_url = url
    section = config.get("bilibili") if isinstance(config.get("bilibili"), dict) else {}
    cookies = config.get_bilibili_cookies()

    async with BiliAPIClient(
        cookies,
        proxy=config.get("proxy"),
        request_interval=float(section.get("request_interval", 0.5) or 0),
    ) as api_client:
        if progress_reporter:
            progress_reporter.advance_step("解析链接", "检查 B 站短链并解析 URL")
        if is_bili_short_url(url):
            resolved_url = await api_client.resolve_short_url(normalize_bili_short_url(url))
            if resolved_url:
                url = resolved_url
            else:
                if progress_reporter:
                    progress_reporter.update_step("解析链接", "短链解析失败")
                display.print_error(f"B 站短链展开失败：{url}")
                return None

        parsed = BiliURLParser.parse(url)
        if not parsed:
            if progress_reporter:
                progress_reporter.update_step("解析链接", "URL 解析失败")
            display.print_error(f"无法解析为受支持的 B 站链接：{url}")
            return None

        gated_detail = BILI_UNSUPPORTED_URL_TYPE_DETAIL.get(str(parsed.get("type") or ""))
        if gated_detail:
            if progress_reporter:
                progress_reporter.update_step("解析链接", gated_detail)
            display.print_error(gated_detail)
            return None

        if not progress_reporter:
            display.print_info(f"Platform: bilibili | URL type: {parsed['type']}")

        # 登录态探测：只在「配了 Cookie」或「该类型必须要登录」时做一次，用于
        # 在下载开始前就把「显然拿不到高清 / 收藏夹不可用」讲清楚，而不是等
        # 下完一批 480P 才发现。
        needs_login = parsed.get("type") == "favlist"
        if cookies or needs_login:
            await api_client.ensure_wbi_keys()
        if not cookies:
            display.print_warning(
                "未配置 bilibili.cookies：清晰度上限 480P，收藏夹功能不可用。"
                "在 config.yml 的 bilibili.cookies.SESSDATA 中填入登录凭据可解锁 1080P。"
            )
        elif api_client.is_login is False:
            display.print_warning(
                "bilibili.cookies 中的 SESSDATA 未能通过登录校验（可能已过期）。"
                "请重新登录 B 站后复制新的 SESSDATA。"
            )

        if progress_reporter:
            progress_reporter.advance_step("创建下载器", f"B 站 · {parsed['type']}")
        downloader = BiliDownloaderFactory.create(
            parsed["type"],
            config=config,
            api_client=api_client,
            file_manager=file_manager,
            database=database,
            rate_limiter=rate_limiter,
            retry_handler=retry_handler,
            queue_manager=queue_manager,
            progress_reporter=progress_reporter,
        )
        if not downloader:
            if progress_reporter:
                progress_reporter.update_step("创建下载器", "未找到匹配下载器")
            display.print_error(f"No bilibili downloader found for type: {parsed['type']}")
            return None

        if progress_reporter:
            progress_reporter.advance_step("执行下载", "开始拉取与下载资源")
        try:
            result = await downloader.download(parsed)
        except BiliLoginRequiredError as exc:
            if progress_reporter:
                progress_reporter.update_step("执行下载", f"需要登录：{exc.message}")
            display.print_error(
                f"该链接需要登录态（{exc.message or '账号未登录'}）。"
                f"请在 config.yml 的 bilibili.cookies 中填入 SESSDATA 后重试。"
            )
            return None
        except BiliRiskControlError as exc:
            if progress_reporter:
                progress_reporter.update_step("执行下载", f"风控拦截：{exc.message}")
            display.print_error(
                f"被 B 站风控拦截（{exc.message or exc.code}）。"
                f"可尝试调大 bilibili.request_interval、调小 thread，或稍后重试。"
            )
            return None
        except Exception as exc:
            if progress_reporter:
                progress_reporter.update_step("执行下载", f"失败：{exc}")
            display.print_error(f"B 站下载失败 for {url}: {exc}")
            return None

        if progress_reporter:
            progress_reporter.advance_step(
                "记录历史",
                "写入数据库历史" if (result and database) else "数据库未启用，跳过",
            )
        if result and database:
            await database.add_history(
                {
                    "url": original_url,
                    "url_type": f"bilibili:{parsed['type']}",
                    "total_count": result.total,
                    "success_count": result.success,
                    "config": _config_snapshot(config),
                }
            )

        if progress_reporter:
            if result:
                progress_reporter.advance_step(
                    "收尾",
                    f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}",
                )
            else:
                progress_reporter.advance_step("收尾", "无可统计结果")

        return result


def ytdlp_error_hint(error: YtdlpDownloadError, platform_name: str) -> str:
    """按错误类别给出可操作的提示；yt-dlp 的原始文案附在后面供排查。"""
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


async def download_ytdlp_url(
    url: str,
    config: ConfigLoader,
    file_manager: FileManager,
    rate_limiter: RateLimiter,
    retry_handler: RetryHandler,
    queue_manager: QueueManager,
    database: Database = None,
    progress_reporter: ProgressDisplay = None,
):
    """yt-dlp 平台链路：平台门禁 → 解析 → 下载器 → 历史落库。

    与 B 站链路同构；差别是没有站方 API 客户端（HTTP 由 yt-dlp 自管），也没有
    短链展开步骤（快手 / 小红书的短链由 yt-dlp 自行跟随跳转）。
    """
    parsed = YtdlpURLParser.parse(url)
    if not parsed:
        if progress_reporter:
            progress_reporter.update_step("解析链接", "URL 解析失败")
        display.print_error(f"无法识别为受支持的平台链接：{url}")
        return None

    platform = parsed["platform"]
    name = parsed.get("platform_name") or platform_display_name(platform)

    if not config.get_ytdlp_enabled():
        if progress_reporter:
            progress_reporter.update_step("解析链接", "第三方平台下载已在配置中关闭")
        display.print_error(
            "其他平台下载已在配置中关闭（ytdlp.enabled: false）。改为 true 或删除该配置项后重试。"
        )
        return None
    if not config.get_ytdlp_platform_enabled(platform):
        if progress_reporter:
            progress_reporter.update_step("解析链接", f"{name} 已在配置中关闭")
        display.print_error(
            f"{name} 下载已在配置中关闭（ytdlp.platforms.{platform}: false）。改为 true 后重试。"
        )
        return None

    original_url = url
    if not progress_reporter:
        display.print_info(f"Platform: {name} ({platform}) | engine: yt-dlp")
    if progress_reporter:
        progress_reporter.advance_step("解析链接", f"{name} · yt-dlp 引擎")

    if not config.get_ytdlp_cookies(platform) and not str(
        (config.get("ytdlp") or {}).get("cookie_file") or ""
    ).strip():
        display.print_warning(
            f"未配置 {name} 的 Cookie（ytdlp.cookies.{platform}）：只能下载免费 / 未登录可看的内容，"
            "清晰度可能受限。"
        )

    if progress_reporter:
        progress_reporter.advance_step("创建下载器", f"{name} · video")
    downloader = YtdlpDownloader(
        config=config,
        file_manager=file_manager,
        database=database,
        rate_limiter=rate_limiter,
        retry_handler=retry_handler,
        queue_manager=queue_manager,
        progress_reporter=progress_reporter,
    )

    if progress_reporter:
        progress_reporter.advance_step("执行下载", "开始解析与下载资源")
    try:
        result = await downloader.download(parsed)
    except YtdlpMissingError as exc:
        if progress_reporter:
            progress_reporter.update_step("执行下载", "未安装 yt-dlp")
        display.print_error(str(exc))
        return None
    except YtdlpDownloadError as exc:
        hint = ytdlp_error_hint(exc, name)
        if progress_reporter:
            progress_reporter.update_step("执行下载", f"失败：{exc.kind}")
        display.print_error(hint)
        return None
    except Exception as exc:
        if progress_reporter:
            progress_reporter.update_step("执行下载", f"失败：{exc}")
        display.print_error(f"{name} 下载失败 for {url}: {exc}")
        return None

    # 整条链接全部失败时把最后一次错误的分类提示打出来：yt-dlp 的逐条报错
    # 已经进了日志，但用户在进度条上只看到「失败 N」，需要一句能行动的解释。
    if result and result.total and result.success == 0 and downloader.last_error:
        display.print_error(ytdlp_error_hint(downloader.last_error, name))

    if progress_reporter:
        progress_reporter.advance_step(
            "记录历史",
            "写入数据库历史" if (result and database) else "数据库未启用，跳过",
        )
    if result and database:
        await database.add_history(
            {
                "url": original_url,
                "url_type": f"ytdlp:{platform}:{parsed['type']}",
                "total_count": result.total,
                "success_count": result.success,
                "config": _config_snapshot(config),
            }
        )

    if progress_reporter:
        if result:
            progress_reporter.advance_step(
                "收尾",
                f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}",
            )
        else:
            progress_reporter.advance_step("收尾", "无可统计结果")

    return result


async def main_async(args):
    if not args.serve:
        display.show_banner()

    if args.config:
        config_path = args.config
    else:
        config_path = "config.yml"

    # 若 config 不存在且使用了 --hot-board / --search / --serve / --channels 等
    # 独立子命令，允许以默认配置运行（只要命令行提供了 --path）。
    if not Path(config_path).exists():
        if not (args.hot_board is not None or args.search or args.serve or args.channels):
            display.print_error(f"Config file not found: {config_path}")
            return
        # For ``--serve`` we still pass the (yet-missing) path so later
        # ``config.save()`` calls from the REST settings endpoint create
        # the file in the right place (e.g. Electron's userData dir).
        # Other subcommands keep the historical behaviour of in-memory
        # defaults.
        if args.serve and args.config:
            config = ConfigLoader(config_path)
        else:
            config = ConfigLoader(None)
    else:
        config = ConfigLoader(config_path)

    if args.path:
        config.update(path=args.path)

    # 独立子命令：热榜 / 搜索 / 服务
    if args.hot_board is not None or args.search:
        discovery_cm = CookieManager()
        discovery_cm.set_cookies(config.get_cookies())
        await _run_with_relogin(
            lambda: _run_discovery_subcommand(args, config, discovery_cm),
            discovery_cm,
            serve=False,
        )
        return
    if args.serve:
        await _run_serve_subcommand(args, config)
        return
    if args.channels:
        from cli.channels_session import run_channels_session

        await run_channels_session(config, port=args.channels_port)
        return

    if args.url:
        urls = args.url if isinstance(args.url, list) else [args.url]
        for url in urls:
            if url not in config.get("link", []):
                config.update(link=config.get("link", []) + [url])

    if args.thread:
        config.update(thread=args.thread)

    if not config.validate():
        display.print_error("Invalid configuration: missing required fields")
        return

    urls = config.get_links()
    # 平台分流在准备阶段就要落地：只有确实存在抖音链接时才去读写抖音的
    # Cookie 文件。否则纯 B 站 / 纯第三方平台任务会用一个空字典覆盖掉用户的
    # 抖音登录态。
    from channels.url_parser import CHANNELS_URL_HINT, is_channels_url

    channels_urls = [item for item in urls if is_channels_url(item)]
    if channels_urls:
        display.print_warning(
            f"检测到 {len(channels_urls)} 个视频号链接，无法直链下载。{CHANNELS_URL_HINT}"
        )
    bilibili_urls = [item for item in urls if detect_platform(item) == "bilibili"]
    ytdlp_urls = [
        item
        for item in urls
        if detect_platform(item) != "bilibili"
        and detect_ytdlp_platform(item) is not None
        and not is_channels_url(item)
    ]
    douyin_urls = [
        item
        for item in urls
        if detect_platform(item) != "bilibili"
        and detect_ytdlp_platform(item) is None
        and not is_channels_url(item)
    ]

    cookie_manager = CookieManager()
    if douyin_urls:
        cookie_manager.set_cookies(config.get_cookies())
        if not cookie_manager.validate_cookies():
            display.print_warning("Cookies may be invalid or incomplete")

    database = None
    if config.get("database"):
        db_path = config.get("database_path", "dy_downloader.db") or "dy_downloader.db"
        database = Database(db_path=str(db_path))
        await database.initialize()
        display.print_success("Database initialized")

    display.print_info(f"Found {len(urls)} URL(s) to process")
    if bilibili_urls:
        display.print_info(
            f"其中哔哩哔哩链接 {len(bilibili_urls)} 个"
            + ("（已配置登录 Cookie）" if config.get_bilibili_cookies() else "（未配置登录 Cookie，清晰度上限 480P）")
        )
    if ytdlp_urls:
        platform_names = sorted(
            {platform_display_name(detect_ytdlp_platform(item)) for item in ytdlp_urls}
        )
        display.print_info(
            f"其中其他平台链接 {len(ytdlp_urls)} 个（{' / '.join(platform_names)}，yt-dlp 引擎）"
        )

    all_results = []
    progress_config = config.get("progress", {}) or {}
    quiet_by_config = _as_bool(progress_config.get("quiet_logs", True), default=True)
    quiet_progress_logs = quiet_by_config and not (args.verbose or args.show_warnings)
    if quiet_progress_logs:
        # Progress 运行期间若有大量错误日志会触发 rich 反复重绘，导致屏幕出现重复块。
        # 默认静默控制台日志，下载完成后再恢复。
        set_console_log_level(logging.CRITICAL)

    display.start_download_session(len(urls))
    try:
        for i, url in enumerate(urls, 1):
            display.start_url(i, len(urls), url)

            result = await _run_with_relogin(
                lambda u=url: download_url(
                    u,
                    config,
                    cookie_manager,
                    database,
                    progress_reporter=display,
                ),
                cookie_manager,
                serve=False,
            )
            if result:
                all_results.append(result)
                display.complete_url(result)
            else:
                display.fail_url("下载失败或链接无效")
    finally:
        display.stop_download_session()
        if database is not None:
            await database.close()
        if quiet_progress_logs:
            set_console_log_level(logging.ERROR)

    if all_results:
        from core.downloader_base import DownloadResult

        total_result = DownloadResult()
        for r in all_results:
            total_result.total += r.total
            total_result.success += r.success
            total_result.failed += r.failed
            total_result.skipped += r.skipped

        display.print_success("\n=== Overall Summary ===")
        display.show_result(total_result)

        await _dispatch_notifications(config, total_result, len(urls))
    else:
        # 所有链接都失败时，也发通知（若启用）
        await _dispatch_notifications(config, None, len(urls))


async def _run_discovery_subcommand(
    args, config: ConfigLoader, cookie_manager: CookieManager
) -> None:
    """处理 --hot-board 与 --search 子命令。"""
    from core.discovery import dump_hot_board, search_and_dump

    base_path = Path(config.get("path") or "./Downloaded/")

    async with DouyinAPIClient(
        cookie_manager.get_cookies(),
        proxy=config.get("proxy"),
    ) as api_client:
        if args.hot_board is not None:
            display.print_info("拉取抖音热搜榜...")
            result = await dump_hot_board(api_client, base_path, limit=int(args.hot_board or 0))
            display.print_success(f"热榜已保存：{result['count']} 条 -> {result['path']}")
        if args.search:
            display.print_info(f"搜索关键词：{args.search}")
            result = await search_and_dump(
                api_client,
                args.search,
                base_path,
                max_items=int(args.search_max or 50),
            )
            display.print_success(f"搜索结果已保存：{result['count']} 条 -> {result['path']}")


async def _run_serve_subcommand(args, config: ConfigLoader) -> None:
    """启动 REST API 服务模式（fastapi + uvicorn 为可选依赖）。"""
    try:
        from server.app import run_server
    except ImportError as exc:
        display.print_error(
            f"REST 服务模式需要安装可选依赖 fastapi + uvicorn："
            f"\n  pip install fastapi uvicorn\n原始错误：{exc}"
        )
        return

    display.print_info(f"启动 REST 服务：http://{args.serve_host}:{args.serve_port}")
    await run_server(config, host=args.serve_host, port=args.serve_port)


async def _dispatch_notifications(config: ConfigLoader, total_result: Any, url_count: int) -> None:
    notifier = build_notifier(config)
    if not notifier.enabled:
        return

    if total_result is None:
        title = "视频下载：全部失败"
        body = f"共处理 {url_count} 个链接，无成功结果"
        level = "failure"
    else:
        fail_or_partial = total_result.failed > 0 or total_result.success == 0
        level = "failure" if fail_or_partial else "success"
        title = "视频下载完成" if level == "success" else "视频下载部分失败"
        body = (
            f"链接 {url_count} / 总作品 {total_result.total} / "
            f"成功 {total_result.success} / 失败 {total_result.failed} / "
            f"跳过 {total_result.skipped}"
        )

    try:
        summary = await notifier.send(title=title, body=body, level=level)
        if summary:
            succ = sum(1 for ok in summary.values() if ok)
            logger.info(
                "Notification dispatched to %d provider(s), %d ok",
                len(summary),
                succ,
            )
    except Exception as exc:  # 通知失败不应影响主流程
        logger.warning("Notification dispatch error: %s", exc)


def main():
    parser = argparse.ArgumentParser(description="Douyin Downloader - 抖音批量下载工具")
    parser.add_argument("-u", "--url", action="append", help="Download URL(s)")
    parser.add_argument("-c", "--config", help="Config file path (default: config.yml)")
    parser.add_argument("-p", "--path", help="Save path")
    parser.add_argument("-t", "--thread", type=int, help="Thread count")
    parser.add_argument("--show-warnings", action="store_true", help="Show warning logs in console")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose console logs")
    parser.add_argument(
        "--hot-board",
        type=int,
        nargs="?",
        const=0,
        default=None,
        metavar="N",
        help="拉取抖音热搜榜并导出 JSONL，可选上限 N（默认全部）",
    )
    parser.add_argument(
        "--search",
        type=str,
        default=None,
        metavar="KEYWORD",
        help="按关键词搜索作品并导出 JSONL",
    )
    parser.add_argument(
        "--search-max",
        type=int,
        default=50,
        help="--search 场景下最多拉取条数（默认 50）",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="以 REST API 服务模式运行（需要安装 fastapi + uvicorn）",
    )
    parser.add_argument("--serve-host", type=str, default="127.0.0.1", help="REST 服务监听地址")
    parser.add_argument("--serve-port", type=int, default=8000, help="REST 服务监听端口")
    parser.add_argument(
        "--channels",
        action="store_true",
        help="进入微信视频号嗅探会话：拦截本机微信流量自动捕获并下载"
        "（需要安装 mitmproxy：pip install \".[channels]\"）",
    )
    parser.add_argument(
        "--channels-port",
        type=int,
        default=None,
        help="视频号嗅探代理端口（默认取 channels.proxy_port 配置，8899）",
    )
    try:
        from __init__ import __version__
    except ImportError:
        __version__ = "2.0.0"
    parser.add_argument("--version", action="version", version=__version__)

    args = parser.parse_args()

    if args.verbose:
        set_console_log_level(logging.INFO)
    elif args.show_warnings:
        set_console_log_level(logging.WARNING)
    else:
        set_console_log_level(logging.ERROR)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        display.print_warning("\nDownload interrupted by user")
        sys.exit(0)
    except Exception as e:
        display.print_error(f"Fatal error: {e}")
        logger.exception("Fatal error occurred")
        sys.exit(1)


if __name__ == "__main__":
    main()
