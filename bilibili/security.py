"""B 站链路的出站 URL 安全校验。

下载器要请求的 URL 有三类来源，风险各不相同：

* 代码内写死的固定 API 域名 —— 不需要校验；
* 用户输入（``b23.tv`` 短链及其 302 落点）—— host 看似已知，但落点是服务端
  控制的跳转目标；
* 接口响应给出的媒体 / 封面 / 字幕地址 —— 正常是 hdslb.com / bilivideo.com
  等自家 CDN，但本质上是**不可信数据**：响应被篡改或域名策略变更时可能指向
  内网地址，构成 SSRF 向量。

统一策略（出站前必须全部通过）：

1. scheme 只允许 ``http`` / ``https``；
2. host 必须存在，且拒绝 localhost、环回、私有、链路本地、保留与组播地址
   （IP 字面量直接按网段判定；域名按字面量特征与内网后缀判定）。

刻意不做 DNS 解析检查：下载链路的 host 全是公网 CDN 域名，逐条 DNS 查询的
延迟与故障面远大于收益；解析结果的时效性也让它挡不住 TOCTOU。字面量级校验
已经封死「把请求发进内网」的主要路径。
"""

from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlparse

# 出站请求仅允许这两种 scheme；file / ftp / ws 等一律拒绝。
ALLOWED_SCHEMES = ("http", "https")

# 视为内网/不可公网解析的域名后缀（大小写不敏感，含 mDNS 的 .local）。
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
)

# 直接点名的 host（不依赖后缀规则）。
_BLOCKED_HOSTS = ("localhost",)

# is_private 各 Python 版本口径不一（例如 100.64/10 在 3.12.4 被移出），黑名单
# 网段必须显式列出，不依赖版本细节。
_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT 运营商级 NAT
    ipaddress.ip_network("192.0.0.0/24"),  # IETF 协议分配
    ipaddress.ip_network("198.18.0.0/15"),  # 基准测试保留网段
)


def _is_blocked_ip(host: str) -> bool:
    """host 是 IP 字面量时按网段黑名单判定；不是 IP 则返回 None 由调用方继续。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return any(ip in network for network in _EXTRA_BLOCKED_NETWORKS)


def _is_blocked_host(host: str) -> bool:
    hostname = (host or "").strip().rstrip(".").lower()
    if not hostname:
        return True
    if hostname in _BLOCKED_HOSTS:
        return True
    if any(hostname.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
        return True
    return _is_blocked_ip(hostname)


def is_safe_url(url: str) -> bool:
    """出站 URL 安全校验：scheme 白名单 + host 黑名单。"""
    candidate = str(url or "").strip()
    if not candidate:
        return False
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    if (parsed.scheme or "").lower() not in ALLOWED_SCHEMES:
        return False
    return not _is_blocked_host(parsed.hostname or "")


def assert_safe_url(url: str) -> str:
    """校验失败的 URL 直接抛 :class:`ValueError`，供必须「请求或拒绝」的调用点使用。"""
    if not is_safe_url(url):
        raise ValueError(f"blocked unsafe outbound url: {url!r}")
    return url


def safe_target_host(url: str) -> Optional[str]:
    """返回通过校验的 URL 的 host（小写）；未通过返回 None。诊断用。"""
    if not is_safe_url(url):
        return None
    return (urlparse(str(url).strip()).hostname or "").lower() or None
