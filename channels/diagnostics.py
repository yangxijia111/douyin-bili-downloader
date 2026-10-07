"""视频号嗅探链路的诊断计数器与分级状态评估。

v2.0.1 的真实缺陷：Windows 微信里「嗅探会话启动、证书/系统代理正常，但始终
捕获不到视频」时，工具只能显示「暂无嗅探结果」——用户与开发者都无法判断
断点在哪一环。本模块把整条链路拆成可观测的计数器与状态阶梯::

    微信没有经过代理                → proxy_connections = 0
    代理正常但没有命中视频号域名     → target_domain_connections = 0
    已命中 channels.weixin.qq.com   → html_pages_seen / candidate_responses
    HTML 已拦截                     → html_pages_seen > 0
    注入脚本已运行                  → injected_pages > 0
    前端已执行（心跳）              → frontend_heartbeat > 0
    按钮已创建                      → buttons_created > 0
    Feed 已获取                     → parsed_feeds > 0
    下载成功                        → 由调用方以 DownloadResult 一并评估

计数器只记录**条数与时间**，不记录任何用户正文（URL / 标题 / decodeKey 均
不落诊断）。所有自增都发生在 mitmproxy 事件循环线程内（与 FeedStore 同一
约束），同步方法、无锁。

分级诊断（:meth:`advice`）覆盖六种典型故障::

    A  proxy_connections = 0                       微信可能没有走系统代理
    B  target_domain_connections = 0               有代理流量但没有视频号域
    C  html_pages_seen > 0 且 injected_pages = 0    页面看到了但注入失败
    D  injected_pages > 0 且 frontend_heartbeat = 0 脚本写入了但没执行（CSP）
    E  frontend_heartbeat > 0 且 parsed_feeds = 0   按钮在跑但数据规则失效
       （v2.0.4：MSE 时代 feed 不走页面流量，此态为常态——MSE 分片自动
        保存会经 record_feeds 推动 parsed_feeds / 「下载成功」计数）
    F  parsed_feeds > 0 但下载失败                 CDN / decodeKey / ISAAC64
"""

from __future__ import annotations

import ipaddress
import math
import re
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from channels.domains import normalize_host

__all__ = [
    "COUNTER_KEYS",
    "ChannelsDiagnostics",
    "STAGE_LABELS",
]

# 规范要求的 11 个计数器（Web/CLI 展示与测试断言都以这份清单为准）。
COUNTER_KEYS: tuple = (
    "proxy_connections",
    "target_domain_connections",
    "tls_intercepted",
    "html_pages_seen",
    "js_bundles_seen",
    "candidate_responses",
    "json_responses",
    "object_desc_responses",
    "parsed_feeds",
    "injected_pages",
    "frontend_heartbeat",
)

# 额外的辅助计数器（定位 Strategy B/C/D 与按钮细节，不喧宾夺主）。
_EXTRA_KEYS: tuple = (
    "connect_requests",
    "tls_client_failures",
    "buttons_created",
    "feed_bridge_requests",
    "feed_bridge_rejected",
    "injected_errors",
    "patch_applied",
    "patch_failures",
    "virtual_asset_requests",
    # MSE 分片旁路（Strategy E，v2.0.4）：页面 appendBuffer 捕获。
    "mse_capture_streams",
    "mse_capture_bytes",
    # 媒体分片 URL 记录（直播回放全量下载的依据，2026-09-25）。
    "media_requests",
    "media_request_bytes",
    # 直播回放全量下载（replay_downloader）：解密拼装成功落盘数。
    "replay_downloads",
)

# 状态阶梯 → 人类可读标签（Web 状态链按此顺序渲染）。
STAGE_LABELS: Dict[str, str] = {
    "no_proxy": "微信没有经过代理",
    "no_target_domain": "代理正常但没有命中视频号域名",
    "target_domain": "已命中 channels.weixin.qq.com",
    "html_seen": "视频号页面已拦截（HTML）",
    "injected": "注入脚本已写入页面",
    "heartbeat": "前端脚本已执行（心跳）",
    "buttons": "页面按钮已创建",
    "feed_captured": "Feed 已获取",
}

_LEVEL_OK = "ok"
_LEVEL_WARN = "warn"
_LEVEL_ERROR = "error"

_JS_SAFE_INTEGER = 9007199254740991
_PROBE_REASON_CODES = frozenset((
    "pending_source_end", "missing_initialization", "truncated", "missing_duration",
    "missing_start", "coverage_gap", "missing_end", "no_video", "complete",
))


def _probe_integer(value: Any, maximum: int = _JS_SAFE_INTEGER) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return min(maximum, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _probe_seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and 0 <= number <= 604800 else None


def _probe_ranges(value: Any) -> List[List[float]]:
    result: List[List[float]] = []
    if not isinstance(value, (list, tuple)):
        return result
    for pair in value[:64]:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = (_probe_seconds(item) for item in pair)
        if start is not None and end is not None and start <= end:
            result.append([start, end])
            if len(result) == 16:
                break
    return result


def _probe_names(value: Any, maximum: int) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        name for name in value[:maximum]
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_.$]{0,63}", name)
    ]


def _probe_mime(value: Any) -> str:
    if isinstance(value, str) and len(value) <= 160 and re.fullmatch(
        r"(?:audio|video)/[a-z0-9.+-]+(?:;[a-z0-9 ._,=\"'-]+)?", value, re.IGNORECASE
    ):
        return value
    return ""


def _probe_reason(value: Any) -> str:
    return value if isinstance(value, str) and value in _PROBE_REASON_CODES else ""


def _clean_probe(probe: Dict[str, Any]) -> Dict[str, Any]:
    page_id = probe.get("page_id")
    visibility = probe.get("visibility")
    result = {
        "page_id": page_id if isinstance(page_id, str) and re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", page_id
        ) else "",
        "visibility": visibility if visibility in ("visible", "hidden", "prerender") else "",
        "hooked": _probe_names(probe.get("hooked"), 16),
        "fetch_hooked": bool(probe.get("fetch_hooked")),
        "xhr_hooked": bool(probe.get("xhr_hooked")),
        "index_size": _probe_integer(probe.get("index_size"), 1000000),
        "view_size": _probe_integer(probe.get("view_size"), 1000000),
        "window_keys": _probe_names(probe.get("window_keys"), 60),
        "video": None,
        "mse": None,
    }
    video = probe.get("video")
    if isinstance(video, dict):
        result["video"] = {
            "current_time": _probe_seconds(video.get("current_time")),
            "duration": _probe_seconds(video.get("duration")),
            "ready_state": _probe_integer(video.get("ready_state"), 4),
            "network_state": _probe_integer(video.get("network_state"), 3),
            "paused": bool(video.get("paused")),
            "ended": bool(video.get("ended")),
            "buffered": _probe_ranges(video.get("buffered")),
            "src_kind": video.get("src_kind") if video.get("src_kind") in (
                "blob", "http", "none"
            ) else "none",
        }
    mse = probe.get("mse")
    if isinstance(mse, dict):
        tracks = []
        values = mse.get("tracks")
        for track in values[:8] if isinstance(values, (list, tuple)) else ():
            if not isinstance(track, dict):
                continue
            tracks.append({
                "mime": _probe_mime(track.get("mime")),
                "bytes": _probe_integer(track.get("bytes")),
                "duration": _probe_seconds(track.get("duration")),
                "ranges": _probe_ranges(track.get("ranges")),
                "truncated": bool(track.get("truncated")),
                "source_finished": bool(track.get("source_finished")),
                "initialized": bool(track.get("initialized")),
                "complete": bool(track.get("complete")),
                "reason_code": _probe_reason(track.get("reason_code")),
            })
        result["mse"] = {
            name: _probe_integer(mse.get(name)) for name in (
                "streams", "total_bytes", "truncated", "current_bytes",
            )
        }
        result["mse"].update({
            "current_mime": _probe_mime(mse.get("current_mime")),
            "complete": bool(mse.get("complete")),
            "reason_code": _probe_reason(mse.get("reason_code")),
            "tracks": tracks,
        })
    return result


class ChannelsDiagnostics:
    """嗅探链路的计数器集合 + 分级评估（单事件循环内使用，无线锁）。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """原地清除上一会话的证据，保持补丁/捕获策略持有的诊断引用有效。"""
        now = time.time()
        self._counters: Dict[str, int] = {
            key: 0 for key in COUNTER_KEYS + _EXTRA_KEYS
        }
        self._started_at: float = now
        self._last_feed_at: Optional[float] = None
        self._last_heartbeat_at: Optional[float] = None
        self._last_parse_error: str = ""
        self._last_page_type: str = ""
        self._last_page_url: str = ""
        # 最近一次前端运行时探针（属性名/计数，无用户数据）。
        self._last_probe: Dict[str, Any] = {}
        self._page_probes: Dict[str, Dict[str, Any]] = {}
        # 最近命中的白名单域响应路径（不含查询串；有界环形缓冲，仅用于
        # 真机诊断「数据到底从哪个接口来」——不记录任何正文/参数）。
        self._candidate_paths: List[str] = []
        # 连接证据只包含主机名与来源，绝不保存路径、查询参数或响应正文。
        self._observed_hosts: List[Dict[str, Any]] = []
        # 媒体分片请求记录（有界环形缓冲）：直播回放全量下载需要分片 URL
        # 规律（单 URL Range / seq 分段），只存头信息与完整 URL。
        self._media_urls: List[Dict[str, Any]] = []
        # 每种捕获策略各自成功入库的条数（A/B/C/D 占比一目了然）。
        self._strategy_stats: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # 自增
    # ------------------------------------------------------------------

    def incr(self, key: str, amount: int = 1) -> None:
        """计数器自增；未知 key 静默忽略（防止手写 key 打爆测试）。"""
        if key in self._counters:
            self._counters[key] += amount

    def record_feeds(self, count: int, *, strategy: str = "") -> None:
        """一次成功入库：更新 parsed_feeds / 时间戳 / 策略分布。"""
        if count <= 0:
            return
        self._counters["parsed_feeds"] += count
        self._last_feed_at = time.time()
        if strategy:
            self._strategy_stats[strategy] = (
                self._strategy_stats.get(strategy, 0) + count
            )

    def record_parse_error(self, reason: str) -> None:
        self._last_parse_error = str(reason)[:300]

    def record_candidate_path(self, path: str) -> None:
        """记录白名单域响应路径（去查询串，有界环形缓冲≤32 条）。

        真机诊断用：当 parsed_feeds 长期为 0 时，这份路径清单能直接看出
        微信的数据到底从哪些接口来（以及是否根本不是 HTTP 响应）。
        """
        from urllib.parse import urlsplit

        clean = urlsplit(str(path) or "").path
        if not clean:
            return
        self._candidate_paths.append(clean[:200])
        if len(self._candidate_paths) > 32:
            self._candidate_paths = self._candidate_paths[-32:]

    def candidate_paths(self) -> List[str]:
        return list(self._candidate_paths)

    def record_host(self, host: str, *, source: str, target: bool = False) -> None:
        """记录最近 32 个主机/来源组合，拒绝把 URL 或请求内容当主机名。"""
        if source not in {"connect", "server", "sni", "http", "tls"}:
            return
        raw = str(host or "").strip()
        if not raw or any(char.isspace() or char in "/?#@" for char in raw):
            return
        clean = normalize_host(raw)
        if clean.startswith("["):
            match = re.fullmatch(r"\[([^]]+)\](?::\d+)?", clean)
            if not match:
                return
            clean = match.group(1)
        if ":" in clean:
            try:
                clean = ipaddress.ip_address(clean).compressed
            except ValueError:
                return
        elif not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", clean):
            return
        for item in self._observed_hosts:
            if item["host"] == clean and item["source"] == source:
                item["count"] += 1
                item["target"] = item["target"] or bool(target)
                return
        self._observed_hosts.append({
            "host": clean, "source": source, "count": 1, "target": bool(target),
        })
        del self._observed_hosts[:-32]

    def record_media_request(
        self,
        *,
        method: str = "",
        status: int = 0,
        range_header: str = "",
        content_range: str = "",
        content_type: str = "",
        content_length: int = 0,
        url: str = "",
    ) -> None:
        """记录一条媒体分片请求（最近 60 条，环形淘汰最老）。

        URL 是全量下载的关键证据（encfilekey/token/分段参数），完整保留
        但单条截断到 1200 字符；其余字段为响应头摘要。失败静默——诊断
        数据绝不影响代理。
        """
        try:
            self._counters["media_requests"] += 1
            self._counters["media_request_bytes"] += max(0, int(content_length or 0))
            self._media_urls.append(
                {
                    "ts": round(time.time(), 3),
                    "method": method[:8],
                    "status": int(status or 0),
                    "range": range_header[:120],
                    "content_range": content_range[:160],
                    "content_type": content_type[:80],
                    "bytes": max(0, int(content_length or 0)),
                    "url": url[:1200],
                }
            )
            del self._media_urls[:-60]
        except Exception:  # noqa: BLE001 —— 诊断记录失败静默
            pass

    def record_heartbeat(
        self, *, page_type: str = "", page_url: str = "", buttons_created: int = 0,
        probe: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._counters["frontend_heartbeat"] += 1
        self._last_heartbeat_at = time.time()
        if page_type:
            self._last_page_type = page_type if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", page_type) else ""
        if page_url:
            try:
                path = urlsplit(page_url).path
            except ValueError:
                path = ""
            self._last_page_url = path[:300] if path.startswith("/") else ""
        buttons_created = _probe_integer(buttons_created, 1000)
        if buttons_created > 0:
            # 取最大值：按钮是「当前存在数量」而非累计创建次数。
            self._counters["buttons_created"] = max(
                self._counters["buttons_created"], buttons_created
            )
        if isinstance(probe, dict):
            self._last_probe = _clean_probe(probe)
        else:
            self._last_probe = {}
        page_id = self._last_probe.get("page_id") or ""
        key = page_id or self._last_page_type + "\n" + self._last_page_url
        self._page_probes.pop(key, None)
        self._page_probes[key] = {
            "page_id": page_id,
            "page_type": self._last_page_type,
            "page_url": self._last_page_url,
            "last_heartbeat_at": self._last_heartbeat_at,
            "probe": self._last_probe,
        }
        while len(self._page_probes) > 16:
            self._page_probes.pop(next(iter(self._page_probes)))

    def _selected_page_probe(self, now: float) -> Optional[Dict[str, Any]]:
        recent = [
            item for item in self._page_probes.values()
            if 0 <= now - item["last_heartbeat_at"] <= 30.0
        ]
        if not recent:
            return None
        return max(recent, key=lambda item: (
            isinstance(item["probe"].get("video"), dict),
            item["probe"].get("visibility") == "visible",
            item["last_heartbeat_at"],
        ))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def last_feed_at(self) -> Optional[float]:
        return self._last_feed_at

    def get(self, key: str) -> int:
        return self._counters.get(key, 0)

    def counters(self) -> Dict[str, int]:
        return dict(self._counters)

    def strategy_stats(self) -> Dict[str, int]:
        return dict(self._strategy_stats)

    def stage(self) -> str:
        """当前链路最深到达的阶段（见 STAGE_LABELS）。"""
        c = self._counters
        if c["proxy_connections"] <= 0:
            return "no_proxy"
        if c["target_domain_connections"] <= 0:
            return "no_target_domain"
        if c["html_pages_seen"] <= 0:
            return "target_domain"
        if c["injected_pages"] <= 0:
            return "html_seen"
        if c["frontend_heartbeat"] <= 0:
            return "injected"
        if c["buttons_created"] <= 0:
            return "heartbeat"
        if c["parsed_feeds"] <= 0:
            return "buttons"
        return "feed_captured"

    def advice(self) -> Dict[str, Any]:
        """分级诊断：level（ok/warn/error）+ 面向用户的处置建议。"""
        stage = self.stage()
        if stage == "no_proxy":
            return {
                "level": _LEVEL_ERROR,
                "stage": stage,
                "message": (
                    "尚未检测到任何经过嗅探代理的连接：微信可能没有走系统代理。"
                    "请确认微信是在启动嗅探**之后**重新打开的（会话前已运行的微信"
                    "不会读到新代理设置），且没有其它 VPN / 加速器覆盖系统代理。"
                ),
            }
        if stage == "no_target_domain":
            return {
                "level": _LEVEL_ERROR,
                "stage": stage,
                "message": (
                    "嗅探代理收到连接，但这些连接也可能来自浏览器或其他应用；"
                    "尚未命中视频号目标域（channels.weixin.qq.com）。请关闭视频号"
                    "窗口后重新打开；若仍无命中，完全退出微信后重开，使其重新读取"
                    "系统代理，并检查其他代理工具是否覆盖了微信的连接。"
                ),
            }
        if stage == "target_domain":
            if self.get("tls_client_failures") > 0 and self.get("tls_intercepted") <= 0:
                return {
                    "level": _LEVEL_ERROR,
                    "stage": stage,
                    "message": (
                        "已命中视频号目标域，但微信与嗅探代理的 TLS 握手失败。"
                        "请检查嗅探证书是否受信任，并在确认后完全退出微信重新打开。"
                    ),
                }
            return {
                "level": _LEVEL_WARN,
                "stage": stage,
                "message": (
                    "已命中视频号域名，但还没有拦截到页面 HTML。请在微信里进入"
                    "视频号首页 / 详情页 / 直播页（等待页面完整加载）。"
                ),
            }
        if stage == "html_seen":
            return {
                "level": _LEVEL_ERROR,
                "stage": stage,
                "message": (
                    "已检测到视频号页面，但注入失败（injected_pages = 0）。"
                    "请查看服务端日志中的注入错误，并在 GitHub Issues 反馈"
                    "（附微信版本号）。"
                ),
            }
        if stage == "injected":
            return {
                "level": _LEVEL_ERROR,
                "stage": stage,
                "message": (
                    "注入脚本已写入 HTML 但没有执行（frontend_heartbeat = 0）："
                    "页面可能被 CSP 拦截，或页面结构变化导致脚本位置失效。"
                    "请查看日志并反馈微信版本号。"
                ),
            }
        if stage == "heartbeat":
            return {
                "level": _LEVEL_WARN,
                "stage": stage,
                "message": (
                    "注入脚本已执行，但尚未创建下载按钮。请在微信里播放视频 1–2 秒"
                    "或切换一次视频；若按钮始终不出现，请反馈微信版本号。"
                ),
            }
        if stage == "buttons":
            return {
                "level": _LEVEL_WARN,
                "stage": stage,
                "message": (
                    "页面按钮已运行，但当前微信版本不再经页面流量下发 feed 数据"
                    "（parsed_feeds = 0，播放走 MSE）。请从开头连续播放，加载结束后经"
                    "MSE 完整性校验并自动保存（channels.mse_auto_save，默认开启；手动"
                    "保存可点页面里的「下载」按钮；提前切换、跳播造成的残缺缓存不会"
                    "标记为下载成功）。"
                ),
            }
        return {
            "level": _LEVEL_OK,
            "stage": stage,
            "message": (
                "Feed 捕获正常。若下载失败，请排查 CDN / decodeKey / ISAAC64 链路。"
            ),
        }

    def chain(self, *, downloads_success: int = 0) -> List[Dict[str, Any]]:
        """状态链（Web 控制台按序渲染；第一个 ok=False 的环节即当前断点）。"""
        c = self._counters
        steps = [
            ("proxy", "代理连接", c["proxy_connections"], "所有应用到嗅探代理的 TCP 连接"),
            ("domain", "目标域命中", c["target_domain_connections"],
             "channels.weixin.qq.com"),
            ("html", "HTML 拦截", c["html_pages_seen"], "视频号页面响应"),
            ("inject", "脚本注入", c["injected_pages"], "bootstrap 写入 <head>"),
            ("heartbeat", "前端心跳", c["frontend_heartbeat"], "注入脚本已执行"),
            ("buttons", "页面按钮", c["buttons_created"], "微信页面内下载按钮"),
            ("feed", "Feed 获取", c["parsed_feeds"], "成功解析的动态数"),
            ("download", "下载成功", downloads_success, "成功落盘数"),
        ]
        chain: List[Dict[str, Any]] = []
        reached = True
        for key, label, value, detail in steps:
            ok = reached and value > 0
            if ok is False:
                reached = False
            chain.append(
                {
                    "key": key,
                    "label": label,
                    "ok": ok,
                    "value": value,
                    "detail": detail,
                    "current": False,
                }
            )
        # current 语义：第一个 ok=False 的环节（全部 ok 时无 current）。
        first_fail = next((i for i, s in enumerate(chain) if not s["ok"]), None)
        if first_fail is not None:
            chain[first_fail]["current"] = True
        return chain

    def snapshot(self, *, downloads_success: int = 0) -> Dict[str, Any]:
        """完整诊断快照（status 端点 / Web 轮询的唯一数据源）。"""
        advice = self.advice()
        selected = self._selected_page_probe(time.time())
        return {
            "counters": self.counters(),
            "strategy_stats": self.strategy_stats(),
            "stage": advice["stage"],
            "stage_label": STAGE_LABELS.get(advice["stage"], advice["stage"]),
            "level": advice["level"],
            "message": advice["message"],
            "chain": self.chain(downloads_success=downloads_success),
            "last_feed_at": self._last_feed_at,
            "last_heartbeat_at": self._last_heartbeat_at,
            "last_parse_error": self._last_parse_error,
            "candidate_paths": self.candidate_paths(),
            "observed_hosts": [dict(item) for item in self._observed_hosts],
            "media_urls": list(self._media_urls),
            "runtime_probe": deepcopy(selected["probe"]) if selected else {},
            "runtime_probe_at": selected["last_heartbeat_at"] if selected else None,
            "page_probes": deepcopy(list(self._page_probes.values())),
            "page_type": selected["page_type"] if selected else self._last_page_type,
            "page_url": selected["page_url"] if selected else self._last_page_url,
            "uptime_seconds": round(time.time() - self._started_at, 1),
        }
