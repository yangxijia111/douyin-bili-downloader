"""视频号页面前端 ↔ Python 后端的同源虚拟接口（``/__cuin/*``）。

注入的 bootstrap 捕获到 Feed 后，需要送到 Python 后端做下载 / 解密 / 落盘。
本模块在 mitmproxy 里**本地响应**这些请求，不转发腾讯服务器::

    GET  /__cuin/assets/channels.js      虚拟静态资源（注入的 bootstrap）
    GET  /__cuin/assets/channels.css     虚拟静态资源（按钮样式）
    POST /__cuin/feed                    页面上传捕获的 feed 节点
    POST /__cuin/heartbeat                前端心跳（页面类型 / 按钮数）
    POST /__cuin/task                     页面按钮触发下载 / 录制
    GET  /__cuin/task/{id}                任务状态轮询

安全边界（每一条都有离线测试）：

* **同源虚拟**：资源与接口都挂在 ``channels.weixin.qq.com`` 源下——无
  CORS、无公网暴露、无额外认证问题；敏感数据（decodeKey / 直链）也**不会**
  经任何腾讯接口外泄；
* **永不转发上游**：``/__cuin/*`` 请求在 mitmproxy ``request`` 钩子里直接
  以本地响应终结，不进腾讯服务器；非 ``/__cuin/*`` 请求完全不受影响；
* **严格校验**：body 大小上限、Content-Type 必须 JSON、Origin/Referer
  必须同源、feed 节点必须是非空 dict 列表且数量有上限；
* **任务不重造轮子**：下载 / 录制复用 :class:`channels.task_hub.
  ChannelsTaskHub`（内部就是 ChannelsDownloader + FeedStore），前端只
  轮询状态，不做任何解密。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from channels.diagnostics import ChannelsDiagnostics
from channels.domains import should_intercept_host
from channels.feed import ChannelFeed
from channels.pipeline import FeedCapturePipeline

__all__ = [
    "VIRTUAL_PREFIX",
    "VIRTUAL_ASSETS_DIR",
    "MAX_FEED_BODY_BYTES",
    "MAX_FEED_NODES",
    "MAX_TASK_BODY_BYTES",
    "VirtualHostAddon",
    "build_feed_bridge_view",
    "validate_feed_payload",
]

VIRTUAL_PREFIX = "/__cuin"
VIRTUAL_ASSETS_DIR = Path(__file__).resolve().parent / "inject" / "assets"

# 页面上传的原始 feed 节点整体不超过 4MB（列表类接口响应量级）。
MAX_FEED_BODY_BYTES = 4 * 1024 * 1024
# 单次上传节点数上限（推荐流一次通常 < 50）。
MAX_FEED_NODES = 200
# 任务请求体极小，4KB 足够。
MAX_TASK_BODY_BYTES = 4 * 1024

_ASSET_CONTENT_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def build_feed_bridge_view(feed: ChannelFeed) -> Dict[str, Any]:
    """桥接响应里返回给页面的**能力视图**（不含直链 / decodeKey）。

    页面只需要知道：这条 feed 能不能下、有哪些画质档、封面可不可用——
    足以驱动按钮菜单，不泄露任何下载敏感字段。
    """
    qualities: List[str] = []
    if feed.kind == "video":
        qualities = ["highest", "lowest"]
        for spec in feed.specs:
            text = str(spec.get("fileFormat") or "")
            if text and text not in qualities:
                qualities.append(text)
    return {
        "feed_id": feed.feed_id,
        "object_id": feed.object_id,
        "kind": feed.kind,
        "title": feed.title,
        "author_name": feed.author_name,
        "duration": feed.duration,
        "file_size": feed.file_size,
        "has_url": bool(feed.url or feed.images),
        "has_decode_key": feed.decode_key is not None,
        "has_cover": bool(feed.cover_url),
        "qualities": qualities,
    }


def validate_feed_payload(
    payload: Any,
) -> Tuple[List[Dict[str, Any]], str, str]:
    """校验 ``/__cuin/feed`` 请求体。

    返回 ``(节点列表, 策略标签, 页面类型)``；不合法抛 ``ValueError``
    （调用方转 400）。节点保持微信原始结构（含 objectDesc），字段提取
    在 Python 侧统一完成。
    """
    if not isinstance(payload, dict):
        raise ValueError("body 必须是 JSON object")
    nodes = payload.get("feeds")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("feeds 必须是非空数组")
    if len(nodes) > MAX_FEED_NODES:
        raise ValueError(f"feeds 节点数超过上限 {MAX_FEED_NODES}")
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("feeds 节点必须是 object")
    strategy = payload.get("strategy")
    if not isinstance(strategy, str) or len(strategy) > 64:
        strategy = ""
    page = payload.get("page")
    if not isinstance(page, str) or len(page) > 32:
        page = ""
    return nodes, strategy, page


class VirtualHostAddon:
    """mitmproxy addon：``/__cuin/*`` 虚拟端点（本地响应，永不转发上游）。"""

    def __init__(
        self,
        pipeline: FeedCapturePipeline,
        diagnostics: Optional[ChannelsDiagnostics] = None,
        *,
        allowed_suffixes: Tuple[str, ...] = ("weixin.qq.com",),
        task_hub: Any = None,
        assets_dir: Optional[Path] = None,
    ) -> None:
        self.pipeline = pipeline
        self.diagnostics = diagnostics or ChannelsDiagnostics()
        self.allowed_suffixes = tuple(allowed_suffixes) or ("weixin.qq.com",)
        self.task_hub = task_hub
        self.assets_dir = assets_dir or VIRTUAL_ASSETS_DIR

    # ------------------------------------------------------------------
    # mitmproxy 入口
    # ------------------------------------------------------------------

    def request(self, flow) -> None:
        try:
            handled = self._handle(flow)
        except Exception as exc:  # noqa: BLE001 —— 虚拟端点异常不影响页面
            self.diagnostics.incr("feed_bridge_rejected")
            self._respond(flow, 500, {"ok": False, "error": f"internal: {exc}"})
            return
        if handled:
            return
        # 非 /__cuin/* ：完全不干预，走正常上游转发。

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def _is_virtual(self, flow) -> bool:
        host = getattr(flow.request, "pretty_host", "") or ""
        if not should_intercept_host(host, self.allowed_suffixes):
            return False
        path = urlsplit(flow.request.path or "").path
        return path == VIRTUAL_PREFIX or path.startswith(VIRTUAL_PREFIX + "/")

    def _handle(self, flow) -> bool:
        if not self._is_virtual(flow):
            return False
        path = urlsplit(flow.request.path or "").path
        method = (flow.request.method or "GET").upper()

        if path.startswith(VIRTUAL_PREFIX + "/assets/"):
            if method not in ("GET", "HEAD"):
                self._respond(flow, 405, {"ok": False, "error": "method not allowed"})
                return True
            self._serve_asset(flow, path)
            return True

        if path == VIRTUAL_PREFIX + "/feed":
            if method != "POST":
                self._respond(flow, 405, {"ok": False, "error": "POST required"})
                return True
            self._handle_feed(flow)
            return True

        if path == VIRTUAL_PREFIX + "/heartbeat":
            if method != "POST":
                self._respond(flow, 405, {"ok": False, "error": "POST required"})
                return True
            self._handle_heartbeat(flow)
            return True

        if path == VIRTUAL_PREFIX + "/task":
            if method != "POST":
                self._respond(flow, 405, {"ok": False, "error": "POST required"})
                return True
            self._handle_task_submit(flow)
            return True

        if path.startswith(VIRTUAL_PREFIX + "/task/"):
            if method not in ("GET", "HEAD"):
                self._respond(flow, 405, {"ok": False, "error": "GET required"})
                return True
            self._handle_task_status(flow, path[len(VIRTUAL_PREFIX + "/task/"):])
            return True

        self._respond(flow, 404, {"ok": False, "error": "unknown __cuin endpoint"})
        return True

    # ------------------------------------------------------------------
    # 虚拟静态资源
    # ------------------------------------------------------------------

    def _serve_asset(self, flow, path: str) -> None:
        self.diagnostics.incr("virtual_asset_requests")
        relative = path[len(VIRTUAL_PREFIX + "/assets/"):]
        # 只允许一层文件名（防路径穿越）。
        if not relative or "/" in relative or "\\" in relative or ".." in relative:
            self._respond(flow, 400, {"ok": False, "error": "bad asset path"})
            return
        suffix = Path(relative).suffix.lower()
        content_type = _ASSET_CONTENT_TYPES.get(suffix)
        if content_type is None:
            self._respond(flow, 404, {"ok": False, "error": "unknown asset type"})
            return
        asset = self.assets_dir / relative
        try:
            body = asset.read_bytes()
        except OSError:
            self._respond(flow, 404, {"ok": False, "error": "asset not found"})
            return
        from mitmproxy.http import Response

        flow.response = Response.make(
            200,
            body,
            {
                "Content-Type": content_type,
                "Cache-Control": "no-store",
                "X-CUIN-Virtual": "asset",
            },
        )

    # ------------------------------------------------------------------
    # Feed 桥接
    # ------------------------------------------------------------------

    def _handle_feed(self, flow) -> None:
        self.diagnostics.incr("feed_bridge_requests")
        payload, error = self._read_json_body(flow, MAX_FEED_BODY_BYTES)
        if error is not None:
            self.diagnostics.incr("feed_bridge_rejected")
            self._respond(flow, 400, {"ok": False, "error": error})
            return
        if not self._same_origin(flow):
            self.diagnostics.incr("feed_bridge_rejected")
            self._respond(flow, 403, {"ok": False, "error": "origin mismatch"})
            return
        try:
            nodes, strategy, page = validate_feed_payload(payload)
        except ValueError as exc:
            self.diagnostics.incr("feed_bridge_rejected")
            self.diagnostics.record_parse_error(f"feed bridge 拒绝: {exc}")
            self._respond(flow, 400, {"ok": False, "error": str(exc)})
            return
        fresh = self.pipeline.ingest_page_nodes(nodes, strategy=strategy, page=page)
        self._respond(
            flow,
            200,
            {
                "ok": True,
                "accepted": len(fresh),
                "feeds": [build_feed_bridge_view(f) for f in fresh],
            },
        )

    def _handle_heartbeat(self, flow) -> None:
        payload, error = self._read_json_body(flow, MAX_TASK_BODY_BYTES)
        if error is not None:
            self.diagnostics.incr("feed_bridge_rejected")
            self._respond(flow, 400, {"ok": False, "error": error})
            return
        body = payload if isinstance(payload, dict) else {}
        page = body.get("page")
        page_url = body.get("url")
        buttons = body.get("buttons")
        probe = body.get("probe")
        self.diagnostics.record_heartbeat(
            page_type=page if isinstance(page, str) else "",
            page_url=page_url if isinstance(page_url, str) else "",
            buttons_created=buttons if isinstance(buttons, int) else 0,
            probe=probe if isinstance(probe, dict) else None,
        )
        self._respond(flow, 200, {"ok": True})

    # ------------------------------------------------------------------
    # 任务（下载 / 录制）
    # ------------------------------------------------------------------

    def _handle_task_submit(self, flow) -> None:
        if self.task_hub is None:
            self._respond(flow, 503, {"ok": False, "error": "task hub unavailable"})
            return
        payload, error = self._read_json_body(flow, MAX_TASK_BODY_BYTES)
        if error is not None:
            self._respond(flow, 400, {"ok": False, "error": error})
            return
        if not self._same_origin(flow):
            self._respond(flow, 403, {"ok": False, "error": "origin mismatch"})
            return
        body = payload if isinstance(payload, dict) else {}
        feed_id = body.get("feed_id")
        action = body.get("action", "download")
        quality = body.get("quality")
        if not isinstance(feed_id, str) or not feed_id or len(feed_id) > 128:
            self._respond(flow, 400, {"ok": False, "error": "feed_id required"})
            return
        if action not in ("download", "record", "stop", "cover"):
            self._respond(flow, 400, {"ok": False, "error": "bad action"})
            return
        if quality is not None and (not isinstance(quality, str) or len(quality) > 32):
            self._respond(flow, 400, {"ok": False, "error": "bad quality"})
            return
        try:
            task_id = self.task_hub.submit(
                feed_id, action=action, quality=quality
            )
        except KeyError:
            self._respond(flow, 404, {"ok": False, "error": "feed not found"})
            return
        except Exception as exc:  # noqa: BLE001 —— 任务提交失败返回可读错误
            self._respond(flow, 400, {"ok": False, "error": str(exc)[:200]})
            return
        self._respond(flow, 200, {"ok": True, "task_id": task_id})

    def _handle_task_status(self, flow, task_id: str) -> None:
        if self.task_hub is None:
            self._respond(flow, 503, {"ok": False, "error": "task hub unavailable"})
            return
        record = self.task_hub.status(task_id)
        if record is None:
            self._respond(flow, 404, {"ok": False, "error": "task not found"})
            return
        self._respond(flow, 200, {"ok": True, "task": record})

    # ------------------------------------------------------------------
    # 请求体 / 同源校验 / 响应
    # ------------------------------------------------------------------

    def _read_json_body(
        self, flow, max_bytes: int
    ) -> Tuple[Optional[Any], Optional[str]]:
        """读取并解析 JSON 请求体；(None, 错误信息) 表示拒绝。"""
        request = flow.request
        try:
            raw = request.get_content()
        except Exception:  # noqa: BLE001 —— 流式/异常体一律拒绝
            return None, "无法读取请求体"
        if raw is None:
            return None, "empty body"
        if len(raw) > max_bytes:
            return None, f"body 超过上限 {max_bytes} 字节"
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" not in content_type:
            return None, "Content-Type 必须为 application/json"
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"JSON 解析失败: {exc}"

    def _same_origin(self, flow) -> bool:
        """Origin / Referer 必须与请求 host 同源（防跨站滥用虚拟接口）。"""
        host = getattr(flow.request, "pretty_host", "") or ""
        headers = flow.request.headers
        origin = headers.get("origin")
        if origin:
            return urlsplit(origin).netloc.split(":")[0].lower() == host.lower()
        referer = headers.get("referer")
        if referer:
            return urlsplit(referer).netloc.split(":")[0].lower() == host.lower()
        return False  # 两者都没有：拒绝（正常页面 POST 必带其一）

    def _respond(self, flow, status: int, payload: Dict[str, Any]) -> None:
        from mitmproxy.http import Response

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        flow.response = Response.make(
            status,
            body,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
                "X-CUIN-Virtual": "1",
            },
        )


def schedule(coro) -> Optional[asyncio.Task]:
    """在有运行循环时调度协程（虚拟钩子是同步的）；否则放弃。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(coro)
