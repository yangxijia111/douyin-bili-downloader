"""CLI 视频号嗅探会话（``python run.py --channels``）。

会话生命周期::

    ① 检查并（首次）安装本机嗅探根证书 —— Windows 会弹确认框
    ② 启动 mitmproxy 嗅探代理 + 开启系统代理（微信内嵌浏览器走代理）
    ③ rich 实时表格展示捕获列表；auto_download 开启时后台协程逐条下载
    ④ Ctrl+C 退出：关代理、还原系统代理、关数据库

使用方式：会话启动后在本机微信里打开「视频号」刷视频，feed（直链 +
decodeKey）随 API 响应被动落入捕获列表。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from channels.feed_store import FeedStore
from channels.interceptor import (
    MITMPROXY_INSTALL_HINT,
    CertificateManager,
    ChannelsInterceptor,
    SystemProxyManager,
    mitmproxy_available,
)
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


async def run_channels_session(config: ConfigLoader, *, port: Optional[int] = None) -> None:
    """CLI 嗅探会话主入口。"""
    display = ProgressDisplay()
    if not mitmproxy_available():
        display.print_error(MITMPROXY_INSTALL_HINT)
        return
    section = _section(config)
    if not section.get("enabled", True):
        display.print_error("channels.enabled 已关闭（config.yml），无操作。")
        return

    listen_port = int(port or section.get("proxy_port", 8899) or 8899)
    auto_download = bool(section.get("auto_download", True))
    live_record = bool(section.get("live_record", False))
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

    downloader = ChannelsDownloader(config, file_manager, database=database)

    proxy_manager = SystemProxyManager()
    stats = DownloadResult()

    # 先处理上次异常退出可能残留的系统代理（不能覆盖用户新设置）。
    from channels.proxy_recovery import recover_stale_proxy

    report = recover_stale_proxy()
    if report["action"] == "restored":
        display.print_warning(f"检测到上次异常退出残留的系统代理，已自动恢复：{report['detail']}")
    elif report["action"] == "kept":
        display.print_info(f"代理恢复检查：{report['detail']}")

    display.print_info(
        f"启动嗅探代理 127.0.0.1:{listen_port} 并接管系统代理……"
        "（本机微信的视频号页面流量将被读取；结束后自动还原）"
    )
    tasks = []
    interceptor: Optional[ChannelsInterceptor] = None
    try:
        interceptor = ChannelsInterceptor(
            store, port=listen_port, cert_manager=cert_manager,
            extra_domains=extra_domains,
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

        await _interactive_loop(display, store, stats, auto_download=auto_download)
    except KeyboardInterrupt:
        pass  # 正常退出路径：Ctrl+C
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
    auto_download: bool,
) -> None:
    """rich Live 实时表格，Ctrl+C 打断返回。"""
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

    hint = Text.from_markup(
        "[dim]现在打开本机微信 → 视频号，浏览/播放视频即可捕获。"
        + ("自动下载已开启。" if auto_download else "自动下载未开启（channels.auto_download=false），仅捕获列表。")
        + " 按 Ctrl+C 结束会话。[/dim]"
    )

    import contextlib

    with Live(Group(render_table(), hint), refresh_per_second=2) as live:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await store.wait_for_new(timeout=0.5)
            live.update(Group(render_table(), hint))
