"""微信视频号视频的 ISAAC64 头部解密。

视频号的普通视频既不是 DRM 也不是 AES 加密的 HLS，而是「明文 MP4 + 头部流
密码异或」：文件前 :data:`ENCRYPTED_HEADER_SIZE` 字节与 ISAAC64 伪随机数
发生器（种子为接口响应里的 ``decodeKey``，uint64）产生的密钥流逐字节异或，
其余部分原样。微信客户端播放时由官方 WASM 库（``wasm_video_decode.js`` 的
``WxIsaac64``）就地解密，因此 ``decodeKey`` 必然随 feed 数据明文下发——嗅探
到响应即可解密，无需逆向客户端。

算法移植自 Hanson/WechatSphDecrypt 的 Go 实现（MIT 许可，
https://github.com/Hanson/WechatSphDecrypt ），ltaoo/wx_channels_download 的
``pkg/scraper/wxchannels/decrypt.go`` 亦源于同一实现。与标准 ISAAC-64 播种的
区别：``Seed[0] = decodeKey``、其余 255 项清零，然后用 golden 常数
``0x9e3779b97f4a7c13`` 做 4 轮 mix 与两轮状态初始化。密钥流按 8 字节一组
从 PRNG 输出的大端序编码顺序拼接，与文件的字节序一一对应，因此流式下载时
直接用「文件绝对偏移」切片异或即可，无需按块对齐重算。

XOR 只作用于头部，所以同一密钥流既可解密也可还原加密态（对称）。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import List, Optional

__all__ = [
    "ENCRYPTED_HEADER_SIZE",
    "Isaac64Cipher",
    "decrypt_file_inplace",
    "is_mp4_header",
]

_MASK64 = 0xFFFFFFFFFFFFFFFF
_GOLDEN = 0x9E3779B97F4A7C13

# 加密头长度。微信 WASM 解密库固定 generate(131072)，即仅前 128 KiB 加密。
ENCRYPTED_HEADER_SIZE = 131072


def _mix(s: List[int]) -> None:
    """ISAAC-64 的 8 变量混合函数（Bob Jenkins 原版），就地更新。

    Go 原实现的 ``uint64`` 算术自然回绕，Python 侧对应：加减结果与左移后的
    操作数必须 ``& _MASK64``；右移不产生高位、异或不扩大范围，无需再掩码。
    变量下标映射 a..h = s[0..7]。
    """
    s[0] = (s[0] - s[4]) & _MASK64
    s[5] ^= s[7] >> 9
    s[7] = (s[7] + s[0]) & _MASK64
    s[1] = (s[1] - s[5]) & _MASK64
    s[6] ^= (s[0] << 9) & _MASK64
    s[0] = (s[0] + s[1]) & _MASK64
    s[2] = (s[2] - s[6]) & _MASK64
    s[7] ^= s[1] >> 23
    s[1] = (s[1] + s[2]) & _MASK64
    s[3] = (s[3] - s[7]) & _MASK64
    s[0] ^= (s[2] << 15) & _MASK64
    s[2] = (s[2] + s[3]) & _MASK64
    s[4] = (s[4] - s[0]) & _MASK64
    s[1] ^= s[3] >> 14
    s[3] = (s[3] + s[4]) & _MASK64
    s[5] = (s[5] - s[1]) & _MASK64
    s[2] ^= (s[4] << 20) & _MASK64
    s[4] = (s[4] + s[5]) & _MASK64
    s[6] = (s[6] - s[2]) & _MASK64
    s[3] ^= s[5] >> 17
    s[5] = (s[5] + s[6]) & _MASK64
    s[7] = (s[7] - s[3]) & _MASK64
    s[4] ^= (s[6] << 14) & _MASK64
    s[6] = (s[6] + s[7]) & _MASK64


class _Isaac64State:
    """ISAAC-64 上下文：``mm`` 状态数组、``seed`` 输出缓冲与 aa/bb/cc 计数器。"""

    __slots__ = ("mm", "seed", "aa", "bb", "cc", "_remain", "_packed")

    _BUF_SIZE = 256 * 8

    def __init__(self, decode_key: int):
        self.mm: List[int] = [0] * 256
        self.seed: List[int] = [0] * 256
        self.seed[0] = decode_key & _MASK64
        self.aa = 0
        self.bb = 0
        self.cc = 0
        # 输出缓冲剩余字节数。Go 原实现 RandCnt 从 255 递减到 0 再重新生成，
        # 即每轮缓冲按 Seed[255..0] 的顺序整层消费；这里用字节数表达同一顺序，
        # 避免非整块消费时的游标对齐问题。
        self._remain = self._BUF_SIZE
        self._packed: Optional[bytes] = None
        self._init_state()

    def _init_state(self) -> None:
        s = [_GOLDEN] * 8
        for _ in range(4):
            _mix(s)
        for i in range(0, 256, 8):
            for j in range(8):
                s[j] = (s[j] + self.seed[i + j]) & _MASK64
            _mix(s)
            self.mm[i : i + 8] = s[:]
        for i in range(0, 256, 8):
            for j in range(8):
                s[j] = (s[j] + self.mm[i + j]) & _MASK64
            _mix(s)
            self.mm[i : i + 8] = s[:]
        self._isaac64()

    def _isaac64(self) -> None:
        """生成下一轮 256 个输出（填充 ``seed`` 缓冲）。"""
        mm = self.mm
        seed = self.seed
        self.cc = (self.cc + 1) & _MASK64
        self.bb = (self.bb + self.cc) & _MASK64
        aa = self.aa
        bb = self.bb
        for i in range(256):
            step = i & 3
            if step == 0:
                aa = (~(aa ^ ((aa << 21) & _MASK64))) & _MASK64
            elif step == 1:
                aa ^= aa >> 5
            elif step == 2:
                aa ^= (aa << 12) & _MASK64
            else:
                aa ^= aa >> 33
            aa = (aa + mm[(i + 128) & 255]) & _MASK64
            x = mm[i]
            y = (mm[(x >> 3) & 255] + aa + bb) & _MASK64
            mm[i] = y
            bb = (mm[(y >> 11) & 255] + x) & _MASK64
            seed[i] = bb
        self.aa = aa
        self.bb = bb
        self._packed = None

    def _pack_buffer(self) -> bytes:
        """把 ``seed`` 缓冲编码为按消费顺序（Seed[255..0]）排列的字节串。"""
        if self._packed is None:
            self._packed = b"".join(struct.pack(">Q", v) for v in reversed(self.seed))
        return self._packed

    def next_bytes(self, count: int) -> bytes:
        """顺序产出 ``count`` 字节密钥流（自动按轮刷新缓冲）。"""
        out = bytearray(count)
        pos = 0
        buf_size = self._BUF_SIZE
        while pos < count:
            if self._remain == 0:
                self._isaac64()
                self._remain = buf_size
            buf = self._pack_buffer()
            start = buf_size - self._remain
            take = min(self._remain, count - pos)
            out[pos : pos + take] = buf[start : start + take]
            pos += take
            self._remain -= take
        return bytes(out)


class Isaac64Cipher:
    """单个视频的头部解密器。

    ``decode_key`` 为 feed 响应里的 ``decodeKey``（uint64 种子）。密钥流按需
    生成一次（懒加载），之后 :meth:`xor_header` 可被流式下载的任意分块重复
    调用——``file_offset`` 是分块在完整文件中的绝对偏移，超出加密头范围的
    部分原样返回。
    """

    def __init__(self, decode_key: int, *, header_size: int = ENCRYPTED_HEADER_SIZE):
        try:
            key = int(decode_key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid decodeKey: {decode_key!r}") from exc
        if key < 0:
            raise ValueError(f"decodeKey must be unsigned: {decode_key!r}")
        self.decode_key = key & _MASK64
        self.header_size = int(header_size)
        self._keystream: Optional[bytes] = None

    @property
    def keystream(self) -> bytes:
        if self._keystream is None:
            state = _Isaac64State(self.decode_key)
            self._keystream = state.next_bytes(self.header_size)
        return self._keystream

    def xor_header(self, data: bytes, file_offset: int = 0) -> bytes:
        """对落在加密头范围内的字节做异或，范围外原样返回。

        异或用大整数整段完成，128 KiB 头部的单次开销在毫秒级。
        """
        if not data or file_offset >= self.header_size:
            return data
        ks = self.keystream
        end = min(file_offset + len(data), self.header_size)
        n = end - file_offset
        head = bytes(data[:n])
        if n:
            x = int.from_bytes(head, "little") ^ int.from_bytes(
                ks[file_offset:end], "little"
            )
            head = x.to_bytes(n, "little")
        return head + bytes(data[n:])


def decrypt_file_inplace(path: Path, decode_key: int) -> bool:
    """就地解密一个已完整下载的文件头部（小文件校验/修复场景用）。

    返回是否实际写入。下载主链路不走这里（流式边下边解密），保留它是为了
    对历史文件重算与单元测试方便。
    """
    cipher = Isaac64Cipher(decode_key)
    data = path.read_bytes()
    decrypted = cipher.xor_header(data, 0)
    if decrypted == data:
        return False
    path.write_bytes(decrypted)
    return True


def is_mp4_header(data: bytes) -> bool:
    """检查解密后的头部是否是合法 MP4（``ftyp`` / ``moov`` / ``mdat`` box）。

    用于下载完成后的自校验：解密正确则 box 魔数必然出现，解密失败（key 错、
    头长度变化）这里是第一道报警。
    """
    if len(data) < 12:
        return False
    box_type = data[4:8]
    if box_type in (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"styp"):
        return True
    # 部分转码文件 ftyp 不在最前，退一步检查任意位置 8 字节内是否有 ftyp。
    return b"ftyp" in data[:64]
