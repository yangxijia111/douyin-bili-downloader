"""Server 视频号状态端点的诊断负载测试（离线）。

验证 Web 控制台「视频号」页拿到的数据足以渲染九级状态链，
而不是笼统的「暂无嗅探结果」。
"""

from __future__ import annotations

import pytest

from config import ConfigLoader

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
