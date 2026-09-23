"""FeedCapturePipeline 四策略统一捕获测试（离线）。"""

from __future__ import annotations

from channels.diagnostics import ChannelsDiagnostics
from channels.feed_store import FeedStore
from channels.pipeline import (
    STRATEGY_NETWORK_HOOK,
    STRATEGY_PASSIVE,
    STRATEGY_RUNTIME_HOOK,
    FeedCapturePipeline,
)


def _node(object_id: str, *, nonce: str = "", title: str = "") -> dict:
    return {
        "objectId": object_id,
        "objectNonceId": nonce or f"n_{object_id}",
        "contact": {"nickname": "作者"},
        "objectDesc": {
            "mediaType": 4,
            "description": title or f"视频{object_id}",
            "media": [{"url": f"https://cdn/{object_id}.mp4", "decodeKey": "123"}],
        },
    }


class TestPassiveStrategy:
    def test_ingest_passive_adds_feeds(self):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        fresh = pipeline.ingest_passive(
            {"data": [_node("a"), _node("b")]}, source_api="/finderPcFlow"
        )
        assert [f.object_id for f in fresh] == ["a", "b"]
        assert len(store) == 2
        assert diagnostics.get("parsed_feeds") == 2
        assert diagnostics.strategy_stats() == {STRATEGY_PASSIVE: 2}
        # 来源策略记录在 source_api 上（诊断可见）。
        assert fresh[0].source_api == "/finderPcFlow"

    def test_ingest_passive_dedup(self):
        store = FeedStore()
        pipeline = FeedCapturePipeline(store, ChannelsDiagnostics())
        pipeline.ingest_passive({"data": [_node("a")]})
        fresh = pipeline.ingest_passive({"data": [_node("a")]})
        assert fresh == []
        assert len(store) == 1


class TestPageStrategies:
    def test_ingest_page_nodes_network_hook(self):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        fresh = pipeline.ingest_page_nodes(
            [_node("a")], strategy=STRATEGY_NETWORK_HOOK, page="home"
        )
        assert len(fresh) == 1
        assert diagnostics.strategy_stats() == {STRATEGY_NETWORK_HOOK: 1}
        assert fresh[0].source_api == f"page:{STRATEGY_NETWORK_HOOK}:home"

    def test_ingest_page_nodes_runtime_hook(self):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        fresh = pipeline.ingest_page_nodes(
            [_node("a")], strategy=STRATEGY_RUNTIME_HOOK, page="feed"
        )
        assert len(fresh) == 1
        assert diagnostics.strategy_stats() == {STRATEGY_RUNTIME_HOOK: 1}

    def test_unknown_strategy_label_falls_back(self):
        """前端伪造/未知策略标签不得污染统计（归并到 network_hook）。"""
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        pipeline.ingest_page_nodes([_node("a")], strategy="evil_strategy")
        assert diagnostics.strategy_stats() == {STRATEGY_NETWORK_HOOK: 1}

    def test_non_dict_nodes_filtered(self):
        store = FeedStore()
        pipeline = FeedCapturePipeline(store, ChannelsDiagnostics())
        fresh = pipeline.ingest_page_nodes([_node("a"), "junk", None, 42])
        assert len(fresh) == 1
        assert len(store) == 1

    def test_mixed_strategies_unify_in_one_store(self):
        """A/B/C 三路捕获汇入同一 FeedStore，按 objectId 去重。"""
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        pipeline.ingest_passive({"data": [_node("a")]})
        pipeline.ingest_page_nodes([_node("a"), _node("b")], strategy=STRATEGY_NETWORK_HOOK)
        pipeline.ingest_page_nodes([_node("b"), _node("c")], strategy=STRATEGY_RUNTIME_HOOK)
        assert len(store) == 3
        assert diagnostics.get("parsed_feeds") == 3
        assert diagnostics.strategy_stats() == {
            STRATEGY_PASSIVE: 1,
            STRATEGY_NETWORK_HOOK: 1,
            STRATEGY_RUNTIME_HOOK: 1,
        }

    def test_extract_exception_does_not_propagate(self):
        """提取异常必须被吞掉并记入诊断（不拖垮代理/页面）。"""

        class _Exploding(dict):
            def values(self):
                raise RuntimeError("boom")

        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        fresh = pipeline.ingest_page_nodes([_Exploding()])
        assert fresh == []
        assert diagnostics.snapshot()["last_parse_error"]

    def test_on_capture_callback_failure_ignored(self):
        def _boom(_feeds):
            raise RuntimeError("callback boom")

        store = FeedStore()
        pipeline = FeedCapturePipeline(store, ChannelsDiagnostics(), on_capture=_boom)
        fresh = pipeline.ingest_passive({"data": [_node("a")]})
        assert len(fresh) == 1  # 捕获本身不受回调异常影响

    def test_strategy_report(self):
        store = FeedStore()
        diagnostics = ChannelsDiagnostics()
        pipeline = FeedCapturePipeline(store, diagnostics)
        pipeline.ingest_passive({"data": [_node("a")]})
        assert pipeline.strategy_report() == {STRATEGY_PASSIVE: 1}
