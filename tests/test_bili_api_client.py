"""B 站 API 客户端测试（全部离线，不触碰真实接口）。

重点覆盖三件事：业务错误码到异常类型的映射、Cookie 的域名作用域、以及 wbi
签名是否真的挂到了请求参数上。这三处任一出问题都表现为「接口莫名失败」，
没有网络也能测出来，所以必须测。
"""

import pytest

from bilibili.api_client import (
    BiliAPIClient,
    BiliAPIError,
    BiliLoginRequiredError,
    BiliRiskControlError,
)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.url = "https://api.bilibili.com/x/mock"
        self.headers = {}

    async def json(self, content_type=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def text(self):
        import json

        return json.dumps(self.payload)

    async def read(self):
        return b""


class _FakeRequest:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    """按序弹出预置响应，用尽后重复最后一个。"""

    closed = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append({"url": url, "kwargs": kwargs})
        if len(self.responses) > 1:
            response = self.responses.pop(0)
        else:
            response = self.responses[0]
        if not isinstance(response, _FakeResponse):
            # 便利写法：直接给裸 payload 字典时自动包一层。
            response = _FakeResponse(response)
        return _FakeRequest(response)


def _client(responses, cookies=None):
    client = BiliAPIClient(cookies or {}, request_interval=0)
    client._session = _FakeSession(responses)
    return client


NAV_OK = {
    "code": 0,
    "data": {
        "isLogin": True,
        "uname": "tester",
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        },
    },
}

SPI_OK = {"code": 0, "data": {"b_3": "abcd1234infoc", "b_4": "efgh5678infoc"}}


# ----------------------------------------------------------------------
# 错误码映射
# ----------------------------------------------------------------------


async def test_login_required_code_maps_to_dedicated_exception():
    client = _client([{"code": -101, "message": "账号未登录"}])
    with pytest.raises(BiliLoginRequiredError) as excinfo:
        await client._get_json("/x/v3/fav/resource/list")
    assert excinfo.value.code == -101
    # 子类关系必须保持，调用方按基类兜底时不能漏掉它。
    assert isinstance(excinfo.value, BiliAPIError)


@pytest.mark.parametrize("code", [-352, -403, -412, -509])
async def test_risk_control_codes_map_to_dedicated_exception(code):
    client = _client([{"code": code, "message": "风控校验失败"}])
    with pytest.raises(BiliRiskControlError):
        await client._get_json("/x/space/wbi/arc/search", signed=False)


async def test_other_codes_raise_plain_api_error():
    client = _client([{"code": -404, "message": "啥都木有"}])
    with pytest.raises(BiliAPIError) as excinfo:
        await client._get_json("/x/web-interface/view", {"bvid": "BV1"})
    assert not isinstance(excinfo.value, (BiliLoginRequiredError, BiliRiskControlError))
    assert excinfo.value.code == -404


async def test_non_200_http_status_raises():
    client = _client([_FakeResponse({}, status=503)])
    with pytest.raises(BiliAPIError):
        await client._get_json("/x/web-interface/view")


async def test_successful_response_returns_data_payload():
    client = _client([{"code": 0, "data": {"bvid": "BV1GJ411x7h7"}}])
    assert await client._get_json("/x/web-interface/view") == {"bvid": "BV1GJ411x7h7"}


# ----------------------------------------------------------------------
# Cookie 作用域
# ----------------------------------------------------------------------


def test_cookie_header_only_sent_to_bilibili_hosts():
    client = BiliAPIClient({"SESSDATA": "secret"}, request_interval=0)
    assert "SESSDATA=secret" in client.cookie_header_for("https://api.bilibili.com/x/y")
    assert "SESSDATA=secret" in client.cookie_header_for("https://i0.hdslb.com/bfs/a.jpg")
    assert "SESSDATA=secret" in client.cookie_header_for("https://upos-sz.bilivideo.com/v.m4s")
    # 第三方域名绝不能拿到账号凭据。
    assert client.cookie_header_for("https://evil.example.com/x") == ""
    assert client.cookie_header_for("https://notbilibili.com.cn/x") == ""


def test_download_headers_include_referer_and_cookie():
    client = BiliAPIClient({"SESSDATA": "secret"}, request_interval=0)
    headers = client.download_headers("https://upos-sz.bilivideo.com/v.m4s")
    assert headers["Referer"] == "https://www.bilibili.com/"
    assert headers["Cookie"] == "SESSDATA=secret"
    assert "User-Agent" in headers


def test_download_headers_honour_custom_referer():
    client = BiliAPIClient({}, request_interval=0)
    referer = "https://www.bilibili.com/video/BV1GJ411x7h7"
    headers = client.download_headers("https://cdn/x.m4s", referer=referer)
    assert headers["Referer"] == referer
    assert "Cookie" not in headers


# ----------------------------------------------------------------------
# nav / 登录态 / WBI
# ----------------------------------------------------------------------


async def test_nav_records_login_state_and_keys():
    client = _client([NAV_OK, SPI_OK])
    img_key, sub_key = await client.ensure_wbi_keys()
    assert img_key == "7cd084941338484aae1ad9425b84077c"
    assert sub_key == "4932caff0ff746eab6f01bf08b70ac45"
    assert client.is_login is True
    assert client.login_uname == "tester"
    # buvid3 缺失时自动向 spi 领取。
    assert client.cookies.get("buvid3") == "abcd1234infoc"


async def test_wbi_keys_are_cached_between_calls():
    client = _client([NAV_OK, SPI_OK, {"code": 0, "data": {}}])
    await client.ensure_wbi_keys()
    requests_after_first = len(client._session.requests)
    await client.ensure_wbi_keys()
    assert len(client._session.requests) == requests_after_first


async def test_signed_request_carries_wts_and_w_rid():
    client = _client([NAV_OK, SPI_OK, {"code": 0, "data": {"list": {"vlist": []}, "page": {}}}])
    await client.get_user_videos("271779326", page=1, page_size=30)
    params = client._session.requests[-1]["kwargs"]["params"]
    assert "wts" in params
    assert len(params["w_rid"]) == 32
    # 前端指纹必须一起带上，否则接口回 -412。
    assert params["dm_img_list"] == "[]"
    assert "mid" in params


async def test_unsigned_request_has_no_w_rid():
    client = _client([{"code": 0, "data": {"bvid": "BV1"}}])
    await client.get_video_detail(bvid="BV1")
    params = client._session.requests[-1]["kwargs"]["params"]
    assert "w_rid" not in params


async def test_synthetic_buvid3_used_when_spi_fails():
    client = _client([NAV_OK, {"code": -404, "message": "未找到"}])
    await client.ensure_wbi_keys()
    assert client.cookies.get("buvid3", "").endswith("infoc")


# ----------------------------------------------------------------------
# 各接口的响应归一化
# ----------------------------------------------------------------------


async def test_get_user_videos_normalizes_paged_response():
    payload = {
        "code": 0,
        "data": {
            "list": {"vlist": [{"bvid": "BV1GJ411x7h7", "created": 1700000000}]},
            "page": {"count": 100},
        },
    }
    client = _client([NAV_OK, SPI_OK, payload])
    data = await client.get_user_videos("271779326", page=1, page_size=30)
    assert data["total"] == 100
    assert data["has_more"] is True
    assert data["items"][0]["bvid"] == "BV1GJ411x7h7"


async def test_get_user_videos_last_page_has_no_more():
    payload = {"code": 0, "data": {"list": {"vlist": [{"bvid": "BV1"}]}, "page": {"count": 1}}}
    client = _client([NAV_OK, SPI_OK, payload])
    data = await client.get_user_videos("1", page=1, page_size=30)
    assert data["has_more"] is False


async def test_get_season_archives_keeps_meta_name():
    payload = {
        "code": 0,
        "data": {
            "archives": [{"bvid": "BV1"}],
            "meta": {"name": "我的合集", "total": 5},
            "page": {"total": 5},
        },
    }
    client = _client([{"code": 0, "data": payload["data"]}])
    data = await client.get_season_archives("1", "12345")
    assert data["meta"]["name"] == "我的合集"
    assert data["total"] == 5
    assert data["has_more"] is False


async def test_get_fav_resources_normalizes_items_and_info():
    payload = {
        "code": 0,
        "data": {
            "info": {"title": "我的收藏夹", "media_count": 2},
            "medias": [{"bvid": "BV1", "type": 2}],
            "has_more": True,
        },
    }
    client = _client([payload])
    data = await client.get_fav_resources("999")
    assert data["info"]["title"] == "我的收藏夹"
    assert data["has_more"] is True
    assert data["items"][0]["bvid"] == "BV1"


async def test_get_seasons_series_list_returns_metas():
    payload = {
        "code": 0,
        "data": {
            "items_lists": {
                "seasons_list": [{"meta": {"season_id": 12345, "name": "合集A"}}],
                "series_list": [{"meta": {"series_id": 6789, "name": "系列B"}}],
            }
        },
    }
    client = _client([payload])
    data = await client.get_seasons_series_list("271779326")
    assert data["seasons"][0]["season_id"] == 12345
    assert data["series"][0]["series_id"] == 6789


async def test_get_user_card_falls_back_to_wbi_endpoint():
    """card 接口失效时退到 space/wbi/acc/info，不能让 UP 主信息拿不到。"""
    client = _client(
        [
            {"code": -404, "message": "nope"},
            NAV_OK,
            SPI_OK,
            {"code": 0, "data": {"mid": 271779326, "name": "备用UP", "face": "", "sign": ""}},
        ]
    )
    card = await client.get_user_card("271779326")
    assert card["name"] == "备用UP"


async def test_get_subtitles_returns_empty_on_error():
    client = _client([{"code": -404, "message": "nope"}])
    assert await client.get_subtitles("BV1", 111) == []


async def test_get_subtitles_extracts_list():
    payload = {
        "code": 0,
        "data": {"subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": "//a/b.json"}]}},
    }
    client = _client([payload])
    subtitles = await client.get_subtitles("BV1", 111)
    assert subtitles[0]["lan"] == "zh-CN"
