"""B 站下载器测试（全部离线）。

覆盖三条最容易出错、且出错时不报错的路径：

* **磁盘增量**：把「只下到第 1 P 的多 P 稿件」误判成整条已完成，缺失的分 P
  就永远补不回来；
* **DASH 合并**：视频轨 + 音频轨两次下载后的合并与临时文件清理，失败时必须
  显式失败而不是留下无声视频；
* **durl 兜底**：没有 DASH 清单的老稿件要走单文件流，否则直接判失败。
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bilibili.api_client import BiliAPIClient
from bilibili.downloader_base import (
    _seconds_to_timestamp,
    _subtitle_body_to_srt,
    _video_tokens,
)
from bilibili.merger import FFmpegMissingError
from bilibili.video_downloader import BiliVideoDownloader
from config import ConfigLoader
from control import RetryHandler
from storage import FileManager

BVID = "BV1GJ411x7h7"

DETAIL = {
    "bvid": BVID,
    "aid": 170001,
    "title": "测试稿件",
    "desc": "稿件描述",
    "pubdate": 1700000000,
    "pic": "//i0.hdslb.com/bfs/archive/cover.jpg",
    "cid": 111,
    "owner": {"mid": 271779326, "name": "测试UP"},
    "pages": [
        {"cid": 111, "page": 1, "part": "第一话"},
        {"cid": 222, "page": 2, "part": "第二话"},
    ],
}

SINGLE_PAGE_DETAIL = dict(DETAIL, pages=[{"cid": 111, "page": 1, "part": "测试稿件"}])

PLAYINFO = {
    "quality": 80,
    "format": "mp4",
    "timelength": 100000,
    "accept_quality": [80, 64, 32],
    "dash": {
        "duration": 100,
        "video": [
            {
                "id": 80,
                "codecs": "avc1.640028",
                "bandwidth": 2000000,
                "width": 1920,
                "height": 1080,
                "baseUrl": "https://upos-sz.bilivideo.com/video.m4s",
                "backupUrl": ["https://upos-hz.bilivideo.com/video.m4s"],
            }
        ],
        "audio": [
            {
                "id": 30280,
                "bandwidth": 300000,
                "baseUrl": "https://upos-sz.bilivideo.com/audio.m4s",
            }
        ],
    },
}


class _FakeDatabase:
    def __init__(self):
        self.add_aweme = AsyncMock()
        self.is_downloaded = AsyncMock(return_value=False)


def _make_downloader(
    tmp_path,
    *,
    database=None,
    progress=None,
    mocks=None,
    retries=0,
    top_level=None,
    **bili_overrides,
):
    """构造 :class:`BiliVideoDownloader`。

    ``top_level`` 放与抖音共用的顶层键（``start_time`` / ``redownload_missing_files``
    …），``**bili_overrides`` 放 ``bilibili:`` 段下的键。
    """
    config = ConfigLoader(None)
    config.update(path=str(tmp_path))
    section = {"request_interval": 0}
    section.update(bili_overrides)
    config.update(bilibili=section)
    if top_level:
        config.update(**top_level)

    file_manager = FileManager(str(tmp_path))
    client = BiliAPIClient({}, request_interval=0)

    if mocks is not None:
        async def _fake_download(url, save_path, session=None, headers=None, proxy=None, **_kwargs):
            target = Path(save_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"stream-bytes")
            return True

        file_manager.download_file = _fake_download
        client.get_session = AsyncMock(return_value=object())
        client.get_video_detail = mocks.get("detail", AsyncMock(return_value=DETAIL))
        client.get_playurl = mocks.get("playurl", AsyncMock(return_value=PLAYINFO))

    downloader = BiliVideoDownloader(
        config,
        client,
        file_manager,
        database=database,
        retry_handler=RetryHandler(max_retries=retries),
        progress_reporter=progress,
    )
    return downloader, client, file_manager


def _patch_merge(monkeypatch, *, write=True):
    calls = []

    async def _fake_merge(video_path, audio_path, output_path, *, ffmpeg_path=None):
        calls.append(
            {
                "video": Path(video_path).name,
                "audio": Path(audio_path).name if audio_path else None,
                "output": Path(output_path).name,
                "ffmpeg_path": ffmpeg_path,
            }
        )
        if write:
            Path(output_path).write_bytes(b"merged-output")
        return True

    monkeypatch.setattr("bilibili.downloader_base.merge_dash_streams", _fake_merge)
    return calls


# ----------------------------------------------------------------------
# 磁盘增量
# ----------------------------------------------------------------------


async def test_single_page_file_marks_video_downloaded(tmp_path):
    (tmp_path / f"2023-11-15_标题_{BVID}.mp4").write_bytes(b"x" * 8)
    downloader, _, _ = _make_downloader(tmp_path)

    assert await downloader._should_download(BVID, url_type="video") is False


async def test_multi_page_partial_stays_eligible_for_examination(tmp_path):
    """看到 bvid_p1 时不能判定整条完成，否则缺失的 P 永远补不回来。"""
    (tmp_path / f"2023-11-15_标题_{BVID}_p1.mp4").write_bytes(b"x" * 8)
    downloader, _, _ = _make_downloader(tmp_path)

    assert await downloader._should_download(BVID, url_type="video") is True
    assert await downloader._should_download_page(f"{BVID}_p1") is False
    assert await downloader._should_download_page(f"{BVID}_p2") is True


async def test_partially_downloaded_multi_page_is_topped_up(tmp_path, monkeypatch):
    """只有 p1 时，重跑必须下 p2——这是最容易写错、也最致命的判定。"""
    (tmp_path / f"d_{BVID}_p1.mp4").write_bytes(b"x" * 8)
    _patch_merge(monkeypatch)
    downloader, client, _ = _make_downloader(tmp_path, mocks={})

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="post", url_type="video"
    )

    assert status == "success"
    names = sorted(path.name for path in tmp_path.rglob("*.mp4"))
    assert any("_p2" in name for name in names)
    # p1 不应被重复下载。
    assert len([name for name in names if "_p1" in name]) == 1
    assert client.get_playurl.await_count == 1


async def test_fully_downloaded_multi_page_reports_skipped(tmp_path, monkeypatch):
    """下齐的多 P 稿件重跑时全部页命中，整条记为 skipped 且不再请求播放地址。"""
    _patch_merge(monkeypatch)
    (tmp_path / f"d_{BVID}_p1.mp4").write_bytes(b"x" * 8)
    (tmp_path / f"d_{BVID}_p2.mp4").write_bytes(b"x" * 8)
    downloader, client, _ = _make_downloader(tmp_path, mocks={})

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="post", url_type="video"
    )

    assert status == "skipped"
    # 详情请求仍需一次（用来知道有几 P），但没有任何媒体下载。
    assert client.get_playurl.await_count == 0


async def test_sidecar_files_do_not_count_as_downloaded(tmp_path):
    (tmp_path / f"d_{BVID}_cover.jpg").write_bytes(b"x" * 8)
    (tmp_path / f"d_{BVID}.danmaku.xml").write_bytes(b"<i/>")
    downloader, _, _ = _make_downloader(tmp_path)

    assert await downloader._should_download(BVID, url_type="video") is True


async def test_leftover_m4s_fragments_do_not_count_as_downloaded(tmp_path):
    """进程被杀留下的 .m4s 分片不能算已下载，否则该稿件永远补不下。"""
    (tmp_path / f"d_{BVID}.video.m4s").write_bytes(b"x" * 8)
    downloader, _, _ = _make_downloader(tmp_path)

    assert await downloader._should_download(BVID, url_type="video") is True


async def test_empty_file_does_not_count_as_downloaded(tmp_path):
    (tmp_path / f"d_{BVID}.mp4").write_bytes(b"")
    downloader, _, _ = _make_downloader(tmp_path)

    assert await downloader._should_download(BVID, url_type="video") is True


async def test_increase_disabled_forces_redownload(tmp_path):
    (tmp_path / f"d_{BVID}.mp4").write_bytes(b"x" * 8)
    downloader, _, _ = _make_downloader(tmp_path, increase={"video": False})

    assert await downloader._should_download(BVID, url_type="video") is True


async def test_increase_scoped_per_url_type(tmp_path):
    """关掉 user 的增量不该影响 video 的增量。"""
    (tmp_path / f"d_{BVID}.mp4").write_bytes(b"x" * 8)
    downloader, _, _ = _make_downloader(tmp_path, increase={"user": False})

    assert await downloader._should_download(BVID, url_type="video") is False
    assert await downloader._should_download(BVID, url_type="user") is True


async def test_database_history_used_when_redownload_missing_disabled(tmp_path):
    """redownload_missing_files=false 时，磁盘缺失但历史库有记录则跳过。"""
    database = _FakeDatabase()
    database.is_downloaded = AsyncMock(return_value=True)
    downloader, _, _ = _make_downloader(
        tmp_path,
        database=database,
        top_level={"redownload_missing_files": False},
    )

    assert await downloader._should_download(BVID, url_type="video") is False
    assert await downloader._should_download(BVID, url_type="post") is True


# ----------------------------------------------------------------------
# DASH 下载与合并
# ----------------------------------------------------------------------


async def test_multi_page_download_produces_one_mp4_per_page(tmp_path, monkeypatch):
    calls = _patch_merge(monkeypatch)
    downloader, client, _ = _make_downloader(tmp_path, mocks={})

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="post", url_type="video"
    )

    assert status == "success"
    mp4s = sorted(tmp_path.rglob("*.mp4"))
    assert len(mp4s) == 2
    names = [path.name for path in mp4s]
    assert any("_p1" in name for name in names)
    assert any("_p2" in name for name in names)
    # 两页各一次合并，输入分别是视频轨与音频轨，且没有临时分片残留。
    assert len(calls) == 2
    assert all(call["audio"] and call["audio"].endswith(".audio.m4s") for call in calls)
    assert list(tmp_path.rglob("*.m4s")) == []
    assert client.get_playurl.await_count == 2


async def test_download_writes_manifest_and_database_record(tmp_path, monkeypatch):
    _patch_merge(monkeypatch)
    database = _FakeDatabase()
    downloader, _, _ = _make_downloader(
        tmp_path, database=database, mocks={"detail": AsyncMock(return_value=SINGLE_PAGE_DETAIL)}
    )

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="post", url_type="video"
    )

    assert status == "success"
    lines = (tmp_path / "download_manifest.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["platform"] == "bilibili"
    assert record["bvid"] == BVID
    assert record["author_url"] == "https://space.bilibili.com/271779326/video"
    assert len(record["file_names"]) == 1
    assert record["file_names"][0].endswith(".mp4")

    # 数据库复用 aweme 表，用 bili_video 前缀把两个平台分开。
    assert database.add_aweme.await_count == 1
    row = database.add_aweme.await_args.args[0]
    assert row["aweme_id"] == BVID
    assert row["aweme_type"] == "bili_video"
    assert row["author_name"] == "测试UP"


async def test_single_video_entry_skips_without_any_api_call(tmp_path, monkeypatch):
    """单稿件入口的廉价预检命中时，连详情请求都不该发。"""
    _patch_merge(monkeypatch)
    (tmp_path / f"d_{BVID}.mp4").write_bytes(b"x" * 8)
    downloader, client, _ = _make_downloader(tmp_path, mocks={})

    result = await downloader.download({"type": "video", "bvid": BVID})

    assert result.total == 1
    assert result.skipped == 1
    assert client.get_video_detail.await_count == 0
    assert client.get_playurl.await_count == 0


async def test_download_skips_when_already_present(tmp_path, monkeypatch):
    _patch_merge(monkeypatch)
    (tmp_path / f"d_{BVID}.mp4").write_bytes(b"x" * 8)
    downloader, client, _ = _make_downloader(tmp_path, mocks={})

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="post", url_type="video"
    )

    assert status == "skipped"
    # 详情请求用于确认分 P 数量，但不该有任何媒体下载。
    assert client.get_playurl.await_count == 0


async def test_force_bypasses_whole_video_check(tmp_path, monkeypatch):
    """链接里显式写了 ?p=N 时必须能补下，即使第 1 P 已在盘上。"""
    _patch_merge(monkeypatch)
    (tmp_path / f"d_{BVID}_p1.mp4").write_bytes(b"x" * 8)
    downloader, client, _ = _make_downloader(tmp_path, mocks={})

    status = await downloader.download_video_item(
        {"bvid": BVID},
        author_name="x",
        author_mid=None,
        mode="video",
        url_type="video",
        page_filter=2,
        force=True,
    )

    assert status == "success"
    assert client.get_playurl.await_count == 1
    assert list(tmp_path.rglob("*_p2.mp4"))


async def test_audio_only_saves_m4a_and_skips_video_track(tmp_path, monkeypatch):
    calls = _patch_merge(monkeypatch)
    downloader, _, _ = _make_downloader(
        tmp_path,
        mocks={"detail": AsyncMock(return_value=SINGLE_PAGE_DETAIL)},
        audio_only=True,
    )

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="video", url_type="video"
    )

    assert status == "success"
    assert list(tmp_path.rglob("*.m4a"))
    assert list(tmp_path.rglob("*.mp4")) == []
    assert calls[0]["video"].endswith(".audio.m4s")
    assert calls[0]["audio"] is None


async def test_missing_dash_falls_back_to_durl_single_file(tmp_path, monkeypatch):
    _patch_merge(monkeypatch)
    playurl = AsyncMock(
        side_effect=[
            {},  # 没有 dash
            {
                "format": "mp4",
                "durl": [
                    {
                        "url": "https://upos-sz.bilivideo.com/single.mp4",
                        "backup_url": ["https://upos-hz.bilivideo.com/single.mp4"],
                    }
                ],
            },
        ]
    )
    downloader, _, _ = _make_downloader(
        tmp_path,
        mocks={
            "detail": AsyncMock(return_value=SINGLE_PAGE_DETAIL),
            "playurl": playurl,
        },
    )

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="video", url_type="video"
    )

    assert status == "success"
    assert list(tmp_path.rglob("*.mp4"))
    assert playurl.await_count == 2
    # 第二次必须是 fnval=1（单文件流）。
    assert playurl.await_args_list[1].kwargs.get("fnval") == 1


async def test_flv_format_keeps_flv_extension(tmp_path, monkeypatch):
    _patch_merge(monkeypatch)
    playurl = AsyncMock(
        side_effect=[{}, {"format": "flv", "durl": [{"url": "https://cdn/x.flv"}]}]
    )
    downloader, _, _ = _make_downloader(
        tmp_path,
        mocks={"detail": AsyncMock(return_value=SINGLE_PAGE_DETAIL), "playurl": playurl},
    )

    await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="video", url_type="video"
    )

    assert list(tmp_path.rglob("*.flv"))


async def test_no_playable_stream_fails_item(tmp_path, monkeypatch):
    _patch_merge(monkeypatch)
    playurl = AsyncMock(side_effect=[{}, {"format": "mp4", "durl": []}])
    downloader, _, _ = _make_downloader(
        tmp_path,
        mocks={"detail": AsyncMock(return_value=SINGLE_PAGE_DETAIL), "playurl": playurl},
    )

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="video", url_type="video"
    )

    assert status == "failed"
    assert list(tmp_path.rglob("*.mp4")) == []


async def test_merge_failure_removes_fragments_and_fails(tmp_path, monkeypatch):
    async def _failing_merge(video_path, audio_path, output_path, *, ffmpeg_path=None):
        return False

    monkeypatch.setattr("bilibili.downloader_base.merge_dash_streams", _failing_merge)
    downloader, _, _ = _make_downloader(
        tmp_path, mocks={"detail": AsyncMock(return_value=SINGLE_PAGE_DETAIL)}
    )

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="video", url_type="video"
    )

    assert status == "failed"
    assert list(tmp_path.rglob("*.mp4")) == []
    assert list(tmp_path.rglob("*.m4s")) == []


async def test_optional_assets_are_saved_when_enabled(tmp_path, monkeypatch):
    _patch_merge(monkeypatch)
    (tmp_path / "unused").write_text("")
    downloader, client, _ = _make_downloader(
        tmp_path,
        mocks={"detail": AsyncMock(return_value=SINGLE_PAGE_DETAIL)},
        download_cover=True,
        download_json=True,
    )

    status = await downloader.download_video_item(
        {"bvid": BVID}, author_name="x", author_mid=None, mode="video", url_type="video"
    )

    assert status == "success"
    assert list(tmp_path.rglob("*_cover.jpg"))
    json_files = list(tmp_path.rglob("*_data.json"))
    assert json_files
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert payload["bvid"] == BVID
    assert payload["_page"]["page"] == 1


# ----------------------------------------------------------------------
# 合并降级
# ----------------------------------------------------------------------


async def test_finalize_media_renames_single_track_without_ffmpeg(tmp_path, monkeypatch):
    async def _raise(*_args, **_kwargs):
        raise FFmpegMissingError("no ffmpeg")

    monkeypatch.setattr("bilibili.downloader_base.merge_dash_streams", _raise)
    downloader, _, _ = _make_downloader(tmp_path)

    video_tmp = tmp_path / "a.video.m4s"
    video_tmp.write_bytes(b"video-only")
    target = tmp_path / "a.mp4"

    assert await downloader._finalize_media(video_tmp, None, target) is True
    assert target.read_bytes() == b"video-only"
    assert not video_tmp.exists()


async def test_finalize_media_fails_two_tracks_without_ffmpeg(tmp_path, monkeypatch):
    """双轨缺 ffmpeg 必须失败：静默产出无声视频是更糟的结果。"""

    async def _raise(*_args, **_kwargs):
        raise FFmpegMissingError("no ffmpeg")

    monkeypatch.setattr("bilibili.downloader_base.merge_dash_streams", _raise)
    downloader, _, _ = _make_downloader(tmp_path)

    video_tmp = tmp_path / "a.video.m4s"
    video_tmp.write_bytes(b"video")
    audio_tmp = tmp_path / "a.audio.m4s"
    audio_tmp.write_bytes(b"audio")
    target = tmp_path / "a.mp4"

    assert await downloader._finalize_media(video_tmp, audio_tmp, target) is False
    assert not target.exists()
    assert not video_tmp.exists()
    assert not audio_tmp.exists()


# ----------------------------------------------------------------------
# 真实 ffmpeg 合并（不 monkeypatch，填补 .part 输出格式的测试盲区）
# ----------------------------------------------------------------------


def _ffmpeg_or_skip() -> str:
    from core.ffmpeg import resolve_ffmpeg_path

    executable = resolve_ffmpeg_path()
    if not executable:
        pytest.skip("ffmpeg not available")
    return executable


def _make_track(ffmpeg: str, path: Path, args: list) -> None:
    import subprocess

    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args, "-f", "mp4", str(path)],
        check=True,
        capture_output=True,
    )


def test_real_ffmpeg_merges_dash_into_part_output(tmp_path):
    """端到端验证合并命令：输出临时名是 ``.mp4.part``，ffmpeg 无法从该扩展名
    推断封装格式（线上曾因此全部合并失败、静默走 durl 兜底），必须显式传
    ``-f``。此测试不 monkeypatch，直接调真实 ffmpeg。"""
    from bilibili.merger import merge_dash_streams

    ffmpeg = _ffmpeg_or_skip()
    video_in = tmp_path / "in.video.m4s"
    audio_in = tmp_path / "in.audio.m4s"
    _make_track(
        ffmpeg, video_in,
        ["-f", "lavfi", "-i", "testsrc=duration=0.5:size=128x72:rate=10", "-c:v", "libx264"],
    )
    _make_track(
        ffmpeg, audio_in,
        ["-f", "lavfi", "-i", "sine=duration=0.5:sample_rate=44100", "-c:a", "aac"],
    )

    target = tmp_path / "out.mp4"
    merged = asyncio.run(merge_dash_streams(video_in, audio_in, target))

    assert merged is True
    assert target.stat().st_size > 0
    # merge 自身不删输入；输入轨由调用方（_finalize_media）清理。
    assert video_in.exists() and audio_in.exists()


def test_real_ffmpeg_wraps_audio_only_into_m4a(tmp_path):
    """纯音频落盘走 ipod muxer（.m4a），同样经 ``.part`` 临时名。"""
    from bilibili.merger import merge_dash_streams

    ffmpeg = _ffmpeg_or_skip()
    audio_in = tmp_path / "in.audio.m4s"
    _make_track(
        ffmpeg, audio_in,
        ["-f", "lavfi", "-i", "sine=duration=0.5:sample_rate=44100", "-c:a", "aac"],
    )

    target = tmp_path / "out.m4a"
    merged = asyncio.run(merge_dash_streams(audio_in, None, target))

    assert merged is True
    assert target.stat().st_size > 0


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


def test_video_tokens_omit_bare_bvid_for_multi_page_files():
    """分 P 文件名只产出 bvid_pN——产出裸 bvid 会把部分下载误判成整条完成。"""
    assert _video_tokens("2023-11-15_标题_BV1GJ411x7h7.mp4") == ["BV1GJ411x7h7"]
    assert _video_tokens("2023-11-15_标题_BV1GJ411x7h7_p2.mp4") == ["BV1GJ411x7h7_p2"]


def test_video_tokens_ignore_unrelated_filenames():
    assert _video_tokens("random-name.mp4") == []
    assert _video_tokens("") == []


def test_subtitle_body_to_srt_formats_timestamps():
    srt = _subtitle_body_to_srt(
        [{"from": 1.5, "to": 3.25, "content": "你好"}, {"from": 3.25, "to": 5.0, "content": "世界"}]
    )
    assert "00:00:01,500 --> 00:00:03,250" in srt
    assert "你好" in srt
    assert srt.startswith("1\n")


def test_subtitle_body_to_srt_skips_blank_content():
    assert _subtitle_body_to_srt([{"from": 0, "to": 1, "content": "   "}]) == ""


def test_seconds_to_timestamp_handles_bad_input():
    assert _seconds_to_timestamp(None) == "00:00:00,000"
    assert _seconds_to_timestamp("abc") == "00:00:00,000"
    assert _seconds_to_timestamp(-5) == "00:00:00,000"
    assert _seconds_to_timestamp(3661.5) == "01:01:01,500"


def test_time_filter_keeps_items_with_unknown_pubdate(tmp_path):
    """发布时间缺失时宁可多下一次，也不能静默漏掉用户要的稿件。"""
    downloader, _, _ = _make_downloader(tmp_path, top_level={"start_time": "2024-01-01"})
    items = [{"bvid": "BV1"}, {"bvid": "BV2", "pubdate": None}]
    assert len(downloader._filter_by_time(items)) == 2


def test_time_filter_drops_items_outside_window(tmp_path):
    downloader, _, _ = _make_downloader(
        tmp_path, top_level={"start_time": "2024-01-01", "end_time": "2024-01-31"}
    )
    items = [
        {"bvid": "old", "pubdate": 1700000000},  # 2023-11
        {"bvid": "in", "pubdate": 1704153600},  # 2024-01-02
        {"bvid": "late", "pubdate": 1709251200},  # 2024-03-01
    ]
    assert [item["bvid"] for item in downloader._filter_by_time(items)] == ["in"]
