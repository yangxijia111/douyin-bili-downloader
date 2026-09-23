"""CLI 视频号嗅探会话（``python run.py --channels``）。

会话生命周期::

    ① 检查并（首次）安装本机嗅探根证书 —— Windows 会弹确认框
    ② 启动 mitmproxy 嗅探代理 + 开启系统代理（微信内嵌浏览器走代理）
    ③ rich 实时表格展示捕获列表与链路诊断；auto_download 开启时后台协程
       逐条下载；微信页面内的下载按钮经 /__cuin/task 触发同一下载器
    ④ Ctrl+C 退出：关代理、还原系统代理、关数据库

使用方式：会话启动后在本机微信里打开「视频号」刷视频，页面里会出现
「下载」按钮（v2.0.2 起的主要交互）；feed（直链 + decodeKey）由页面 hook
与被动嗅探双路捕获，落入捕获列表。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from channels.diagnostics import ChannelsDiagnostics
from channels.feed_store import FeedStore
from channels.interceptor import (
    MITMPROXY_INSTALL_HINT,
    CertificateManager,
    ChannelsInterceptor,
    SystemProxyManager,
    mitmproxy_available,
)
from channels.patches import PatchRegistry
from cli.progress_display import ProgressDisplay
from config import ConfigLoader
from core.downloader_base import DownloadResult
from storage import Database, FileManager
from utils.logger import setup_logger

logger = setup_logger("ChannelsSession")

# 表格最多展示最近多少条（捕获列表本身不截断，只影响渲染）。
_MAX_TABLE_ROWS = 30

_KIND_LABELS = {"video": "视频", "image": "图文", "live": "直播"}


def _fmt_size(size: int) -> str:
    if not size:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def _fmt_duration(seconds: int) -> str:
    if not seconds:
        return "-"
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes}:{sec:02d}"


def _section(config: ConfigLoader) -> Dict[str, Any]:
    section = config.get("channels")
    return section if isinstance(section, dict) else {}


async def _open_database(config: ConfigLoader) -> Optional[Database]:
    if not config.get("database"):
        return None
    db_path = config.get("database_path", "dy_downloader.db") or "dy_downloader.db"
    database = Database(db_path=str(db_path))
    await database.initialize()
    return database


def _prepare_certificate(display: ProgressDisplay, cert_manager: CertificateManager) -> bool:
    """确保 CA 存在且已受信任；失败时给出指引并返回 False。"""
    try:
        cert_path = cert_manager.ensure_ca()
    except RuntimeError as exc:
        display.print_error(str(exc))
        return False

    try:
        if cert_manager.is_installed():
            return True
    except Exception as exc:  # noqa: BLE001 —— 查询失败按未安装处理
        display.print_warning(f"证书信任状态查询失败：{exc}")

    display.print_warning(
        "首次使用视频号嗅探需要信任本机根证书（仅在本机生成，"
        f"位于 {cert_path}）。即将弹出 Windows 确认框，请点「是」继续。"
    )
    ok, _detail = cert_manager.install()
    if ok:
        display.print_success("根证书已加入当前用户的信任存储")
        return True
    display.print_error(
        "证书安装未完成（可能在确认框里点了「否」）。手动安装：双击 "
        f"{cert_path} → 安装证书 → 存储位置「当前用户」→ "
        "「将所有的证书都放入下列存储」→ 浏览 → 「受信任的根证书颁发机构」。"
    )
    return False


async def run_channels_session(
    config: ConfigLoader,
    *,
    port: Optional[int] = None,
    link: Optional[Any] = None,
) -> None:
    """CLI 嗅探会话主入口。

    ``link`` 为分享链接模式（v2.0.3）：粘贴视频号分享链接后启动，
    强制自动下载（用户意图明确=就要这个视频），引导在微信里打开链接，
    目标视频下载完成后自动结束会话。
    """
    display = ProgressDisplay()
    if not mitmproxy_available():
        display.print_error(MITMPROXY_INSTALL_HINT)
        return
    section = _section(config)
    if not section.get("enabled", True):
        display.print_error("channels.enabled 已关闭（config.yml），无操作。")
        return

    listen_port = int(port or section.get("proxy_port", 8899) or 8899)
    # v2.0.2 默认仅捕获不自动下载：页面按钮模式下用户点按钮才下载。
    auto_download = bool(section.get("auto_download", False))
    live_record = bool(section.get("live_record", False))
    inject_ui = bool(section.get("inject_ui", True))
    # MITM 解密白名单扩展（默认只有 weixin.qq.com，见 channels.domains）。
    extra_domains = section.get("intercept_domains")
    if not isinstance(extra_domains, (list, tuple)):
        extra_domains = ()
    store = FeedStore()
    cert_manager = CertificateManager()
    if not _prepare_certificate(display, cert_manager):
        return

    database = await _open_database(config)
    file_manager = FileManager(config.get("path"))

    from channels.downloader import ChannelsDownloader
    from channels.task_hub import ChannelsTaskHub

    downloader = ChannelsDownloader(config, file_manager, database=database)
    # 诊断计数器与任务中心：CLI 与 Server 形态一致（跨 stop 后仍可查看）。
    diagnostics = ChannelsDiagnostics()
    patch_registry = PatchRegistry(diagnostics)
    stats = DownloadResult()
    task_hub = ChannelsTaskHub(store, downloader, stats)

    proxy_manager = SystemProxyManager()

    # 先处理上次异常退出可能残留的系统代理（不能覆盖用户新设置）。
    from channels.proxy_recovery import recover_stale_proxy

    report = recover_stale_proxy()
    if report["action"] == "restored":
        display.print_warning(f"检测到上次异常退出残留的系统代理，已自动恢复：{report['detail']}")
    elif report["action"] == "kept":
        display.print_info(f"代理恢复检查：{report['detail']}")

    # 分享链接模式：用户意图明确（就要这个视频），强制自动下载。
    if link is not None:
        auto_download = True

    display.print_info(
        f"启动嗅探代理 127.0.0.1:{listen_port} 并接管系统代理……"
        "（本机微信的视频号页面流量将被读取；结束后自动还原）"
    )
    if link is not None:
        _print_link_guidance(display, link)
        _try_open_link(link.full_url)
    tasks = []
    interceptor: Optional[ChannelsInterceptor] = None
    try:
        interceptor = ChannelsInterceptor(
            store, port=listen_port, cert_manager=cert_manager,
            extra_domains=extra_domains, diagnostics=diagnostics,
            task_hub=task_hub, inject_enabled=inject_ui,
            patch_registry=patch_registry,
        )
        await interceptor.start()
        try:
            proxy_manager.enable(port=listen_port)
        except Exception as exc:  # noqa: BLE001 —— SystemProxyError 等
            await interceptor.stop()
            interceptor = None
            display.print_error(f"系统代理设置失败：{exc}")
            return

        if auto_download:
            from channels.worker import run_auto_download_worker

            tasks.append(
                asyncio.create_task(
                    run_auto_download_worker(
                        store, downloader, live_record=live_record, stats=stats
                    ),
                    name="channels-auto-download",
                )
            )

        await _interactive_loop(
            display, store, stats, diagnostics=diagnostics, auto_download=auto_download,
            link_mode=link is not None,
        )
    except KeyboardInterrupt:
        pass  # 正常退出路径：Ctrl+C
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 微信页面按钮的任务（含直播录制）随之取消。
        task_hub.cancel_all()
        await task_hub.wait_idle(timeout=5.0)
        if interceptor is not None:
            try:
                await interceptor.stop()
            except Exception:  # noqa: BLE001
                pass
        proxy_manager.restore()  # 幂等；无论怎么退出都还原系统代理
        try:
            await downloader.aclose()
        except Exception:  # noqa: BLE001
            pass
        if database is not None:
            try:
                await database.close()
            except Exception:  # noqa: BLE001
                pass
        display.print_info(
            f"嗅探会话结束 —— 捕获 {len(store)} 条，下载成功 {stats.success}、"
            f"跳过 {stats.skipped}、失败 {stats.failed}。系统代理已还原。"
        )


async def _interactive_loop(
    display: ProgressDisplay,
    store: FeedStore,
    stats: DownloadResult,
    *,
    diagnostics: ChannelsDiagnostics,
    auto_download: bool,
    link_mode: bool = False,
) -> None:
    """rich Live 实时表格，Ctrl+C 打断返回。

    ``link_mode``：分享链接模式——目标视频下载成功后自动结束会话
    （用户意图明确，不需要继续挂着代理）。
    """
    from rich.console import Group
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text

    def render_table() -> Table:
        table = Table(
            title=f"视频号嗅探捕获（{len(store)} 条；成功 {stats.success} / 跳过 {stats.skipped} / 失败 {stats.failed}）",
            show_lines=False,
        )
        for column in ("时间", "作者", "标题", "类型", "大小", "时长", "状态"):
            table.add_column(column, overflow="ellipsis", no_wrap=True)
        from datetime import datetime

        status_style = {
            "pending": "dim",
            "downloading": "yellow",
            "done": "green",
            "skipped": "cyan",
            "failed": "red",
        }
        for feed in reversed(store.all()[-_MAX_TABLE_ROWS:]):
            time_text = (
                datetime.fromtimestamp(feed.create_time).strftime("%m-%d %H:%M")
                if feed.create_time
                else "-"
            )
            status_text = feed.status
            if feed.status == "skipped" and feed.kind == "live":
                status_text = "直播(手动)"
            if feed.status == "failed" and feed.error:
                status_text = f"失败:{feed.error[:18]}"
            table.add_row(
                time_text,
                feed.author_name[:12] or "-",
                feed.title[:38] or "-",
                _KIND_LABELS.get(feed.kind, feed.kind),
                _fmt_size(feed.file_size),
                _fmt_duration(feed.duration),
                Text(status_text, style=status_style.get(feed.status, "")),
            )
        return table

    def render_chain() -> Text:
        """链路诊断状态链（断点一目了然，替代笼统的「暂无嗅探结果」）。"""
        advice = diagnostics.advice()
        style = {"ok": "green", "warn": "yellow", "error": "red"}[advice["level"]]
        marks = {"ok": "✓", "warn": "…", "error": "✗"}
        parts = []
        for step in diagnostics.chain(downloads_success=stats.success):
            mark = marks["ok"] if step["ok"] else marks[advice["level"]]
            parts.append(f"[{style}]{mark} {step['label']}[/{style}]")
        return Text.from_markup(" ".join(parts) + f"\n[dim]{advice['message']}[/dim]")

    hint = Text.from_markup(
        "[dim]现在打开本机微信 → 视频号，页面中会出现「下载」按钮；"
        + ("自动下载已开启。" if auto_download else "自动下载未开启（channels.auto_download=false），仅捕获 + 按钮下载。")
        + " 按 Ctrl+C 结束会话。[/dim]"
    )

    import contextlib

    with Live(Group(render_table(), render_chain(), hint), refresh_per_second=2) as live:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await store.wait_for_new(timeout=0.5)
            live.update(Group(render_table(), render_chain(), hint))
            if link_mode and _link_target_done(store, stats):
                return  # 目标视频已下载完成：自动结束会话


def _link_target_done(store: FeedStore, stats: DownloadResult) -> bool:
    """分享链接模式的目标是否已完成（至少一条下载成功）。"""
    return stats.success > 0


def _print_link_guidance(display: ProgressDisplay, link) -> None:
    """分享链接模式的操作引导。"""
    display.print_info(
        f"已识别视频号分享链接（id: {link.share_id}）。"
        "请在**本机微信**里打开该链接（任意聊天窗口发送后点击，或浏览器打开后"
        "选择「在微信中打开」）——预览页会在微信内置浏览器里加载，工具自动捕获"
        "并下载该视频。"
    )
    display.print_info(f"链接地址：{link.original}")


def _try_open_link(url: str) -> None:
    """尝试用系统默认方式打开链接（可能唤起微信；失败不影响会话）。"""
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            # start 是 cmd 内建命令；经 cmd /c 调用，URL 带 & 需整体引号传。
            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as exc:  # noqa: BLE001 —— 打不开不影响手动打开
        logger.info("自动打开链接失败（请手动在微信中打开）: %s", exc)


async def run_channels_link_session(
    config: ConfigLoader, link, *, port: Optional[int] = None
) -> None:
    """``--channels-link`` 入口：分享链接下载会话。"""
    await run_channels_session(config, port=port, link=link)
