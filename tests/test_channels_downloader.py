"""ChannelsDownloader 测试（全部离线，aiohttp session 用假对象替代）。

核心覆盖「边下边解密」链路：构造明文 MP4 → 用真 ISAAC64 加密 → 假 session
分块吐出密文 → 下载器落盘 → 与明文逐字节一致。这条链路同时验证了流式分块
解密、MP4 魔数自校验、命名/清单落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from channels.downloader import ChannelsDownloader
from channels.feed import ChannelFeed
from channels.isaac64 import Isaac64Cipher
from config import ConfigLoader
from storage import FileManager

# 明文 MP4：ftyp 头 + 足够跨过加密头（128 KiB）的数据。
PLAIN_MP4 = (
    b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isom"
    + bytes((i * 37 + 11) & 0xFF for i in range(300 * 1024))
)


class _FakeContent:
    def __init__(self, data: bytes, chunk: int):
        self._data = data
        self._chunk = chunk

    async def iter_chunked(self, _size: int):
        for i in range(0, len(self._data), self._chunk):
            yield self._data[i : i + self._chunk]


class _FakeResponse:
    def __init__(self, data: bytes, chunk: int = 64 * 1024, status: int = 200):
        self.status = status
        self.content = _FakeContent(data, chunk)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    """按 URL 前缀匹配返回预置响应；未命中抛 KeyError（测试 bug）。

    与 aiohttp 真实语义一致：``get`` 是同步方法，返回支持
    ``async with`` 的 context manager。
    """

    def __init__(self, mapping):
        self._mapping = mapping
        self.closed = False
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        for prefix, target in self._mapping.items():
            if url.startswith(prefix):
                if isinstance(target, Exception):
                    raise target
                return target
        raise KeyError(url)

    async def close(self):
        self.closed = True


def _video_feed(object_id: str = "obj1", decode_key: int = 20260922, **kw) -> ChannelFeed:
    defaults = dict(
        object_id=object_id,
        nonce_id=f"n_{object_id}",
        kind="video",
        url=(
            "https://finder.video.qq.com/302/20304/v.mp4"
            f"?dis_k=1&encfilekey=ek{object_id}&token=tk{object_id}"
        ),
        decode_key=decode_key,
        title="测试视频标题",
        author_name="测试作者",
        author_id="wxid_1",
        file_size=len(PLAIN_MP4),
        duration=61,
        create_time=1760000100,
    )
    defaults.update(kw)
    return ChannelFeed(**defaults)


def _make_downloader(tmp_path: Path, session=None, channels_cfg=None):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path / "downloads"))
    if channels_cfg:
        config.update(channels=channels_cfg)
    downloader = ChannelsDownloader(config, FileManager(config.get("path")))
    if session is not None:
        downloader._session = session
    return downloader


@pytest.mark.asyncio
async def test_download_video_decrypts_header(tmp_path):
    cipher = Isaac64Cipher(20260922)
    encrypted = cipher.xor_header(PLAIN_MP4, 0)
    # 用小分块（9KB，非 8 倍数/非块边界）把流式解密的错位风险暴露出来。
    session = _FakeSession({"https://finder.video.qq.com/": _FakeResponse(encrypted, chunk=9217)})
    dl = _make_downloader(tmp_path, session)

    status = await dl.download_feed(_video_feed(), manual=True)
    assert status == "done"

    files = list((tmp_path / "downloads").rglob("*.mp4"))
    assert len(files) == 1
    assert files[0].read_bytes() == PLAIN_MP4  # 解密后与明文逐字节一致
    assert not list((tmp_path / "downloads").rglob("*.part"))  # 无临时残留

    # 清单写入与平台字段。
    manifest = tmp_path / "downloads" / "download_manifest.jsonl"
    assert manifest.exists()
    record = json.loads(manifest.read_text(encoding="utf-8").strip())
    assert record["platform"] == "channels"
    assert record["aweme_id"] == "channels_obj1"


@pytest.mark.asyncio
async def test_download_video_without_key_saves_as_is(tmp_path):
    session = _FakeSession({"https://finder.video.qq.com/": _FakeResponse(PLAIN_MP4)})
    dl = _make_downloader(tmp_path, session)
    status = await dl.download_feed(_video_feed(decode_key=None), manual=True)
    assert status == "done"
    files = list((tmp_path / "downloads").rglob("*.mp4"))
    assert files[0].read_bytes() == PLAIN_MP4


@pytest.mark.asyncio
async def test_wrong_key_fails_and_leaves_no_file(tmp_path):
    cipher = Isaac64Cipher(111111)  # 用错误的 key 加密
    encrypted = cipher.xor_header(PLAIN_MP4, 0)
    session = _FakeSession({"https://finder.video.qq.com/": _FakeResponse(encrypted)})
    dl = _make_downloader(tmp_path, session)
    feed = _video_feed(decode_key=20260922)  # 下载侧持正确 key → 解密失败
    status = await dl.download_feed(feed, manual=True)
    assert status == "failed"
    assert "解密校验失败" in feed.error
    assert not list((tmp_path / "downloads").rglob("*.mp4"))
    assert not list((tmp_path / "downloads").rglob("*.part"))


@pytest.mark.asyncio
async def test_incremental_skip_when_token_on_disk(tmp_path):
    # 磁盘上已有含 channels_obj1 token 的文件 → 自动模式跳过。
    existing = tmp_path / "downloads" / "作者" / "2026-01-01_旧视频_channels_obj1.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"x")
    dl = _make_downloader(tmp_path)
    status = await dl.download_feed(_video_feed())  # manual=False 走增量
    assert status == "skipped"


@pytest.mark.asyncio
async def test_incremental_disabled(tmp_path):
    existing = tmp_path / "downloads" / "作者" / "2026-01-01_旧视频_channels_obj1.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"x")
    session = _FakeSession({"https://finder.video.qq.com/": _FakeResponse(PLAIN_MP4)})
    dl = _make_downloader(tmp_path, session, channels_cfg={"increase": False})
    status = await dl.download_feed(_video_feed(decode_key=None))
    assert status == "done"


@pytest.mark.asyncio
async def test_download_images(tmp_path):
    jpg = b"\xff\xd8\xff\xe0" + b"j" * 64
    session = _FakeSession(
        {
            "https://img.qq.com/a": _FakeResponse(jpg),
            "https://img.qq.com/b": _FakeResponse(jpg),
            "https://music.qq.com/": _FakeResponse(b"id3mp3data"),
        }
    )
    dl = _make_downloader(tmp_path, session)
    feed = ChannelFeed(
        object_id="img1",
        kind="image",
        title="图文动态",
        author_name="图片作者",
        images=["https://img.qq.com/a.jpg?token=1", "https://img.qq.com/b.jpg?token=2"],
        bgm_url="https://music.qq.com/x.mp3",
        create_time=1760000100,
    )
    assert await dl.download_feed(feed, manual=True) == "done"
    saved = sorted(p.name for p in (tmp_path / "downloads").rglob("*") if p.is_file())
    assert any("_01.jpg" in name for name in saved)
    assert any("_02.jpg" in name for name in saved)
    assert any("_bgm.mp3" in name for name in saved)


@pytest.mark.asyncio
async def test_live_auto_skipped_manual_records(tmp_path):
    dl = _make_downloader(tmp_path)

    class _FakeRecorder:
        def __init__(self):
            self.calls = []

        async def record(self, url, output):
            self.calls.append((url, output))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"FLV...")
            return 0

    recorder = _FakeRecorder()
    dl._live_recorder = recorder
    feed = ChannelFeed(object_id="live1", kind="live", url="https://live.qq.com/x.flv", title="直播")

    # 自动模式默认跳过（live_record=False）。
    assert await dl.download_feed(feed) == "skipped"
    assert recorder.calls == []

    # 手动触发（网页「录制」按钮）→ ffmpeg 录制。
    assert await dl.download_feed(feed, manual=True) == "done"
    assert len(recorder.calls) == 1
    assert list((tmp_path / "downloads").rglob("*.flv"))


@pytest.mark.asyncio
async def test_download_contract_counts(tmp_path):
    session = _FakeSession({"https://finder.video.qq.com/": _FakeResponse(PLAIN_MP4)})
    dl = _make_downloader(tmp_path, session)
    result = await dl.download(
        {"feeds": [_video_feed("a", decode_key=None), _video_feed("b", decode_key=None)]}
    )
    assert (result.total, result.success, result.failed, result.skipped) == (2, 2, 0, 0)
