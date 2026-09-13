"""批量来源（UP 主 / 合集 / 收藏夹 / 工厂）的采集逻辑测试。

这几条链路的共同风险是**静默多下或少下**：翻页没收敛导致重复请求，或者提前
收敛导致漏掉用户要的稿件。测试聚焦在选择与收敛规则上，逐条稿件的下载行为
已在 test_bili_downloader.py 覆盖，这里把 download_video_item 打桩。
"""

from unittest.mock import AsyncMock

import pytest

from bilibili.api_client import BiliAPIClient, BiliLoginRequiredError, BiliRiskControlError
from bilibili.collection_downloader import BiliCollectionDownloader
from bilibili.factory import BiliDownloaderFactory
from bilibili.fav_downloader import BiliFavDownloader
from bilibili.user_downloader import BiliUserDownloader
from config import ConfigLoader
from control import RetryHandler
from storage import FileManager

MID = "271779326"


def _build(downloader_cls, tmp_path, *, top_level=None, **bili_overrides):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path))
    section = {"request_interval": 0}
    section.update(bili_overrides)
    config.update(bilibili=section)
    if top_level:
        config.update(**top_level)

    client = BiliAPIClient({}, request_interval=0)
    client.get_user_card = AsyncMock(return_value={"mid": MID, "name": "测试UP"})
    downloader = downloader_cls(
        config,
        client,
        FileManager(str(tmp_path)),
        retry_handler=RetryHandler(max_retries=0),
    )
    # 采集逻辑与逐条下载解耦：这里只关心「选了哪些、按什么顺序选」。
    downloader.download_video_item = AsyncMock(return_value="success")
    return downloader, client


# ----------------------------------------------------------------------
# UP 主空间
# ----------------------------------------------------------------------


async def test_user_downloader_paginates_until_no_more(tmp_path):
    downloader, client = _build(BiliUserDownloader, tmp_path)
    client.get_user_videos = AsyncMock(
        side_effect=[
            {"items": [{"bvid": "BV1"}, {"bvid": "BV2"}], "total": 3, "has_more": True},
            {"items": [{"bvid": "BV3"}], "total": 3, "has_more": False},
        ]
    )

    result = await downloader.download({"type": "user", "mid": MID})

    assert client.get_user_videos.await_count == 2
    assert result.total == 3
    assert result.success == 3
    # 模式固定为 post，与抖音侧的作者目录布局保持一致。
    assert downloader.download_video_item.await_args_list[0].kwargs["mode"] == "post"
    assert downloader.download_video_item.await_args_list[0].kwargs["author_name"] == "测试UP"


async def test_user_downloader_stops_early_when_page_crosses_start_time(tmp_path):
    """pubdate 倒序下，出现早于 start_time 的稿件即可停止翻页。"""
    downloader, client = _build(
        BiliUserDownloader, tmp_path, top_level={"start_time": "2024-01-01"}
    )
    client.get_user_videos = AsyncMock(
        side_effect=[
            {"items": [{"bvid": "BV1", "created": 1704153600}], "total": 2, "has_more": True},
            {"items": [{"bvid": "BV0", "created": 1600000000}], "total": 2, "has_more": True},
            {"items": [{"bvid": "BV-1", "created": 1500000000}], "total": 2, "has_more": True},
        ]
    )

    result = await downloader.download({"type": "user", "mid": MID})

    # 第 2 页越界即停，第 3 页不该被请求。
    assert client.get_user_videos.await_count == 2
    assert result.total == 1


async def test_user_downloader_limit_stops_paging_without_time_filter(tmp_path):
    downloader, client = _build(BiliUserDownloader, tmp_path, number={"user": 2})
    client.get_user_videos = AsyncMock(
        return_value={
            "items": [{"bvid": "BV1"}, {"bvid": "BV2"}],
            "total": 100,
            "has_more": True,
        }
    )

    result = await downloader.download({"type": "user", "mid": MID})

    assert client.get_user_videos.await_count == 1
    assert result.total == 2


async def test_user_downloader_breaks_when_pagination_stalls(tmp_path):
    """接口在最后一页重复返回同一批时必须收手，否则会翻到 MAX_PAGES。"""
    downloader, client = _build(BiliUserDownloader, tmp_path)
    client.get_user_videos = AsyncMock(
        return_value={"items": [{"bvid": "BV1"}], "total": 100, "has_more": True}
    )

    result = await downloader.download({"type": "user", "mid": MID})

    assert client.get_user_videos.await_count == 2
    assert result.total == 1


async def test_user_downloader_without_mid_returns_empty(tmp_path):
    downloader, client = _build(BiliUserDownloader, tmp_path)
    client.get_user_videos = AsyncMock()

    result = await downloader.download({"type": "user"})

    assert result.total == 0
    assert client.get_user_videos.await_count == 0


async def test_user_downloader_reports_no_uploads(tmp_path):
    downloader, client = _build(BiliUserDownloader, tmp_path)
    client.get_user_videos = AsyncMock(return_value={"items": [], "total": 0, "has_more": False})

    result = await downloader.download({"type": "user", "mid": MID})

    assert result.total == 0
    assert downloader.download_video_item.await_count == 0


async def test_user_downloader_propagates_risk_control(tmp_path):
    downloader, client = _build(BiliUserDownloader, tmp_path)
    client.get_user_videos = AsyncMock(
        side_effect=BiliRiskControlError(-412, "请求被拦截", "/x/space/wbi/arc/search")
    )

    with pytest.raises(BiliRiskControlError):
        await downloader.download({"type": "user", "mid": MID})


# ----------------------------------------------------------------------
# 合集 / 系列
# ----------------------------------------------------------------------


def _season_page(items, name="合集A", has_more=False):
    return {"items": items, "total": len(items), "has_more": has_more, "meta": {"name": name}}


async def test_collection_downloader_uses_season_api_and_name_as_subdir(tmp_path):
    downloader, client = _build(BiliCollectionDownloader, tmp_path)
    client.get_season_archives = AsyncMock(return_value=_season_page([{"bvid": "BV1"}]))
    client.get_series_archives = AsyncMock()

    result = await downloader.download(
        {"type": "collection", "season_id": "12345", "mid": MID}
    )

    assert result.total == 1
    assert client.get_series_archives.await_count == 0
    # 合集名插入为独立目录层，合集之间不混在一起。
    assert (
        downloader.download_video_item.await_args_list[0].kwargs["collection_dir"] == "合集A"
    )
    assert downloader.download_video_item.await_args_list[0].kwargs["mode"] == "collection"


async def test_collection_downloader_auto_corrects_kind(tmp_path):
    """链接写 collectiondetail 但 sid 其实是系列时自动纠正，而不是报空。"""
    downloader, client = _build(BiliCollectionDownloader, tmp_path)
    client.get_season_archives = AsyncMock(
        return_value={"items": [], "total": 0, "has_more": False, "meta": {}}
    )
    client.get_series_archives = AsyncMock(return_value=_season_page([{"bvid": "BV1"}], name=""))

    result = await downloader.download(
        {"type": "collection", "season_id": "6789", "mid": MID}
    )

    assert result.total == 1
    assert client.get_season_archives.await_count == 1
    assert client.get_series_archives.await_count == 1
    assert downloader.download_video_item.await_args_list[0].kwargs["mode"] == "series"


async def test_collection_downloader_falls_back_to_synthetic_dir_name(tmp_path):
    downloader, client = _build(BiliCollectionDownloader, tmp_path)
    client.get_season_archives = AsyncMock(
        return_value={"items": [{"bvid": "BV1"}], "total": 1, "has_more": False, "meta": {}}
    )

    await downloader.download({"type": "collection", "season_id": "12345", "mid": MID})

    assert (
        downloader.download_video_item.await_args_list[0].kwargs["collection_dir"]
        == "collection_12345"
    )


async def test_collection_downloader_paginates(tmp_path):
    downloader, client = _build(BiliCollectionDownloader, tmp_path)
    client.get_season_archives = AsyncMock(
        side_effect=[
            _season_page([{"bvid": "BV1"}, {"bvid": "BV2"}], has_more=True),
            _season_page([{"bvid": "BV3"}], has_more=False),
        ]
    )

    result = await downloader.download({"type": "collection", "season_id": "1", "mid": MID})

    assert result.total == 3
    # 第 1 页在 _resolve_kind 里取到后复用，不重复请求。
    assert client.get_season_archives.await_count == 2


async def test_collection_downloader_requires_mid_and_sid(tmp_path):
    downloader, client = _build(BiliCollectionDownloader, tmp_path)
    client.get_season_archives = AsyncMock()

    assert (await downloader.download({"type": "collection", "season_id": "1"})).total == 0
    assert (await downloader.download({"type": "collection", "mid": MID})).total == 0
    assert client.get_season_archives.await_count == 0


async def test_series_downloader_uses_series_api_first(tmp_path):
    downloader, client = _build(BiliCollectionDownloader, tmp_path)
    client.get_series_archives = AsyncMock(return_value=_season_page([{"bvid": "BV1"}], name=""))
    client.get_season_archives = AsyncMock()

    result = await downloader.download({"type": "series", "series_id": "6789", "mid": MID})

    assert result.total == 1
    assert client.get_series_archives.await_count == 1
    assert client.get_season_archives.await_count == 0


# ----------------------------------------------------------------------
# 收藏夹
# ----------------------------------------------------------------------


FAV_PAGE = {
    "info": {"title": "我的收藏夹"},
    "has_more": False,
    "items": [
        {"bvid": "BV1", "type": 2, "attr": 0, "id": 1, "pubtime": 1704153600},
        {"bvid": "BV2", "type": 2, "attr": 1, "id": 2, "pubtime": 1704153600},  # 已失效
        {"bvid": "BV3", "type": 12, "attr": 0, "id": 3, "pubtime": 1704153600},  # 音频
        {"bvid": "BV4", "type": 21, "attr": 0, "id": 4, "pubtime": 1704153600},  # 合集条目
        {"bvid": "BV5", "type": 2, "attr": 0, "id": 5, "pubtime": 1704153600},
    ],
}


async def test_fav_downloader_skips_invalid_and_non_video_entries(tmp_path):
    downloader, client = _build(BiliFavDownloader, tmp_path)
    client.get_fav_resources = AsyncMock(return_value=FAV_PAGE)

    result = await downloader.download({"type": "favlist", "media_id": "999"})

    assert result.total == 2
    collected = [
        call.args[0]["bvid"] for call in downloader.download_video_item.await_args_list
    ]
    assert collected == ["BV1", "BV5"]


async def test_fav_downloader_uses_favlist_name_as_subdir(tmp_path):
    downloader, client = _build(BiliFavDownloader, tmp_path)
    client.get_fav_resources = AsyncMock(return_value=FAV_PAGE)

    await downloader.download({"type": "favlist", "media_id": "999"})

    kwargs = downloader.download_video_item.await_args_list[0].kwargs
    assert kwargs["collection_dir"] == "我的收藏夹"
    assert kwargs["mode"] == "favlist"
    # 作者目录由稿件详情里的真实 UP 主决定，这里只是兜底值。
    assert kwargs["author_name"] == "我的收藏夹"


async def test_fav_downloader_maps_pubtime_into_pubdate(tmp_path):
    downloader, client = _build(BiliFavDownloader, tmp_path)
    client.get_fav_resources = AsyncMock(return_value=FAV_PAGE)

    await downloader.download({"type": "favlist", "media_id": "999"})

    item = downloader.download_video_item.await_args_list[0].args[0]
    assert item["pubdate"] == 1704153600


async def test_fav_downloader_propagates_login_required(tmp_path):
    """收藏夹未登录必须冒泡成可执行提示，而不是静默变成「没有内容」。"""
    downloader, client = _build(BiliFavDownloader, tmp_path)
    client.get_fav_resources = AsyncMock(
        side_effect=BiliLoginRequiredError(-101, "账号未登录", "/x/v3/fav/resource/list")
    )

    with pytest.raises(BiliLoginRequiredError):
        await downloader.download({"type": "favlist", "media_id": "999"})


async def test_fav_downloader_paginates(tmp_path):
    downloader, client = _build(BiliFavDownloader, tmp_path)
    client.get_fav_resources = AsyncMock(
        side_effect=[
            {
                "info": {"title": "夹子"},
                "has_more": True,
                "items": [{"bvid": "BV1", "type": 2, "attr": 0, "pubtime": 1704153600}],
            },
            {
                "info": {},
                "has_more": False,
                "items": [{"bvid": "BV2", "type": 2, "attr": 0, "pubtime": 1704153600}],
            },
        ]
    )

    result = await downloader.download({"type": "favlist", "media_id": "999"})

    assert result.total == 2
    assert client.get_fav_resources.await_count == 2


async def test_fav_downloader_requires_media_id(tmp_path):
    downloader, client = _build(BiliFavDownloader, tmp_path)
    client.get_fav_resources = AsyncMock()

    assert (await downloader.download({"type": "favlist"})).total == 0
    assert client.get_fav_resources.await_count == 0


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url_type,expected",
    [
        ("video", "BiliVideoDownloader"),
        ("user", "BiliUserDownloader"),
        ("collection", "BiliCollectionDownloader"),
        ("series", "BiliCollectionDownloader"),
        ("favlist", "BiliFavDownloader"),
    ],
)
def test_factory_maps_url_types(tmp_path, url_type, expected):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path))
    downloader = BiliDownloaderFactory.create(
        url_type,
        config=config,
        api_client=BiliAPIClient({}, request_interval=0),
        file_manager=FileManager(str(tmp_path)),
    )
    assert type(downloader).__name__ == expected


@pytest.mark.parametrize("url_type", ["short", "bangumi", "unknown-thing"])
def test_factory_rejects_unsupported_types(tmp_path, url_type):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path))
    assert (
        BiliDownloaderFactory.create(
            url_type,
            config=config,
            api_client=BiliAPIClient({}, request_interval=0),
            file_manager=FileManager(str(tmp_path)),
        )
        is None
    )
