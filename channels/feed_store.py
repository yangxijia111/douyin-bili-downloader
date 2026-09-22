"""视频号嗅探会话的捕获存储。

mitmproxy 的 addon 回调与下载协程跑在同一个 asyncio 事件循环里（单线程），
所以 :meth:`FeedStore.add` 做成同步方法、无锁；新捕获通过 :class:`asyncio.Event`
通知消费方（CLI 实时表格 / Server 轮询端点 / 自动下载协程）。

去重键是 ``objectId``（一条动态的稳定标识）；同一条动态再次刷到（推荐流重复
下发）时只刷新直链等时效字段——微信 CDN 的 URL 带一次性签名参数，新的响应
里参数更新鲜——不重新入队，避免重复下载。
"""

from __future__ import annotations

import asyncio
from typing import Dict, Iterable, List, Optional

from channels.feed import ChannelFeed

__all__ = ["FeedStore"]


class FeedStore:
    def __init__(self, *, max_items: int = 2000):
        # dict 保持插入顺序；键为 dedup_key（objectId 优先）。
        self._feeds: Dict[str, ChannelFeed] = {}
        # 展示/查询索引：feed_id（nonce 优先，回退 objectId）→ dedup_key。
        self._id_index: Dict[str, str] = {}
        self._new_event = asyncio.Event()
        self.max_items = max_items

    # -- 供 mitmproxy addon（事件循环线程内）同步调用 -----------------------

    def add(self, feeds: Iterable[ChannelFeed]) -> List[ChannelFeed]:
        """合入一批捕获，返回其中**新出现**的 feed（调用方据此触发下载/UI）。"""
        fresh: List[ChannelFeed] = []
        for feed in feeds:
            key = feed.dedup_key
            existing = self._feeds.get(key)
            if existing is not None:
                # 重复刷到：只刷新时效字段（直链签名参数是新的），状态保持
                # 不变——已完成的不会重复入队。
                existing.url = feed.url or existing.url
                if feed.decode_key:
                    existing.decode_key = feed.decode_key
                if feed.images:
                    existing.images = feed.images
                if feed.bgm_url:
                    existing.bgm_url = feed.bgm_url
                if feed.specs:
                    existing.specs = feed.specs
                continue
            self._feeds[key] = feed
            self._id_index[feed.feed_id] = key
            self._id_index.setdefault(feed.object_id, key)
            fresh.append(feed)
        if fresh:
            self._trim()
            self._new_event.set()
        return fresh

    # -- 供消费协程异步使用 -------------------------------------------------

    async def wait_for_new(self, timeout: Optional[float] = None) -> bool:
        """等待新捕获；返回是否等到（超时返回 False）。事件由调用方清除。"""
        if timeout is None:
            await self._new_event.wait()
            return True
        try:
            await asyncio.wait_for(self._new_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def clear_new_event(self) -> None:
        self._new_event.clear()

    @property
    def has_new(self) -> bool:
        return self._new_event.is_set()

    # -- 查询与状态 ----------------------------------------------------------

    def get(self, feed_id: str) -> Optional[ChannelFeed]:
        key = self._id_index.get(feed_id)
        if key is None:
            # 兼容直接传 dedup_key 的调用方。
            return self._feeds.get(feed_id)
        return self._feeds.get(key)

    def all(self) -> List[ChannelFeed]:
        """按捕获先后返回（前端展示时自行反转取最新在前）。"""
        return list(self._feeds.values())

    def __len__(self) -> int:
        return len(self._feeds)

    def set_status(
        self,
        feed: ChannelFeed,
        status: str,
        *,
        error: str = "",
        downloaded_paths: Optional[List[str]] = None,
    ) -> None:
        feed.status = status
        if error:
            feed.error = error
        if downloaded_paths:
            feed.downloaded_paths = list(downloaded_paths)

    def _trim(self) -> None:
        """超过容量时淘汰最旧的 pending/failed 条目（done 状态保留，供增量判断展示）。"""
        if len(self._feeds) <= self.max_items:
            return
        overflow = len(self._feeds) - self.max_items
        drop_keys = []
        for key, feed in self._feeds.items():
            if overflow == 0:
                break
            if feed.status in ("pending", "failed"):
                drop_keys.append(key)
                overflow -= 1
        for key in drop_keys:
            feed = self._feeds.pop(key, None)
            if feed:
                self._id_index.pop(feed.feed_id, None)
