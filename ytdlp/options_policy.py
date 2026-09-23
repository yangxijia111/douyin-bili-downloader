"""``ytdlp.extra_options`` 的白名单分级策略（P1 安全边界）。

``extra_options`` 原实现是裸 ``options.update(extra)``：任何能拿到配置
（尤其是 Web API 的 ``overrides.ytdlp``）的调用方都等价于可以直接传
``outtmpl``（写任意路径）、``exec_cmd``（执行任意命令）、``external_downloader``
（调用任意本机可执行文件）、``cookiefile`` / ``proxy``（改变凭据与流量走向）。

分级（默认只放行第一级）：

* **SAFE_EXTRA_OPTIONS** —— 只影响网络行为与传输性能、无本地副作用；
* **UNSAFE_EXTRA_OPTIONS** —— 能写盘 / 执行命令 / 改下载目标 / 触碰凭据，
  只有显式 ``ytdlp.unsafe_extra_options: true`` 才放行；
* 未知键一律 ``ValueError`` 拒绝（失败关闭，不放任上游新增的危险参数）。

应用语义为**全有或全无**：批次里只要有一个被拒，整个批次都不生效，
避免「安全的一半被应用、危险的一半报错」造成的半应用状态。
"""

from __future__ import annotations

from typing import Dict, MutableMapping

__all__ = [
    "SAFE_EXTRA_OPTIONS",
    "UNSAFE_EXTRA_OPTIONS",
    "UNSAFE_TOGGLE_KEY",
    "apply_extra_options",
]

# 确认无本地副作用的参数：网络出口 / 超时 / 限速 / 分片并发 / 重试 /
# 播放列表切片范围。逐项依据 yt-dlp embedded API 语义。
SAFE_EXTRA_OPTIONS = frozenset(
    {
        # 网络出口与协议行为
        "geo_bypass",
        "geo_bypass_country",
        "geo_bypass_ip_block",
        "http_headers",
        "force_ipv4",
        "force_ipv6",
        "source_address",
        "socket_timeout",
        # 限速与并发
        "ratelimit",
        "limit_rate",
        "concurrent_fragment_downloads",
        "sleep_interval_requests",
        "min_sleep_interval",
        "max_sleep_interval",
        # 重试与续传
        "retries",
        "fragment_retries",
        "skip_unavailable_fragments",
        "continuedl",
        "nopart",
        "buffersize",
        "noresizebuffer",
        # 列表范围（条数裁剪语义，无副作用）
        "playliststart",
        "playlistend",
        "playlist_items",
    }
)

# 已知危险的参数：可写任意文件、执行命令、调用本机可执行文件、
# 触碰凭据或改写流量出口。unsafe 开关下才放行（未知键始终拒绝）。
UNSAFE_EXTRA_OPTIONS = frozenset(
    {
        "outtmpl",              # 任意路径写文件
        "outtmpl_default",
        "exec_cmd",             # 下载后执行任意命令
        "exec_before_dl_cmd",
        "postprocessors",       # ExecPP / ExtractAudio 等可携带命令
        "external_downloader",  # 任意本机可执行文件
        "external_downloader_args",
        "ffmpeg_location",
        "cookiefile",           # 读取任意路径作为 Cookie 源
        "cookiesfrombrowser",   # 直接读浏览器 Cookie 存储
        "proxy",                # 覆盖流量出口
        "geo_verification_proxy",
        "download_archive",     # 任意路径读写
        "cache_dir",
        # 凭据入配置
        "username",
        "password",
        "twofactor",
        "ap_mso",
        "ap_username",
        "ap_password",
        # 文件名策略（本项目已用受控 outtmpl，这些会干扰产物判定）
        "usetitle",
        "writedescription",
        "writeannotations",
        "writethumbnail",
        "writesubtitles",
        "writeinfojson",
        "write_link_json",
    }
)

# unsafe 开关的配置键（ytdlp 段内，默认 false）。
UNSAFE_TOGGLE_KEY = "unsafe_extra_options"


class UnsafeExtraOptionError(ValueError):
    """extra_options 含未放行参数。"""


def apply_extra_options(
    options: MutableMapping,
    extra: Dict,
    *,
    unsafe_enabled: bool = False,
) -> None:
    """把白名单内的 extra_options 合入 yt-dlp 选项（全有或全无）。

    抛出 :class:`UnsafeExtraOptionError`（``ValueError`` 子类）时 ``options``
    保持原样。 unsafe=True 也只放行已知的危险键——未知键仍然拒绝，避免
    yt-dlp 上游新增危险参数被静默放行。
    """
    if not isinstance(extra, dict) or not extra:
        return
    rejected = []
    for key in extra:
        if key in SAFE_EXTRA_OPTIONS:
            continue
        if key in UNSAFE_EXTRA_OPTIONS and unsafe_enabled:
            continue
        rejected.append(key)
    if rejected:
        raise UnsafeExtraOptionError(
            "ytdlp.extra_options 含未放行的 yt-dlp 参数: "
            + ", ".join(sorted(rejected))
            + (
                "（危险参数需在 config.yml 显式设置 ytdlp.unsafe_extra_options: true，"
                "未知参数始终拒绝）"
                if not unsafe_enabled
                else "（未知参数始终拒绝）"
            )
        )
    options.update(extra)
