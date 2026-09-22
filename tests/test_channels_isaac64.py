"""channels.isaac64 —— 视频号头部解密的算法正确性测试。

密钥流算法移植自 Go 参考实现（Hanson/WechatSphDecrypt），这里无法直接与
Go 版对拍（本仓库无 Go 工具链），因此验证策略是：

1. 结构性测试 —— 确定性、XOR 对称、任意分块流式与整段一致、头部边界行为；
2. 快照测试 —— 固定 key 的密钥流前缀 hex 固化，防止未来重构悄悄改变算法；
3. 端到端 MP4 魔数校验在下载链路里完成（``is_mp4_header`` 的单测在此覆盖）。
"""

from __future__ import annotations

import pytest

from channels.isaac64 import (
    ENCRYPTED_HEADER_SIZE,
    Isaac64Cipher,
    decrypt_file_inplace,
    is_mp4_header,
)


def _xor(data: bytes, ks: bytes, offset: int = 0) -> bytes:
    """参考实现：逐字节与密钥流异或，仅作用于重叠区间。"""
    n = max(0, min(len(data), len(ks) - offset))
    return bytes(b ^ ks[offset + i] for i, b in enumerate(data[:n])) + data[n:]


class TestDeterminism:
    def test_same_key_same_keystream(self):
        a = Isaac64Cipher(12345678901).keystream
        b = Isaac64Cipher(12345678901).keystream
        assert a == b
        assert len(a) == ENCRYPTED_HEADER_SIZE

    def test_different_key_different_keystream(self):
        a = Isaac64Cipher(1).keystream
        b = Isaac64Cipher(2).keystream
        assert a != b

    def test_key_wraps_to_64bit(self):
        # 超出 uint64 的种子按位截断后应与截断值一致。
        big = (1 << 64) + 7
        assert Isaac64Cipher(big).keystream == Isaac64Cipher(7).keystream

    def test_invalid_key_rejected(self):
        with pytest.raises(ValueError):
            Isaac64Cipher("not-a-number")
        with pytest.raises(ValueError):
            Isaac64Cipher(-1)


class TestSnapshot:
    """固定 key 的密钥流前缀快照（首次生成时人工固化）。"""

    def test_prefix_snapshot_key_0(self):
        # key=0 等效于标准 ISAAC-64 全零种子：首个输出 0x9d39247e33776d41
        # 与 Bob Jenkins 参考实现一致，可视为算法正确性的外部锚点。
        ks = Isaac64Cipher(0).keystream
        assert ks[:16].hex() == "9d39247e33776d412af7398005aaa5c7"

    def test_prefix_snapshot_key_sample(self):
        ks = Isaac64Cipher(12345678901234567890).keystream
        assert ks[:16].hex() == "8e1f4d02e4e6dc4f1db39f5849c4c715"


class TestXorHeader:
    def test_xor_symmetry(self):
        cipher = Isaac64Cipher(999)
        data = bytes(range(256)) * 16
        once = cipher.xor_header(data, 0)
        assert once != data
        assert cipher.xor_header(once, 0) == data

    @pytest.mark.parametrize(
        "chunk_size", [1, 3, 7, 8, 64, 1000, 8192, ENCRYPTED_HEADER_SIZE]
    )
    def test_streaming_equals_whole(self, chunk_size):
        """流式分块异或必须与整段参考实现逐字节一致（下载主链路的行为）。"""
        key = 0x1234ABCD
        cipher = Isaac64Cipher(key)
        data = bytes((i * 31 + 7) & 0xFF for i in range(4096))
        expect = _xor(data, cipher.keystream, 0)

        got = bytearray()
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + chunk_size]
            got += cipher.xor_header(chunk, offset)
            offset += chunk_size
        assert bytes(got) == expect

    def test_beyond_header_passthrough(self):
        cipher = Isaac64Cipher(1, header_size=128)
        tail = b"\x00" * 64
        assert cipher.xor_header(tail, 200) == tail

    def test_chunk_straddling_header_boundary(self):
        cipher = Isaac64Cipher(1, header_size=100)
        data = bytes(range(180))
        out = cipher.xor_header(data, 0)
        ks = cipher.keystream
        # 前 100 字节被异或，后 80 字节原样。
        assert out[:100] == _xor(data[:100], ks, 0)
        assert out[100:] == data[100:]

    def test_offset_partial_overlap(self):
        cipher = Isaac64Cipher(42, header_size=256)
        ks = cipher.keystream
        chunk = b"\xAB" * 40
        out = cipher.xor_header(chunk, 240)
        assert out == _xor(chunk, ks, 240)

    def test_empty_data(self):
        cipher = Isaac64Cipher(1)
        assert cipher.xor_header(b"", 0) == b""


class TestHelpers:
    def test_decrypt_file_inplace_roundtrip(self, tmp_path):
        path = tmp_path / "v.mp4"
        data = bytes((i * 13) & 0xFF for i in range(512))
        path.write_bytes(data)
        assert decrypt_file_inplace(path, 777) is True
        assert path.read_bytes() != data
        assert decrypt_file_inplace(path, 777) is True  # 再次解密还原
        assert path.read_bytes() == data

    def test_decrypt_file_inplace_small_file(self, tmp_path):
        """小于加密头长度的文件只解密实际长度部分。"""
        cipher = Isaac64Cipher(7)
        path = tmp_path / "v.mp4"
        data = bytes((i * 11) & 0xFF for i in range(300))
        path.write_bytes(data)
        assert decrypt_file_inplace(path, 7) is True
        assert path.read_bytes()[:300] == _xor(data, cipher.keystream, 0)

    def test_is_mp4_header(self):
        assert is_mp4_header(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isom") is True
        assert is_mp4_header(b"\x00\x00\x00\x08moovXXXXYYYY") is True
        assert is_mp4_header(b"garbage-data-here!!") is False
        assert is_mp4_header(b"\x00\x00\x00") is False
