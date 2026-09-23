"""纯 Python 的后台下载任务模型，不依赖 FastAPI。

将 job 生命周期从 HTTP 层解耦，便于被 CLI 以外的入口复用（如未来的 MCP server）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

# 当前正在执行的 job。``_run`` 在调用 executor 前 set，executor 通过它拿到
# 自己要驱动进度/暂停的那个 job —— 这样 executor 的签名保持 ``(url, overrides)``
# 不变，既有的测试替身（单参 executor）无需改动。
CURRENT_JOB: ContextVar[Optional["DownloadJob"]] = ContextVar("current_job", default=None)


def _now_iso() -> str:
    # 统一使用 timezone-aware UTC ISO-8601 字符串
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = frozenset({SUCCESS, FAILED, CANCELLED})


class DownloadJob:
    def __init__(self, job_id: str, url: str):
        self.job_id = job_id
        self.url = url
        self.status = JobStatus.PENDING
        self.created_at = _now_iso()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        # 单调时钟时间戳，用于 TTL / LRU 剪裁（不受系统时钟跳变影响）
        self.started_monotonic: Optional[float] = None
        self.finished_monotonic: Optional[float] = None
        self.total = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.error: Optional[str] = None
        # Per-submission config overrides (mode / number / quality / …).
        # ``None`` means "use the server-wide config as-is"; the web console
        # uses this so each queued link can carry its own scope without
        # mutating the shared config other jobs are reading.
        self.overrides: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None

        # ---- 实时进度（由 server/progress.py 的 reporter 就地写入）----
        # 这些字段是给网页控制台的进度条/日志用的，只在单线程事件循环里被
        # 同步读写，所以不加锁。
        self.step = ""
        self.detail = ""
        self.processed = 0
        self.last_item = ""
        self.current: Optional[Dict[str, Any]] = None
        self.author_nickname: Optional[str] = None
        self.author_sec_uid: Optional[str] = None
        self.output_dirs: List[str] = []
        self.cancel_requested = False
        self.paused = False
        self._resume_gate: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------
    # 暂停 / 取消
    # ------------------------------------------------------------------

    def _gate(self) -> asyncio.Event:
        """Lazily create the resume gate inside the running loop.

        3.9 上在无运行事件循环时构造 asyncio.Event 会绑定错误的 loop，
        因此和 core 里的 Lock 一样延迟到首次使用再创建。
        """
        if self._resume_gate is None:
            self._resume_gate = asyncio.Event()
            self._resume_gate.set()
        return self._resume_gate

    async def wait_if_paused(self) -> None:
        """Block while paused. Awaited before every API call of this job."""
        await self._gate().wait()

    def pause(self) -> None:
        self.paused = True
        self._gate().clear()

    def resume(self) -> None:
        self.paused = False
        self._gate().set()

    # ------------------------------------------------------------------
    # 展示字段
    # ------------------------------------------------------------------

    def duration_seconds(self) -> Optional[float]:
        if self.started_monotonic is None:
            return None
        end = self.finished_monotonic if self.finished_monotonic is not None else time.monotonic()
        return round(max(0.0, end - self.started_monotonic), 1)

    def progress_percent(self) -> Optional[int]:
        """0-100，未知总数时返回 None（前端显示不确定态进度条）。"""
        if self.status in JobStatus.TERMINAL:
            return 100
        if self.total > 0:
            return max(0, min(99, int(self.processed * 100 / self.total)))
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "url": self.url,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "error": self.error,
            "overrides": self.overrides,
            # 进度
            "step": self.step,
            "detail": self.detail,
            "processed": self.processed,
            "last_item": self.last_item,
            "current": self.current,
            "author_nickname": self.author_nickname,
            "author_sec_uid": self.author_sec_uid,
            "output_dirs": list(self.output_dirs),
            "paused": self.paused,
            "progress_percent": self.progress_percent(),
            "duration_seconds": self.duration_seconds(),
        }


class JobManager:
    """内存 job 存储 + 并发执行器，带 TTL + 容量上限。

    不做持久化——进程重启就丢失——因为当前目标只是暴露 HTTP 接口。
    如需持久化可以后续在此加一层 SQLite。

    剪裁策略：
    - 每次 submit 前先剪裁一次：
        a. 丢弃 finished_monotonic 超过 job_ttl_seconds 的终态 job；
        b. 若剩余总数仍超过 max_jobs，按 finished_monotonic 升序淘汰最老的终态 job；
        c. in-flight（pending/running）job 永不淘汰。
    """

    DEFAULT_MAX_JOBS = 500
    DEFAULT_JOB_TTL_SECONDS = 24 * 3600  # 24 小时

    def __init__(
        self,
        executor: Callable[..., Awaitable[Dict[str, int]]],
        *,
        max_concurrency: int = 2,
        max_jobs: int = DEFAULT_MAX_JOBS,
        job_ttl_seconds: float = DEFAULT_JOB_TTL_SECONDS,
    ):
        self.executor = executor
        self._jobs: Dict[str, DownloadJob] = {}
        # 惰性原语：py<=3.10 构造期急切绑定事件循环，asyncio.run 之后再
        # 构造会直接 RuntimeError（CI py3.9 实测）。首次使用时再创建。
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._lock: Optional[asyncio.Lock] = None
        self._max_concurrency = max(1, int(max_concurrency))
        self.max_jobs = max(1, int(max_jobs))
        self.job_ttl_seconds = max(0.0, float(job_ttl_seconds))

    @property
    def _sem(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    @property
    def sync_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def submit(
        self, url: str, *, overrides: Optional[Dict[str, Any]] = None
    ) -> DownloadJob:
        job_id = uuid.uuid4().hex[:12]
        job = DownloadJob(job_id=job_id, url=url)
        job.overrides = overrides or None
        async with self.sync_lock:
            self._prune_locked()
            self._jobs[job_id] = job
        # 异步调度，立即返回 job 给调用方
        job._task = asyncio.create_task(self._run(job))
        return job

    def _prune_locked(self) -> None:
        """持锁内调用：按 TTL + 容量上限剪裁终态 job。"""
        now = time.monotonic()

        # 1) TTL
        if self.job_ttl_seconds > 0:
            expired_ids = [
                jid
                for jid, j in self._jobs.items()
                if j.status in JobStatus.TERMINAL
                and j.finished_monotonic is not None
                and (now - j.finished_monotonic) > self.job_ttl_seconds
            ]
            for jid in expired_ids:
                self._jobs.pop(jid, None)

        # 2) 容量上限：只淘汰终态 job，保留 in-flight
        if len(self._jobs) < self.max_jobs:
            return
        terminal_jobs = [
            (j.finished_monotonic or 0.0, jid)
            for jid, j in self._jobs.items()
            if j.status in JobStatus.TERMINAL
        ]
        terminal_jobs.sort(key=lambda pair: pair[0])
        overflow = len(self._jobs) - self.max_jobs + 1  # +1 是为新 job 腾位
        for _, jid in terminal_jobs[:overflow]:
            self._jobs.pop(jid, None)

    async def _run(self, job: DownloadJob) -> None:
        token = CURRENT_JOB.set(job)
        try:
            async with self._sem:
                job.status = JobStatus.RUNNING
                job.started_at = _now_iso()
                job.started_monotonic = time.monotonic()
                job.step = "执行下载"
                try:
                    # Overrides are forwarded only when present, so the documented
                    # single-argument executor contract keeps working for existing
                    # callers and tests that swap in their own ``executor``.
                    if job.overrides:
                        counts = await self.executor(job.url, job.overrides)
                    else:
                        counts = await self.executor(job.url)
                    job.total = int(counts.get("total", 0))
                    job.success = int(counts.get("success", 0))
                    job.failed = int(counts.get("failed", 0))
                    job.skipped = int(counts.get("skipped", 0))
                    job.processed = job.success + job.failed + job.skipped
                    # 只要跑完就是 success；具体成功/失败个数通过字段区分
                    job.status = JobStatus.SUCCESS if job.failed == 0 else JobStatus.FAILED
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    job.status = JobStatus.FAILED
                    job.error = f"{type(exc).__name__}: {exc}"
        except asyncio.CancelledError:
            # 用户取消：吞掉 CancelledError，让任务正常收尾，避免
            # "Task exception was never retrieved" 噪声。
            job.status = JobStatus.CANCELLED
            job.error = job.error or "已取消"
        finally:
            if job.status not in JobStatus.TERMINAL:
                job.status = JobStatus.CANCELLED
                job.error = job.error or "已取消"
            job.paused = False
            job.current = None
            job.step = job.step or "已结束"
            job.finished_at = _now_iso()
            job.finished_monotonic = time.monotonic()
            CURRENT_JOB.reset(token)

    async def get(self, job_id: str) -> Optional[DownloadJob]:
        async with self.sync_lock:
            return self._jobs.get(job_id)

    async def list_jobs(self) -> List[DownloadJob]:
        async with self.sync_lock:
            return list(self._jobs.values())

    def set_max_concurrency(self, value: int) -> None:
        """Swap the worker semaphore so a settings change applies live.

        Existing in-flight jobs keep their slot until they finish; only the
        number of *new* jobs allowed to start in parallel changes.
        """
        # 置空让下一次使用按新并发数重建（set_max_concurrency 本身可能运行
        # 在无事件循环的同步上下文里）。
        self._semaphore = None
        self._max_concurrency = max(1, int(value))

    async def cancel(self, job_id: str) -> Optional[DownloadJob]:
        """Stop a queued or running job. Returns the job, or None if unknown."""
        async with self.sync_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        job.cancel_requested = True
        # 必须先解除暂停：暂停中的 job 正卡在 gate 上，不放开的话
        # task.cancel() 也会等它先返回，取消看起来像没反应。
        job.resume()
        if job.status in JobStatus.TERMINAL:
            return job
        if job._task is not None and not job._task.done():
            job._task.cancel()
            # 等任务真正收尾再返回，否则调用方（HTTP 处理器）拿到的是还没
            # 翻成 cancelled 的旧状态，按钮点下去像没反应。5 秒上限只是
            # 防御——CancelledError 会在下一个 await 点立刻抛出。
            try:
                await asyncio.wait_for(job._task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:  # noqa: BLE001 - 任务自身的失败已经记在 job 上
                pass
        else:
            job.status = JobStatus.CANCELLED
            job.error = job.error or "已取消"
            job.finished_at = _now_iso()
            job.finished_monotonic = time.monotonic()
        return job

    async def remove(self, job_id: str) -> bool:
        """从列表里彻底删掉一个任务（活跃的先取消）。

        只影响内存中的任务记录，磁盘上已下载的文件一律保留。
        """
        job = await self.cancel(job_id)
        if job is None:
            return False
        async with self.sync_lock:
            self._jobs.pop(job_id, None)
        return True

    async def clear_finished(self, *, include_active: bool = False) -> int:
        """清理终态任务，返回删除条数。

        ``include_active`` 为 True 时连同排队中/下载中的任务一起清掉
        （会先取消它们，避免留下没有引用的协程）。
        """
        if include_active:
            for job in await self.list_jobs():
                if job.status not in JobStatus.TERMINAL:
                    await self.cancel(job.job_id)
        async with self.sync_lock:
            targets = (
                list(self._jobs) if include_active
                else [jid for jid, j in self._jobs.items() if j.status in JobStatus.TERMINAL]
            )
            for jid in targets:
                self._jobs.pop(jid, None)
            return len(targets)

    async def set_paused(self, job_id: str, paused: bool) -> Optional[DownloadJob]:
        """Pause/resume a job. Pausing takes effect before its next API call.

        单文件传输途中无法中断（aiohttp 流式下载不经过限速器），所以暂停是
        在「当前作品下载完之后、下一次请求之前」生效——对作者主页这类批量
        任务就是逐条暂停。
        """
        async with self.sync_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in JobStatus.TERMINAL:
            return job
        if paused:
            job.pause()
            job.detail = "已暂停（当前作品下载完后停止）"
        else:
            job.resume()
            job.detail = ""
        return job

    async def shutdown(self) -> None:
        """等待所有 pending/running 任务结束。"""
        tasks = [j._task for j in self._jobs.values() if j._task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
