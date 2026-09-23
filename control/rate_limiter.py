import asyncio
import random
import time
from typing import Optional


class RateLimiter:
    def __init__(self, max_per_second: float = 2):
        if max_per_second <= 0:
            max_per_second = 2
        self.max_per_second = max_per_second
        self.min_interval = 1.0 / max_per_second
        self.last_request = 0.0
        # 惰性锁（同 queue_manager.semaphore 约定）：避免 py<=3.10 的
        # 构造期事件循环绑定。
        self._lock: Optional[asyncio.Lock] = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self):
        async with self.lock:
            current = time.time()
            time_since_last = current - self.last_request

            if time_since_last < self.min_interval:
                wait_time = self.min_interval - time_since_last
                await asyncio.sleep(wait_time)

            # Jitter must run inside the lock so that the next caller waits
            # min_interval since the actual fire time, not since the prior
            # caller acquired the lock.
            await asyncio.sleep(random.uniform(0, 0.5))
            self.last_request = time.time()
