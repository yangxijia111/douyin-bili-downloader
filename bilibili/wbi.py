"""Bilibili WBI 签名。

2023-03 起，``api.bilibili.com`` 的一批接口（UP 主投稿列表、用户信息等）在
普通参数之外额外校验两个签名参数：

* ``wts``   —— 当前秒级时间戳；
* ``w_rid`` —— 把参数按 key 排序、URL 编码后拼上 ``mixin_key`` 取 MD5。

``mixin_key`` 由 ``/x/web-interface/nav`` 下发的 ``wbi_img.img_url`` /
``sub_url`` 两个文件名去扩展名后拼接，再按固定的 64 位重排表打乱、截断 32 位
得到。缺签名时这些接口统一返回 ``code=-403``（风控），与「未登录」在报错上
几乎无法区分，所以签名必须是所有 wbi 接口的默认行为，而不是可选优化。

实现对齐社区公认的算法（bilibili-API-collect / SocialSisterYi），
``mixinKeyEncTab`` 是 2023-03 起的固定表。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlencode

# 固定重排表：把 64 位原始串按此顺序取字符，再截断到 32 位得到 mixin_key。
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

MIXIN_KEY_LENGTH = 32
RAW_KEY_LENGTH = 64

# 参数值里必须剔除的字符（bilibili 官方 demo 的 filter 集合）。留着会让
# URL 编码结果与官方不一致，签名校验直接失败。
_FORBIDDEN_VALUE_CHARS = "!'()*"


def _strip_url_to_key(url: str) -> str:
    """从 ``https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png``
    这类 URL 里取出不含扩展名的文件名（即 img_key / sub_key）。"""
    if not url:
        return ""
    tail = url.rsplit("/", 1)[-1]
    return tail.split(".", 1)[0]


def extract_keys(img_url: str, sub_url: str) -> tuple:
    """把 nav 的 ``wbi_img`` 两个 URL 转成 ``(img_key, sub_key)``。"""
    return _strip_url_to_key(img_url), _strip_url_to_key(sub_url)


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """按重排表算出 mixin_key。

    两个 key 各截断 32 位后拼接，因此不足 64 位时按实际长度取字符——官方
    demo 用 ``''.join(orig[i] for i in tab)`` 会在越界时抛 IndexError，这里
    改为跳过越界下标，避免接口临时换成短 key 时整条链路崩掉。
    """
    raw = f"{img_key}{sub_key}"[:RAW_KEY_LENGTH]
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:MIXIN_KEY_LENGTH]


def sign_params(
    params: Mapping[str, Any],
    img_key: str,
    sub_key: str,
    *,
    timestamp: Optional[int] = None,
) -> Dict[str, Any]:
    """返回带 ``wts`` / ``w_rid`` 的新参数表（不修改入参）。

    ``timestamp`` 只用于测试注入固定时间，正常调用留空。
    """
    mixin_key = get_mixin_key(img_key, sub_key)
    signed: Dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        cleaned = str(value)
        signed[str(key)] = "".join(ch for ch in cleaned if ch not in _FORBIDDEN_VALUE_CHARS)

    signed["wts"] = int(timestamp if timestamp is not None else time.time())
    signed = dict(sorted(signed.items()))

    query = urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed
