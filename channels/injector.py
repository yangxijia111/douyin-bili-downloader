"""视频号页面的 HTML bootstrap 注入（v2.0.2 主要捕获方案的载体）。

拦截 ``https://channels.weixin.qq.com/web/pages/{home,feed,live,profile}``
的 HTML 响应，向 ``<head>`` 注入本项目自己的 bootstrap::

    <link  rel="stylesheet" href="/__cuin/assets/channels.css">
    <script src="/__cuin/assets/cuin_core.js"></script>
    <script src="/__cuin/assets/bootstrap.js"></script>

三个资源都由 mitmproxy **本地虚拟响应**（:mod:`channels.virtual_host`），
不访问腾讯服务器：同源、无 CORS、无 mixed-content、无需开放公网端口。

正确性约束（每一条都有离线测试覆盖）：

* **幂等**：响应里已含注入标记时跳过，刷新/重定向不重复注入；
* **压缩透明**：gzip / br / deflate 响应先解码再改写，mitmproxy 按原
  content-encoding 重新编码并修正 content-length；无法解码时整体
  passthrough（绝不把压缩字节当文本改坏）；
* **CSP**：同时处理响应头 ``content-security-policy`` 与 HTML 内
  ``<meta http-equiv>`` 两种形式——为 script-src / style-src / default-src /
  connect-src / img-src / media-src / font-src 补 ``'self'``（注入资源与
  ``/__cuin`` 桥接都依赖它）；原策略带 nonce 时把 nonce 复制到注入的
  script 标签，不引入 ``'unsafe-inline'``；
* **不影响原页面**：任何异常（结构不符 / 解码失败 / 改写出错）都记录
  诊断并原样放行，注入失败永远降级为「不注入」而不是「坏页面」。

Strategy D（res.wx.qq.com JS bundle 补丁）的挂点也在本模块：
``js_bundles_seen`` 计数 + :class:`channels.patches.PatchRegistry`
处理，补丁未登记时什么都不做（默认状态）。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Pattern

from channels.diagnostics import ChannelsDiagnostics
from channels.domains import should_intercept_host
from channels.patches import PatchRegistry

__all__ = [
    "InjectorAddon",
    "CHANNELS_PAGE_PATH_RE",
    "VIRTUAL_CSS_PATH",
    "VIRTUAL_JS_PATH",
    "INJECT_MARKER",
    "relax_csp",
    "inject_bootstrap",
]

# 视频号页面路径（查询串忽略）；至少覆盖 home / feed / live / profile。
CHANNELS_PAGE_PATH_RE: Pattern[str] = re.compile(
    r"^/web/pages/(?:home|feed|live|profile)(?:[/?.]|$)", re.IGNORECASE
)

# 虚拟资源路径（与 virtual_host.VIRTUAL_PREFIX 保持一致，由本机代理提供）。
VIRTUAL_CSS_PATH = "/__cuin/assets/channels.css"
VIRTUAL_JS_PATH = "/__cuin/assets/bootstrap.js"
VIRTUAL_CORE_JS_PATH = "/__cuin/assets/cuin_core.js"

# 幂等标记：bootstrap 运行时会写 window.__CUIN__，HTML 里以 data 属性标记。
INJECT_MARKER = "data-cuin-injected"

# 需要保证 'self' 的 CSP 指令（注入 script/link 与 /__cuin fetch 都依赖）。
_CSP_SOURCE_DIRECTIVES = frozenset(
    {
        "script-src",
        "style-src",
        "default-src",
        "connect-src",
        "img-src",
        "media-src",
        "font-src",
    }
)

_META_CSP_RE = re.compile(
    r"(<meta[^>]+http-equiv=[\"']?content-security-policy[\"']?[^>]*>)",
    re.IGNORECASE,
)
_META_CONTENT_RE = re.compile(r"content=([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_NONCE_RE = re.compile(r"<script[^>]+nonce=[\"']([^\"']+)[\"']", re.IGNORECASE)
_HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)


def relax_csp(policy: str) -> str:
    """为注入所需指令补 ``'self'``（最小放宽，不动其它指令）。

    非目标指令（frame-ancestors / report-uri 等）原样保留；已含
    ``'self'`` 或 ``*`` 的指令不动。解析失败时原样返回。
    """
    if not policy:
        return policy
    try:
        directives = []
        for raw in policy.split(";"):
            part = raw.strip()
            if not part:
                continue
            name, _, rest = part.partition(" ")
            tokens = rest.split()
            if name.lower() in _CSP_SOURCE_DIRECTIVES:
                if "'self'" not in tokens and "*" not in tokens:
                    tokens.append("'self'")
            directives.append(" ".join([name] + tokens))
        return "; ".join(directives)
    except Exception:  # noqa: BLE001 —— 解析异常不拦注入
        return policy


def _relax_meta_csp(html: str) -> str:
    """改写 HTML 内 meta CSP 的 content（没有则原样返回）。"""

    def _rewrite_tag(match: "re.Match[str]") -> str:
        tag = match.group(1)
        content_match = _META_CONTENT_RE.search(tag)
        if not content_match:
            return tag
        policy = content_match.group(2)
        relaxed = relax_csp(policy)
        if relaxed == policy:
            return tag
        start, end = content_match.span(2)
        return tag[:start] + relaxed + tag[end:]

    return _META_CSP_RE.sub(_rewrite_tag, html)


def inject_bootstrap(html: str, *, js_path: str = VIRTUAL_JS_PATH,
                     css_path: str = VIRTUAL_CSS_PATH,
                     core_js_path: str = VIRTUAL_CORE_JS_PATH) -> str:
    """把 bootstrap 标签插入 ``<head>``（幂等由调用方预检 marker）。

    注入两个经典脚本（按顺序执行，保证 hook 早于页面脚本）::

        cuin_core.js   纯逻辑核心（objectDesc 收集 / hook 工厂 / 匹配）
        bootstrap.js   浏览器胶水（fetch/XHR hook / 心跳 / 按钮 / 模块加载）

    返回改写后的 HTML；找不到 ``<head>`` 时插入到文档最前面（浏览器对
    错位 html 依然渲染，注入不成立时调用方已计数失败）。
    """
    if INJECT_MARKER in html:
        return html
    nonce_match = _NONCE_RE.search(html)
    nonce_attr = f' nonce="{nonce_match.group(1)}"' if nonce_match else ""
    snippet = (
        f'<link rel="stylesheet" href="{css_path}" {INJECT_MARKER}="1">'
        f'<script src="{core_js_path}" {INJECT_MARKER}="1"{nonce_attr}></script>'
        f'<script src="{js_path}" {INJECT_MARKER}="1"{nonce_attr}></script>'
    )
    head_match = _HEAD_RE.search(html)
    if head_match:
        pos = head_match.end()
        return html[:pos] + snippet + html[pos:]
    return snippet + html


class InjectorAddon:
    """mitmproxy addon：连接计数 + HTML 注入 + JS bundle 补丁挂点。"""

    def __init__(
        self,
        diagnostics: Optional[ChannelsDiagnostics] = None,
        *,
        allowed_suffixes: Iterable[str] = ("weixin.qq.com",),
        inject_enabled: bool = True,
        patch_registry: Optional[PatchRegistry] = None,
    ) -> None:
        self.diagnostics = diagnostics or ChannelsDiagnostics()
        self.allowed_suffixes = tuple(allowed_suffixes) or ("weixin.qq.com",)
        self.inject_enabled = inject_enabled
        self.patch_registry = patch_registry

    # ------------------------------------------------------------------
    # 连接级计数（诊断阶梯第 1–3 级）
    # ------------------------------------------------------------------

    def client_connected(self, data) -> None:
        """任何客户端 TCP 连接（微信走了代理就会触发）。"""
        self.diagnostics.incr("proxy_connections")

    def server_connect(self, data) -> None:
        """上游连接：白名单域命中计数；443 端口即被 TLS 中间人。"""
        try:
            host, port = data.server.address
        except Exception:  # noqa: BLE001 —— 地址信息异常不影响代理
            return
        if should_intercept_host(str(host), self.allowed_suffixes):
            self.diagnostics.incr("target_domain_connections")
            if int(port) == 443:
                self.diagnostics.incr("tls_intercepted")

    # ------------------------------------------------------------------
    # 响应处理
    # ------------------------------------------------------------------

    def response(self, flow) -> None:
        try:
            self._handle(flow)
        except Exception as exc:  # noqa: BLE001 —— 注入失败绝不破坏页面
            self.diagnostics.incr("injected_errors")
            try:
                from utils.logger import setup_logger

                setup_logger("ChannelsInjector").warning(
                    "页面注入失败 %s: %s",
                    getattr(flow.request, "pretty_host", "?"),
                    exc,
                )
            except Exception:  # noqa: BLE001 —— 日志本身失败也只能忽略
                pass

    def _handle(self, flow) -> None:
        request = flow.request
        host = request.pretty_host or ""
        if not should_intercept_host(host, self.allowed_suffixes):
            return
        response = flow.response
        if response is None:
            return
        path = request.path or ""
        from urllib.parse import urlsplit

        url_path = urlsplit(path).path

        # ① JS bundle（Strategy D 挂点，res.wx.qq.com 场景）。
        content_type = (response.headers.get("content-type") or "").lower()
        if "javascript" in content_type or url_path.endswith(".js"):
            self._maybe_patch_js(host, url_path, response)
            return

        # ② 视频号页面 HTML 注入。
        if not self.inject_enabled:
            return
        if "html" not in content_type:
            return
        if not CHANNELS_PAGE_PATH_RE.match(url_path):
            return
        self.diagnostics.incr("html_pages_seen")
        self._inject_html(response)

    # ------------------------------------------------------------------
    # HTML 注入
    # ------------------------------------------------------------------

    def _inject_html(self, response) -> None:
        try:
            html = response.get_text(strict=True)
        except ValueError:
            # 压缩/编码无法解码（br 缺库等）：passthrough，绝不在压缩字节上动手。
            self.diagnostics.incr("injected_errors")
            self.diagnostics.record_parse_error("HTML 响应编码无法解码，已跳过注入")
            return
        if not html:
            return
        if INJECT_MARKER in html:
            # 已注入过（重复响应 / 代理缓存命中）：幂等跳过，不重复计数。
            return
        rewritten = _relax_meta_csp(html)
        rewritten = inject_bootstrap(rewritten)
        if rewritten == html:
            # 注入未产生变化（页面结构异常）：按注入失败计，原样放行。
            self.diagnostics.incr("injected_errors")
            self.diagnostics.record_parse_error("bootstrap 注入未改变页面内容")
            return
        response.text = rewritten
        header = response.headers.get("content-security-policy")
        if header:
            response.headers["content-security-policy"] = relax_csp(header)
        response.headers.pop("content-security-policy-report-only", None)
        self.diagnostics.incr("injected_pages")

    # ------------------------------------------------------------------
    # JS bundle 补丁（Strategy D）
    # ------------------------------------------------------------------

    def _maybe_patch_js(self, host: str, url_path: str, response) -> None:
        self.diagnostics.incr("js_bundles_seen")
        registry = self.patch_registry
        if registry is None or registry.empty:
            return
        try:
            body = response.get_text(strict=True)
        except ValueError:
            return  # 解码失败：passthrough
        if not body:
            return
        rewritten, outcome = registry.process(f"{host}{url_path}", body)
        if outcome.patched:
            response.text = rewritten
