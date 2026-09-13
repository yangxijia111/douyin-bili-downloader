"""WBI 签名测试。

签名一旦算错，所有 wbi 接口（空间投稿、用户信息）都会返回 -403，而报错与
「风控」「未登录」无法区分。所以这里锁死两件事：mixin_key 的重排结果，以及
``w_rid`` 对参数集合的确定性映射。
"""

import hashlib

from bilibili import wbi

# 真实 nav 响应里的 img_url / sub_url（文件名即 key）。
IMG_URL = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
SUB_URL = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"


def test_extract_keys_strips_path_and_extension():
    img_key, sub_key = wbi.extract_keys(IMG_URL, SUB_URL)
    assert img_key == "7cd084941338484aae1ad9425b84077c"
    assert sub_key == "4932caff0ff746eab6f01bf08b70ac45"


def test_extract_keys_tolerates_empty_input():
    assert wbi.extract_keys("", "") == ("", "")


def test_get_mixin_key_is_deterministic_and_32_chars():
    img_key, sub_key = wbi.extract_keys(IMG_URL, SUB_URL)
    first = wbi.get_mixin_key(img_key, sub_key)
    second = wbi.get_mixin_key(img_key, sub_key)
    assert first == second
    assert len(first) == wbi.MIXIN_KEY_LENGTH
    # 必须是对原始 64 位串的重排，而不是原样截断。
    raw = (img_key + sub_key)[: wbi.RAW_KEY_LENGTH]
    assert first != raw[: wbi.MIXIN_KEY_LENGTH]


def test_get_mixin_key_skips_out_of_range_indices():
    """key 比预期短时不能抛 IndexError，否则 nav 换了 key 长度就会整条链路崩。"""
    assert len(wbi.get_mixin_key("abc", "def")) <= wbi.MIXIN_KEY_LENGTH


def test_sign_params_adds_wts_and_w_rid():
    signed = wbi.sign_params({"mid": "271779326", "ps": 30}, "imgkey", "subkey", timestamp=1700000000)
    assert signed["wts"] == 1700000000
    assert len(signed["w_rid"]) == 32
    assert signed["mid"] == "271779326"
    assert signed["ps"] == "30"


def test_sign_params_is_deterministic_for_fixed_timestamp():
    args = ({"mid": "1", "pn": 1}, "imgkey", "subkey")
    assert wbi.sign_params(*args, timestamp=1700000000) == wbi.sign_params(
        *args, timestamp=1700000000
    )


def test_sign_params_rid_matches_recomputed_query():
    """独立复算一遍 w_rid，确保排序/编码/去字符三个环节都与官方算法一致。"""
    img_key, sub_key = wbi.extract_keys(IMG_URL, SUB_URL)
    signed = wbi.sign_params(
        {"mid": "271779326", "order": "pubdate", "pn": 1}, img_key, sub_key, timestamp=1700000000
    )
    rid = signed.pop("w_rid")

    from urllib.parse import urlencode

    ordered = dict(sorted(signed.items()))
    query = urlencode({k: str(v) for k, v in ordered.items()})
    expected = hashlib.md5((query + wbi.get_mixin_key(img_key, sub_key)).encode("utf-8")).hexdigest()
    assert rid == expected


def test_sign_params_strips_forbidden_value_chars():
    """值里的 ``!'()*`` 必须剔除，否则 URL 编码结果与官方不一致、签名校验失败。"""
    signed = wbi.sign_params({"keyword": "a!b'c(d)e*f"}, "k", "s", timestamp=1700000000)
    assert signed["keyword"] == "abcdef"


def test_sign_params_drops_none_values():
    signed = wbi.sign_params({"mid": "1", "keyword": None}, "k", "s", timestamp=1700000000)
    assert "keyword" not in signed
