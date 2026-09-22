"""Playwright 页面签名桥。

抖音 2026 年起对 ``/aweme/v1/web/`` 系列端点逐步收紧 ArgusSecurityPlugin
门禁：请求必须携带由页面 secsdk 生成的 ``uifid`` / ``timestamp`` /
``x-secsdk-web-signature`` / ``a_bogus`` 等参数，纯 Python 侧无法稳定复现
（签名为 SDK 内部 MD5 派生，输入含未公开的运行时状态）。

本桥维持一个 headless Chromium 页面，在页面上下文里用 XMLHttpRequest
发请求，让抖音自己的 SDK hook 自动补齐全部签名参数——与桌面版
``page_bridge``（Electron 隐藏登录窗口）同一思路，供 CLI/Server 在
aiohttp 直连被 Argus 拒绝（HTTP 403，body 含 ``ArgusSecurityPlugin``）时
自动回退使用。

桥是懒启动的：第一次 :meth:`fetch` 才拉起浏览器，正常链路（未被门禁的
端点、B 站、yt-dlp 平台）零开销；会话结束调用 :meth:`aclose` 释放。

关于代理：桥默认**直连**抖音，不继承下载器的 ``proxy`` 配置。实测本地
代理（Clash 类）下 Chromium 整页加载会卡死或 SDK 拿不到 uifid，而桥只发
几 KB 的 JSON 签名请求、不碰媒体 CDN，直连是国内网络的最佳路径。确需
代理（海外环境）时由调用方显式传 ``proxy``。
"""

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from utils.logger import setup_logger

logger = setup_logger("PlaywrightPageBridge")

# 页面内 XHR 执行器。SDK 会 hook XMLHttpRequest.open/send 并在请求发出前
# 把 uifid / timestamp / x-secsdk-web-signature / a_bogus 追加到 query。
# 用 XHR 而不是 fetch：fetch 的 hook 在部分页面上会挂起，XHR 路径与抖音
# 自家业务代码（axios）一致，实测稳定。
_XHR_EVALUATE = """
({url, method, body, timeoutMs}) => new Promise((resolve) => {
  const xhr = new XMLHttpRequest();
  xhr.open(method, url, true);
  xhr.timeout = timeoutMs;
  if (body) {
    xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded");
  }
  xhr.ontimeout = () => resolve({status: 0, text: "", err: "timeout"});
  xhr.onerror = () => resolve({status: 0, text: "", err: "network error"});
  xhr.onload = () => resolve({status: xhr.status, text: xhr.responseText});
  xhr.send(body || null);
})
"""


def _is_argus_block(text: str) -> bool:
    return "ArgusSecurityPlugin" in (text or "")


# 这些参数由页面 SDK 在 XHR 发出前自行追加。若调用方已在 query 里放了同名键
# （典型：aiohttp 路径的 ``uifid=""`` 占位），SDK 会视为"已存在"而跳过，
# 服务端便收到空值并回 "Uifid Not Found"。桥统一剥掉，交给 SDK 生成。
_SDK_MANAGED_PARAMS = frozenset(
    {
        "uifid",
        "msToken",
        "a_bogus",
        "X-Bogus",
        "timestamp",
        "x-secsdk-web-signature",
        "webid",
        "verifyFp",
        "fp",
    }
)


def _strip_sdk_params(params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in params.items()
        if key not in _SDK_MANAGED_PARAMS and value is not None and str(value) != ""
    }


class BridgeFetchResult:
    """与桌面版 page_bridge 结果鸭子类型兼容的返回对象。"""

    def __init__(self, http_status: int, text: str):
        self.http_status = int(http_status)
        self.text = text or ""
        try:
            self.body = json.loads(self.text) if self.text else None
        except ValueError:
            self.body = None


class PlaywrightPageBridge:
    """懒启动的 Chromium 页面签名桥（详见模块 docstring）。"""

    def __init__(
        self,
        cookie_provider: Callable[[], List[Dict[str, str]]],
        *,
        user_agent: str,
        proxy: Optional[str] = None,
        headless: bool = True,
        request_timeout_seconds: float = 20.0,
    ):
        self._cookie_provider = cookie_provider
        self._user_agent = user_agent
        self._proxy = str(proxy or "").strip()
        self._headless = headless
        self._request_timeout = request_timeout_seconds
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def fetch(
        self,
        path: str,
        params: Dict[str, Any],
        *,
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        request_headers: Optional[Dict[str, str]] = None,
    ) -> BridgeFetchResult:
        """在页面上下文里发一次请求，返回带签名的响应。

        两类失败各重试一次：
        - 页面不健康（evaluate 超时 / Target crashed / XHR 网络错误）：回收
          浏览器重建后再发，避免一次崩溃拖垮整个会话；
        - Argus 仍拒绝：SDK 可能尚未预热完成，等 2s 再发。
        """
        url = f"{path}?{urlencode(_strip_sdk_params(params))}"
        body = urlencode(data or {}) if (method.upper() == "POST" and data) else None
        # request_headers 仅为与桌面桥签名对齐而接收：浏览器禁止 XHR 改写
        # Referer/Origin，Content-Type 已在页面脚本内按 POST 自动设置。
        payload = {
            "url": url,
            "method": method.upper(),
            "body": body,
            "timeoutMs": int(self._request_timeout * 1000),
        }

        started = asyncio.get_running_loop().time()
        outcome: Optional[BridgeFetchResult] = None
        for attempt in range(2):
            page = await self._ensure_page()
            try:
                raw = await asyncio.wait_for(
                    page.evaluate(_XHR_EVALUATE, payload),
                    timeout=self._request_timeout + 10,
                )
            except Exception as exc:  # noqa: BLE001 - 含 asyncio.TimeoutError / 页面崩溃
                raw = {"status": 0, "text": "", "err": f"{type(exc).__name__}: {exc}"}
            if not raw.get("status"):
                logger.warning(
                    "Page bridge request unhealthy (attempt %d/2): path=%s err=%s; recycling browser",
                    attempt + 1,
                    path,
                    str(raw.get("err") or "-")[:160],
                )
                await self._recycle()
                if attempt == 0:
                    continue
                raise RuntimeError(f"page bridge request failed: {raw.get('err')}")

            outcome = BridgeFetchResult(raw.get("status") or 0, raw.get("text") or "")
            if outcome.http_status != 403 or not _is_argus_block(outcome.text):
                return outcome
            if attempt == 0:
                logger.warning(
                    "Page bridge hit Argus gate on first try (SDK warming up?), retrying once: %s",
                    path,
                )
                await asyncio.sleep(2)

        elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        logger.warning(
            "Page bridge still blocked by Argus after retry: path=%s duration_ms=%d", path, elapsed_ms
        )
        assert outcome is not None
        return outcome

    async def aclose(self) -> None:
        async with self._lock:
            await self._teardown()

    async def _recycle(self) -> None:
        """页面失去响应或崩溃时回收浏览器；下一次 fetch 会重新拉起。"""
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        """释放浏览器资源；须在持有 ``_lock`` 时调用。

        崩溃后的 Target 上 close() 可能永不返回，每步都限时，宁可泄漏一个
        僵尸进程也不能把整条下载链路挂死。
        """
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await asyncio.wait_for(closer.close(), timeout=10)
                except Exception as exc:  # noqa: BLE001 - 关闭路径尽力而为
                    logger.debug("Bridge close step failed: %s", exc)
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=10)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Playwright stop failed: %s", exc)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def _ensure_page(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            try:
                await self._launch()
            except BaseException:
                # 半初始化的浏览器必须回收，否则下次 fetch 会泄漏一个进程。
                await self._teardown()
                raise
            return self._page

    async def _launch(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError(
                "playwright 未安装，页面签名桥不可用（pip install playwright && playwright install chromium）"
            ) from exc

        started = asyncio.get_running_loop().time()
        self._playwright = await async_playwright().start()
        launch_options: Dict[str, Any] = {
            "headless": self._headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        }
        proxy = self._proxy
        if proxy:
            if proxy.startswith("socks5h://"):
                proxy = "socks5://" + proxy[len("socks5h://") :]
            launch_options["proxy"] = {"server": proxy}
        self._browser = await self._playwright.chromium.launch(**launch_options)
        self._context = await self._browser.new_context(
            user_agent=self._user_agent,
            locale="zh-CN",
            viewport={"width": 1600, "height": 900},
        )
        # 首页推荐流会自动拉取并播放多路视频，headless 下几分钟内就能把页面
        # 主线程卡死直到 Target crashed。桥只需要 SDK 脚本，媒体流一律掐掉；
        # 图片 / 字体保留——SDK 的 beacon 上报走 <img>，拦掉可能影响初始化。
        await self._context.route("**/*", self._drop_media_route)
        cookies = self._cookie_provider()
        if cookies:
            await self._context.add_cookies(cookies)
        self._page = await self._context.new_page()
        try:
            await self._page.goto(
                "https://www.douyin.com/?recommend=1",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as exc:  # noqa: BLE001 - goto 超时也常能完成 SDK 初始化
            logger.warning("Bridge page goto failed, continue with current state: %s", exc)
        # 等 React 应用挂完、secsdk 就绪。readyState complete + 固定宽限即可；
        # 万一仍早于 SDK，fetch 内部的 Argus 重试会兜住。
        try:
            await self._page.wait_for_function(
                "() => document.readyState === 'complete'", timeout=15000
            )
        except Exception:  # noqa: BLE001
            pass
        await self._page.wait_for_timeout(2000)
        logger.info(
            "Page bridge ready: duration_ms=%d headless=%s proxy_enabled=%s cookie_count=%d",
            int((asyncio.get_running_loop().time() - started) * 1000),
            self._headless,
            bool(self._proxy),
            len(cookies),
        )

    @staticmethod
    async def _drop_media_route(route) -> None:
        if route.request.resource_type == "media":
            await route.abort()
        else:
            await route.continue_()
