"""直播回放全量下载器（ReplayDownloadManager）测试。

覆盖三块：
  - consider() 的长视频判定（分段数 × 总大小阈值、同 encfilekey 去重、
    token 刷新后同 key 仍只触发一次）；
  - 解密拼装：密钥流 = MSE 明文头 XOR 密文头 → 解出 ftyp → 完整落盘；
    拿错明文（ftyp 不匹配）拒绝并保留 .part；
  - 与 SnifferAddon / MseCaptureStore 的接线（分段响应触发 consider、
    MSE finalize 通知 on_mse_saved）。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from channels.diagnostics import ChannelsDiagnostics
from channels.feed_store import FeedStore
from channels.replay_downloader import (
    ReplayDownloadManager,
    _encfilekey,
)

URL = "https://finder.video.qq.com/251/20302/stodownload?encfilekey=KEY123&token=T1"


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _manager(tmp_path: Path, **kw):
    kw.setdefault("min_total_bytes", 1000)
    kw.setdefault("trigger_segments", 3)
    return ReplayDownloadManager(
        FeedStore(),
        ChannelsDiagnostics(),
        temp_dir=tmp_path / "replay",
        **kw,
    )


class TestConsider:
    def test_thresholds_and_dedup(self):
        m = _manager(Path("."), min_total_bytes=1000, trigger_segments=3)
        assert m.consider(URL, 500) is False       # 段1
        assert m.consider(URL, 1500) is False      # 段2（大小够了段不够）
        assert m.consider(URL, 1500) is True       # 段3 + ≥1000 → 触发
        assert m.consider(URL, 1500) is False      # 已触发去重
        # token 刷新（同 encfilekey 新 URL）不再触发。
        assert m.consider(URL.replace("T1", "T2"), 1500) is False

    def test_short_video_never_triggers(self):
        m = _manager(Path("."), min_total_bytes=1024 * 1024)
        for _ in range(10):
            assert m.consider(URL, 1024) is False

    def test_encfilekey_extraction(self):
        assert _encfilekey(URL) == "KEY123"
        assert _encfilekey("https://x.example/nothing") == ""


class TestFinalize:
    def _prepare(self, tmp_path: Path, total: int = 8192):
        """构造密文 .part（真实场景是 CDN 下载）与 MSE 明文头文件。"""
        m = _manager(tmp_path)
        plain_head = (
            b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2"
            + os.urandom(total - 24)
        )
        key_stream = os.urandom(total)
        cipher_head = _xor(plain_head, key_stream)
        tail = os.urandom(4096)  # 131072 之后的「明文」尾段
        part = tmp_path / "cipher.part"
        part.write_bytes(cipher_head + tail)
        plain = tmp_path / "mse_plain.mp4"
        plain.write_bytes(plain_head)
        return m, part, plain, plain_head, cipher_head, tail

    def test_finalize_refuses_unverified_prefix_replacement(self, tmp_path):
        from channels.replay_downloader import _PendingReplay

        m, part, plain, plain_head, cipher_head, tail = self._prepare(tmp_path)
        m._pending["KEY123"] = _PendingReplay(
            "KEY123", URL, len(cipher_head) + len(tail), part
        )
        ok = m._finalize_with(m._pending["KEY123"], plain)
        assert ok is False
        assert part.exists()
        assert "KEY123" in m._pending
        assert len(m.feed_store) == 0
        assert m.diagnostics.get("replay_downloads") == 0

    def test_wrong_plain_rejected_pending_kept(self, tmp_path):
        from channels.replay_downloader import _PendingReplay

        m, part, _plain, _ph, cipher_head, tail = self._prepare(tmp_path)
        m._pending["KEY123"] = _PendingReplay(
            "KEY123", URL, len(cipher_head) + len(tail), part
        )
        # 拿错明文（XOR 后解不出 ftyp）：拒绝且不删 .part。
        wrong = tmp_path / "wrong.mp4"
        wrong.write_bytes(os.urandom(8192))
        assert m._finalize_with(m._pending["KEY123"], wrong) is False
        assert "KEY123" in m._pending
        assert part.exists()

    def test_short_plain_does_not_finalize_partial_head(self, tmp_path):
        """明文头不足 128KB（用户只播了几秒）：可解部分截断，文件仍落盘。"""
        from channels.replay_downloader import _PendingReplay

        total = 16384
        plain_full = b"\x00\x00\x00\x20ftypisom" + os.urandom(total - 12)
        key_stream = os.urandom(total)
        cipher_head = _xor(plain_full, key_stream)
        tail = os.urandom(2048)
        part = tmp_path / "c.part"
        part.write_bytes(cipher_head + tail)
        # MSE 明文只有前 4096 字节。
        plain_short = tmp_path / "short.mp4"
        plain_short.write_bytes(plain_full[:4096])
        m = _manager(tmp_path)
        m._pending["K"] = _PendingReplay("K", URL, total + len(tail), part)
        assert m._finalize_with(m._pending["K"], plain_short) is False
        saved = list((tmp_path / "replay" / "saved").glob("*.mp4"))
        assert saved == []
        assert part.exists()


class TestWiring:
    def test_sniffer_triggers_replay_on_segments(self):
        """SnifferAddon 收到第三次带 /TOTAL 的分段响应 → replay.consider 触发。"""
        from channels.interceptor import SnifferAddon

        calls = []

        class FakeReplay:
            def consider(self, url, total):
                calls.append((url, total))
                return len(calls) >= 3

            async def download(self, url, total):
                raise AssertionError("download 由调用方 create_task，此测试不触达")

        addon = SnifferAddon(FeedStore(), replay=FakeReplay())

        def seg(url_total: str, length: int = 262144):
            return SimpleNamespace(
                request=SimpleNamespace(
                    pretty_host="finder.video.qq.com",
                    path="stodownload",
                    method="GET",
                    url=URL,
                    headers={},
                ),
                response=SimpleNamespace(
                    status_code=206,
                    headers={
                        "content-type": "video/mp4",
                        "content-length": str(length),
                        "content-range": f"bytes 0-262143/{url_total}",
                    },
                ),
            )

        addon.response(seg("5000"))
        addon.response(seg("5000"))
        # consider 每次分段都会被调用（返回 False 表示未达触发条件）。
        assert len(calls) == 2
        addon.response(seg("99999999"))
        assert len(calls) == 3

    def test_mse_finalize_does_not_guess_replay_key(self, tmp_path, channels_real_mp4):
        import base64

        from channels.feed import ChannelFeed
        from channels.mse_capture import MseCaptureStore

        notified = []

        class FakeReplay:
            def on_mse_saved(self, path):
                notified.append(Path(path))

        store = MseCaptureStore(
            FeedStore(), ChannelsDiagnostics(), replay=FakeReplay(), temp_dir=tmp_path / "parts"
        )
        store.begin("stream1234", mime="video/mp4", page="feed")
        store.feed_store.add([ChannelFeed(object_id="original1234", kind="video", duration=2,
                                          title="t")])
        # init segment（ftyp 开头）+ 一段 media 数据。
        payload = base64.b64encode(channels_real_mp4.read_bytes()).decode("ascii")
        store.chunk("stream1234", payload)
        result = store.finalize("stream1234", {
            "feed_id": "original1234", "complete": True, "duration": 2,
        })
        assert Path(result["path"]).exists()
        assert notified == []
