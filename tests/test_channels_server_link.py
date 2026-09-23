"""Server 分享链接端点测试（离线）。"""

from __future__ import annotations

import pytest

from config import ConfigLoader

fastapi_testclient = pytest.importorskip("fastapi.testclient")
httpx = pytest.importorskip("httpx")

from server.app import build_app  # noqa: E402

_SHARE_URL = "https://weixin.qq.com/sph/AseYzCvBg3"


def _client(tmp_path):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path))
    app = build_app(config)
    return fastapi_testclient.TestClient(app)


def test_link_parse_valid(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/v1/channels/link/parse", json={"url": _SHARE_URL})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["share_id"] == "AseYzCvBg3"
        assert payload["full_url"] == (
            "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AseYzCvBg3"
        )


def test_link_parse_invalid(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/v1/channels/link/parse", json={"url": "https://example.com"})
        assert resp.status_code == 400
        assert "分享链接" in resp.json()["detail"]


def test_link_parse_missing_url(tmp_path):
    with _client(tmp_path) as client:
        resp = client.post("/api/v1/channels/link/parse", json={})
        assert resp.status_code == 400


def test_link_start_invalid_url(tmp_path):
    """无效链接不得启动会话。"""
    with _client(tmp_path) as client:
        resp = client.post("/api/v1/channels/link", json={"url": "nope"})
        assert resp.status_code == 400
        # 会话没有被动启动。
        status = client.get("/api/v1/channels/status").json()
        assert status["running"] is False


def test_link_clear(tmp_path):
    with _client(tmp_path) as client:
        resp = client.request("DELETE", "/api/v1/channels/link")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
