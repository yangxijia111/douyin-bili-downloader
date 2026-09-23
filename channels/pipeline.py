"""视频号 Feed 捕获流水线：把四种捕获策略统一送入 :class:`FeedStore`。

v2.0.1 只有一种捕获方式——mitmproxy 被动响应（Strategy A）。Windows 真机
实测证明它不足以覆盖真实微信链路：页面数据可能只存在于前端运行时对象里，
或走未解密域。v2.0.2 起页面注入成为主要捕获方案，四种策略并行::

    A. passive_response      mitmproxy 被动响应（保留，SnifferAddon）
    B. page_network_hook     注入脚本 hook window.fetch / XMLHttpRequest
    C. page_runtime_hook     注入脚本包装 finderPcFlow 等页面运行时函数
    D. compatibility_patch   res.wx.qq.com JS bundle 兼容补丁（框架就绪，
                             默认无补丁；只有真机证据才登记，见 patches.py）

B/C 的策略标签由注入脚本通过 ``/__cuin/feed`` 桥接上传时声明；D 的产物
（改写后的 JS 让页面 hook 生效）最终仍经 B/C 汇入。所有策略共用同一套
提取逻辑（:func:`channels.feed.extract_feeds`）与同一份 FeedStore，去重 /
淘汰 / 通知语义完全一致，四种来源对下游不可见。

设计约束：

* 单事件循环内同步调用（与 FeedStore 一致），无线锁；
* 任何策略的解析异常都被吞掉并记入诊断（``record_parse_error``），绝不
  拖垮代理或页面；
* 每条成功入库都按策略计数（``strategy_stats``），真机验收时能直接看出
  当前是哪种策略在供数。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

from channels.diagnostics import ChannelsDiagnostics
from channels.feed import ChannelFeed, extract_feeds, extract_preview_feeds
from channels.feed_store import FeedStore

__all__ = [
    "STRATEGY_PASSIVE",
    "STRATEGY_NETWORK_HOOK",
    "STRATEGY_RUNTIME_HOOK",
    "STRATEGY_PATCH",
    "FeedCapturePipeline",
]

STRATEGY_PASSIVE = "passive_response"
STRATEGY_NETWORK_HOOK = "page_network_hook"
STRATEGY_RUNTIME_HOOK = "page_runtime_hook"
STRATEGY_PATCH = "compatibility_patch"

# 桥接上传允许声明的策略集合（防止前端伪造策略标签污染统计）。
ALLOWED_PAGE_STRATEGIES = (STRATEGY_NETWORK_HOOK, STRATEGY_RUNTIME_HOOK)


class FeedCapturePipeline:
    """四策略统一入口：nodes → extract_feeds → FeedStore.add → 诊断/回调。"""

    def __init__(
        self,
        store: FeedStore,
        diagnostics: Optional[ChannelsDiagnostics] = None,
        on_capture: Optional[Callable[[List[ChannelFeed]], None]] = None,
    ) -> None:
        self.store = store
        self.diagnostics = diagnostics
        self.on_capture = on_capture

    # ------------------------------------------------------------------
    # 策略 A：mitmproxy 被动响应
    # ------------------------------------------------------------------

    def ingest_passive(
        self, payload: Any, *, source_api: str = ""
    ) -> List[ChannelFeed]:
        """被动响应整棵 JSON 直接提取（调用方已做过 objectDesc 预检）。"""
        return self._ingest(
            payload, strategy=STRATEGY_PASSIVE, source_api=source_api
        )

    # ------------------------------------------------------------------
    # 策略 B/C：页面桥接上传（fetch/XHR hook 或运行时 hook 捕获的节点）
    # ------------------------------------------------------------------

    def ingest_page_nodes(
        self,
        nodes: Iterable[Any],
        *,
        strategy: str = STRATEGY_NETWORK_HOOK,
        page: str = "",
    ) -> List[ChannelFeed]:
        """页面上传的**原始 feed 节点**（含 objectDesc 的 dict）批量入库。

        节点保持微信原始结构，字段提取仍由 Python 侧唯一实现
        （:func:`extract_feeds`）——前端不做字段解析，避免两套逻辑漂移。
        ``strategy`` 只做统计归因，不改变提取行为；非法标签按
        ``page_network_hook`` 归并。
        """
        label = strategy if strategy in ALLOWED_PAGE_STRATEGIES else STRATEGY_NETWORK_HOOK
        source_api = f"page:{label}" + (f":{page}" if page else "")
        node_list = [n for n in nodes if isinstance(n, dict)]
        return self._ingest(node_list, strategy=label, source_api=source_api)

    # ------------------------------------------------------------------
    # 策略 D：兼容补丁（patches.py 产出可提取 JSON 时由此入库）
    # ------------------------------------------------------------------

    def ingest_patch(self, payload: Any, *, source_api: str = "") -> List[ChannelFeed]:
        """Strategy D 的入库通道（当前无补丁登记，通道预留给真机证据）。"""
        return self._ingest(
            payload, strategy=STRATEGY_PATCH, source_api=source_api
        )

    # ------------------------------------------------------------------
    # 统一内部实现
    # ------------------------------------------------------------------

    def _ingest(
        self, nodes: Any, *, strategy: str, source_api: str
    ) -> List[ChannelFeed]:
        try:
            feeds = extract_feeds(nodes, source_api=source_api)
            if not feeds:
                # 分享链接预览页（finder-preview）的 sceneInfo 模式：
                # 特征是 videoUrl / picInfo 而不是 objectDesc。
                feeds = extract_preview_feeds(nodes, source_api=source_api)
        except Exception as exc:  # noqa: BLE001 —— 单策略失败不影响其它策略
            if self.diagnostics is not None:
                self.diagnostics.record_parse_error(f"{strategy}: {exc}")
            return []
        if not feeds:
            return []
        fresh = self.store.add(feeds)
        if fresh and self.diagnostics is not None:
            self.diagnostics.record_feeds(len(fresh), strategy=strategy)
        if fresh and self.on_capture is not None:
            try:
                self.on_capture(fresh)
            except Exception:  # noqa: BLE001 —— 回调失败不影响捕获
                pass
        return fresh

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def strategy_report(self) -> Dict[str, int]:
        """各策略累计入库条数（Web 控制台「捕获策略分布」用）。"""
        if self.diagnostics is None:
            return {}
        return self.diagnostics.strategy_stats()
