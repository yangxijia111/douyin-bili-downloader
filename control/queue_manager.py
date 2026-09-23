import asyncio
from typing import Any, Callable, List, Optional, TypeVar

from utils.logger import setup_logger

logger = setup_logger("QueueManager")

T = TypeVar("T")


class QueueManager:
    def __init__(self, max_workers: int = 5):
        self.max_workers = max_workers
        # 惰性信号量：asyncio.Semaphore() 在 py<=3.10 构造时急切绑定当前
        # 事件循环，"同步流程中 asyncio.run() 之后再构造"的合法序列会直接
        # RuntimeError（CI py3.9 双平台实测）。改为首次使用（必然在事件
        # 循环内）时创建。
        self._semaphore: Optional[asyncio.Semaphore] = None

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_workers)
        return self._semaphore

    async def process_tasks(self, tasks: List[Callable], *args, **kwargs) -> List[Any]:
        # Failures surface as exception instances in the result list (via
        # return_exceptions=True). Callers can filter with isinstance(r, BaseException).
        async def _task_wrapper(task):
            async with self.semaphore:
                try:
                    return await task(*args, **kwargs)
                except Exception:
                    logger.exception("Task failed")
                    raise

        return await asyncio.gather(
            *[_task_wrapper(task) for task in tasks], return_exceptions=True
        )

    async def download_batch(self, download_func: Callable, items: List[Any]) -> List[Any]:
        async def _download_wrapper(item):
            async with self.semaphore:
                try:
                    return await download_func(item)
                except Exception:
                    logger.exception("Download failed for item: %r", item)
                    raise

        return await asyncio.gather(
            *[_download_wrapper(item) for item in items], return_exceptions=True
        )
