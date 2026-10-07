"""Offline regressions for filtered paging, metadata feeds and share URLs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.api_client import LoginRequiredError
from core.mix_downloader import MixDownloader
from core.music_downloader import MusicDownloader
from core.url_parser import URLParser
from core.user_downloader import UserDownloader
from core.user_modes.like_strategy import LikeUserModeStrategy
from core.user_modes.mix_strategy import MixUserModeStrategy
from core.user_modes.music_strategy import MusicUserModeStrategy


def _downloader(api, mode, limit=0):
    return SimpleNamespace(
        api_client=api,
        config={"number": {mode: limit}},
        rate_limiter=SimpleNamespace(acquire=AsyncMock()),
        _filter_by_time=lambda items: [item for item in items if item.get("eligible", True)],
        _limit_count=lambda items, _mode: items[:limit] if limit else items,
    )


@pytest.mark.asyncio
async def test_like_limit_counts_items_after_date_filter():
    fetch = AsyncMock(side_effect=[
        {"items": [{"aweme_id": "old", "eligible": False}], "has_more": 1, "max_cursor": 1},
        {"items": [{"aweme_id": "new"}], "has_more": 0, "max_cursor": 2},
    ])
    strategy = LikeUserModeStrategy(_downloader(SimpleNamespace(get_user_like=fetch), "like", 1))
    items = strategy.apply_filters(await strategy.collect_items("sec", {}))
    assert [item["aweme_id"] for item in items] == ["new"]
    assert fetch.await_count == 2


@pytest.mark.parametrize("strategy_cls, mode, id_key", [
    (MixUserModeStrategy, "mix", "mix_id"),
    (MusicUserModeStrategy, "music", "music_id"),
])
@pytest.mark.asyncio
async def test_mixed_metadata_feed_keeps_all_works(strategy_cls, mode, id_key):
    listing = AsyncMock(return_value={
        "items": [{"aweme_id": "direct"}, {id_key: "meta"}], "has_more": False,
    })
    expansion = AsyncMock(return_value={
        "items": [{"aweme_id": "direct"}, {"aweme_id": "expanded"}], "has_more": False,
    })
    api = SimpleNamespace(**{f"get_user_{mode}": listing, f"get_{mode}_aweme": expansion})
    strategy = strategy_cls(_downloader(api, mode))
    items = await strategy.collect_items("sec", {})
    assert [item["aweme_id"] for item in items] == ["direct", "expanded"]


@pytest.mark.parametrize("strategy_cls, mode, id_key", [
    (MixUserModeStrategy, "mix", "mix_id"),
    (MusicUserModeStrategy, "music", "music_id"),
])
@pytest.mark.asyncio
async def test_metadata_feed_limit_does_not_count_containers(strategy_cls, mode, id_key):
    listing = AsyncMock(side_effect=[
        {"items": [{id_key: "empty"}], "has_more": True, "max_cursor": 1},
        {"items": [{id_key: "valid"}], "has_more": False, "max_cursor": 2},
    ])
    expansion = AsyncMock(side_effect=[
        {"items": [], "has_more": False},
        {"items": [{"aweme_id": "work"}], "has_more": False},
    ])
    api = SimpleNamespace(**{f"get_user_{mode}": listing, f"get_{mode}_aweme": expansion})
    strategy = strategy_cls(_downloader(api, mode, 1))
    items = strategy.apply_filters(await strategy.collect_items("sec", {}))
    assert [item["aweme_id"] for item in items] == ["work"]
    assert listing.await_count == 2


@pytest.mark.asyncio
async def test_metadata_expansion_propagates_expired_login():
    api = SimpleNamespace(
        get_user_mix=AsyncMock(return_value={"items": [{"mix_id": "meta"}]}),
        get_mix_aweme=AsyncMock(side_effect=LoginRequiredError(2483, "请先登录", "/mix")),
    )
    strategy = MixUserModeStrategy(_downloader(api, "mix"))
    with pytest.raises(LoginRequiredError):
        await strategy.collect_items("sec", {})


def test_share_url_modal_id_takes_priority_over_old_video_path():
    parsed = URLParser.parse("https://www.douyin.com/video/111?modal_id=222")
    assert parsed["aweme_id"] == "222"


def test_share_url_modal_id_is_parsed_as_query_value():
    parsed = URLParser.parse("https://www.douyin.com/user/sec?modal_id=%32%32%32")
    assert parsed["aweme_id"] == "222"


@pytest.mark.parametrize("downloader_cls, helper, api_method", [
    (MixDownloader, "_get_mix_detail", "get_mix_detail"),
    (MusicDownloader, "_get_music_detail", "get_music_detail"),
    (MusicDownloader, "_get_first_music_aweme", "get_music_aweme"),
])
@pytest.mark.asyncio
async def test_optional_api_helpers_preserve_login_failure(downloader_cls, helper, api_method):
    downloader = object.__new__(downloader_cls)
    downloader.api_client = SimpleNamespace(**{
        api_method: AsyncMock(side_effect=LoginRequiredError(2483, "请先登录", "/test")),
    })
    with pytest.raises(LoginRequiredError):
        await getattr(downloader, helper)("123")


@pytest.mark.asyncio
async def test_self_profile_resolution_preserves_login_failure():
    downloader = object.__new__(UserDownloader)
    downloader.progress_reporter = None
    downloader.api_client = SimpleNamespace(
        get_self_info=AsyncMock(side_effect=LoginRequiredError(2483, "请先登录", "/self")),
    )
    with pytest.raises(LoginRequiredError):
        await downloader._resolve_self_alias("self", ["post"])


@pytest.mark.parametrize("collection", ["aweme", "entries", "expansion"])
@pytest.mark.asyncio
async def test_paging_stops_when_cursor_cycles_to_previously_seen_page(collection):
    calls = []

    async def fetch(_id, max_cursor=0, count=20, *, cursor=None):
        current = max_cursor if cursor is None else cursor
        calls.append(current)
        assert len(calls) <= 3, "cursor cycle fetched an already visited page"
        return {
            "items": [{"aweme_id": str(current)}],
            "has_more": True,
            "max_cursor": {0: 1, 1: 2, 2: 1}[current],
        }

    strategy = LikeUserModeStrategy(_downloader(SimpleNamespace(get_user_like=fetch), "like"))
    if collection == "aweme":
        items = await strategy.collect_items("sec", {})
    elif collection == "entries":
        items = await strategy._collect_paged_entries(fetch, "sec")
    else:
        strategy.downloader.api_client.get_mix_aweme = fetch
        items = await strategy._expand_metadata_items(
            [{"mix_id": "meta"}], "mix_id", ["mixId"], "get_mix_aweme",
        )
    assert calls == [0, 1, 2]
    assert [item["aweme_id"] for item in items] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_like_limit_counts_unique_works_across_page_boundaries():
    fetch = AsyncMock(side_effect=[
        {"items": [{"aweme_id": "one"}], "has_more": True, "max_cursor": 1},
        {"items": [{"aweme_id": "one"}], "has_more": True, "max_cursor": 2},
        {"items": [{"aweme_id": "two"}], "has_more": False, "max_cursor": 3},
    ])
    strategy = LikeUserModeStrategy(_downloader(SimpleNamespace(get_user_like=fetch), "like", 2))
    items = strategy.apply_filters(await strategy.collect_items("sec", {}))
    assert [item["aweme_id"] for item in items] == ["one", "two"]
    assert fetch.await_count == 3
