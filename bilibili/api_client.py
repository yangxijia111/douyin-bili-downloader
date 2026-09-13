"""哔哩哔哩 Web API 异步客户端。

与抖音的 ``core/api_client.py`` 完全独立：B 站用 ``code`` 字段表达业务错误
（HTTP 一律 200），用 ``bvid``/``cid`` 定位媒体，用 WBI 签名保护空间接口。

三个必须遵守的约束，违反任意一条都会表现为「接口随机失败」：

1. **Referer 必须带**。``www.bilibili.com`` 的 Referer 缺失时，空间/播放地址
   接口会返回 ``code=-403``（风控），CDN 直链会返回 HTTP 403。
2. **Cookie 按域名作用域下发**。SESSDATA 只发给 bilibili 域及自家 CDN，不
   随 aiohttp 的 session 级 cookie 全站广播。
3. **wbi 接口必须先取 ``nav`` 里的 img_key/sub_key**。见 :mod:`bilibili.wbi`。
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

from utils.logger import setup_logger

from .wbi import extract_keys, sign_params

logger = setup_logger("BiliAPIClient")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 需要下发登录 Cookie 的域名后缀（含自家 CDN）。
COOKIE_SCOPE_SUFFIXES = (
    "bilibili.com",
    "bilibili.tv",
    "hdslb.com",
    "bilivideo.com",
    "bilivideo.cn",
    "biliapi.net",
    "bcdn.net",
)

# 业务错误码 -> 处理方式
LOGIN_REQUIRED_CODES = frozenset({-101, -102})
RISK_CONTROL_CODES = frozenset({-352, -403, -412, -509})

# wbi key 的缓存时长。key 由 nav 下发且长期稳定，按小时级缓存即可；过期后
# 或遇到风控码时刷新一次。
WBI_KEY_TTL_SECONDS = 3600

# 空间接口附带的前端指纹参数。缺这些参数时 ``space/wbi/arc/search`` 会返回
# ``-412 请求被拦截``（即使 w_rid 正确）。取值对齐 Web 端固定常量。
_WEB_FINGERPRINT = {
    "dm_img_list": "[]",
    "dm_img_str": (
        "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ"
    ),
    "dm_cover_img_str": (
        "QU5HTEUgKEludGVsLCBJbnRlbChSKSBVSEQgR3JhcGhpY3MgKDB4MDAwMDQ2MjYpIERpcmVjdDNEMTEg"
        "dnNfNV8wIHBzXzVfMCwgRDMxMSlHb29nbGUgSW5jLiAoTlZJRElBKQ"
    ),
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}


class BiliAPIError(RuntimeError):
    """B 站接口返回了非 0 的 ``code``。"""

    def __init__(self, code: int, message: str, path: str = ""):
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"bilibili api error code={code} at {path}: {message}")


class BiliLoginRequiredError(BiliAPIError):
    """需要登录态才能访问（``-101`` 账号未登录 / ``-102`` 账号封停）。"""

    def __init__(self, code: int, message: str, path: str = ""):
        super().__init__(code, message, path)


class BiliRiskControlError(BiliAPIError):
    """被风控拦截（``-352`` / ``-403`` / ``-412`` / ``-509``）。

    与「未登录」严格区分：重试或换签名能救，补 Cookie 救不了。
    """


class BiliAPIClient:
    BASE_URL = "https://www.bilibili.com"
    API_URL = "https://api.bilibili.com"
    SPACE_URL = "https://space.bilibili.com"

    def __init__(
        self,
        cookies: Optional[Dict[str, str]] = None,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_interval: float = 0.5,
    ):
        self.cookies: Dict[str, str] = {
            str(key): str(value)
            for key, value in (cookies or {}).items()
            if key and value not in (None, "")
        }
        self.proxy = str(proxy or "").strip()
        self.headers = {
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
        }
        self._session: Optional[aiohttp.ClientSession] = None
        self._wbi_img_key = ""
        self._wbi_sub_key = ""
        self._wbi_fetched_at = 0.0
        self._wbi_lock: Optional[asyncio.Lock] = None
        self._request_interval = max(float(request_interval or 0), 0.0)
        self._last_request_at = 0.0
        self._request_lock: Optional[asyncio.Lock] = None
        # 登录态快照，由 nav 接口填充（-1 表示尚未探测）。
        self.is_login: Optional[bool] = None
        self.login_uname = ""

    # ------------------------------------------------------------------
    # 会话与请求
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=20),
                raise_for_status=False,
            )

    async def get_session(self) -> aiohttp.ClientSession:
        await self._ensure_session()
        if self._session is None:  # pragma: no cover — 防御
            raise RuntimeError("Failed to create aiohttp session")
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "BiliAPIClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def cookie_header_for(self, url: str) -> str:
        """按目标域名决定是否附带登录 Cookie。

        SESSDATA 是账号级凭据，只应发给 B 站自己的域名与 CDN；把 session 级
        cookie 播给全部主机（aiohttp 默认行为）会把凭据发给每一个第三方图片
        或统计域名。
        """
        host = (urlparse(url).hostname or "").lower()
        if not any(host == suffix or host.endswith("." + suffix) for suffix in COOKIE_SCOPE_SUFFIXES):
            return ""
        return "; ".join(f"{key}={value}" for key, value in self.cookies.items())

    def download_headers(self, url: str, referer: Optional[str] = None) -> Dict[str, str]:
        """媒体下载请求头。CDN 直链缺 Referer 会 403，缺 Cookie 会拿不到高码率。"""
        headers = {
            "User-Agent": self.headers["User-Agent"],
            "Referer": referer or f"{self.BASE_URL}/",
            "Origin": self.BASE_URL,
            "Accept": "*/*",
        }
        cookie = self.cookie_header_for(url)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _throttle(self) -> None:
        """接口级最小请求间隔，降低触发风控的概率。"""
        if self._request_interval <= 0:
            return
        if self._request_lock is None:
            self._request_lock = asyncio.Lock()
        async with self._request_lock:
            now = asyncio.get_running_loop().time()
            wait = self._request_interval - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = asyncio.get_running_loop().time()

    async def _get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        base: Optional[str] = None,
        signed: bool = False,
        referer: Optional[str] = None,
    ) -> Any:
        """请求一个 JSON 接口并返回 ``data`` 字段。

        非 0 ``code`` 一律抛 :class:`BiliAPIError`（或其子类）。接口返回的
        ``data`` 可能是 ``None``（例如空列表场景），调用方需自行兜底。
        """
        await self._ensure_session()
        await self._throttle()

        url = f"{base or self.API_URL}{path}"
        query: Dict[str, Any] = dict(params or {})
        if signed:
            img_key, sub_key = await self.ensure_wbi_keys()
            query = sign_params(query, img_key, sub_key)

        headers = {**self.headers, "Referer": referer or f"{self.BASE_URL}/"}
        cookie = self.cookie_header_for(url)
        if cookie:
            headers["Cookie"] = cookie

        try:
            async with self._session.get(
                url,
                params=query,
                headers=headers,
                proxy=self.proxy or None,
            ) as response:
                if response.status != 200:
                    raise BiliAPIError(response.status, f"HTTP {response.status}", path)
                payload = await response.json(content_type=None)
        except BiliAPIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BiliAPIError(-1, f"network error: {exc}", path) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise BiliAPIError(-2, f"invalid json response: {exc}", path) from exc

        if not isinstance(payload, dict):
            raise BiliAPIError(-2, "unexpected payload shape", path)

        code = payload.get("code")
        if code != 0:
            message = str(payload.get("message") or payload.get("msg") or "")
            if code in LOGIN_REQUIRED_CODES:
                raise BiliLoginRequiredError(int(code), message, path)
            if code in RISK_CONTROL_CODES:
                raise BiliRiskControlError(int(code), message, path)
            raise BiliAPIError(int(code if code is not None else -3), message, path)

        return payload.get("data")

    # ------------------------------------------------------------------
    # 登录态与 WBI 密钥
    # ------------------------------------------------------------------

    async def get_nav(self) -> Dict[str, Any]:
        """``/x/web-interface/nav``：登录态 + wbi 密钥来源。"""
        data = await self._get_json("/x/web-interface/nav")
        return data if isinstance(data, dict) else {}

    async def ensure_wbi_keys(self) -> tuple:
        """返回 ``(img_key, sub_key)``，必要时拉取并缓存。"""
        now = asyncio.get_running_loop().time()
        if self._wbi_img_key and self._wbi_sub_key and (
            now - self._wbi_fetched_at < WBI_KEY_TTL_SECONDS
        ):
            return self._wbi_img_key, self._wbi_sub_key

        if self._wbi_lock is None:
            self._wbi_lock = asyncio.Lock()
        async with self._wbi_lock:
            now = asyncio.get_running_loop().time()
            if self._wbi_img_key and self._wbi_sub_key and (
                now - self._wbi_fetched_at < WBI_KEY_TTL_SECONDS
            ):
                return self._wbi_img_key, self._wbi_sub_key

            nav = await self.get_nav()
            self._record_login_state(nav)
            await self._ensure_buvid3()
            wbi_img = nav.get("wbi_img") if isinstance(nav.get("wbi_img"), dict) else {}
            img_key, sub_key = extract_keys(
                str(wbi_img.get("img_url") or ""),
                str(wbi_img.get("sub_url") or ""),
            )
            if not img_key or not sub_key:
                # 未登录时 nav 也可能不下发 wbi_img（实测极少），此时签名无法
                # 生成——直接用占位 key 让请求带上结构完整的参数，风控接口会
                # 明确报错，比静默降级成未签名请求更容易排查。
                logger.warning(
                    "nav did not return wbi_img keys; wbi-signed requests will fail. "
                    "is_login=%s", self.is_login,
                )
            self._wbi_img_key = img_key
            self._wbi_sub_key = sub_key
            self._wbi_fetched_at = now
            return img_key, sub_key

    def _record_login_state(self, nav: Dict[str, Any]) -> None:
        self.is_login = bool(nav.get("isLogin"))
        self.login_uname = str(nav.get("uname") or "")
        if self.is_login:
            logger.info("Bilibili login detected: %s", self.login_uname)
        else:
            logger.info(
                "Bilibili running without login; high quality (1080P+/4K/8K) "
                "and favourites are unavailable"
            )

    async def _ensure_buvid3(self) -> None:
        """补齐 ``buvid3``。

        空间投稿等接口在缺 ``buvid3`` 时更容易被风控；Web 端首次访问会从
        ``/x/frontend/finger/spi`` 领一个，这里照做并把结果并入 Cookie。
        """
        if self.cookies.get("buvid3"):
            return
        try:
            data = await self._get_json("/x/frontend/finger/spi")
        except BiliAPIError as exc:
            logger.debug("Failed to fetch buvid3 from spi: %s", exc)
            data = None
        buvid3 = ""
        if isinstance(data, dict):
            buvid3 = str(data.get("b_3") or "").strip()
        if not buvid3:
            buvid3 = f"{uuid.uuid4()}{random.randint(0, 9)}infoc"
            logger.debug("Generated synthetic buvid3")
        self.cookies["buvid3"] = buvid3

    # ------------------------------------------------------------------
    # 视频
    # ------------------------------------------------------------------

    async def get_video_detail(
        self, *, bvid: Optional[str] = None, aid: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """``/x/web-interface/view``：稿件详情（含分P ``pages``）。"""
        params: Dict[str, Any] = {}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = int(aid)
        else:
            return None
        data = await self._get_json("/x/web-interface/view", params)
        return data if isinstance(data, dict) else None

    async def get_playurl(
        self,
        bvid: str,
        cid: int,
        *,
        qn: int = 127,
        fnval: int = 4048,
        fourk: bool = True,
    ) -> Dict[str, Any]:
        """``/x/player/playurl``：播放地址。

        ``fnval=4048`` 请求 DASH（含 4K/HDR/杜比/8K/AV1）。``fnval=1`` 请求
        ``durl`` 单文件流，作为 DASH 缺失时的兜底。
        """
        params = {
            "bvid": bvid,
            "cid": int(cid),
            "qn": int(qn),
            "fnver": 0,
            "fnval": int(fnval),
            "otype": "json",
            "platform": "pc",
        }
        if fourk:
            params["fourk"] = 1
        data = await self._get_json(
            "/x/player/playurl",
            params,
            referer=f"{self.BASE_URL}/video/{bvid}",
        )
        return data if isinstance(data, dict) else {}

    async def get_subtitles(self, bvid: str, cid: int) -> List[Dict[str, Any]]:
        """``/x/player/v2``：字幕列表（含 AI 字幕，需要登录）。"""
        try:
            data = await self._get_json(
                "/x/player/v2",
                {"bvid": bvid, "cid": int(cid)},
                referer=f"{self.BASE_URL}/video/{bvid}",
            )
        except BiliAPIError as exc:
            logger.debug("player/v2 failed for %s/%s: %s", bvid, cid, exc)
            return []
        if not isinstance(data, dict):
            return []
        subtitle = data.get("subtitle")
        if not isinstance(subtitle, dict):
            return []
        subtitles = subtitle.get("subtitles")
        if not isinstance(subtitles, list):
            return []
        return [item for item in subtitles if isinstance(item, dict)]

    async def get_danmaku_xml(self, cid: int) -> str:
        """``comment.bilibili.com/{cid}.xml``：弹幕（deflate 压缩的 XML）。"""
        import zlib

        await self._ensure_session()
        url = f"https://comment.bilibili.com/{int(cid)}.xml"
        headers = {**self.headers, "Referer": f"{self.BASE_URL}/"}
        try:
            async with self._session.get(
                url, headers=headers, proxy=self.proxy or None
            ) as response:
                if response.status != 200:
                    logger.debug("danmaku fetch failed: status=%s cid=%s", response.status, cid)
                    return ""
                raw = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("danmaku fetch error for cid=%s: %s", cid, exc)
            return ""

        if raw[:1] == b"<":
            return raw.decode("utf-8", errors="replace")
        try:
            return zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", errors="replace")
        except zlib.error as exc:
            logger.debug("danmaku decompress failed for cid=%s: %s", cid, exc)
            return ""

    # ------------------------------------------------------------------
    # UP 主 / 合集 / 系列 / 收藏夹
    # ------------------------------------------------------------------

    async def get_user_card(self, mid: str) -> Optional[Dict[str, Any]]:
        """UP 主资料。优先用免签名的 card 接口，失败再退到 wbi 的 acc/info。"""
        mid = str(mid)
        try:
            data = await self._get_json("/x/web-interface/card", {"mid": mid})
            if isinstance(data, dict) and isinstance(data.get("card"), dict):
                card = data["card"]
                return {
                    "mid": str(card.get("mid") or mid),
                    "name": card.get("name") or "",
                    "face": card.get("face") or "",
                    "sign": card.get("sign") or "",
                }
        except BiliAPIError as exc:
            logger.debug("card endpoint failed for mid=%s: %s", mid, exc)

        try:
            data = await self._get_json("/x/space/wbi/acc/info", {"mid": mid}, signed=True)
        except BiliAPIError as exc:
            logger.debug("space/wbi/acc/info failed for mid=%s: %s", mid, exc)
            return None
        if not isinstance(data, dict):
            return None
        return {
            "mid": str(data.get("mid") or mid),
            "name": data.get("name") or "",
            "face": data.get("face") or "",
            "sign": data.get("sign") or "",
        }

    async def get_user_videos(
        self, mid: str, *, page: int = 1, page_size: int = 30, order: str = "pubdate"
    ) -> Dict[str, Any]:
        """``/x/space/wbi/arc/search``：UP 主投稿列表一页。

        返回 ``{"items": [...], "total": int, "has_more": bool}``。
        """
        params: Dict[str, Any] = {
            "mid": str(mid),
            "ps": int(page_size),
            "pn": int(page),
            "tid": 0,
            "keyword": "",
            "order": order,
            "platform": "web",
            "web_location": 1550101,
            "order_avoided": "true",
        }
        params.update(_WEB_FINGERPRINT)
        data = await self._get_json(
            "/x/space/wbi/arc/search",
            params,
            signed=True,
            referer=f"{self.SPACE_URL}/{mid}/video",
        )
        if not isinstance(data, dict):
            return {"items": [], "total": 0, "has_more": False}

        raw_list = data.get("list") if isinstance(data.get("list"), dict) else {}
        items = raw_list.get("vlist")
        items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        page_info = data.get("page") if isinstance(data.get("page"), dict) else {}
        total = _to_int(page_info.get("count"), 0)
        return {
            "items": items,
            "total": total,
            "has_more": bool(items) and page * int(page_size) < total,
        }

    async def get_seasons_series_list(self, mid: str) -> Dict[str, List[Dict[str, Any]]]:
        """``/x/polymer/web-space/seasons_series_list``：UP 主的合集与系列清单。"""
        data = await self._get_json(
            "/x/polymer/web-space/seasons_series_list",
            {"mid": str(mid), "page_num": 1, "page_size": 100, "web_location": 333.1387},
            referer=f"{self.SPACE_URL}/{mid}/video",
        )
        empty: Dict[str, List[Dict[str, Any]]] = {"seasons": [], "series": []}
        if not isinstance(data, dict):
            return empty
        items_lists = data.get("items_lists")
        if not isinstance(items_lists, dict):
            return empty

        seasons = items_lists.get("seasons_list")
        series = items_lists.get("series_list")

        def _metas(raw: Any, key: str) -> List[Dict[str, Any]]:
            if not isinstance(raw, list):
                return []
            result: List[Dict[str, Any]] = []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                meta = entry.get("meta")
                if isinstance(meta, dict) and meta.get(key) is not None:
                    result.append(meta)
            return result

        return {
            "seasons": _metas(seasons, "season_id"),
            "series": _metas(series, "series_id"),
        }

    async def get_season_archives(
        self, mid: str, season_id: str, *, page: int = 1, page_size: int = 30
    ) -> Dict[str, Any]:
        """``/x/polymer/web-space/seasons_archives_list``：合集内稿件一页。"""
        data = await self._get_json(
            "/x/polymer/web-space/seasons_archives_list",
            {
                "mid": str(mid),
                "season_id": str(season_id),
                "sort_reverse": "false",
                "page_num": int(page),
                "page_size": int(page_size),
                "web_location": 333.1387,
            },
            referer=f"{self.SPACE_URL}/{mid}/channel/collectiondetail?sid={season_id}",
        )
        return _paged_archives(data, page, page_size)

    async def get_series_archives(
        self, mid: str, series_id: str, *, page: int = 1, page_size: int = 30
    ) -> Dict[str, Any]:
        """``/x/series/archives``：系列内稿件一页。"""
        data = await self._get_json(
            "/x/series/archives",
            {
                "mid": str(mid),
                "series_id": str(series_id),
                "only_normal": "true",
                "sort": "desc",
                "pn": int(page),
                "ps": int(page_size),
            },
            referer=f"{self.SPACE_URL}/{mid}/channel/seriesdetail?sid={series_id}",
        )
        return _paged_archives(data, page, page_size)

    async def get_fav_resources(
        self, media_id: str, *, page: int = 1, page_size: int = 20
    ) -> Dict[str, Any]:
        """``/x/v3/fav/resource/list``：收藏夹内容一页（需登录）。

        返回 ``{"items": [...], "info": {...}, "has_more": bool}``。
        """
        data = await self._get_json(
            "/x/v3/fav/resource/list",
            {
                "media_id": str(media_id),
                "pn": int(page),
                "ps": int(page_size),
                "platform": "web",
            },
            referer=f"{self.BASE_URL}/",
        )
        if not isinstance(data, dict):
            return {"items": [], "info": {}, "has_more": False}
        medias = data.get("medias")
        items = [item for item in medias if isinstance(item, dict)] if isinstance(medias, list) else []
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        return {
            "items": items,
            "info": info,
            "has_more": bool(data.get("has_more")),
        }

    async def resolve_short_url(self, url: str) -> Optional[str]:
        """展开 ``b23.tv`` / ``acg.tv`` 短链到真实地址。

        手动逐跳跟随重定向（最多 5 跳）而不是让 aiohttp 自动跟随：短链落点
        是服务端下发的跳转目标，自动跟随会在任何校验发生之前就把请求发到
        中间地址。每一跳都先过 :mod:`bilibili.security` 的出站校验，
        不安全（非 http/https、内网/保留地址）时放弃并返回 None。
        """
        from urllib.parse import urljoin

        from .security import assert_safe_url

        await self._ensure_session()
        current = str(url or "").strip()
        try:
            for _ in range(5):
                assert_safe_url(current)
                async with self._session.get(
                    current,
                    headers={**self.headers, "Referer": f"{self.BASE_URL}/"},
                    proxy=self.proxy or None,
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            logger.warning(
                                "Short url redirect without Location: %s", current
                            )
                            return None
                        current = urljoin(current, location)
                        continue
                    return str(response.url)
            logger.warning("Short url exceeded max redirects: %s", url)
            return None
        except ValueError as exc:
            logger.error("Blocked unsafe short url target: %s", exc)
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("Failed to resolve bilibili short url %s: %s", url, exc)
            return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _paged_archives(data: Any, page: int, page_size: int) -> Dict[str, Any]:
    """把合集/系列两个接口同构的分页响应归一化。

    ``meta`` 原样带出：合集接口会返回合集名（用作输出目录层），系列接口没有，
    调用方按需兜底。
    """
    if not isinstance(data, dict):
        return {"items": [], "total": 0, "has_more": False, "meta": {}}
    archives = data.get("archives")
    items = [item for item in archives if isinstance(item, dict)] if isinstance(archives, list) else []
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    page_info = data.get("page") if isinstance(data.get("page"), dict) else {}
    total = _to_int(page_info.get("total"), 0)
    if not total:
        total = _to_int(meta.get("total"), 0)
    has_more = page * int(page_size) < total if total else bool(items)
    return {"items": items, "total": total, "has_more": has_more, "meta": meta}
