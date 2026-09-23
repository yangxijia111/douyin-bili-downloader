"""REST API 认证边界与 feed 脱敏测试（P0 安全边界）。

远程客户端用 httpx ``ASGITransport(client=...)`` 模拟（TestClient 固定以
``testclient`` 为 peer，会被视为本机——这正是本机行为不变的验收口径）。
"""

from __future__ import annotations

import pytest

from config import ConfigLoader

fastapi_testclient = pytest.importorskip("fastapi.testclient")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from channels.feed import ChannelFeed  # noqa: E402
from channels.feed_store import FeedStore  # noqa: E402
from core.downloader_base import DownloadResult  # noqa: E402
from server.app import build_app  # noqa: E402
from server.auth import (  # noqa: E402
    ENV_TOKEN_VAR,
    AuthPolicy,
    extract_presented_token,
    is_loopback_host,
    resolve_auth_token,
)
from server.channels import ChannelsSessionManager  # noqa: E402

REMOTE = ("192.0.2.10", 5000)  # TEST-NET-1，不可能是真实本机地址


def _remote_client(app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=REMOTE), base_url="http://testserver"
    )


class TestIsLoopbackHost:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "127.9.9.9", "localhost", "::1", "::ffff:127.0.0.1", "testclient", "", None]
    )
    def test_loopback(self, host):
        assert is_loopback_host(host)

    @pytest.mark.parametrize("host", ["192.0.2.10", "0.0.0.0", "::", "10.1.2.3", "evil", "127.0.0.1.evil.com"])
    def test_remote(self, host):
        assert not is_loopback_host(host)


class TestAuthPolicy:
    def test_loopback_always_allowed(self):
        policy = AuthPolicy("")
        ok, _, _ = policy.check("127.0.0.1", None)
        assert ok

    def test_remote_without_token_forbidden(self):
        ok, status, detail = AuthPolicy("").check("192.0.2.10", None)
        assert not ok and status == 403 and "auth_token" in detail

    def test_remote_with_wrong_token_unauthorized(self):
        ok, status, _ = AuthPolicy("secret").check("192.0.2.10", "wrong")
        assert not ok and status == 401
        ok, status, _ = AuthPolicy("secret").check("192.0.2.10", None)
        assert not ok and status == 401

    def test_remote_with_correct_token_allowed(self):
        ok, _, _ = AuthPolicy("secret").check("192.0.2.10", "secret")
        assert ok

    def test_resolve_token_env_overrides_config(self, monkeypatch):
        config = ConfigLoader(None)
        config.update(server={"auth_token": "from-config"})
        monkeypatch.setenv(ENV_TOKEN_VAR, "from-env")
        assert resolve_auth_token(config) == "from-env"
        monkeypatch.delenv(ENV_TOKEN_VAR)
        assert resolve_auth_token(config) == "from-config"

    def test_extract_presented_token(self):
        class H(dict):
            def get(self, key, default=None):
                return super().get(key, default)

        assert extract_presented_token(H({"X-Auth-Token": "abc"})) == "abc"
        assert extract_presented_token(H({"Authorization": "Bearer abc"})) == "abc"
        assert extract_presented_token(H({})) is None


class TestRemoteBoundaryHttp:
    """非环回客户端的 HTTP 层行为：403 / 401 / 放行。"""

    def _app(self, monkeypatch, token=None):
        monkeypatch.delenv(ENV_TOKEN_VAR, raising=False)
        config = ConfigLoader(None)
        if token:
            config.update(server={"auth_token": token})
        return build_app(config)

    @pytest.mark.asyncio
    async def test_remote_without_token_rejected_on_sensitive_endpoints(self, monkeypatch):
        app = self._app(monkeypatch, token=None)
        protected = [
            ("get", "/api/v1/config", None),
            ("get", "/api/v1/jobs", None),
            ("get", "/api/v1/channels/status", None),
            ("get", "/api/v1/channels/feeds", None),
            ("get", "/api/v1/channels/certificate", None),
            ("post", "/api/v1/download", {"url": "https://v.douyin.com/x/"}),
            ("post", "/api/v1/channels/start", {}),
            ("post", "/api/v1/channels/stop", {}),
            ("post", "/api/v1/channels/certificate/install", {}),
            ("post", "/api/v1/channels/auto-download", {"enabled": True}),
            ("put", "/api/v1/config", {"updates": {"thread": 1}}),
            ("post", "/api/v1/open-folder", {"path": ""}),
        ]
        async with _remote_client(app) as client:
            for method, path, body in protected:
                response = await client.request(method, path, json=body)
                assert response.status_code == 403, f"{method} {path} -> {response.status_code}"

    @pytest.mark.asyncio
    async def test_remote_health_stays_open(self, monkeypatch):
        app = self._app(monkeypatch, token=None)
        async with _remote_client(app) as client:
            response = await client.get("/api/v1/health")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_remote_with_wrong_token_unauthorized(self, monkeypatch):
        app = self._app(monkeypatch, token="right-token")
        async with _remote_client(app) as client:
            response = await client.get("/api/v1/config", headers={"X-Auth-Token": "wrong"})
            assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_remote_with_correct_token_allowed(self, monkeypatch):
        app = self._app(monkeypatch, token="right-token")
        async with _remote_client(app) as client:
            response = await client.get("/api/v1/config", headers={"X-Auth-Token": "right-token"})
            assert response.status_code == 200
            response = await client.get(
                "/api/v1/config", headers={"Authorization": "Bearer right-token"}
            )
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_loopback_unchanged_without_token(self, monkeypatch):
        """默认 127.0.0.1 行为保持不变：无 token 也可用（TestClient=本机）。"""
        app = self._app(monkeypatch, token=None)
        client = TestClient(app)
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/config").status_code == 200
        assert client.get("/api/v1/channels/status").status_code == 200


class TestCredentialLeakage:
    def test_config_get_redacts_auth_token(self):
        config = ConfigLoader(None)
        config.update(server={"auth_token": "super-secret", "max_jobs": 7})
        app = build_app(config)
        client = TestClient(app)
        payload = client.get("/api/v1/config").json()
        assert payload["config"]["server"]["auth_token"] == "***"
        assert "super-secret" not in str(payload)

    def test_config_put_cannot_set_auth_token(self):
        config = ConfigLoader(None)
        config.update(server={"auth_token": "original"})
        app = build_app(config)
        client = TestClient(app)
        response = client.put(
            "/api/v1/config", json={"updates": {"server": {"auth_token": "attacker", "max_jobs": 9}}}
        )
        assert response.status_code == 200
        # token 不被覆盖；其它 server 子键正常更新。
        assert config.get("server")["auth_token"] == "original"
        assert config.get("server")["max_jobs"] == 9

    def test_fork_config_strips_ytdlp_escape_hatches(self):
        from server.app import _fork_config

        config = ConfigLoader(None)
        forked = _fork_config(
            config,
            {
                "ytdlp": {
                    "quality": "720p",
                    "extra_options": {"exec_cmd": "calc.exe"},
                    "unsafe_extra_options": True,
                }
            },
        )
        section = forked.get("ytdlp")
        assert section["quality"] == "720p"
        # 默认配置自带空 extra_options 占位；注入的危险键必须被剥掉，
        # unsafe 开关也不得经 HTTP 打开。
        assert "exec_cmd" not in (section.get("extra_options") or {})
        assert not section.get("unsafe_extra_options")


class TestFeedSanitization:
    def test_public_dict_excludes_sensitive_fields(self):
        feed = ChannelFeed(
            object_id="obj1",
            nonce_id="nonce1",
            url="https://cdn/v.mp4?token=ONCE",
            decode_key=123456789,
            bgm_url="https://cdn/bgm.m4a",
            images=["https://cdn/1.jpg"],
            source_api="/mmfinderassist/feed",
        )
        public = feed.to_public_dict()
        assert "url" not in public and "decode_key" not in public
        assert "bgm_url" not in public and "images" not in public
        assert "source_api" not in public and "specs" not in public
        # 展示必需字段保留。
        for key in ("feed_id", "title", "author_name", "status", "cover_url", "downloaded_paths"):
            assert key in public
        # 完整视图仅供进程内使用，仍包含下载所需字段。
        assert feed.to_dict()["decode_key"] == 123456789

    def test_session_feeds_endpoint_returns_sanitized_items(self, tmp_path):
        config = ConfigLoader(None)
        manager = ChannelsSessionManager(config, file_manager=object())
        store = FeedStore()
        store.add(
            [
                ChannelFeed(
                    object_id="obj1",
                    nonce_id="nonce1",
                    url="https://cdn/v.mp4?token=ONCE",
                    decode_key=42,
                    title="标题",
                )
            ]
        )
        manager._running = {
            "store": store,
            "stats": DownloadResult(),
            "auto": True,
            "port": 8899,
            "live_record": False,
            "worker": None,
        }
        payload = manager.feeds()
        assert payload["running"] is True
        item = payload["feeds"][0]
        assert item["feed_id"] == "nonce1"
        assert "url" not in item and "decode_key" not in item and "source_api" not in item

    def test_status_payload_has_no_certificate_path(self, tmp_path):
        config = ConfigLoader(None)
        manager = ChannelsSessionManager(config, file_manager=object())
        payload = manager.status()
        assert "path" not in payload["certificate"]
