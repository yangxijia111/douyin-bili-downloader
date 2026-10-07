"""Server 视频号状态端点的诊断负载测试（离线）。

验证 Web 控制台「视频号」页拿到的数据足以渲染九级状态链，
而不是笼统的「暂无嗅探结果」。
"""

from __future__ import annotations

import pytest

from config import ConfigLoader
from core.downloader_base import DownloadResult

fastapi_testclient = pytest.importorskip("fastapi.testclient")
httpx = pytest.importorskip("httpx")

from server.app import build_app  # noqa: E402


def _client(tmp_path):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path))
    app = build_app(config)
    return fastapi_testclient.TestClient(app)


def test_status_includes_diagnostics_chain(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/v1/channels/status")
        assert resp.status_code == 200
        payload = resp.json()
        diagnostics = payload["diagnostics"]
        # 未启动会话：第一级（代理连接）就是断点。
        assert diagnostics["stage"] == "no_proxy"
        assert diagnostics["level"] == "error"
        assert diagnostics["message"]
        chain = diagnostics["chain"]
        assert [s["key"] for s in chain] == [
            "proxy", "domain", "html", "inject", "heartbeat", "buttons", "feed", "download",
        ]
        assert chain[0]["current"] is True
        counters = diagnostics["counters"]
        for key in (
            "proxy_connections",
            "target_domain_connections",
            "tls_intercepted",
            "html_pages_seen",
            "js_bundles_seen",
            "candidate_responses",
            "json_responses",
            "object_desc_responses",
            "parsed_feeds",
            "injected_pages",
            "frontend_heartbeat",
        ):
            assert key in counters
        # 新配置键：自动下载默认关闭 + 页面注入默认开启。
        assert payload["auto_download"] is False
        assert payload["inject_ui"] is True


def test_feeds_endpoint_has_strategy_stats(tmp_path):
    with _client(tmp_path) as client:
        resp = client.get("/api/v1/channels/feeds")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["running"] is False
        assert payload["strategy_stats"] == {}


def _running_manager(tmp_path, connection):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from server.channels import ChannelsSessionManager

    manager = ChannelsSessionManager(ConfigLoader(None), Mock())
    manager._cert_manager = SimpleNamespace(ca_cert_path=tmp_path / "absent.cer")
    manager._connection_monitor = Mock()
    manager._connection_monitor.snapshot.return_value = connection
    manager._running = {"port": 8899, "stats": DownloadResult(), "auto": False, "store": []}
    manager.diagnostics.incr("proxy_connections", 100)
    return manager


def test_status_identifies_connection_proxy_mismatch(tmp_path):
    manager = _running_manager(tmp_path, {
        "supported": True, "wechat_running": True, "proxy_connected": False,
        "connection_proxy_matches": False, "connection_proxy_ports": [7897],
    })
    payload = manager.status()
    assert payload["diagnostics"]["stage"] == "no_target_domain"
    assert "7897" in payload["diagnostics"]["message"]
    assert "8899" in payload["diagnostics"]["message"]
    assert "不一致" in payload["diagnostics"]["message"]
    manager._connection_monitor.refresh.assert_called_once_with(8899)


def test_other_proxy_clients_do_not_prove_wechat_connection(tmp_path):
    manager = _running_manager(tmp_path, {
        "supported": True, "wechat_running": True, "proxy_connected": False,
        "other_proxy_ports": [7897],
    })
    assert "其他应用" in manager.status()["diagnostics"]["message"]


def test_read_failure_keeps_generic_advice_and_working_capture_is_not_overridden(tmp_path):
    manager = _running_manager(tmp_path, {"wechat_running": None, "proxy_connected": None})
    assert manager.status()["diagnostics"]["message"] == manager.diagnostics.advice()["message"]
    manager._connection_monitor.snapshot.return_value = {
        "wechat_running": True, "proxy_connected": False,
        "connection_proxy_matches": False, "connection_proxy_ports": [7897],
    }
    manager.diagnostics.incr("target_domain_connections")
    assert manager.status()["diagnostics"]["message"] == manager.diagnostics.advice()["message"]
