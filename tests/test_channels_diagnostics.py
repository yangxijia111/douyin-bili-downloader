"""链路诊断计数器与分级状态测试（纯离线，无 mitmproxy 依赖）。

覆盖规范要求的 11 个计数器、九级状态链与 A–F 六种故障诊断。
"""

from __future__ import annotations

from channels.diagnostics import COUNTER_KEYS, ChannelsDiagnostics


class TestCounters:
    def test_all_spec_counters_exist_and_start_at_zero(self):
        diagnostics = ChannelsDiagnostics()
        counters = diagnostics.counters()
        for key in COUNTER_KEYS:
            assert key in counters, f"缺少规范计数器 {key}"
            assert counters[key] == 0
        # 规范要求的 11 个一个不少。
        assert len(COUNTER_KEYS) == 11

    def test_incr_unknown_key_is_ignored(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.incr("not_a_counter")
        assert "not_a_counter" not in diagnostics.counters()

    def test_record_feeds_updates_counter_and_strategy_stats(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.record_feeds(3, strategy="page_network_hook")
        diagnostics.record_feeds(2, strategy="passive_response")
        assert diagnostics.get("parsed_feeds") == 5
        assert diagnostics.strategy_stats() == {
            "page_network_hook": 3,
            "passive_response": 2,
        }
        assert diagnostics.last_feed_at is not None

    def test_record_feeds_zero_is_noop(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.record_feeds(0, strategy="passive_response")
        assert diagnostics.get("parsed_feeds") == 0
        assert diagnostics.strategy_stats() == {}

    def test_record_heartbeat_tracks_page_and_button_peak(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.record_heartbeat(page_type="home", page_url="/web/pages/home", buttons_created=1)
        diagnostics.record_heartbeat(page_type="home", buttons_created=3)
        diagnostics.record_heartbeat(page_type="home", buttons_created=1)
        assert diagnostics.get("frontend_heartbeat") == 3
        # 按钮数取峰值（当前存在数量），不是累计创建次数。
        assert diagnostics.get("buttons_created") == 3

    def test_record_parse_error_truncated(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.record_parse_error("x" * 1000)
        assert len(diagnostics.snapshot()["last_parse_error"]) == 300


class TestStageLadder:
    """A–F 六种故障 + 正常路径，逐级点亮。"""

    def test_a_no_proxy_connection(self):
        diagnostics = ChannelsDiagnostics()
        assert diagnostics.stage() == "no_proxy"
        advice = diagnostics.advice()
        assert advice["level"] == "error"
        assert "系统代理" in advice["message"]

    def test_b_proxy_but_no_target_domain(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.incr("proxy_connections", 5)
        assert diagnostics.stage() == "no_target_domain"
        advice = diagnostics.advice()
        assert advice["level"] == "error"
        assert "channels.weixin.qq.com" in advice["message"]

    def test_target_domain_but_no_html(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.incr("proxy_connections")
        diagnostics.incr("target_domain_connections")
        assert diagnostics.stage() == "target_domain"
        assert diagnostics.advice()["level"] == "warn"

    def test_c_html_seen_but_injection_failed(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.incr("proxy_connections")
        diagnostics.incr("target_domain_connections")
        diagnostics.incr("html_pages_seen", 3)
        assert diagnostics.stage() == "html_seen"
        advice = diagnostics.advice()
        assert advice["level"] == "error"
        assert "注入失败" in advice["message"]

    def test_d_injected_but_no_heartbeat(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.incr("proxy_connections")
        diagnostics.incr("target_domain_connections")
        diagnostics.incr("html_pages_seen")
        diagnostics.incr("injected_pages", 2)
        assert diagnostics.stage() == "injected"
        advice = diagnostics.advice()
        assert advice["level"] == "error"
        assert "CSP" in advice["message"]

    def test_heartbeat_but_no_buttons(self):
        diagnostics = ChannelsDiagnostics()
        self._reach_injected(diagnostics)
        diagnostics.record_heartbeat(page_type="home", buttons_created=0)
        assert diagnostics.stage() == "heartbeat"
        assert diagnostics.advice()["level"] == "warn"

    def test_e_buttons_but_no_feeds(self):
        diagnostics = ChannelsDiagnostics()
        self._reach_injected(diagnostics)
        diagnostics.record_heartbeat(page_type="home", buttons_created=2)
        assert diagnostics.stage() == "buttons"
        advice = diagnostics.advice()
        assert advice["level"] == "warn"
        assert "数据捕获规则失效" in advice["message"]

    def test_f_feed_captured(self):
        diagnostics = ChannelsDiagnostics()
        self._reach_injected(diagnostics)
        diagnostics.record_heartbeat(page_type="home", buttons_created=2)
        diagnostics.record_feeds(4, strategy="page_network_hook")
        assert diagnostics.stage() == "feed_captured"
        assert diagnostics.advice()["level"] == "ok"

    @staticmethod
    def _reach_injected(diagnostics: ChannelsDiagnostics) -> None:
        diagnostics.incr("proxy_connections")
        diagnostics.incr("target_domain_connections")
        diagnostics.incr("html_pages_seen")
        diagnostics.incr("injected_pages")


class TestChain:
    def test_chain_first_failure_is_current(self):
        diagnostics = ChannelsDiagnostics()
        chain = diagnostics.chain(downloads_success=0)
        assert [s["key"] for s in chain] == [
            "proxy", "domain", "html", "inject", "heartbeat", "buttons", "feed", "download",
        ]
        assert chain[0]["current"] is True
        assert all(s["ok"] is False for s in chain)

    def test_chain_all_ok_has_no_current(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.incr("proxy_connections")
        diagnostics.incr("target_domain_connections")
        diagnostics.incr("html_pages_seen")
        diagnostics.incr("injected_pages")
        diagnostics.record_heartbeat(page_type="home", buttons_created=1)
        diagnostics.record_feeds(2, strategy="passive_response")
        chain = diagnostics.chain(downloads_success=1)
        assert all(s["ok"] for s in chain)
        assert not any(s["current"] for s in chain)
        assert chain[-1]["value"] == 1

    def test_chain_partial_progress(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.incr("proxy_connections")
        diagnostics.incr("target_domain_connections")
        diagnostics.incr("html_pages_seen")
        chain = diagnostics.chain()
        ok_keys = [s["key"] for s in chain if s["ok"]]
        current = next(s for s in chain if s["current"])
        assert ok_keys == ["proxy", "domain", "html"]
        assert current["key"] == "inject"

    def test_snapshot_shape(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.incr("proxy_connections")
        snapshot = diagnostics.snapshot(downloads_success=0)
        # proxy_connections > 0 但未命中目标域 → no_target_domain。
        assert snapshot["stage"] == "no_target_domain"
        assert snapshot["level"] == "error"
        assert snapshot["stage_label"]
        assert isinstance(snapshot["chain"], list)
        assert snapshot["uptime_seconds"] >= 0
        assert snapshot["counters"]["proxy_connections"] == 1
