"""ytdlp.extra_options 白名单分级策略测试（P1 安全边界）。

原则：``ytdlp.extra_options`` 是 yt-dlp Python API 逃生舱，其中混有可写任意
文件（``outtmpl``）、执行任意命令（``exec_cmd`` / ``postprocessors``）、指
向任意本机可执行文件（``external_downloader`` / ``ffmpeg_location``）的参数。
默认只放行确认无害的网络/性能参数；危险参数必须显式
``ytdlp.unsafe_extra_options: true`` 才生效；未知参数一律拒绝。
"""

from __future__ import annotations

import pytest

from ytdlp.options_policy import (
    SAFE_EXTRA_OPTIONS,
    UNSAFE_EXTRA_OPTIONS,
    apply_extra_options,
)


class TestSafeDefaults:
    def test_safe_options_applied(self):
        options: dict = {}
        apply_extra_options(options, {"geo_bypass": True, "socket_timeout": 60})
        assert options["geo_bypass"] is True
        assert options["socket_timeout"] == 60

    def test_http_headers_allowed(self):
        options: dict = {}
        apply_extra_options(options, {"http_headers": {"User-Agent": "x"}})
        assert options["http_headers"] == {"User-Agent": "x"}

    def test_unknown_key_rejected(self):
        options: dict = {}
        with pytest.raises(ValueError):
            apply_extra_options(options, {"totally_unknown_option": 1})

    def test_dangerous_keys_rejected_by_default(self):
        dangerous = [
            ("outtmpl", {"default": "/etc/pwn/%(id)s.%(ext)s"}),
            ("exec_cmd", "calc.exe"),
            ("exec_before_dl_cmd", "rm -rf /"),
            ("postprocessors", [{"key": "Exec", "exec_cmd": "sh"}]),
            ("external_downloader", "C:/Windows/System32/cmd.exe"),
            ("external_downloader_args", ["-evil"]),
            ("ffmpeg_location", "C:/evil/ffmpeg.exe"),
            ("cookiefile", "/root/.secret/cookies.txt"),
            ("cookiesfrombrowser", ("chrome",)),
            ("proxy", "socks5://attacker:1080"),
            ("download_archive", "/anywhere/archive.txt"),
        ]
        for key, value in dangerous:
            options: dict = {}
            with pytest.raises(ValueError):
                apply_extra_options(options, {key: value})
            assert key not in options

    def test_non_string_outtmpl_blocked_even_when_nested_in_safe_batch(self):
        """危险参数夹在安全参数中间：安全的生效，危险的报错。"""
        options: dict = {}
        with pytest.raises(ValueError):
            apply_extra_options(options, {"geo_bypass": True, "exec_cmd": "evil"})
        # 全有或全无：报错时不得写入任何键（防止半应用）。
        assert options == {}


class TestUnsafeToggle:
    def test_unsafe_flag_allows_dangerous_keys(self):
        options: dict = {}
        apply_extra_options(options, {"exec_cmd": "echo hi"}, unsafe_enabled=True)
        assert options["exec_cmd"] == "echo hi"

    def test_unsafe_flag_does_not_allow_unknown_keys(self):
        options: dict = {}
        with pytest.raises(ValueError):
            apply_extra_options(options, {"unknown_key": 1}, unsafe_enabled=True)

    def test_unsafe_sets_are_disjoint(self):
        assert not (SAFE_EXTRA_OPTIONS & UNSAFE_EXTRA_OPTIONS)


class TestDownloaderIntegration:
    def _downloader(self, tmp_path, ytdlp_section):
        from config import ConfigLoader
        from ytdlp.downloader import YtdlpDownloader

        config = ConfigLoader(None)
        config.update(ytdlp=ytdlp_section)
        from storage.file_manager import FileManager

        return YtdlpDownloader(config, FileManager(str(tmp_path)))

    def test_base_options_rejects_dangerous_extra_loudly(self, tmp_path):
        """下载器层同样失败关闭：危险参数直接报错，而不是静默吞掉。"""
        downloader = self._downloader(
            tmp_path,
            {"extra_options": {"geo_bypass": True, "outtmpl": "/evil/%(id)s"}},
        )
        with pytest.raises(ValueError):
            downloader._base_options("iqiyi", None)

    def test_base_options_allows_unsafe_with_toggle(self, tmp_path):
        downloader = self._downloader(
            tmp_path,
            {
                "extra_options": {"exec_cmd": "echo hi"},
                "unsafe_extra_options": True,
            },
        )
        options = downloader._base_options("iqiyi", None)
        assert options["exec_cmd"] == "echo hi"

    def test_base_options_rejects_unknown_extra(self, tmp_path):
        downloader = self._downloader(
            tmp_path,
            {"extra_options": {"nonsense": True}},
        )
        with pytest.raises(ValueError):
            downloader._base_options("iqiyi", None)
