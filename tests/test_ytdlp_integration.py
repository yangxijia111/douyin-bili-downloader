"""yt-dlp 平台的入口层集成测试（离线）。

覆盖 CLI / Server 两个入口的：平台分流、配置门禁、凭据脱敏与历史快照。
下载器本身在 test_ytdlp_downloader.py 里单独测，这里一律用假下载器替代。
"""

import json
from unittest.mock import AsyncMock

import pytest

try:
    import fastapi  # noqa: F401  # server.app 依赖 fastapi
except ImportError:  # core 矩阵（只装 [dev]）未安装 fastapi 时整文件跳过
    pytest.skip("fastapi not installed", allow_module_level=True)


from config import ConfigLoader
from core.downloader_base import DownloadResult
from server.app import _config_snapshot as server_config_snapshot
from server.app import _redacted_config, extract_url_from_text
from ytdlp import YtdlpDownloadError, YtdlpMissingError

# ----------------------------------------------------------------------
# 配置读取
# ----------------------------------------------------------------------


def _config(**ytdlp_section):
    config = ConfigLoader(None)
    config.update(ytdlp=ytdlp_section)
    return config


def test_get_ytdlp_cookies_accepts_string_and_dict():
    config = _config(
        cookies={
            "iqiyi": "P00001=tok; QC005=dev",
            "tencent": {"vqq_vuserid": "u", "vqq_access_token": "t"},
            "youku": "",
        }
    )
    assert config.get_ytdlp_cookies("iqiyi") == {"P00001": "tok", "QC005": "dev"}
    assert config.get_ytdlp_cookies("tencent") == {"vqq_vuserid": "u", "vqq_access_token": "t"}
    assert config.get_ytdlp_cookies("youku") == {}
    assert config.get_ytdlp_cookies("mgtv") == {}
    assert config.get_ytdlp_cookies("") == {}


def test_get_ytdlp_cookies_tolerates_malformed_section():
    config = ConfigLoader(None)
    config.update(ytdlp="not-a-dict")
    assert config.get_ytdlp_cookies("iqiyi") == {}
    config.update(ytdlp={"cookies": "P00001=tok"})  # 忘了按平台分层
    assert config.get_ytdlp_cookies("iqiyi") == {}


def test_get_ytdlp_enabled_and_platform_flags():
    assert ConfigLoader(None).get_ytdlp_enabled() is True
    assert _config(enabled=False).get_ytdlp_enabled() is False
    assert _config(enabled="off").get_ytdlp_enabled() is False
    assert _config(enabled="yes").get_ytdlp_enabled() is True

    config = _config(platforms={"iqiyi": False, "tencent": "true"})
    assert config.get_ytdlp_platform_enabled("iqiyi") is False
    assert config.get_ytdlp_platform_enabled("tencent") is True
    assert config.get_ytdlp_platform_enabled("youku") is True  # 未声明视为开启
    # 总开关关闭时单平台开关无效
    config = _config(enabled=False, platforms={"iqiyi": True})
    assert config.get_ytdlp_platform_enabled("iqiyi") is False


def test_env_ytdlp_cookie_file_override(monkeypatch, tmp_path):
    monkeypatch.setenv("YTDLP_COOKIE_FILE", str(tmp_path / "c.txt"))
    config = ConfigLoader(None)
    assert config.get("ytdlp")["cookie_file"] == str(tmp_path / "c.txt")
    # 其余默认值不受影响
    assert config.get("ytdlp")["quality"] == "highest"


def test_default_config_ships_ytdlp_section():
    section = ConfigLoader(None).get("ytdlp")
    assert section["enabled"] is True
    assert section["quality"] == "highest"
    assert section["number"] == {"video": 0}
    assert section["increase"] == {"video": True}
    assert section["cookies"] == {}


# ----------------------------------------------------------------------
# 凭据脱敏与历史快照
# ----------------------------------------------------------------------


def test_redacted_config_masks_ytdlp_credentials():
    config = {
        "ytdlp": {
            "enabled": True,
            "quality": "720p",
            "cookies": {
                "iqiyi": "P00001=secret",
                "tencent": {"vqq_vuserid": "secret", "empty": ""},
            },
            "cookie_file": "/home/me/cookies.txt",
        },
        "cookies": {"ttwid": "douyin-token"},
    }
    safe = _redacted_config(config)

    assert safe["ytdlp"]["cookies"] == {
        "iqiyi": "***",
        "tencent": {"vqq_vuserid": "***", "empty": ""},
    }
    assert safe["ytdlp"]["cookie_file"] == "***"
    assert safe["ytdlp"]["quality"] == "720p"
    assert safe["cookies"]["ttwid"] == "***"
    # 原字典不能被改
    assert config["ytdlp"]["cookies"]["iqiyi"] == "P00001=secret"


def test_redacted_config_without_ytdlp_section():
    safe = _redacted_config({"cookies": {"ttwid": "x"}})
    assert "ytdlp" not in safe


def _loaded_config_with_all_secrets(tmp_path):
    config = ConfigLoader(None)
    config.update(
        path=str(tmp_path),
        cookies={"ttwid": "douyin-secret"},
        bilibili={"quality": "1080p", "cookie": "SESSDATA=bili-secret", "cookies": {"SESSDATA": "s"}},
        ytdlp={
            "quality": "720p",
            "cookies": {"iqiyi": "P00001=iqiyi-secret"},
            "cookie_file": "/secret/path.txt",
        },
    )
    return config


@pytest.mark.parametrize("snapshot_fn_name", ["cli", "server"])
def test_config_snapshot_strips_every_platform_secret(tmp_path, snapshot_fn_name):
    """哪怕当前任务只涉及一个平台，其他平台的凭据也不能跟着快照进历史库。"""
    from cli.main import _config_snapshot as cli_config_snapshot

    fn = cli_config_snapshot if snapshot_fn_name == "cli" else server_config_snapshot
    config = _loaded_config_with_all_secrets(tmp_path)

    snapshot = json.loads(fn(config))
    raw = json.dumps(snapshot, ensure_ascii=False)

    assert "douyin-secret" not in raw
    assert "bili-secret" not in raw
    assert "iqiyi-secret" not in raw
    assert "/secret/path.txt" not in raw
    assert "cookies" not in snapshot
    assert "cookie" not in snapshot
    assert "transcript" not in snapshot
    # 非凭据参数保留，便于回看当时的下载设置
    assert snapshot["bilibili"]["quality"] == "1080p"
    assert snapshot["ytdlp"]["quality"] == "720p"
    assert snapshot["path"] == str(tmp_path)
    # 原配置未被修改
    assert config.get("ytdlp")["cookies"] == {"iqiyi": "P00001=iqiyi-secret"}
    assert config.get("bilibili")["cookie"] == "SESSDATA=bili-secret"


# ----------------------------------------------------------------------
# 分享文案里的裸链接提取
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("快手 v.kuaishou.com/AbCd12 复制此链接打开", "https://v.kuaishou.com/AbCd12"),
        ("小红书 xhslink.com/a/xyz，快看", "https://xhslink.com/a/xyz"),
        ("看 www.iqiyi.com/v_abc.html）", "https://www.iqiyi.com/v_abc.html"),
        ("m.v.qq.com/x/m/play?cid=abc 腾讯", "https://m.v.qq.com/x/m/play?cid=abc"),
        # 带 scheme 的走通用正则，优先级最高
        ("看 https://www.mgtv.com/b/1/2.html。", "https://www.mgtv.com/b/1/2.html"),
        # 抖音裸链优先级仍在前
        ("v.douyin.com/abc/ 和 v.kuaishou.com/x", "https://v.douyin.com/abc/"),
    ],
)
def test_extract_url_from_text_recognizes_bare_ytdlp_domains(text, expected):
    assert extract_url_from_text(text) == expected


def test_extract_url_from_text_ignores_qq_non_video_hosts():
    """qq.com 主站 / 邮箱不是视频站，不能被裸域名正则误抓。"""
    assert extract_url_from_text("mail.qq.com/cgi-bin/x") == "mail.qq.com/cgi-bin/x"


# ----------------------------------------------------------------------
# Server 执行链路的分流与门禁
# ----------------------------------------------------------------------


def _server_config(tmp_path, **ytdlp_overrides):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path), ytdlp=ytdlp_overrides)
    return config


async def test_execute_download_routes_ytdlp_url(tmp_path, monkeypatch):
    """爱奇艺链接必须走 yt-dlp 链路，而不是抖音解析器或 B 站链路。"""
    from server import app as server_app
    from server.app import _ServerDeps

    config = _server_config(tmp_path)
    deps = _ServerDeps(config)
    calls = {}

    async def fake_ytdlp(url, deps_, config_, database, limiter, reporter, job):
        calls["url"] = url
        return {"total": 1, "success": 1, "failed": 0, "skipped": 0}

    async def fake_bili(*args, **kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("bilibili 链路不应被调用")

    monkeypatch.setattr(server_app, "_execute_ytdlp_download", fake_ytdlp)
    monkeypatch.setattr(server_app, "_execute_bilibili_download", fake_bili)

    counts = await server_app._execute_download("https://www.iqiyi.com/v_abc.html", deps)
    assert counts == {"total": 1, "success": 1, "failed": 0, "skipped": 0}
    assert calls["url"] == "https://www.iqiyi.com/v_abc.html"


async def test_execute_download_still_routes_bilibili_first(tmp_path, monkeypatch):
    from server import app as server_app
    from server.app import _ServerDeps

    deps = _ServerDeps(_server_config(tmp_path))
    called = {}

    async def fake_bili(url, *args):
        called["bili"] = url
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    async def fake_ytdlp(*args):  # pragma: no cover
        raise AssertionError("ytdlp 链路不应被调用")

    monkeypatch.setattr(server_app, "_execute_bilibili_download", fake_bili)
    monkeypatch.setattr(server_app, "_execute_ytdlp_download", fake_ytdlp)

    await server_app._execute_download("https://www.bilibili.com/video/BV1GJ411x7h7", deps)
    assert called["bili"].endswith("BV1GJ411x7h7")


async def test_execute_ytdlp_download_respects_enabled_flag(tmp_path):
    from server import app as server_app
    from server.app import _ServerDeps

    config = _server_config(tmp_path, enabled=False)
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_ytdlp_download(
            "https://www.iqiyi.com/v_abc.html", deps, config, None, None, None, None
        )
    assert "ytdlp.enabled" in str(excinfo.value)


async def test_execute_ytdlp_download_respects_platform_flag(tmp_path):
    from server import app as server_app
    from server.app import _ServerDeps

    config = _server_config(tmp_path, platforms={"iqiyi": False})
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_ytdlp_download(
            "https://www.iqiyi.com/v_abc.html", deps, config, None, None, None, None
        )
    assert "ytdlp.platforms.iqiyi" in str(excinfo.value)


async def test_execute_ytdlp_download_rejects_foreign_url(tmp_path):
    from server import app as server_app
    from server.app import _ServerDeps

    config = _server_config(tmp_path)
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_ytdlp_download(
            "https://www.douyin.com/video/1", deps, config, None, None, None, None
        )
    assert "无法识别" in str(excinfo.value)


class _FakeYtdlpDownloader:
    """替代真实下载器：按预设返回结果或抛错，并记录构造参数。"""

    created = []
    result = None
    error = None
    last_error = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.last_error = type(self).last_error
        type(self).created.append(self)

    async def download(self, parsed):
        if type(self).error is not None:
            raise type(self).error
        return type(self).result


@pytest.fixture
def fake_downloader(monkeypatch):
    from cli import main as cli_main
    from server import app as server_app

    _FakeYtdlpDownloader.created = []
    _FakeYtdlpDownloader.result = None
    _FakeYtdlpDownloader.error = None
    _FakeYtdlpDownloader.last_error = None
    monkeypatch.setattr(server_app, "YtdlpDownloader", _FakeYtdlpDownloader)
    monkeypatch.setattr(cli_main, "YtdlpDownloader", _FakeYtdlpDownloader)
    return _FakeYtdlpDownloader


def _result(total, success, failed=0, skipped=0):
    result = DownloadResult()
    result.total, result.success, result.failed, result.skipped = total, success, failed, skipped
    return result


async def test_execute_ytdlp_download_records_history_and_strips_secrets(
    tmp_path, fake_downloader
):
    from server import app as server_app
    from server.app import _ServerDeps

    fake_downloader.result = _result(2, 2)
    config = _server_config(tmp_path, cookies={"iqiyi": "P00001=iqiyi-secret"})
    deps = _ServerDeps(config)
    database = type("DB", (), {"add_history": AsyncMock()})()

    counts = await server_app._execute_ytdlp_download(
        "https://www.iqiyi.com/v_abc.html", deps, config, database, None, None, None
    )

    assert counts == {"total": 2, "success": 2, "failed": 0, "skipped": 0}
    database.add_history.assert_awaited_once()
    row = database.add_history.await_args.args[0]
    assert row["url_type"] == "ytdlp:iqiyi:video"
    assert row["total_count"] == 2
    assert "iqiyi-secret" not in row["config"]
    # 下载器拿到的是 server 共享的基础设施
    kwargs = fake_downloader.created[0].kwargs
    assert kwargs["file_manager"] is deps.file_manager
    assert kwargs["database"] is database


async def test_execute_ytdlp_download_surfaces_missing_ytdlp(tmp_path, fake_downloader):
    from server import app as server_app
    from server.app import _ServerDeps

    fake_downloader.error = YtdlpMissingError("未安装 yt-dlp。执行 `pip install yt-dlp`")
    config = _server_config(tmp_path)
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_ytdlp_download(
            "https://www.iqiyi.com/v_abc.html", deps, config, None, None, None, None
        )
    assert "pip install yt-dlp" in str(excinfo.value)


async def test_execute_ytdlp_download_translates_drm_error(tmp_path, fake_downloader):
    from server import app as server_app
    from server.app import _ServerDeps

    fake_downloader.error = YtdlpDownloadError("This video is DRM protected", kind="drm")
    config = _server_config(tmp_path)
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_ytdlp_download(
            "https://www.iqiyi.com/v_abc.html", deps, config, None, None, None, None
        )
    message = str(excinfo.value)
    assert "DRM" in message
    assert "爱奇艺" in message
    assert "This video is DRM protected" in message


async def test_execute_ytdlp_download_raises_when_all_items_failed(tmp_path, fake_downloader):
    """整条链接全部失败时把分类提示抛成 job.error，而不是静默返回 success=0。"""
    from server import app as server_app
    from server.app import _ServerDeps

    fake_downloader.result = _result(3, 0, failed=3)
    fake_downloader.last_error = YtdlpDownloadError("Login required", kind="login")
    config = _server_config(tmp_path)
    deps = _ServerDeps(config)

    with pytest.raises(RuntimeError) as excinfo:
        await server_app._execute_ytdlp_download(
            "https://www.iqiyi.com/v_abc.html", deps, config, None, None, None, None
        )
    assert "ytdlp.cookies" in str(excinfo.value)


async def test_execute_ytdlp_download_partial_failure_is_not_an_error(tmp_path, fake_downloader):
    from server import app as server_app
    from server.app import _ServerDeps

    fake_downloader.result = _result(3, 1, failed=2)
    fake_downloader.last_error = YtdlpDownloadError("x", kind="generic")
    config = _server_config(tmp_path)
    deps = _ServerDeps(config)

    counts = await server_app._execute_ytdlp_download(
        "https://www.iqiyi.com/v_abc.html", deps, config, None, None, None, None
    )
    assert counts["success"] == 1


# ----------------------------------------------------------------------
# CLI 执行链路的分流与门禁
# ----------------------------------------------------------------------


async def test_cli_download_url_routes_ytdlp(tmp_path, monkeypatch):
    from auth import CookieManager
    from cli import main as cli_main

    config = ConfigLoader(None)
    config.update(path=str(tmp_path))
    calls = {}

    async def fake_ytdlp(url, *args, **kwargs):
        calls["url"] = url
        return _result(1, 1)

    async def fake_bili(*args, **kwargs):  # pragma: no cover
        raise AssertionError("bilibili 链路不应被调用")

    monkeypatch.setattr(cli_main, "download_ytdlp_url", fake_ytdlp)
    monkeypatch.setattr(cli_main, "download_bilibili_url", fake_bili)

    result = await cli_main.download_url("https://v.qq.com/x/cover/a/b.html", config, CookieManager())
    assert result.success == 1
    assert calls["url"] == "https://v.qq.com/x/cover/a/b.html"


async def test_cli_download_ytdlp_url_gates_on_enabled(tmp_path, fake_downloader):
    from cli import main as cli_main
    from control import QueueManager, RateLimiter, RetryHandler
    from storage import FileManager

    config = ConfigLoader(None)
    config.update(path=str(tmp_path), ytdlp={"enabled": False})

    result = await cli_main.download_ytdlp_url(
        "https://www.iqiyi.com/v_abc.html",
        config,
        FileManager(str(tmp_path)),
        RateLimiter(),
        RetryHandler(max_retries=0),
        QueueManager(max_workers=1),
    )
    assert result is None
    assert fake_downloader.created == []


async def test_cli_download_ytdlp_url_records_history(tmp_path, fake_downloader):
    from cli import main as cli_main
    from control import QueueManager, RateLimiter, RetryHandler
    from storage import FileManager

    fake_downloader.result = _result(1, 1)
    config = ConfigLoader(None)
    config.update(path=str(tmp_path), ytdlp={"cookies": {"iqiyi": "P00001=iqiyi-secret"}})
    database = type("DB", (), {"add_history": AsyncMock()})()

    result = await cli_main.download_ytdlp_url(
        "https://www.iqiyi.com/v_abc.html",
        config,
        FileManager(str(tmp_path)),
        RateLimiter(),
        RetryHandler(max_retries=0),
        QueueManager(max_workers=1),
        database=database,
    )
    assert result.success == 1
    row = database.add_history.await_args.args[0]
    assert row["url_type"] == "ytdlp:iqiyi:video"
    assert "iqiyi-secret" not in row["config"]


async def test_cli_download_ytdlp_url_handles_missing_dependency(tmp_path, fake_downloader):
    from cli import main as cli_main
    from control import QueueManager, RateLimiter, RetryHandler
    from storage import FileManager

    fake_downloader.error = YtdlpMissingError("未安装 yt-dlp")
    config = ConfigLoader(None)
    config.update(path=str(tmp_path))

    result = await cli_main.download_ytdlp_url(
        "https://www.iqiyi.com/v_abc.html",
        config,
        FileManager(str(tmp_path)),
        RateLimiter(),
        RetryHandler(max_retries=0),
        QueueManager(max_workers=1),
    )
    assert result is None


def test_cli_error_hint_by_kind():
    from cli.main import ytdlp_error_hint

    drm = ytdlp_error_hint(YtdlpDownloadError("raw", kind="drm"), "爱奇艺")
    assert "DRM" in drm and "爱奇艺" in drm and "raw" in drm
    login = ytdlp_error_hint(YtdlpDownloadError("raw", kind="login"), "腾讯视频")
    assert "ytdlp.cookies" in login
    geo = ytdlp_error_hint(YtdlpDownloadError("raw", kind="geo"), "优酷")
    assert "geo_bypass" in geo
    unsupported = ytdlp_error_hint(YtdlpDownloadError("raw", kind="unsupported"), "芒果TV")
    assert "pip install -U yt-dlp" in unsupported
    phantomjs = ytdlp_error_hint(YtdlpDownloadError("raw", kind="phantomjs"), "爱奇艺")
    assert "PhantomJS" in phantomjs and "phantomjs.org" in phantomjs
    generic = ytdlp_error_hint(YtdlpDownloadError("raw", kind="generic"), "快手")
    assert "raw" in generic
