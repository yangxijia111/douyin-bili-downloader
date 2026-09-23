"""视频号嗅探的域名白名单（集中维护，MITM 最小权限边界）。

嗅探只需要「携带视频号 feed 数据的 API 响应」，它们全部来自微信主域
``weixin.qq.com`` 的子域（``channels.weixin.qq.com`` 的 finderFeed /
finderPcFlow / finderUserPage / live_replay_list 等接口）。因此默认白名单
**只有** ``weixin.qq.com`` 这一条后缀：

* 视频 CDN（``finder.video.qq.com`` 等）、QQ 主站其它子域、以及用户访问的
  一切无关网站都**不做 HTTPS 解密**——mitmproxy 侧用 ``allow_hosts`` 把
  非白名单连接直接当作 TCP 隧道转发（tunnel / passthrough）；
* 刻意**不**放宽到 ``*.qq.com``：那会把 QQ 邮箱、腾讯视频等全部流量置于
  中间人解密之下，超出功能所需权限。若后续实测发现新的必需域名，应先在
  本文件补充依据（哪个接口、什么响应），再更新默认白名单。

两层过滤共用这份白名单，结论必须一致：

1. 连接级：:meth:`ChannelsInterceptor.start` 把
   :func:`build_allow_hosts_patterns` 传给 mitmproxy ``allow_hosts``——
   不匹配的连接不解密、不产生明文副本；
2. 响应级：:class:`SnifferAddon` 用 :func:`should_intercept_host` 决定
   是否尝试解析响应体（防御纵深，也覆盖未来接入的非 TLS 场景）。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

__all__ = [
    "DEFAULT_INTERCEPT_SUFFIXES",
    "should_intercept_host",
    "build_allow_hosts_patterns",
    "normalize_host",
]

# 视频号 feed 接口所在主域（后缀匹配，覆盖全部子域）。当前唯一必需域名。
DEFAULT_INTERCEPT_SUFFIXES: Tuple[str, ...] = ("weixin.qq.com",)


def normalize_host(host: str) -> str:
    """小写、去端口、去结尾点（``CHANNELS.WEIXIN.QQ.COM:443.`` → 同一形式）。"""
    text = (host or "").strip().lower()
    if text.endswith("."):
        text = text[:-1]
    # IPv6 字面量带端口形如 [::1]:443；域名端口是最后一个 ":" 之后。
    if text.startswith("["):
        return text
    if text.count(":") == 1:
        text = text.rsplit(":", 1)[0]
    if text.endswith("."):
        text = text[:-1]
    return text


def should_intercept_host(
    host: str,
    suffixes: Sequence[str] = DEFAULT_INTERCEPT_SUFFIXES,
) -> bool:
    """host 是否属于白名单域（精确等于后缀或为其子域）。

    ``evil-weixin.qq.com``、``weixin.qq.com.evil.com`` 这类仿冒域名因缺少
    「.」边界而返回 False。
    """
    normalized = normalize_host(host)
    if not normalized:
        return False
    for suffix in suffixes:
        clean = normalize_host(suffix)
        if not clean:
            continue
        if normalized == clean or normalized.endswith("." + clean):
            return True
    return False


def build_allow_hosts_patterns(
    suffixes: Sequence[str] = DEFAULT_INTERCEPT_SUFFIXES,
) -> List[str]:
    """生成 mitmproxy ``allow_hosts`` 正则（仅白名单域会被解密）。

    mitmproxy 以 ``re.search(pattern, host, IGNORECASE)`` 匹配「主机名:端口」
    字符串（addons/next_layer.py），所以模式必须 ``^`` 锚定且 ``.`` 转义，
    防止 ``evil-weixin.qq.com`` 这类子串误命中。
    """
    patterns: List[str] = []
    for suffix in suffixes:
        clean = normalize_host(suffix)
        if not clean:
            continue
        escaped = r"\.".join(re.escape(part) for part in clean.split("."))
        patterns.append(rf"^(?:[A-Za-z0-9-]+\.)*{escaped}(?::\d+)?$")
    return patterns


def merge_suffixes(extra: Iterable[str] | None) -> Tuple[str, ...]:
    """默认白名单 + 用户扩展域（去重保序；供 config.channels.intercept_domains）。"""
    merged = list(DEFAULT_INTERCEPT_SUFFIXES)
    for item in extra or ():
        clean = normalize_host(str(item))
        if clean and clean not in merged:
            merged.append(clean)
    return tuple(merged)
