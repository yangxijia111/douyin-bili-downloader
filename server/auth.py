"""REST API 访问认证（远程安全边界）。

默认服务只监听 ``127.0.0.1``，行为与 2.0.0 完全一致：本机请求全部放行。
当用户把服务绑定到非环回地址（``0.0.0.0`` / 局域网 IP / ``::``）以便在
手机或别的电脑上访问时，本模块提供边界控制：

* **环回客户端始终放行**（localhost 判断优先于一切）；
* **非环回客户端必须携带有效 token**——``X-Auth-Token: <token>`` 或
  ``Authorization: Bearer <token>``；
* **未配置 token 时**，非环回客户端对全部 ``/api/*`` 一律 403 拒绝——
  宁可功能不可用，也不裸奔暴露「安装证书 / 启动代理 / 改配置 / 触发下载」
  这类高权限接口；
* token 来源（二选一）：``config.yml`` 的 ``server.auth_token`` 或环境变量
  ``DOWNLOADER_API_TOKEN``（环境变量优先级更高，便于部署时注入）；
* token 比较使用 ``hmac.compare_digest``，避免时序侧信道。

健康检查 ``/api/v1/health`` 与网页控制台 HTML 保持公开（不含敏感数据，
且远程访问 Web UI 必须先能加载页面才能输入 token）。
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "AUTH_HEADER",
    "ENV_TOKEN_VAR",
    "AuthPolicy",
    "resolve_auth_token",
    "is_loopback_host",
]

AUTH_HEADER = "X-Auth-Token"
ENV_TOKEN_VAR = "DOWNLOADER_API_TOKEN"

# 视为本机、免认证的客户端地址。``testclient`` 是 FastAPI/Starlette 测试
# 客户端的固定 peer 名，真实 HTTP 服务器（uvicorn 等）不可能产生该值。
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient", ""})


def is_loopback_host(host: Optional[str]) -> bool:
    """客户端地址是否来自本机。"""
    if not host:
        return True  # ASGI 未提供 peer 时按本机处理（单进程内调用场景）
    text = str(host).strip().lower()
    if text in _LOCAL_HOSTS:
        return True
    if text.startswith("::ffff:"):  # IPv6 映射的 IPv4
        text = text[len("::ffff:"):]
    return _is_ipv4_loopback(text)


def _is_ipv4_loopback(text: str) -> bool:
    """严格 IPv4 解析：``127.0.0.1.evil.com`` 这类仿冒串不算环回。"""
    parts = text.split(".")
    if len(parts) != 4:
        return False
    if parts[0] != "127":
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts[1:])


def resolve_auth_token(config: Any = None) -> str:
    """解析 API token：环境变量 > config.server.auth_token。"""
    env_value = os.environ.get(ENV_TOKEN_VAR, "").strip()
    if env_value:
        return env_value
    if config is not None:
        try:
            section = config.get("server")
        except Exception:  # noqa: BLE001 - 配置异常时按未配置处理
            section = None
        if isinstance(section, dict):
            return str(section.get("auth_token") or "").strip()
    return ""


def extract_presented_token(headers: Any) -> Optional[str]:
    """从请求头提取 token：X-Auth-Token 优先，其次 Bearer。"""
    value = headers.get(AUTH_HEADER)
    if value:
        return str(value).strip()
    authorization = headers.get("Authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


class AuthPolicy:
    """按「客户端地址 + 请求头 token」做访问决策。"""

    def check(self, client_host: Optional[str], presented: Optional[str]) -> Tuple[bool, int, str]:
        """返回 (是否放行, 拒绝状态码, 拒绝原因)。

        * 环回客户端 → 放行；
        * 非环回 + 已配置 token → 常数时间比对，通过放行，否则 401；
        * 非环回 + 未配置 token → 403（服务端从未开启远程授权）。
        """
        if is_loopback_host(client_host):
            return True, 200, ""
        token = self._token
        if not token:
            return (
                False,
                403,
                "非本机访问被拒绝：请先在服务端配置 server.auth_token 或环境变量 "
                f"{ENV_TOKEN_VAR}，再携带 X-Auth-Token 访问。",
            )
        if presented and hmac.compare_digest(str(presented).strip(), token):
            return True, 200, ""
        return False, 401, "认证失败：缺少或错误的 X-Auth-Token。"

    __slots__ = ("_token",)

    def __init__(self, token: str = ""):
        self._token = str(token or "").strip()

    @property
    def token_configured(self) -> bool:
        return bool(self._token)

    def describe(self) -> Dict[str, Any]:
        """供 /api/v1/health 等公开端点展示的非敏感策略摘要。"""
        return {
            "token_configured": self.token_configured,
            "header": AUTH_HEADER,
            "env_var": ENV_TOKEN_VAR,
        }
