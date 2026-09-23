"""视频号嗅探域名白名单测试（P0：MITM 最小权限边界）。

白名单是嗅探会话唯一解密依据：mitmproxy 的 ``allow_hosts`` 用
:func:`channels.domains.build_allow_hosts_patterns` 生成的正则做连接级
放行，``SnifferAddon`` 用 :func:`channels.domains.should_intercept_host`
做响应级过滤。两层必须对同一组域名给出一致结论——否则要么漏抓 feed，
要么把无关网站解密（越权 MITM）。
"""

from __future__ import annotations

import json
import re

import pytest

from channels.domains import (
    DEFAULT_INTERCEPT_SUFFIXES,
    build_allow_hosts_patterns,
    should_intercept_host,
)


class TestShouldInterceptHost:
    def test_weixin_subdomains_are_intercepted(self):
        # 视频号 API 实际载体：主站 + 全部子域（finderFeed / finderPcFlow 等）。
        assert should_intercept_host("channels.weixin.qq.com")
        assert should_intercept_host("finderlib.weixin.qq.com")
        assert should_intercept_host("channels.weixin.qq.com.")

    def test_bare_suffix_domain_is_intercepted(self):
        assert should_intercept_host("weixin.qq.com")

    def test_host_case_and_port_insensitive(self):
        assert should_intercept_host("CHANNELS.WEIXIN.QQ.COM")
        assert should_intercept_host("channels.weixin.qq.com:443")

    def test_non_target_domains_are_not_intercepted(self):
        # 其它 qq.com 子域一律不解密：禁止为了「能用」扩大到全部 qq.com。
        for host in (
            "www.qq.com",
            "v.qq.com",
            "mail.qq.com",
            "qq.com",
            "finder.video.qq.com",
            "www.baidu.com",
            "github.com",
            "localhost",
            "127.0.0.1",
            "",
        ):
            assert not should_intercept_host(host), host

    def test_lookalike_domains_are_not_intercepted(self):
        # 攻击者常用连字符仿冒域名；endswith 判断必须要求「.」边界。
        assert not should_intercept_host("evil-weixin.qq.com")
        assert not should_intercept_host("weixin.qq.com.evil.com")
        assert not should_intercept_host("weixin.qq.com.attacker.io")
        assert not should_intercept_host("fakeweixin.qq.com")

    def test_extra_suffixes_extend_whitelist(self):
        extra = ("finder.video.qq.com",)
        assert should_intercept_host("finder.video.qq.com", extra)
        assert not should_intercept_host("v.qq.com", extra)
        # 扩展域不替换默认白名单（需要显式传参才会叠加）。
        combined = DEFAULT_INTERCEPT_SUFFIXES + extra
        assert should_intercept_host("channels.weixin.qq.com", combined)


class TestAllowHostsPatterns:
    """mitmproxy 用 ``re.search(pattern, "host:port")`` 决定是否放行解密；

    这里用与 mitmproxy addons/next_layer 完全相同的匹配方式验证：
    目标域名（含 SNI 形式 ``host:port``）命中，非目标域名不命中。
    """

    def _matches(self, host_with_port: str) -> bool:
        patterns = [re.compile(p, re.IGNORECASE) for p in build_allow_hosts_patterns()]
        return any(p.search(host_with_port) for p in patterns)

    def test_target_hosts_match(self):
        assert self._matches("channels.weixin.qq.com:443")
        assert self._matches("finderlib.weixin.qq.com:443")
        assert self._matches("weixin.qq.com:443")

    def test_non_target_hosts_do_not_match(self):
        for host in (
            "www.qq.com:443",
            "v.qq.com:443",
            "www.baidu.com:443",
            "evil-weixin.qq.com:443",
            "weixin.qq.com.evil.com:443",
            "140.82.121.4:443",
        ):
            assert not self._matches(host), host

    def test_patterns_are_valid_regex(self):
        import re as _re

        for pattern in build_allow_hosts_patterns():
            _re.compile(pattern)

    def test_explicit_suffixes_flow_into_patterns(self):
        patterns = build_allow_hosts_patterns(("finder.video.qq.com",))
        assert any("finder\\.video\\.qq\\.com" in p for p in patterns)


class TestInterceptorWiring:
    """拦截器与白名单的接线：连接级 allow_hosts + 响应级过滤。"""

    def _make_flow(self, host: str, body: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            request=SimpleNamespace(pretty_host=host, path="/mmfinderassist/feed"),
            response=SimpleNamespace(get_text=lambda strict: body),
        )

    def test_build_options_sets_allow_hosts(self):
        pytest.importorskip("mitmproxy")
        from channels.interceptor import ChannelsInterceptor

        opts = ChannelsInterceptor.build_options(
            host="127.0.0.1", port=8899, confdir=".", allowed_suffixes=("weixin.qq.com",)
        )
        assert opts.allow_hosts, "allow_hosts 必须配置，否则 mitmproxy 会解密全部流量"
        assert any("weixin" in p for p in opts.allow_hosts)

    def test_addon_ignores_non_whitelisted_hosts(self):
        from channels.feed_store import FeedStore
        from channels.interceptor import SnifferAddon

        store = FeedStore()
        addon = SnifferAddon(store)
        body = json.dumps({"objectDesc": {"media": [{"url": "https://x/v.mp4"}]}, "objectId": "a"})
        # 非 white 名单域：即使 body 长得很像 feed 也绝不解析。
        addon.response(self._make_flow("www.qq.com", body))
        addon.response(self._make_flow("evil-weixin.qq.com", body))
        assert len(store) == 0
        # 白名单域正常捕获。
        addon.response(self._make_flow("channels.weixin.qq.com", body))
        assert len(store) == 1

    def test_addon_honors_custom_suffixes(self):
        from channels.feed_store import FeedStore
        from channels.interceptor import SnifferAddon

        store = FeedStore()
        addon = SnifferAddon(store, allowed_suffixes=("finder.video.qq.com",))
        body = json.dumps({"objectDesc": {"media": [{"url": "https://x/v.mp4"}]}, "objectId": "a"})
        addon.response(self._make_flow("finder.video.qq.com", body))
        assert len(store) == 1
        addon.response(self._make_flow("channels.weixin.qq.com", body))
        assert len(store) == 1  # 自定义后缀替换而非叠加默认（扩展须显式 merge）
