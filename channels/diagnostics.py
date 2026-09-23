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
    F  parsed_feeds > 0 但下载失败                 CDN / decodeKey / ISAAC64
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

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
    "buttons_created",
    "feed_bridge_requests",
    "feed_bridge_rejected",
    "injected_errors",
    "patch_applied",
    "patch_failures",
    "virtual_asset_requests",
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


class ChannelsDiagnostics:
    """嗅探链路的计数器集合 + 分级评估（单事件循环内使用，无线锁）。"""

    def __init__(self) -> None:
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

    def record_heartbeat(
        self, *, page_type: str = "", page_url: str = "", buttons_created: int = 0
    ) -> None:
        self._counters["frontend_heartbeat"] += 1
        self._last_heartbeat_at = time.time()
        if page_type:
            self._last_page_type = page_type
        if page_url:
            self._last_page_url = page_url[:300]
        if buttons_created > 0:
            # 取最大值：按钮是「当前存在数量」而非累计创建次数。
            self._counters["buttons_created"] = max(
                self._counters["buttons_created"], buttons_created
            )

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
                    "检测到代理流量，但没有命中视频号目标域"
                    "（channels.weixin.qq.com）。请在本机微信里打开「视频号」；"
                    "若实际域名不同，请在 config.yml 的 channels.intercept_domains "
                    "补充（默认仅放行具有实际证据的域名，见 channels/domains.py）。"
                ),
            }
        if stage == "target_domain":
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
                    "下载按钮已运行，但当前微信版本的数据捕获规则失效"
                    "（parsed_feeds = 0）。请播放视频 1–2 秒或切换一次视频；"
                    "仍无捕获请反馈微信版本号与页面类型。"
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
            ("proxy", "代理连接", c["proxy_connections"], "微信流量经过嗅探代理"),
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
            "page_type": self._last_page_type,
            "page_url": self._last_page_url,
            "uptime_seconds": round(time.time() - self._started_at, 1),
        }
