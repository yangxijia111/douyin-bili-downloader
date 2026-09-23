"""HTML bootstrap 注入测试（离线；flow 用假对象，响应用真实 mitmproxy Response）。

覆盖：注入位置 / 幂等 / gzip 透明 / CSP 头与 meta / nonce 复制 /
非目标 passthrough / 连接计数 / JS bundle 补丁挂点。
"""

from __future__ import annotations

import gzip
from types import SimpleNamespace

import pytest

from channels.diagnostics import ChannelsDiagnostics
from channels.injector import (
    INJECT_MARKER,
    InjectorAddon,
    inject_bootstrap,
    relax_csp,
)
from channels.patches import JsBundlePatch, PatchRegistry

mitmproxy_http = pytest.importorskip("mitmproxy.http")
Response = mitmproxy_http.Response

_PAGE_HTML = (
    "<!DOCTYPE html><html><head><title>视频号</title>"
    "<meta charset=\"utf-8\"></head><body><div class=\"slides-item\">video</div>"
    "</body></html>"
)


def _flow(host: str, path: str, response) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(pretty_host=host, path=path, method="GET"),
        response=response,
    )


def _html_response(body: str = _PAGE_HTML, *, headers: dict | None = None) -> Response:
    base = {"Content-Type": "text/html; charset=utf-8"}
    base.update(headers or {})
    return Response.make(200, body.encode("utf-8"), base)


def _gzip_response(body: str = _PAGE_HTML) -> Response:
    """构造真实代理响应形态：raw_content 是压缩字节 + 头声明 gzip。"""
    compressed = gzip.compress(body.encode("utf-8"))
    response = Response.make(200, body.encode("utf-8"), {"Content-Type": "text/html"})
    response.raw_content = compressed
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    return response


class TestInjectBootstrapFunction:
    def test_injects_after_head(self):
        out = inject_bootstrap(_PAGE_HTML)
        assert INJECT_MARKER in out
        # 注入位置在 </head> 之前（<head> 开标签之后）。
        head_pos = out.index("<head>")
        marker_pos = out.index(INJECT_MARKER)
        assert head_pos < marker_pos
        assert "/__cuin/assets/cuin_core.js" in out
        assert "/__cuin/assets/bootstrap.js" in out
        assert "/__cuin/assets/channels.css" in out

    def test_idempotent(self):
        once = inject_bootstrap(_PAGE_HTML)
        twice = inject_bootstrap(once)
        assert once == twice
        assert twice.count(INJECT_MARKER) == 3  # 一次注入的三个标记

    def test_nonce_copied_from_existing_script(self):
        html = (
            "<html><head><script nonce=\"abc123\">var x=1;</script></head>"
            "<body></body></html>"
        )
        out = inject_bootstrap(html)
        assert 'nonce="abc123"' in out

    def test_no_head_falls_back_to_prepend(self):
        out = inject_bootstrap("<body>no head here</body>")
        # 无 <head> 时注入片段置于文档最前（浏览器仍会渲染）。
        assert out.startswith("<link")
        assert INJECT_MARKER in out


class TestRelaxCsp:
    def test_adds_self_to_source_directives(self):
        policy = "default-src 'none'; script-src https://channels.weixin.qq.com"
        relaxed = relax_csp(policy)
        assert "script-src https://channels.weixin.qq.com 'self'" in relaxed
        assert "default-src 'none'" in relaxed  # default-src 无 'self' 时也补

    def test_keeps_existing_self(self):
        policy = "script-src 'self'"
        assert relax_csp(policy) == "script-src 'self'"

    def test_non_source_directives_untouched(self):
        policy = "frame-ancestors 'none'; report-uri /csp"
        assert relax_csp(policy) == policy

    def test_empty_policy(self):
        assert relax_csp("") == ""


class TestInjectorAddon:
    def test_injects_channels_page(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        flow = _flow("channels.weixin.qq.com", "/web/pages/home", _html_response())
        addon.response(flow)
        assert diagnostics.get("html_pages_seen") == 1
        assert diagnostics.get("injected_pages") == 1
        assert INJECT_MARKER in flow.response.get_text()

    def test_all_covered_page_types(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        for page in ("home", "feed", "live", "profile"):
            flow = _flow(
                "channels.weixin.qq.com",
                f"/web/pages/{page}?from=x",
                _html_response(),
            )
            addon.response(flow)
        assert diagnostics.get("injected_pages") == 4

    def test_idempotent_on_repeated_response(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        flow = _flow("channels.weixin.qq.com", "/web/pages/home", _html_response())
        addon.response(flow)
        # 同一响应对象二次处理（代理重放场景）：不重复计数。
        addon.response(flow)
        assert diagnostics.get("html_pages_seen") == 2
        assert diagnostics.get("injected_pages") == 1

    def test_gzip_response_transparent(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        response = _gzip_response()
        flow = _flow("channels.weixin.qq.com", "/web/pages/home", response)
        addon.response(flow)
        # 最终响应仍是 gzip（content-encoding 保持），且解出来含注入。
        assert response.headers.get("content-encoding") == "gzip"
        text = response.get_text()
        assert INJECT_MARKER in text
        assert "视频号" in text
        # content-length 与压缩后实际字节数一致。
        assert int(response.headers["content-length"]) == len(response.raw_content)

    def test_undecodable_encoding_passthrough(self):
        """声明了 content-encoding 但内容无法解码（br 缺库等）：原样放行。"""
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        response = Response.make(200, b"", {"Content-Type": "text/html"})
        response.raw_content = b"\x00\x01\x02not-really-gzip"
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(response.raw_content))
        flow = _flow("channels.weixin.qq.com", "/web/pages/home", response)
        addon.response(flow)
        assert diagnostics.get("injected_pages") == 0
        assert diagnostics.get("injected_errors") == 1
        # 原样放行：压缩字节没有被当文本改坏。
        assert response.raw_content == b"\x00\x01\x02not-really-gzip"

    def test_csp_header_relaxed(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        response = _html_response(
            headers={
                "Content-Security-Policy": "script-src https://channels.weixin.qq.com",
            }
        )
        flow = _flow("channels.weixin.qq.com", "/web/pages/home", response)
        addon.response(flow)
        assert "'self'" in response.headers["content-security-policy"]
        assert diagnostics.get("injected_pages") == 1

    def test_csp_meta_relaxed(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        html = (
            "<html><head>"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"script-src https://channels.weixin.qq.com\">"
            "</head><body></body></html>"
        )
        flow = _flow("channels.weixin.qq.com", "/web/pages/home", _html_response(html))
        addon.response(flow)
        text = flow.response.get_text()
        assert "'self'" in text
        assert INJECT_MARKER in text

    def test_non_target_host_passthrough(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        response = _html_response()
        flow = _flow("www.douyin.com", "/web/pages/home", response)
        addon.response(flow)
        assert INJECT_MARKER not in flow.response.get_text()
        assert diagnostics.get("html_pages_seen") == 0

    def test_non_html_passthrough(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        response = Response.make(
            200, b'{"a":1}', {"Content-Type": "application/json"}
        )
        flow = _flow("channels.weixin.qq.com", "/web/pages/home", response)
        addon.response(flow)
        assert flow.response.get_text() == '{"a":1}'
        assert diagnostics.get("html_pages_seen") == 0

    def test_non_page_path_passthrough(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        response = _html_response()
        flow = _flow("channels.weixin.qq.com", "/web/other/page", response)
        addon.response(flow)
        assert INJECT_MARKER not in flow.response.get_text()
        assert diagnostics.get("html_pages_seen") == 0

    def test_inject_disabled(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics, inject_enabled=False)
        flow = _flow("channels.weixin.qq.com", "/web/pages/home", _html_response())
        addon.response(flow)
        assert INJECT_MARKER not in flow.response.get_text()
        assert diagnostics.get("injected_pages") == 0

    def test_response_hook_exception_passthrough(self):
        """response 钩子自身异常绝不能破坏页面（原样放行 + 计数）。"""
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)

        class _ExplodingResponse:
            # 注意：真实 flow 的 headers 大小写不敏感；假对象用小写键。
            headers = {"content-type": "text/html"}
            raw_content = b"<html><head></head></html>"

            def get_text(self, strict=False):
                raise ValueError("boom")

        flow = _flow("channels.weixin.qq.com", "/web/pages/home", _ExplodingResponse())
        addon.response(flow)  # 不抛
        assert diagnostics.get("injected_errors") == 1
        # 原样放行。
        assert flow.response.raw_content == b"<html><head></head></html>"

    def test_connection_counters(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)
        addon.client_connected(SimpleNamespace(client=SimpleNamespace()))
        addon.server_connect(
            SimpleNamespace(server=SimpleNamespace(address=("channels.weixin.qq.com", 443)))
        )
        addon.server_connect(
            SimpleNamespace(server=SimpleNamespace(address=("finder.video.qq.com", 443)))
        )
        assert diagnostics.get("proxy_connections") == 1
        assert diagnostics.get("target_domain_connections") == 1
        assert diagnostics.get("tls_intercepted") == 1


class _MarkerPatch(JsBundlePatch):
    def apply(self, body: str) -> str:
        return body + "\n//__cuin_patched"


# Strategy D 需要 res.wx.qq.com 在解密白名单里（config intercept_domains
# 显式扩展，见 channels/domains.py）；默认白名单只有 weixin.qq.com。
_RES_SUFFIXES = ("weixin.qq.com", "res.wx.qq.com")


class TestJsBundleHook:
    def test_js_bundle_counted_and_patched(self):
        diagnostics = ChannelsDiagnostics()
        registry = PatchRegistry(
            [_MarkerPatch(name="marker", signature=("FINDER_MARKER",))], diagnostics
        )
        addon = InjectorAddon(
            diagnostics, allowed_suffixes=_RES_SUFFIXES, patch_registry=registry
        )
        response = Response.make(
            200,
            b"var x = 'FINDER_MARKER';",
            {"Content-Type": "application/javascript"},
        )
        flow = _flow("res.wx.qq.com", "/x/js/bundle.js", response)
        addon.response(flow)
        assert diagnostics.get("js_bundles_seen") == 1
        assert diagnostics.get("patch_applied") == 1
        assert b"__cuin_patched" in response.raw_content

    def test_js_bundle_without_patch_passthrough(self):
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics, allowed_suffixes=_RES_SUFFIXES)  # 空注册表（默认状态）
        response = Response.make(
            200,
            b"var x = 'FINDER_MARKER';",
            {"Content-Type": "application/javascript"},
        )
        flow = _flow("res.wx.qq.com", "/x/js/bundle.js", response)
        addon.response(flow)
        assert diagnostics.get("js_bundles_seen") == 1
        assert b"__cuin_patched" not in response.raw_content
        assert response.raw_content == b"var x = 'FINDER_MARKER';"

    def test_res_domain_not_whitelisted_by_default(self):
        """默认白名单只有 weixin.qq.com：res.wx.qq.com 流量根本看不到
        （连接级隧道转发），这是 js_bundles_seen = 0 的诊断意义。"""
        diagnostics = ChannelsDiagnostics()
        addon = InjectorAddon(diagnostics)  # 默认后缀
        response = Response.make(
            200,
            b"var x = 'FINDER_MARKER';",
            {"Content-Type": "application/javascript"},
        )
        flow = _flow("res.wx.qq.com", "/x/js/bundle.js", response)
        addon.response(flow)
        assert diagnostics.get("js_bundles_seen") == 0
        assert response.raw_content == b"var x = 'FINDER_MARKER';"
