"""channels.feed —— 视频号响应 JSON 的 feed 提取与画质选择测试。"""

from __future__ import annotations

from channels.feed import (
    ChannelFeed,
    clean_media_url,
    extract_feeds,
    pick_quality_url,
    resolve_quality,
)


def _video_feed_payload(**overrides) -> dict:
    media = {
        "url": "https://finder.video.qq.com/302/20304/10004/1f0004dbgmaca4abmiwjnvxfrkgidykw2fy.f100040.mp4?dis_k=8be4f&dis_t=1760000000&src=3&vsig=xxx",
        "urlToken": "&encfilekey=abc123xyzdef456&token=qqeyJhbGciOi",
        "decodeKey": "18446744073709551615",
        "fileSize": 52428800,
        "videoPlayLen": 95,
        "coverUrl": "https://wx.qlogo.cn/cover/1.jpg",
        "spec": [
            {"fileFormat": "1", "width": 1080, "height": 1920},
            {"fileFormat": "3", "width": 540, "height": 960},
        ],
    }
    media.update(overrides.pop("media", {}))
    node = {
        "objectId": "obj_1001",
        "objectNonceId": "nonce_abc",
        "createtime": 1760000100,
        "contact": {
            "nickname": "测试作者",
            "username": "wxid_test",
            "headUrl": "https://wx.qlogo.cn/avatar/1.jpg",
        },
        "objectDesc": {
            "mediaType": 4,
            "description": "这是\n视频标题\n#话题",
            "media": [media],
        },
    }
    node.update(overrides)
    return node


class TestExtractFeeds:
    def test_video_full_url_is_retained_without_using_cover_full_url(self):
        original = "https://finder.video.qq.com/original.mp4?token=a%2Fb+z&sign=secret"
        node = _video_feed_payload(media={"fullUrl": original})
        node["objectDesc"]["media"][0]["cover"] = {"fullUrl": "https://cdn/cover.jpg"}
        assert extract_feeds(node)[0].full_url == original
        node["objectDesc"]["media"][0].pop("fullUrl")
        assert extract_feeds(node)[0].full_url == ""

    def test_image_full_url_is_not_a_video_original(self):
        node = _video_feed_payload(media={"fullUrl": "https://cdn/cover.jpg"})
        assert extract_feeds(node)[0].full_url == ""

    def test_original_url_alone_is_a_downloadable_video(self):
        node = _video_feed_payload(media={"url": "", "urlToken": "", "fullUrl": "https://finder.video.qq.com/full.mp4"})
        assert extract_feeds(node)[0].url == "https://finder.video.qq.com/full.mp4"

    def test_full_url_alias_and_current_dimensions(self):
        node = _video_feed_payload(media={"fullURL": "https://finder.video.qq.com/full.mp4", "width": "1280", "height": "720", "spec": []})
        feed = extract_feeds(node)[0]
        assert feed.full_url == "https://finder.video.qq.com/full.mp4"
        selected = resolve_quality(feed)
        assert (selected["width"], selected["height"]) == (1280, 720)
        assert "当前源" in selected["label"] and "720p" in selected["label"]
        options = feed.to_public_dict()["quality_options"]
        assert options[0]["width"] == 1280 and options[0]["height"] == 720

    def test_original_rejects_non_http_or_malformed_url(self):
        for url in ("file:///C:/secret.mp4", "javascript:alert(1)", "https://[invalid"):
            node = _video_feed_payload(media={"fullUrl": url})
            assert extract_feeds(node)[0].full_url == ""

    def test_original_rejects_untrusted_host_userinfo_and_non_default_port(self):
        for url in (
            "https://example.com/full.mp4", "https://finder.video.qq.com.evil.com/full.mp4",
            "https://user:password@finder.video.qq.com/full.mp4", "https://@finder.video.qq.com/full.mp4",
            "https://finder.video.qq.com:8443/full.mp4", "http://finder.video.qq.com:443/full.mp4",
            "https://finder.video.qq.com/cover%2Ejpg",
        ):
            node = _video_feed_payload(media={"fullUrl": url})
            assert extract_feeds(node)[0].full_url == ""

    def test_original_accepts_media_subdomain_and_default_port(self):
        for url in ("https://media.finder.video.qq.com:443/full.mp4", "http://finder.video.qq.com:80/full.mp4"):
            assert extract_feeds(_video_feed_payload(media={"fullUrl": url}))[0].full_url == url

    def test_embedded_channels_id_is_stable_without_nonce(self):
        """微信 4.x 的详情/播放列表以 id 下发 uint64 字符串。"""
        node = _video_feed_payload(id="14990527163746486645")
        node.pop("objectId")
        node.pop("objectNonceId")
        feeds = extract_feeds({"data": {"object": node}})
        assert len(feeds) == 1
        assert feeds[0].object_id == "14990527163746486645"
        assert feeds[0].token == "channels_14990527163746486645"
        assert "objectId" not in node  # 不改微信的原始节点

    def test_embedded_id_alias_deduplicates_with_object_id(self):
        legacy = _video_feed_payload(objectId="14990527163746486645")
        embedded = _video_feed_payload(id="14990527163746486645")
        embedded.pop("objectId")
        assert len(extract_feeds([legacy, embedded])) == 1

    def test_generic_id_is_not_a_feed(self):
        assert extract_feeds({"id": "14990527163746486645", "media": [{"url": "https://cdn/a.mp4"}]}) == []

    def test_video_fields(self):
        payload = {"data": {"object": _video_feed_payload()}}
        feeds = extract_feeds(payload, source_api="finderPcFlow")
        assert len(feeds) == 1
        feed = feeds[0]
        assert feed.kind == "video"
        assert feed.object_id == "obj_1001"
        assert feed.nonce_id == "nonce_abc"
        # decodeKey 以字符串下发（超出 JS 安全整数），必须正确转 int。
        assert feed.decode_key == 18446744073709551615
        assert feed.url.startswith("https://finder.video.qq.com/")
        assert feed.url.endswith("&encfilekey=abc123xyzdef456&token=qqeyJhbGciOi")
        assert feed.title == "这是 视频标题 #话题"  # 换行压缩
        assert feed.author_name == "测试作者"
        assert feed.author_id == "wxid_test"
        assert feed.duration == 95
        assert feed.file_size == 52428800
        assert feed.create_time == 1760000100
        assert len(feed.specs) == 2
        assert feed.source_api == "finderPcFlow"
        assert feed.token == "channels_obj_1001"

    def test_recursive_and_dedup(self):
        """嵌套层（关联推荐）里的 feed 也要提取；同 objectId 只保留一条。"""
        node = _video_feed_payload()
        related = _video_feed_payload(objectId="obj_1002", objectNonceId="nonce_b")
        dup = _video_feed_payload(objectNonceId="nonce_diff_same_object")
        payload = {"data": {"object_list": [node, dup], "extra": {"related": [related]}}}
        feeds = extract_feeds(payload)
        assert [f.object_id for f in feeds] == ["obj_1001", "obj_1002"]

    def test_image_feed(self):
        node = _video_feed_payload()
        node["objectDesc"] = {
            "mediaType": 2,
            "description": "图文动态",
            "media": [
                {"url": "https://img.qq.com/a.jpg", "urlToken": "?token=i1"},
                {"url": "https://img.qq.com/b.jpg"},
            ],
            "followPostInfo": {"musicInfo": {"mediaStreamingUrl": "https://music.qq.com/x.mp3"}},
        }
        feeds = extract_feeds({"data": node})
        feed = feeds[0]
        assert feed.kind == "image"
        assert feed.images == ["https://img.qq.com/a.jpg?token=i1", "https://img.qq.com/b.jpg"]
        assert feed.bgm_url == "https://music.qq.com/x.mp3"

    def test_live_feed(self):
        node = _video_feed_payload()
        node["liveInfo"] = {"streamUrl": "https://live.qq.com/flv/xxx.flv"}
        node["anchorContact"] = {"liveCoverImgUrl": "https://cover/live.jpg"}
        node["objectDesc"]["mediaType"] = 9
        feeds = extract_feeds({"data": node})
        feed = feeds[0]
        assert feed.kind == "live"
        assert feed.url == "https://live.qq.com/flv/xxx.flv"
        assert feed.cover_url == "https://cover/live.jpg"

    def test_live_without_mediatype_marker(self):
        """mediaType 未标 9 但带 streamUrl 的（回放列表常见）也按直播处理。"""
        node = _video_feed_payload()
        node["liveInfo"] = {"streamUrl": "https://live.qq.com/flv/y.flv"}
        feeds = extract_feeds({"data": node})
        assert feeds[0].kind == "live"

    def test_no_media_video_skipped(self):
        node = _video_feed_payload()
        node["objectDesc"]["media"] = []
        assert extract_feeds({"data": node}) == []

    def test_missing_ids_skipped(self):
        node = _video_feed_payload(objectId="", objectNonceId="")
        assert extract_feeds({"data": node}) == []

    def test_malformed_sibling_node_ignored(self):
        """结构异常的节点（objectDesc 非 dict 等）不影响其余提取。"""
        payload = {
            "data": [
                {"objectDesc": "broken"},
                _video_feed_payload(),
                None,
                42,
            ]
        }
        feeds = extract_feeds(payload)
        assert len(feeds) == 1

    def test_decode_key_missing_is_none(self):
        node = _video_feed_payload(media={"decodeKey": ""})
        feeds = extract_feeds({"data": node})
        assert feeds[0].decode_key is None


class TestQualityUrl:
    def _feed(self) -> ChannelFeed:
        return extract_feeds({"data": _video_feed_payload()})[0]

    def test_highest_selects_largest_spec_and_preserves_signed_url(self):
        feed = self._feed()
        assert pick_quality_url(feed, "highest") == feed.url + "&X-snsvideoflag=1"

    def test_highest_ranks_resolution_then_area_then_bitrate_and_size(self):
        feed = ChannelFeed(url="https://cdn/video.mp4?sign=A%2fB+C", specs=[
            {"fileFormat": "portrait", "width": 720, "height": 1920, "bitrate": 9000000},
            {"fileFormat": "1080-low", "width": 1920, "height": 1080, "bitrate": 2000000},
            {"fileFormat": "1080-high", "width": 1920, "height": 1080, "bitrate": 4000000, "fileSize": 100},
            {"fileFormat": "1080-largest", "width": 1920, "height": 1080, "bitrate": 4000000, "fileSize": 200},
        ])
        assert pick_quality_url(feed).endswith("X-snsvideoflag=1080-largest")

    def test_highest_excludes_hdr_and_ranks_video_bitrate(self):
        feed = ChannelFeed(url="https://cdn/v.mp4", specs=[
            {"fileFormat": "hdr", "width": 3840, "height": 2160, "dynamicRangeType": 1},
            {"fileFormat": "hd-low", "width": 1920, "height": 1080, "videoBitrate": 100},
            {"fileFormat": "hd-high", "width": 1920, "height": 1080, "videoBitrate": 200},
        ])
        selected = resolve_quality(feed)
        assert selected["url"] == "https://cdn/v.mp4?X-snsvideoflag=hd-high"
        assert (selected["width"], selected["height"]) == (1920, 1080)
        assert selected["value"] == "highest"
        assert "1080p" in selected["label"]
        assert "hdr" not in str(feed.to_public_dict()["quality_options"])

    def test_original_fallback_is_labeled_as_highest(self):
        selected = resolve_quality(self._feed(), "original")
        assert selected["value"] == "highest"
        assert "最高可用" in selected["label"] and "原画" not in selected["label"]

    def test_exact_selection_has_dimensions_for_media_validation(self):
        selected = resolve_quality(self._feed(), "format:3")
        assert (selected["value"], selected["width"], selected["height"]) == ("format:3", 540, 960)
        assert selected["url"] == pick_quality_url(self._feed(), "format:3")

    def test_1080p_matches_portrait_short_side(self):
        feed = ChannelFeed(url="https://cdn/video.mp4", specs=[
            {"fileFormat": "720", "width": 720, "height": 1280},
            {"fileFormat": "1080", "width": 1080, "height": 1920},
        ])
        assert pick_quality_url(feed, "1080p").endswith("X-snsvideoflag=1080")

    def test_original_uses_explicit_url_or_actual_highest(self):
        feed = self._feed()
        assert pick_quality_url(feed, "original") == pick_quality_url(feed, "highest")
        feed.full_url = "https://finder.video.qq.com/original.mp4?token=a%2Fb+z&flag=x#media"
        assert pick_quality_url(feed, "original") == feed.full_url

    def test_exact_format_selection_preserves_case(self):
        feed = ChannelFeed(url="https://cdn/video.mp4", specs=[
            {"fileFormat": "xWT111", "width": 1920, "height": 1080},
            {"fileFormat": "xWT222", "width": 1280, "height": 720},
        ])
        assert pick_quality_url(feed, "format:xWT222").endswith("X-snsvideoflag=xWT222")

    def test_partial_geometry_is_not_labeled_as_short_edge_resolution(self):
        feed = ChannelFeed(url="https://cdn/v.mp4", specs=[{"fileFormat": "partial", "height": 1080}])
        result = resolve_quality(feed)
        assert result["width"] == 0 and result["height"] == 1080
        assert "高度1080" in result["label"] and "1080p" not in result["label"]
        assert "1080p" not in str(feed.to_public_dict()["quality_options"])
        feed.specs = [{"fileFormat": "partial", "width": 1920}]
        assert "宽度1920" in resolve_quality(feed)["label"]

    def test_partial_geometry_cannot_override_known_resolution(self):
        feed = ChannelFeed(url="https://cdn/v.mp4", specs=[
            {"fileFormat": "partial", "height": 2160},
            {"fileFormat": "known", "width": 1920, "height": 1080},
        ])
        assert resolve_quality(feed)["url"].endswith("X-snsvideoflag=known")
        assert resolve_quality(feed, "2160p")["value"] == "format:known"

    def test_replaces_existing_flags_without_reencoding_signed_query(self):
        feed = ChannelFeed(
            url="https://cdn/video.mp4?token=a%2fb+z&X-snsvideoflag=old&sign=A%2F%3d&X-snsvideoflag=duplicate#end",
            specs=[{"fileFormat": "xWT111", "width": 1920, "height": 1080}],
        )
        assert pick_quality_url(feed) == (
            "https://cdn/video.mp4?token=a%2fb+z&X-snsvideoflag=xWT111&sign=A%2F%3d#end"
        )

    def test_appends_flag_before_fragment(self):
        feed = ChannelFeed(url="https://cdn/video.mp4#end", specs=[{"fileFormat": "1", "height": 1080}])
        assert pick_quality_url(feed) == "https://cdn/video.mp4?X-snsvideoflag=1#end"

    def test_unsafe_format_cannot_be_selected(self):
        feed = self._feed()
        feed.specs.append({"fileFormat": "x&token=secret", "width": 3840, "height": 2160})
        assert pick_quality_url(feed, "format:x&token=secret") == pick_quality_url(feed, "highest")
        assert "token=secret" not in pick_quality_url(feed)

    def test_replay_signature_and_encoded_query_are_preserved_exactly(self):
        url = ("https://finder.video.qq.com/251/20302/stodownload?encfilekey=fixture"
               "&bizid=1023&dotrans=0&idx=1&token=fixture&basedata=CAM%2F%2B"
               "&sign=fixture&web=1&svrbypass=AA%2F%3D&X-snsvideoflag=xWT111")
        feed = ChannelFeed(url=url)
        for quality in ("highest", "original", "", "lowest", "1080p"):
            assert pick_quality_url(feed, quality) == url

    def test_quality_appends_snsvideoflag(self):
        feed = self._feed()
        url = pick_quality_url(feed, "540p")
        assert url.endswith("&X-snsvideoflag=3")  # 竖屏短边 540 → fileFormat=3

    def test_lowest(self):
        feed = self._feed()
        assert pick_quality_url(feed, "lowest").endswith("&X-snsvideoflag=3")

    def test_unknown_quality_falls_back(self):
        feed = self._feed()
        assert pick_quality_url(feed, "weird") == pick_quality_url(feed, "highest")

    def test_no_specs_falls_back(self):
        feed = self._feed()
        feed.specs = []
        assert pick_quality_url(feed, "720p") == pick_quality_url(feed, "highest")

    def test_live_url_passthrough(self):
        feed = self._feed()
        feed.kind = "live"
        feed.url = "https://live.qq.com/x.flv"
        assert pick_quality_url(feed, "720p") == "https://live.qq.com/x.flv"


class TestCleanMediaUrl:
    def test_strips_volatile_params(self):
        url = "https://cdn/v.mp4?dis_k=1&dis_t=2&encfilekey=k&idx=1&token=t"
        assert clean_media_url(url) == "https://cdn/v.mp4?encfilekey=k&token=t"

    def test_keeps_url_without_core_params(self):
        url = "https://cdn/v.mp4?dis_k=1"
        assert clean_media_url(url) == url

    def test_empty(self):
        assert clean_media_url("") == ""


class TestPublicQualityOptions:
    def test_downloaded_quality_exposes_actual_geometry_without_internal_fields(self):
        feed = ChannelFeed(downloaded_quality={
            "width": 1920, "height": 1080, "label": "internal https://cdn/v.mp4?token=secret",
            "requested": "highest", "selected": "format:hd", "url": "https://cdn/v.mp4?token=secret",
            "decodeKey": 123,
        })
        view = feed.to_public_dict()["downloaded_quality"]
        assert view == {"width": 1920, "height": 1080, "label": "1080p · 1920×1080",
                        "requested": "highest", "selected": "format:hd"}
        assert "secret" not in str(view)

    def test_downloaded_quality_redacts_invalid_selection(self):
        feed = ChannelFeed(downloaded_quality={"requested": "https://cdn/v?token=secret", "selected": "format:hd&token=secret"})
        assert feed.to_public_dict()["downloaded_quality"]["requested"] == ""
        assert feed.to_public_dict()["downloaded_quality"]["selected"] == ""

    def test_options_are_safe_sorted_and_include_only_confirmed_original(self):
        from channels.feed import quality_options

        feed = ChannelFeed(url="https://cdn/v.mp4?token=secret", decode_key=123, specs=[
            {"fileFormat": "low", "width": 720, "height": 1280, "url": "https://cdn/low?token=secret"},
            {"fileFormat": "high", "width": 1080, "height": 1920, "decodeKey": "123"},
            {"fileFormat": "unsafe&token=secret", "width": 2160, "height": 3840},
        ])
        options = quality_options(feed)
        assert [o["value"] for o in options] == ["highest", "lowest", "format:high", "format:low"]
        assert all(set(o) == {"value", "label", "width", "height"} for o in options)
        assert (options[0]["width"], options[0]["height"]) == (1080, 1920)
        public = feed.to_public_dict()
        assert public["quality_options"] == options
        assert public["has_original"] is False
        assert "full_url" not in public and "specs" not in public
        assert "secret" not in str(options)
        feed.full_url = "https://finder.video.qq.com/original.mp4?token=secret"
        assert feed.to_public_dict()["has_original"] is True
        assert "original" in [o["value"] for o in quality_options(feed)]

    def test_internal_untrusted_original_cannot_be_advertised_or_selected(self):
        feed = ChannelFeed(url="https://cdn/v.mp4", full_url="https://example.com/full.mp4")
        assert feed.to_public_dict()["has_original"] is False
        assert "original" not in [o["value"] for o in feed.to_public_dict()["quality_options"]]
        assert pick_quality_url(feed, "original") == feed.url

    def test_no_specs_labels_current_source_instead_of_original(self):
        from channels.feed import quality_options

        options = quality_options(ChannelFeed(url="https://cdn/v.mp4"))
        assert options and all("当前源" in o["label"] for o in options)
        assert "original" not in [o["value"] for o in options]
        assert quality_options(ChannelFeed(kind="image")) == []
