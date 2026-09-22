"""yt-dlp 引擎下载器测试（全部离线，yt-dlp 由假模块替代）。

覆盖最容易出错、且出错时不报错的路径：

* **落盘与入库**：产物要落在与抖音 / B 站同构的目录里，数据库与清单要能按
  ``ytdlp_<platform>`` / ``platform`` 字段区分来源；
* **磁盘增量**：``<platform>_<id>`` token 在文件名里能被扫出来，已有的跳过；
* **错误分类**：DRM / 登录 / 地区限制要落成不同的 ``kind``，上层才能给出对的提示；
* **Cookie 注入**：配置里的 Cookie 要变成 yt-dlp 能读的 Netscape 文件，且用完即删；
* **剧集展开**：列表页展开后受 ``number.video`` 截断。
"""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config import ConfigLoader
from storage import FileManager
from ytdlp import downloader as downloader_module
from ytdlp.downloader import (
    YtdlpDownloader,
    YtdlpDownloadError,
    YtdlpMissingError,
    build_video_token,
    classify_download_error,
    format_selector,
    safe_video_id,
    video_tokens_in_filename,
    write_netscape_cookies,
)

ENTRY = {
    "id": "1234567",
    "title": "测试视频",
    "uploader": "测试作者",
    "uploader_id": "u001",
    "uploader_url": "https://www.iqiyi.com/u/u001",
    "upload_date": "20240102",
    "timestamp": 1704153600,
    "webpage_url": "https://www.iqiyi.com/v_abc.html",
    "thumbnail": "https://img.example/x.jpg",
    "tags": ["剧集", "测试"],
    "extractor": "iqiyi",
    "formats": [{"format_id": "x"}] * 30,  # 入库前应被瘦身掉
}

PARSED = {
    "type": "video",
    "platform": "iqiyi",
    "platform_name": "爱奇艺",
    "url": "https://www.iqiyi.com/v_abc.html",
    "original_url": "https://www.iqiyi.com/v_abc.html",
}


# ----------------------------------------------------------------------
# 假 yt-dlp 模块
# ----------------------------------------------------------------------


class _FakeDownloadError(Exception):
    pass


class _FakeExtractorError(Exception):
    pass


class _FakeUtils:
    DownloadError = _FakeDownloadError
    ExtractorError = _FakeExtractorError


def make_fake_ytdlp(
    *,
    info=None,
    extract_error=None,
    download_error=None,
    ext="mp4",
    retcode=0,
):
    """构造一个假的 ``yt_dlp`` 模块，记录每次 ``YoutubeDL`` 的选项与调用。"""

    class FakeYoutubeDL:
        instances = []
        download_calls = []

        def __init__(self, opts):
            self.opts = opts
            FakeYoutubeDL.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            assert download is False
            if extract_error is not None:
                raise extract_error
            return deepcopy(info if info is not None else ENTRY)

        def download(self, urls):
            FakeYoutubeDL.download_calls.append((urls, self.opts))
            if download_error is not None:
                raise download_error
            template = self.opts["outtmpl"]["default"]
            target = Path(template.replace("%(ext)s", ext))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-media")
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
                hook({"status": "finished", "filename": str(target)})
            return retcode

    class FakeModule:
        YoutubeDL = FakeYoutubeDL
        utils = _FakeUtils

    return FakeModule


class _FakeDatabase:
    def __init__(self):
        self.add_aweme = AsyncMock()
        self.is_downloaded = AsyncMock(return_value=False)


class _Progress:
    def __init__(self):
        self.steps = []
        self.items = []
        self.bytes = []
        self.total = None
        self.author = None

    def update_step(self, step, detail=""):
        self.steps.append((step, detail))

    def advance_step(self, step, detail=""):
        self.steps.append((step, detail))

    def set_item_total(self, total, detail=""):
        self.total = total

    def advance_item(self, status, detail=""):
        self.items.append((status, detail))

    def on_item_progress(self, *, aweme_id, bytes_read, bytes_total):
        self.bytes.append((aweme_id, bytes_read, bytes_total))

    def on_author(self, *, nickname, sec_uid):
        self.author = (nickname, sec_uid)


def _make_downloader(tmp_path, *, database=None, progress=None, top_level=None, **overrides):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path))
    section = {}
    section.update(overrides)
    config.update(ytdlp=section)
    if top_level:
        config.update(**top_level)
    file_manager = FileManager(str(tmp_path))
    return YtdlpDownloader(
        config=config,
        file_manager=file_manager,
        database=database,
        progress_reporter=progress,
    )


@pytest.fixture
def fake_ytdlp(monkeypatch):
    """默认的成功路径假模块；测试可自行覆盖。"""

    def _install(**kwargs):
        module = make_fake_ytdlp(**kwargs)
        monkeypatch.setattr(downloader_module, "import_ytdlp", lambda: module)
        return module

    return _install


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "quality, expected",
    [
        ("highest", "bv*+ba/b"),
        ("", "bv*+ba/b"),
        (None, "bv*+ba/b"),
        ("best", "bv*+ba/b"),
        ("lowest", "wv*+wa/w"),
        ("1080p", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"),
        ("720P", "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"),
        ("4k", "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b"),
        ("1440", "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b"),
        ("nonsense", "bv*+ba/b"),
    ],
)
def test_format_selector(quality, expected):
    assert format_selector(quality) == expected


def test_format_selector_audio_only_ignores_quality():
    assert format_selector("1080p", audio_only=True) == "ba/b"


def test_safe_video_id_and_token():
    assert safe_video_id("1234567") == "1234567"
    assert safe_video_id("4123:abc_x") == "4123-abc-x"
    assert safe_video_id("  中文id  ") == "id"  # 非 ASCII 段落成分隔符，保留可用部分
    assert safe_video_id("纯中文") == ""
    assert safe_video_id(None) == ""
    assert build_video_token("weibo", "1034:4xxx") == "weibo_1034-4xxx"
    assert build_video_token("iqiyi", "") == ""


def test_video_tokens_in_filename_roundtrip():
    token = build_video_token("weibo", "1034:4xxx")
    assert video_tokens_in_filename(f"2024-01-01_标题_{token}.mp4") == [token]
    # 模板把 {id} 放前面时，下划线是分隔符，不会把标题吞进 id
    assert video_tokens_in_filename(f"{token}_hello world.mp4") == [token]
    # 抖音 / B 站文件名不会误命中
    assert video_tokens_in_filename("2024-01-01_标题_7123456789.mp4") == []
    assert video_tokens_in_filename("2024-01-01_标题_BV1GJ411x7h7.mp4") == []
    # 相似前缀（如 "xigua" 前面紧贴字母）不算
    assert video_tokens_in_filename("abcxigua_123.mp4") == []


@pytest.mark.parametrize(
    "message, kind",
    [
        ("This video is DRM protected", "drm"),
        ("Widevine license required", "drm"),
        ("Login required to view this video", "login"),
        ("This content is only available for VIP members", "login"),
        ("Use --cookies to pass cookies", "login"),
        ("Cookies (not necessarily logged in) are needed", "login"),
        ("This video is not available in your country", "geo"),
        ("Unsupported URL: https://x", "unsupported"),
        # 站方改版后解析器失效的典型文案（爱奇艺大陆站 2026 年即如此）
        ("ERROR: [iqiyi] Can't find any video; please report this issue", "unsupported"),
        ("Unable to extract tvid", "unsupported"),
        ("ERROR: [iq.com] x: PhantomJS not found, Please download it", "phantomjs"),
        ("Something else went wrong", "generic"),
        ("", "generic"),
    ],
)
def test_classify_download_error(message, kind):
    assert classify_download_error(message) == kind


def test_write_netscape_cookies(tmp_path):
    target = tmp_path / "cookies.txt"
    write_netscape_cookies({"P00001": "tok", "QC005": "dev", "": "ignored"}, ".iqiyi.com", target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    rows = [line.split("\t") for line in lines if line and not line.startswith("#")]
    assert len(rows) == 2
    domain, flag, path, secure, expiry, name, value = rows[0]
    assert domain == ".iqiyi.com"
    assert flag == "TRUE"
    assert path == "/"
    assert secure == "FALSE"
    assert int(expiry) > 0
    assert (name, value) == ("P00001", "tok")


# ----------------------------------------------------------------------
# 下载主流程
# ----------------------------------------------------------------------


async def test_download_single_video_records_db_and_manifest(tmp_path, fake_ytdlp):
    module = fake_ytdlp()
    database = _FakeDatabase()
    progress = _Progress()
    downloader = _make_downloader(tmp_path, database=database, progress=progress)

    result = await downloader.download(PARSED)
    await asyncio.sleep(0)  # 让 call_soon_threadsafe 调度的进度回调跑完

    assert (result.total, result.success, result.failed, result.skipped) == (1, 1, 0, 0)

    # 目录结构：<作者>/<平台键>/<日期_标题_token>/<日期_标题_token>.mp4
    media = list(tmp_path.rglob("*.mp4"))
    assert len(media) == 1
    assert media[0].name == "2024-01-02_测试视频_iqiyi_1234567.mp4"
    assert media[0].parent.name == "2024-01-02_测试视频_iqiyi_1234567"
    assert media[0].parent.parent.name == "iqiyi"
    assert media[0].parent.parent.parent.name == "测试作者"

    # 单条下载时必须关掉列表展开，且 outtmpl 指向渲染好的文件名
    _, opts = module.YoutubeDL.download_calls[0]
    assert opts["noplaylist"] is True
    assert "playlistend" not in opts
    assert opts["outtmpl"]["default"].endswith("2024-01-02_测试视频_iqiyi_1234567.%(ext)s")
    assert opts["format"] == "bv*+ba/b"
    assert opts["merge_output_format"] == "mp4"

    # 数据库：复用 aweme 表，类型前缀区分来源，metadata 已瘦身
    database.add_aweme.assert_awaited_once()
    row = database.add_aweme.await_args.args[0]
    assert row["aweme_id"] == "iqiyi_1234567"
    assert row["aweme_type"] == "ytdlp_iqiyi"
    assert row["author_name"] == "测试作者"
    assert row["author_sec_uid"] == "u001"
    assert row["create_time"] == 1704153600
    metadata = json.loads(row["metadata"])
    assert "formats" not in metadata
    assert metadata["id"] == "1234567"
    assert json.loads(row["cover_urls"]) == ["https://img.example/x.jpg"]

    # 清单：与抖音 / B 站同一个文件，靠 platform 字段区分
    manifest = (tmp_path / "download_manifest.jsonl").read_text(encoding="utf-8").strip()
    record = json.loads(manifest)
    assert record["platform"] == "iqiyi"
    assert record["engine"] == "yt-dlp"
    assert record["aweme_id"] == "iqiyi_1234567"
    assert record["source_id"] == "1234567"
    assert record["file_names"] == ["2024-01-02_测试视频_iqiyi_1234567.mp4"]
    assert record["publish_timestamp"] == 1704153600

    # 进度：作者、条目总数、字节进度、逐条状态
    assert progress.author == ("测试作者", "u001")
    assert progress.total == 1
    assert progress.bytes == [("iqiyi_1234567", 50, 100)]
    assert progress.items == [("success", "iqiyi_1234567")]


async def test_download_skips_when_file_exists_locally(tmp_path, fake_ytdlp):
    module = fake_ytdlp()
    existing = tmp_path / "旧作者" / "iqiyi" / "leaf"
    existing.mkdir(parents=True)
    (existing / "2023-01-01_旧标题_iqiyi_1234567.mp4").write_bytes(b"x")

    downloader = _make_downloader(tmp_path)
    result = await downloader.download(PARSED)

    assert (result.total, result.success, result.skipped) == (1, 0, 1)
    assert module.YoutubeDL.download_calls == []


async def test_download_ignores_temp_and_sidecar_files_in_local_index(tmp_path, fake_ytdlp):
    """``.part`` 与封面 / 字幕不代表主媒体存在，不能触发跳过。"""
    module = fake_ytdlp()
    leaf = tmp_path / "旧作者" / "iqiyi" / "leaf"
    leaf.mkdir(parents=True)
    (leaf / "2023-01-01_旧标题_iqiyi_1234567.mp4.part").write_bytes(b"x")
    (leaf / "2023-01-01_旧标题_iqiyi_1234567.jpg").write_bytes(b"x")
    (leaf / "2023-01-01_旧标题_iqiyi_1234567.zh.srt").write_bytes(b"x")

    downloader = _make_downloader(tmp_path)
    result = await downloader.download(PARSED)

    assert result.success == 1
    assert len(module.YoutubeDL.download_calls) == 1


async def test_download_with_increase_disabled_redownloads(tmp_path, fake_ytdlp):
    module = fake_ytdlp()
    existing = tmp_path / "旧作者" / "iqiyi" / "leaf"
    existing.mkdir(parents=True)
    (existing / "2023-01-01_旧标题_iqiyi_1234567.mp4").write_bytes(b"x")

    downloader = _make_downloader(tmp_path, increase={"video": False})
    result = await downloader.download(PARSED)

    assert result.success == 1
    assert len(module.YoutubeDL.download_calls) == 1


async def test_download_uses_history_when_redownload_missing_disabled(tmp_path, fake_ytdlp):
    module = fake_ytdlp()
    database = _FakeDatabase()
    database.is_downloaded = AsyncMock(return_value=True)
    downloader = _make_downloader(
        tmp_path, database=database, top_level={"redownload_missing_files": False}
    )

    result = await downloader.download(PARSED)

    assert result.skipped == 1
    database.is_downloaded.assert_awaited_once_with("iqiyi_1234567")
    assert module.YoutubeDL.download_calls == []


async def test_download_failure_sets_classified_last_error(tmp_path, fake_ytdlp):
    fake_ytdlp(download_error=_FakeDownloadError("ERROR: This video is DRM protected"))
    progress = _Progress()
    downloader = _make_downloader(tmp_path, progress=progress)

    result = await downloader.download(PARSED)

    assert (result.total, result.success, result.failed) == (1, 0, 1)
    assert isinstance(downloader.last_error, YtdlpDownloadError)
    assert downloader.last_error.kind == "drm"
    assert progress.items == [("failed", "iqiyi_1234567")]
    assert list(tmp_path.rglob("*.mp4")) == []


async def test_extract_failure_raises_classified_error(tmp_path, fake_ytdlp):
    fake_ytdlp(extract_error=_FakeDownloadError("ERROR: Login required, use --cookies"))
    downloader = _make_downloader(tmp_path)

    with pytest.raises(YtdlpDownloadError) as excinfo:
        await downloader.download(PARSED)
    assert excinfo.value.kind == "login"


async def test_extractor_error_is_also_classified(tmp_path, fake_ytdlp):
    fake_ytdlp(extract_error=_FakeExtractorError("not available in your country"))
    downloader = _make_downloader(tmp_path)

    with pytest.raises(YtdlpDownloadError) as excinfo:
        await downloader.download(PARSED)
    assert excinfo.value.kind == "geo"


async def test_download_nonzero_retcode_counts_as_failure(tmp_path, fake_ytdlp):
    fake_ytdlp(retcode=1)
    downloader = _make_downloader(tmp_path)

    result = await downloader.download(PARSED)

    assert result.failed == 1
    assert downloader.last_error is not None


async def test_download_reports_failure_when_no_media_produced(tmp_path, fake_ytdlp):
    """yt-dlp 返回 0 但目录里没有主媒体（例如只落了 .part），必须判失败。"""
    fake_ytdlp(ext="mp4.part")
    downloader = _make_downloader(tmp_path)

    result = await downloader.download(PARSED)

    assert (result.success, result.failed) == (0, 1)


async def test_missing_ytdlp_raises_friendly_error(tmp_path, monkeypatch):
    def _raise():
        raise YtdlpMissingError("未安装 yt-dlp")

    monkeypatch.setattr(downloader_module, "import_ytdlp", _raise)
    downloader = _make_downloader(tmp_path)

    with pytest.raises(YtdlpMissingError):
        await downloader.download(PARSED)


async def test_playlist_expands_and_respects_number_limit(tmp_path, fake_ytdlp):
    entries = []
    for index in range(3):
        entry = dict(ENTRY, id=f"ep{index}", title=f"第{index}集")
        entry["webpage_url"] = f"https://www.iqiyi.com/v_ep{index}.html"
        entries.append(entry)
    playlist = {"_type": "playlist", "id": "album1", "title": "剧集", "entries": entries}
    module = fake_ytdlp(info=playlist)
    progress = _Progress()
    downloader = _make_downloader(tmp_path, progress=progress, number={"video": 2})

    result = await downloader.download(PARSED)

    assert (result.total, result.success) == (2, 2)
    assert progress.total == 2
    # 信息提取阶段把上限传给 yt-dlp，避免对剧集页的全部条目做无谓的元数据请求
    extract_opts = module.YoutubeDL.instances[0].opts
    assert extract_opts["playlistend"] == 2
    assert extract_opts["noplaylist"] is False
    downloaded_urls = [urls[0] for urls, _ in module.YoutubeDL.download_calls]
    assert downloaded_urls == [
        "https://www.iqiyi.com/v_ep0.html",
        "https://www.iqiyi.com/v_ep1.html",
    ]
    assert sorted(p.name for p in tmp_path.rglob("*.mp4")) == [
        "2024-01-02_第0集_iqiyi_ep0.mp4",
        "2024-01-02_第1集_iqiyi_ep1.mp4",
    ]


async def test_nested_playlist_is_flattened_and_none_entries_dropped(tmp_path, fake_ytdlp):
    inner = {"_type": "playlist", "entries": [dict(ENTRY, id="a"), None]}
    outer = {"_type": "playlist", "entries": [inner, None, dict(ENTRY, id="b")]}
    fake_ytdlp(info=outer)
    downloader = _make_downloader(tmp_path)

    result = await downloader.download(PARSED)

    assert (result.total, result.success) == (2, 2)


async def test_empty_playlist_returns_zero_total(tmp_path, fake_ytdlp):
    fake_ytdlp(info={"_type": "playlist", "entries": []})
    progress = _Progress()
    downloader = _make_downloader(tmp_path, progress=progress)

    result = await downloader.download(PARSED)

    assert result.total == 0
    assert ("解析链接", "未解析到可下载的视频") in progress.steps


async def test_cookie_file_is_written_and_cleaned_up(tmp_path, fake_ytdlp):
    module = fake_ytdlp()
    downloader = _make_downloader(tmp_path, cookies={"iqiyi": "P00001=tok; QC005=dev"})

    await downloader.download(PARSED)

    extract_opts = module.YoutubeDL.instances[0].opts
    cookie_path = Path(extract_opts["cookiefile"])
    # 下载期间 Cookie 文件必须可用；任务结束后必须删除（临时文件不留凭据）
    _, download_opts = module.YoutubeDL.download_calls[0]
    assert download_opts["cookiefile"] == str(cookie_path)
    assert not cookie_path.exists()
    assert downloader._temp_cookie_path is None


async def test_cookie_file_content_is_netscape_for_platform_domain(tmp_path, monkeypatch):
    """截获写文件那一刻的内容：域名要落到平台主域。"""
    captured = {}

    real_write = downloader_module.write_netscape_cookies

    def _spy(cookies, domain, target):
        captured["cookies"] = dict(cookies)
        captured["domain"] = domain
        real_write(cookies, domain, target)

    monkeypatch.setattr(downloader_module, "write_netscape_cookies", _spy)
    module = make_fake_ytdlp()
    monkeypatch.setattr(downloader_module, "import_ytdlp", lambda: module)
    downloader = _make_downloader(tmp_path, cookies={"iqiyi": {"P00001": "tok"}})

    await downloader.download(PARSED)

    assert captured == {"cookies": {"P00001": "tok"}, "domain": ".iqiyi.com"}


async def test_cookie_file_is_cleaned_up_even_on_failure(tmp_path, fake_ytdlp):
    module = fake_ytdlp(extract_error=_FakeDownloadError("boom"))
    downloader = _make_downloader(tmp_path, cookies={"iqiyi": "a=b"})

    with pytest.raises(YtdlpDownloadError):
        await downloader.download(PARSED)

    cookie_path = Path(module.YoutubeDL.instances[0].opts["cookiefile"])
    assert not cookie_path.exists()


async def test_explicit_cookie_file_takes_precedence(tmp_path, fake_ytdlp):
    module = fake_ytdlp()
    exported = tmp_path / "exported.txt"
    exported.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    downloader = _make_downloader(
        tmp_path, cookie_file=str(exported), cookies={"iqiyi": "a=b"}
    )

    await downloader.download(PARSED)

    assert module.YoutubeDL.instances[0].opts["cookiefile"] == str(exported)
    # 用户自己的文件绝不能被当成临时文件删掉
    assert exported.exists()


async def test_no_cookie_when_platform_has_none_configured(tmp_path, fake_ytdlp):
    module = fake_ytdlp()
    downloader = _make_downloader(tmp_path, cookies={"tencent": "a=b"})

    await downloader.download(PARSED)

    assert "cookiefile" not in module.YoutubeDL.instances[0].opts


async def test_audio_only_uses_audio_format_and_extractor(tmp_path, fake_ytdlp):
    module = fake_ytdlp(ext="m4a")
    downloader = _make_downloader(tmp_path, audio_only=True)

    result = await downloader.download(PARSED)

    assert result.success == 1
    opts = module.YoutubeDL.instances[0].opts
    assert opts["format"] == "ba/b"
    assert "merge_output_format" not in opts
    assert opts["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert [p.name for p in tmp_path.rglob("*.m4a")] == ["2024-01-02_测试视频_iqiyi_1234567.m4a"]


async def test_options_carry_proxy_quality_and_extra(tmp_path, fake_ytdlp):
    module = fake_ytdlp()
    downloader = _make_downloader(
        tmp_path,
        top_level={"proxy": "http://127.0.0.1:7890", "thread": 3, "retry_times": 7},
        quality="720p",
        download_subtitle=True,
        download_cover=True,
        download_json=True,
        extra_options={"geo_bypass": True},
    )

    await downloader.download(PARSED)

    opts = module.YoutubeDL.instances[0].opts
    assert opts["proxy"] == "http://127.0.0.1:7890"
    assert opts["format"].startswith("bv*[height<=720]")
    assert opts["retries"] == 7
    assert opts["fragment_retries"] == 7
    assert opts["concurrent_fragment_downloads"] == 3
    assert opts["writesubtitles"] is True
    assert opts["writethumbnail"] is True
    assert opts["writeinfojson"] is True
    assert opts["geo_bypass"] is True


async def test_author_falls_back_to_platform_name(tmp_path, fake_ytdlp):
    """影视类站点常没有 uploader，作者目录退到平台中文名。"""
    entry = {k: v for k, v in ENTRY.items() if k not in ("uploader", "uploader_id", "uploader_url")}
    fake_ytdlp(info=entry)
    downloader = _make_downloader(tmp_path)

    await downloader.download(PARSED)

    media = list(tmp_path.rglob("*.mp4"))
    assert media[0].parent.parent.parent.name == "爱奇艺"


async def test_missing_upload_date_falls_back_to_today(tmp_path, fake_ytdlp):
    entry = {k: v for k, v in ENTRY.items() if k not in ("upload_date", "timestamp")}
    fake_ytdlp(info=entry)
    downloader = _make_downloader(tmp_path)

    result = await downloader.download(PARSED)

    assert result.success == 1
    media = list(tmp_path.rglob("*.mp4"))
    assert media[0].name.endswith("_测试视频_iqiyi_1234567.mp4")
    assert len(media[0].name.split("_")[0]) == 10  # YYYY-MM-DD


async def test_entry_without_id_counts_as_failure(tmp_path, fake_ytdlp):
    fake_ytdlp(info={"title": "no id"})
    downloader = _make_downloader(tmp_path)

    result = await downloader.download(PARSED)

    assert (result.total, result.failed) == (1, 1)
