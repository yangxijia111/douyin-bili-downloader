"""链路诊断计数器与分级状态测试（纯离线，无 mitmproxy 依赖）。

覆盖规范要求的 11 个计数器、九级状态链与 A–F 六种故障诊断。
"""

from __future__ import annotations

import json
from unittest.mock import patch

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
        # v2.0.4 起 E 类故障提示 MSE 分片旁路（点页面按钮直接保存）。
        assert "MSE" in advice["message"]

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


class TestMediaRequestRecording:
    """record_media_request：计数 + 环形 60 条截断 + 字段截断。"""

    def _diag(self):
        from channels.diagnostics import ChannelsDiagnostics

        return ChannelsDiagnostics()

    def test_counters_and_snapshot(self):
        d = self._diag()
        d.record_media_request(
            method="GET", status=206, range_header="bytes=0-1",
            content_range="bytes 0-1/100", content_type="video/mp4",
            content_length=2, url="https://finder.video.qq.com/x?k=1",
        )
        snap = d.snapshot()
        assert snap["counters"]["media_requests"] == 1
        assert snap["counters"]["media_request_bytes"] == 2
        assert snap["media_urls"][0]["status"] == 206

    def test_ring_buffer_keeps_last_60(self):
        d = self._diag()
        for i in range(65):
            d.record_media_request(url=f"https://finder.video.qq.com/x?seq={i}")
        snap = d.snapshot()
        assert snap["counters"]["media_requests"] == 65
        assert len(snap["media_urls"]) == 60
        assert "seq=5" in snap["media_urls"][0]["url"]
        assert "seq=64" in snap["media_urls"][-1]["url"]

    def test_long_url_truncated(self):
        d = self._diag()
        d.record_media_request(url="https://f/" + "a" * 3000)
        assert len(d.snapshot()["media_urls"][0]["url"]) == 1200


class TestObservedHosts:
    def test_records_only_normalized_hostnames_without_request_content(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.record_host("CHANNELS.WEIXIN.QQ.COM:443", source="sni", target=True)
        diagnostics.record_host("channels.weixin.qq.com", source="sni", target=True)
        for unsafe in (
            "https://channels.weixin.qq.com/private?token=secret",
            "channels.weixin.qq.com/private",
            "channels.weixin.qq.com?token=secret",
            "secret@channels.weixin.qq.com",
        ):
            diagnostics.record_host(unsafe, source="http", target=True)

        assert diagnostics.snapshot()["observed_hosts"] == [{
            "host": "channels.weixin.qq.com", "source": "sni", "count": 2, "target": True,
        }]

    def test_recent_host_summary_is_bounded_and_snapshot_cannot_modify_it(self):
        diagnostics = ChannelsDiagnostics()
        for number in range(35):
            diagnostics.record_host("host" + str(number) + ".example.com", source="server")
        hosts = diagnostics.snapshot()["observed_hosts"]
        assert len(hosts) == 32
        assert hosts[0]["host"] == "host3.example.com"
        assert hosts[-1]["host"] == "host34.example.com"
        hosts[0]["host"] = "changed.example.com"
        assert diagnostics.snapshot()["observed_hosts"][0]["host"] == "host3.example.com"

    def test_ipv6_host_summary_removes_brackets_and_port(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.record_host("[::1]:443", source="connect")
        diagnostics.record_host("::1", source="connect")
        assert diagnostics.snapshot()["observed_hosts"] == [{
            "host": "::1", "source": "connect", "count": 2, "target": False,
        }]


class TestPageProbes:
    def test_background_profile_heartbeat_does_not_replace_recent_playing_video(self):
        with patch("channels.diagnostics.time.time", return_value=100.0) as clock:
            diagnostics = ChannelsDiagnostics()
            diagnostics.record_heartbeat(
                page_type="feed", page_url="/web/pages/feed", probe={
                    "page_id": "video-page", "visibility": "visible",
                    "video": {"duration": 120.0, "current_time": 15.0},
                },
            )
            clock.return_value = 101.0
            diagnostics.record_heartbeat(
                page_type="profile", page_url="/web/pages/profile", probe={
                    "page_id": "profile-page", "visibility": "hidden", "video": None,
                },
            )
            snapshot = diagnostics.snapshot()

        assert snapshot["runtime_probe"]["video"]["duration"] == 120.0
        assert snapshot["page_type"] == "feed"
        assert snapshot["page_url"] == "/web/pages/feed"
        assert snapshot["runtime_probe_at"] == 100.0
        assert snapshot["last_heartbeat_at"] == 101.0

    def test_progress_probe_is_bounded_finite_and_excludes_urls_html_and_binary(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.record_heartbeat(
            page_type="feed", page_url="https://channels.weixin.qq.com/web/pages/feed?token=secret",
            probe={
                "page_id": "playing", "visibility": "visible", "index_size": "invalid",
                "hooked": ["getFeed", "https://example.com/?token=secret", b"secret"],
                "video": {
                    "src": "https://example.com/video?token=secret", "outer": "<video secret>",
                    "parent_attrs": {"key": "secret"}, "duration": float("inf"),
                    "current_time": float("nan"), "ready_state": 100, "network_state": -1,
                    "buffered": [[0, 10], [float("nan"), 20], [-1, 20], [20, 10]],
                },
                "mse": {
                    "streams": 10 ** 100, "total_bytes": float("inf"),
                    "reason_code": "https://example.com/?token=secret",
                    "tracks": [{
                        "mime": 'video/mp4; codecs="avc1.640028"', "bytes": 10 ** 100,
                        "duration": 120.0, "ranges": [[number, number + 1] for number in range(20)],
                        "chunks": [b"secret"], "url": "https://example.com/?token=secret",
                    } for _ in range(12)],
                },
            },
        )
        snapshot = diagnostics.snapshot()
        probe = snapshot["runtime_probe"]
        serialized = json.dumps(snapshot["page_probes"], allow_nan=False)
        assert "secret" not in serialized
        assert "https://" not in serialized
        assert snapshot["page_url"] == "/web/pages/feed"
        assert probe["hooked"] == ["getFeed"]
        assert probe["index_size"] == 0
        assert probe["video"]["duration"] is None
        assert probe["video"]["current_time"] is None
        assert probe["video"]["buffered"] == [[0.0, 10.0]]
        assert probe["video"]["ready_state"] == 4
        assert probe["video"]["network_state"] == 0
        assert len(probe["mse"]["tracks"]) == 8
        assert len(probe["mse"]["tracks"][0]["ranges"]) == 16
        assert probe["mse"]["tracks"][0]["bytes"] == 9007199254740991
        assert probe["mse"]["total_bytes"] == 0
        assert probe["mse"]["reason_code"] == ""

    def test_recent_visible_video_wins_and_expired_video_cannot_mask_current_page(self):
        with patch("channels.diagnostics.time.time", return_value=100.0) as clock:
            diagnostics = ChannelsDiagnostics()
            for at, page_id, visibility in (
                (100.0, "first", "visible"),
                (101.0, "second", "visible"),
                (102.0, "hidden-video", "hidden"),
            ):
                clock.return_value = at
                diagnostics.record_heartbeat(page_type="feed", page_url="/web/pages/feed", probe={
                    "page_id": page_id, "visibility": visibility, "video": {"duration": 3182.0},
                })
            assert diagnostics.snapshot()["runtime_probe"]["page_id"] == "second"
            clock.return_value = 132.0
            assert diagnostics.snapshot()["runtime_probe"]["page_id"] == "hidden-video"
            clock.return_value = 133.0
            diagnostics.record_heartbeat(page_type="profile", page_url="/web/pages/profile", probe={
                "page_id": "profile", "visibility": "visible", "video": None,
            })
            snapshot = diagnostics.snapshot()
            assert snapshot["runtime_probe"]["page_id"] == "profile"
            assert snapshot["page_type"] == "profile"
            assert snapshot["runtime_probe_at"] == 133.0
            clock.return_value = 164.0
            assert diagnostics.snapshot()["runtime_probe"] == {}
            assert diagnostics.snapshot()["runtime_probe_at"] is None

    def test_page_probe_cache_is_bounded_isolated_from_snapshots_and_resettable(self):
        with patch("channels.diagnostics.time.time", return_value=100.0) as clock:
            diagnostics = ChannelsDiagnostics()
            for number in range(20):
                clock.return_value = 100.0 + number
                diagnostics.record_heartbeat(page_type="feed", page_url="/web/pages/feed", probe={
                    "page_id": "page-" + str(number), "visibility": "visible",
                    "video": {"duration": 100.0 + number},
                })
            snapshot = diagnostics.snapshot()
            assert len(snapshot["page_probes"]) == 16
            assert snapshot["page_probes"][0]["page_id"] == "page-4"
            assert snapshot["runtime_probe"]["page_id"] == "page-19"
            snapshot["page_probes"][0]["probe"]["video"]["duration"] = 999.0
            snapshot["runtime_probe"]["video"]["duration"] = 999.0
            retained = diagnostics.snapshot()
            assert retained["page_probes"][0]["probe"]["video"]["duration"] == 104.0
            assert retained["runtime_probe"]["video"]["duration"] == 119.0
            diagnostics.reset()
            assert diagnostics.snapshot()["page_probes"] == []
            assert diagnostics.snapshot()["runtime_probe"] == {}

    def test_playback_and_track_progress_preserve_the_agreed_safe_fields(self):
        diagnostics = ChannelsDiagnostics()
        diagnostics.record_heartbeat(page_type="feed", page_url="/web/pages/feed", probe={
            "page_id": "replay-page", "visibility": "visible",
            "video": {
                "current_time": 22.0, "duration": 3182.0, "ready_state": 3, "network_state": 2,
                "paused": False, "ended": False, "buffered": [[0.0, 30.0]], "src_kind": "blob",
            },
            "mse": {
                "streams": 2, "total_bytes": 8192, "truncated": 0, "current_bytes": 4096,
                "current_mime": "video/mp4", "complete": False, "reason_code": "coverage_gap",
                "tracks": [{
                    "mime": "video/mp4", "bytes": 4096, "duration": 3182.0,
                    "ranges": [[0.0, 12.0], [18.0, 30.0]], "truncated": False,
                    "source_finished": True, "initialized": True, "complete": False,
                    "reason_code": "coverage_gap",
                }],
            },
        })
        snapshot = diagnostics.snapshot()
        probe = snapshot["runtime_probe"]
        assert probe["video"] == {
            "current_time": 22.0, "duration": 3182.0, "ready_state": 3, "network_state": 2,
            "paused": False, "ended": False, "buffered": [[0.0, 30.0]], "src_kind": "blob",
        }
        assert probe["mse"]["reason_code"] == "coverage_gap"
        assert probe["mse"]["complete"] is False
        assert probe["mse"]["tracks"][0] == {
            "mime": "video/mp4", "bytes": 4096, "duration": 3182.0,
            "ranges": [[0.0, 12.0], [18.0, 30.0]], "truncated": False,
            "source_finished": True, "initialized": True, "complete": False,
            "reason_code": "coverage_gap",
        }
        assert snapshot["counters"]["parsed_feeds"] == 0
