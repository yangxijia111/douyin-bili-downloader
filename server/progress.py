"""把 ``core`` 的进度回调桥接到 ``DownloadJob`` 上。

``core.downloader_base`` 定义了一套进度契约（``update_step`` /
``set_item_total`` / ``advance_item``，以及可选的 ``on_item_progress`` /
``on_output_dir`` / ``on_author``），CLI 用它渲染 Rich 进度条，桌面端用它喂
任务卡片。REST 服务模式此前一直传 ``progress_reporter=None``，所以网页上
只能看到任务"运行中"，看不到任何过程。

本模块提供那个缺失的 reporter：所有回调都是同步的、就地改写 job 字段。
下载流程跑在同一个事件循环里（含 aiohttp 的字节回调），因此不需要加锁；
读取方通过 ``GET /api/v1/jobs`` 快照读，天然拿到某一瞬间的一致视图。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免循环导入
    from server.jobs import DownloadJob


class JobProgressReporter:
    def __init__(self, job: "DownloadJob"):
        self.job = job

    # ---- 必选契约 -----------------------------------------------------

    def update_step(self, step: str, detail: str = "") -> None:
        if step:
            self.job.step = str(step)
        if detail:
            self.job.detail = str(detail)

    def set_item_total(self, total: int, detail: str = "") -> None:
        try:
            self.job.total = max(0, int(total))
        except (TypeError, ValueError):
            pass
        if detail:
            self.job.detail = str(detail)

    def advance_item(self, status: str, detail: str = "") -> None:
        if status == "success":
            self.job.success += 1
        elif status == "skipped":
            self.job.skipped += 1
        else:
            # 未知状态按失败计：core 目前只会发 success / failed / skipped，
            # 保守归到 failed 才能让进度总和始终等于 processed。
            self.job.failed += 1
        self.job.processed = self.job.success + self.job.failed + self.job.skipped
        if detail:
            self.job.last_item = str(detail)
        # 该条处理完毕，清掉上一条的字节进度，避免进度条停留在 100% 旧文件上。
        self.job.current = None

    # ---- 可选契约 -----------------------------------------------------

    def on_item_progress(self, *, aweme_id: str, bytes_read: int, bytes_total: int) -> None:
        try:
            read = int(bytes_read)
            total = int(bytes_total)
        except (TypeError, ValueError):
            return
        percent: Optional[int] = None
        if total > 0:
            percent = max(0, min(100, int(read * 100 / total)))
        payload: Dict[str, Any] = {
            "aweme_id": str(aweme_id),
            "bytes_read": read,
            "bytes_total": total,
            "percent": percent,
        }
        self.job.current = payload

    def on_output_dir(self, *, path: str) -> None:
        value = str(path or "")
        if value and value not in self.job.output_dirs:
            self.job.output_dirs.append(value)

    def on_author(self, *, nickname: Optional[str] = None, sec_uid: Optional[str] = None) -> None:
        if nickname:
            self.job.author_nickname = str(nickname)
        if sec_uid:
            self.job.author_sec_uid = str(sec_uid)
