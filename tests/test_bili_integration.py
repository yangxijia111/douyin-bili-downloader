"""B 站平台集成的增量测试。

覆盖四块最容易回归的行为：

* **出站 URL 安全校验**（bilibili.security）——scheme 白名单与内网/保留地址
  黑名单，SSRF 的主要防线；
* **短链展开的逐跳校验**——302 落点是不可信数据，任何一跳指向内网都必须放弃；
* **server 模式的 B 站分流**——Web 控制台提交 B 站链接要走 B 站链路，凭据
  不进历史库也不明文返回给浏览器；
* **番剧 / 课程等版权内容的门禁**——解析得出但永不下载，必须给明确解释。
"""

from unittest.mock import AsyncMock

import pytest

from bilibili.api_client import BiliAPIClient, BiliLoginRequiredError, BiliRiskControlError
from bilibili.collection_downloader import BiliCollectionDownloader
from bilibili.security import assert_safe_url, is_safe_url
from bilibili.url_parser import BiliURLParser
from config import ConfigLoader
from control import RetryHandler
from server.app import _redacted_config, extract_url_from_text
from storage import FileManager

# ----------------------------------------------------------------------
# 出站 URL 安全校验
# ----------------------------------------------------------------------


def test_safe_url_allows_public_http_and_https():
    assert is_safe_url("https://upos-sz.bilivideo.com/video.m4s")
    assert is_safe_url("https://i0.hdslb.com/bfs/archive/cover.jpg")
    assert is_safe_url("http://api.bilibili.com/x/web-interface/view")
    assert is_safe_url("https://b23.tv/abc123")


def test_safe_url_rejects_non_http_schemes():
    assert not is_safe_url("file:///etc/passwd")
    assert not is_safe_url("ftp://cdn/video.m4s")
    assert not is_safe_url("javascript:alert(1)")
    assert not is_safe_url("")
    assert not is_safe_url(None)


def test_safe_url_rejects_loopback_and_private_addresses():
    # IPv4 环回 / 私有 / 链路本地 / 保留
    assert not is_safe_url("http://127.0.0.1/x")
    assert not is_safe_url("http://10.0.0.5/x")
    assert not is_safe_url("http://172.16.1.1/x")
    assert not is_safe_url("http://192.168.1.1/x")
    assert not is_safe_url("http://169.254.169.254/latest/meta-data")
    assert not is_safe_url("http://0.0.0.0/x")
    assert not is_safe_url("http://100.64.0.1/x")
    # IPv6
    assert not is_safe_url("http://[::1]/x")
    assert not is_safe_url("http://[fe80::1]/x")
    assert not is_safe_url("http://[fd00::1]/x")


def test_safe_url_rejects_internal_hostnames():
    assert not is_safe_url("http://localhost/x")
    assert not is_safe_url("http://localhost:8080/x")
    assert not is_safe_url("http://api.localhost/x")
    assert not is_safe_url("http://nas.local/x")
    assert not is_safe_url("http://service.internal/x")
    assert not is_safe_url("http://LOCALHOST/x")


def test_safe_url_allows_lookalike_public_domains():
    # 这些是合法公网域名，不能被内网规则误伤。
    assert is_safe_url("https://api.bilibili.tv/x")
    assert is_safe_url("https://example.com/local-guide")


def test_assert_safe_url_raises_on_unsafe():
    with pytest.raises(ValueError):
        assert_safe_url("http://127.0.0.1/x")
    assert assert_safe_url("https://cdn.bilivideo.com/a.m4s") == (
        "https://cdn.bilivideo.com/a.m4s"
    )


# ----------------------------------------------------------------------
# 短链展开的逐跳校验
# ----------------------------------------------------------------------


class _FakeRedirectResponse:
    def __init__(self, status, location=None, url="https://placeholder/"):
        self.status = status
        self._location = location
        self.url = url
        self.headers = {"Location": location} if location else {}


class _FakeSession:
    """按入参逐次返回预设响应的最小 session 桩。"""

    closed = False

    def __init__(self, responses):
        self._responses = list(responses)
        self.requested = []

    def get(self, url, headers=None, proxy=None, allow_redirects=True):
        self.requested.append(url)
        response = self._responses.pop(0)
        return _FakeContext(response)


class _FakeContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


def _client_with_session(session):
    client = BiliAPIClient({}, request_interval=0)
    client._session = session
    return client


async def test_resolve_short_url_follows_redirect_chain():
    session = _FakeSession(
        [
            _FakeRedirectResponse(302, "https://www.bilibili.com/video/BV1GJ411x7h7"),
            _FakeRedirectResponse(200, url="https://www.bilibili.com/video/BV1GJ411x7h7"),
        ]
    )
    client = _client_with_session(session)

    resolved = await client.resolve_short_url("https://b23.tv/abc")

    assert resolved == "https://www.bilibili.com/video/BV1GJ411x7h7"
    assert session.requested == [
        "https://b23.tv/abc",
        "https://www.bilibili.com/video/BV1GJ411x7h7",
    ]


async def test_resolve_short_url_refuses_unsafe_entry():
    client = BiliAPIClient({}, request_interval=0)
    client._session = _FakeSession([])

    # 入口 host 就不安全（环回地址）——不发任何请求。
    assert await client.resolve_short_url("http://127.0.0.1/redirect") is None
    assert client._session.requested == []


async def test_resolve_short_url_refuses_unsafe_redirect_target():
    session = _FakeSession(
        [_FakeRedirectResponse(302, "http://169.254.169.254/latest/meta-data")]
    )
    client = _client_with_session(session)

    assert await client.resolve_short_url("https://b23.tv/abc") is None
    # 只请求了第一跳，内网落点没有发出去。
    assert session.requested == ["https://b23.tv/abc"]


# ----------------------------------------------------------------------
# 媒体下载候选地址的安全过滤
# ----------------------------------------------------------------------


async def test_download_mirrors_skips_unsafe_candidates(tmp_path, monkeypatch):
    from bilibili.downloader_base import BiliBaseDownloader

    class _ConcreteDownloader(BiliBaseDownloader):
        async def download(self, parsed_url):
            raise NotImplementedError  # pragma: no cover - 只测基类方法

    downloader = _ConcreteDownloader.__new__(_ConcreteDownloader)
    downloader.api_client = BiliAPIClient({}, request_interval=0)
    downloader.retry_handler = RetryHandler(max_retries=0)
    downloader._download_error_log_count = 0
    downloader._download_error_log_limit = 5

    requested = []

    class _FM:
        async def download_file(
            self, url, dest, session=None, headers=None, proxy=None, on_progress=None
        ):
            requested.append(url)
            return True

    downloader.file_manager = _FM()

    urls = [
        "http://127.0.0.1/evil.m4s",
        "https://upos-sz.bilivideo.com/ok.m4s",
    ]
    ok = await downloader._download_mirrors(urls, tmp_path / "a.m4s", object())

    assert ok is True
    assert requested == ["https://upos-sz.bilivideo.com/ok.m4s"]


# ----------------------------------------------------------------------
# 版权内容门禁
# ----------------------------------------------------------------------


def test_parser_flags_bangumi_and_cheese_as_gated():
    for url in (
        "https://www.bilibili.com/bangumi/play/ep123456",
        "https://www.bilibili.com/bangumi/play/ss789",
        "https://www.bilibili.com/cheese/play/ep1000",
        "https://www.bilibili.com/festival/2026spring?bvid=BV1GJ411x7h7",
    ):
        parsed = BiliURLParser.parse(url)
        assert parsed is not None, url
        assert parsed["type"] == "bangumi", url


def test_parser_still_accepts_normal_videos():
    parsed = BiliURLParser.parse("https://www.bilibili.com/video/BV1GJ411x7h7")
    assert parsed["type"] == "video"
    assert parsed["bvid"] == "BV1GJ411x7h7"


# ----------------------------------------------------------------------
# 分享文案提取（B 站）
# ----------------------------------------------------------------------


def test_extract_url_from_text_supports_bilibili():
    assert (
        extract_url_from_text("快看这个 https://b23.tv/abc123 太好了")
        == "https://b23.tv/abc123"
    )
    assert (
        extract_url_from_text("https://www.bilibili.com/video/BV1GJ411x7h7 分享给你")
        == "https://www.bilibili.com/video/BV1GJ411x7h7"
    )
    assert extract_url_from_text("BV1GJ411x7h7") == "BV1GJ411x7h7"
    assert (
        extract_url_from_text("space.bilibili.com/271779326/video?spm_id_from=333")
        == "https://space.bilibili.com/271779326/video?spm_id_from=333"
    )


def test_extract_url_from_text_keeps_douyin_behaviour():
    assert (
        extract_url_from_text("长按复制 https://v.douyin.com/abcDEf/ 打开抖音")
        == "https://v.douyin.com/abcDEf/"
    )


# ----------------------------------------------------------------------
# 凭据脱敏
# ----------------------------------------------------------------------


def test_redacted_config_masks_bilibili_credentials():
    config = {
        "bilibili": {
            "enabled": True,
            "quality": "highest",
            "cookie": "SESSDATA=secret-token",
            "cookies": {"SESSDATA": "secret-token", "buvid3": "dev"},
        },
        "cookies": {"ttwid": "douyin-token"},
    }
    safe = _redacted_config(config)

    assert safe["bilibili"]["cookie"] == "***"
    assert safe["bilibili"]["cookies"] == {"SESSDATA": "***", "buvid3": "***"}
    # 非凭据字段原样保留
    assert safe["bilibili"]["quality"] == "highest"
    assert safe["cookies"]["ttwid"] == "***"


def test_redacted_config_without_bilibili_section():
    safe = _redacted_config({"cookies": {"ttwid": "x"}})
    assert "bilibili" not in safe
    assert safe["cookies"]["ttwid"] == "***"


# ----------------------------------------------------------------------
# server 执行链路的 B 站分流
# ----------------------------------------------------------------------


def _server_config(tmp_path, **bili_overrides):
    config = ConfigLoader(None)
    section = {"request_interval": 0}
    section.update(bili_overrides)
    config.update(path=str(tmp_path), bilibili=section)
    return config


async def test_execute_download_routes_bilibili_url(tmp_path, monkeypatch):
    """B 站链接必须走 B 站链路，而不是抖音解析器。"""
    from server import app as server_app
    from server.app import _ServerDeps

    config = _server_config(tmp_path)
    deps = _ServerDeps(config)

    calls = {}

    async def fake_bili(url, deps_, config_, database, limiter, reporter, job):
        calls["url"] = url
        return {"total": 1, "success": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(server_app, "_execute_bilibili_download", fake_bili)

    counts = await server_app._execute_download(
        "https://www.bilibili.com/video/BV1GJ411x7h7", deps
    )
    assert counts == {"total": 1, "success": 1, "failed": 0, "skipped": 0}
    assert calls["url"] == "https://www.bilibili.com/video/BV1GJ411x7h7"


async def test_execute_bilibili_download_respects_enabled_flag(tmp_path):
    from server import app as server_app
    from server.app import _ServerDeps

    config = _server_config(tmp_path, enabled=False)
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_bilibili_download(
            "https://www.bilibili.com/video/BV1GJ411x7h7", deps, config, None, None, None, None
        )
    assert "bilibili.enabled" in str(excinfo.value)


async def test_execute_bilibili_download_gates_bangumi(tmp_path):
    from server import app as server_app
    from server.app import _ServerDeps

    config = _server_config(tmp_path)
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_bilibili_download(
            "https://www.bilibili.com/bangumi/play/ep123456",
            deps,
            config,
            None,
            None,
            None,
            None,
        )
    assert "版权" in str(excinfo.value)


async def test_execute_bilibili_download_rejects_unparseable(tmp_path):
    from server import app as server_app
    from server.app import _ServerDeps

    config = _server_config(tmp_path)
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_bilibili_download(
            "https://www.bilibili.com/roaming/index", deps, config, None, None, None, None
        )
    assert "无法解析" in str(excinfo.value)


# ----------------------------------------------------------------------
# 合集下载器的登录 / 风控冒泡
# ----------------------------------------------------------------------


def _collection_downloader(tmp_path, client):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path), bilibili={"request_interval": 0})
    downloader = BiliCollectionDownloader(
        config,
        client,
        FileManager(str(tmp_path)),
        retry_handler=RetryHandler(max_retries=0),
    )
    return downloader


async def test_collection_login_error_bubbles_up(tmp_path):
    client = BiliAPIClient({}, request_interval=0)
    client.get_season_archives = AsyncMock(
        side_effect=BiliLoginRequiredError(-101, "账号未登录", "/x/polymer")
    )
    downloader = _collection_downloader(tmp_path, client)

    with pytest.raises(BiliLoginRequiredError):
        await downloader.download(
            {"type": "collection", "season_id": "123", "mid": "271779326"}
        )


async def test_collection_risk_control_bubbles_up(tmp_path):
    client = BiliAPIClient({}, request_interval=0)
    client.get_season_archives = AsyncMock(
        side_effect=BiliRiskControlError(-412, "请求被拦截", "/x/polymer")
    )
    downloader = _collection_downloader(tmp_path, client)

    with pytest.raises(BiliRiskControlError):
        await downloader.download(
            {"type": "collection", "season_id": "123", "mid": "271779326"}
        )


async def test_collection_plain_api_error_yields_empty(tmp_path):
    """普通接口错误（非登录/风控）仍然按「没有内容」处理，不炸整个任务。"""
    from bilibili.api_client import BiliAPIError

    client = BiliAPIClient({}, request_interval=0)
    client.get_season_archives = AsyncMock(
        side_effect=BiliAPIError(-404, "啥都木有", "/x/polymer")
    )
    downloader = _collection_downloader(tmp_path, client)

    result = await downloader.download(
        {"type": "collection", "season_id": "123", "mid": "271779326"}
    )
    assert result.total == 0
