"""Strategy D：res.wx.qq.com JS bundle 兼容补丁框架。

定位（重要）：本模块当前**不登记任何补丁**。页面注入的 fetch/XHR hook
（Strategy B）与运行时包装（Strategy C）是主要捕获方案；只有当 Windows
真机验证证明 B/C 无法稳定获得 feed 时，才针对**已确认的微信版本**登记
最小补丁，且必须同时满足::

    1. 只处理明确识别的视频号 JS bundle（特征检测命中才应用）；
    2. 不修改其它任何 res.wx.qq.com 内容；
    3. patch 失败（异常 / 结构不符）自动 passthrough 原始内容；
    4. 每条补丁附带 regression fixture（tests/fixtures/）与命中诊断；
    5. 在 CHANGELOG 记录对应微信版本号与证据。

禁止事项：不批量堆正则无测试 ``replace()``；不静默复制
ltaoo/wx_channels_download 的改写规则（MIT + Commons Clause，源码不
可并入本 MIT 项目；实现必须独立）。

启用方式：``channels.patch_js_bundles: true`` 且
``channels.intercept_domains`` 含 ``res.wx.qq.com``（有实际证据时才加，
见 channels/domains.py）。默认关闭——未开启时 res.wx.qq.com 连接被
mitmproxy 隧道转发，本模块根本看不到内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from channels.diagnostics import ChannelsDiagnostics

__all__ = [
    "PatchError",
    "JsBundlePatch",
    "PatchRegistry",
]


class PatchError(RuntimeError):
    """补丁应用失败（调用方据此 passthrough 原始内容）。"""


@dataclass(frozen=True)
class JsBundlePatch:
    """一条 JS bundle 补丁的声明。

    ``signature`` 是**必须同时出现**的特征子串（大小写不敏感）；只有全部
    命中才认为「这个 bundle 是我们认识的视频号业务逻辑」，避免误伤
    res.wx.qq.com 上的其它脚本。``apply`` 为纯函数：异常或返回值与输入
    相同都视为未应用。
    """

    name: str
    signature: Tuple[str, ...]
    description: str = ""
    # 微信版本说明（登记补丁时填写，纯诊断用途）。
    target_version: str = ""

    def matches(self, url: str, body: str) -> bool:
        lowered = body.lower()
        return all(sig.lower() in lowered for sig in self.signature)

    def apply(self, body: str) -> str:
        """改写 JS；失败抛 :class:`PatchError`（框架自动 passthrough）。"""
        raise NotImplementedError  # pragma: no cover - 登记补丁时实现


@dataclass
class PatchOutcome:
    """一次补丁处理的结果（诊断与测试断言用）。"""

    patched: bool
    patch_name: str = ""
    reason: str = ""


class PatchRegistry:
    """补丁注册表：特征检测 → 应用 → 失败passthrough，全程可诊断。"""

    def __init__(
        self,
        patches: Optional[List[JsBundlePatch]] = None,
        diagnostics: Optional[ChannelsDiagnostics] = None,
    ) -> None:
        self._patches: List[JsBundlePatch] = list(patches or [])
        self._diagnostics = diagnostics
        # 最近一次命中/未命中的原因（诊断面板展示）。
        self.last_reason: str = ""

    @property
    def empty(self) -> bool:
        return not self._patches

    def register(self, patch: JsBundlePatch) -> None:
        self._patches.append(patch)

    def process(self, url: str, body: str) -> Tuple[str, PatchOutcome]:
        """处理一个 JS bundle；任何失败都返回**原始内容**。

        返回 ``(可能被改写的 body, 结果说明)``。调用方（InjectorAddon）
        只在 ``outcome.patched`` 为 True 时才替换 flow 响应。
        """
        if self.empty:
            self.last_reason = "无已登记补丁（默认状态）"
            return body, PatchOutcome(False, reason=self.last_reason)
        for patch in self._patches:
            if not patch.matches(url, body):
                continue
            try:
                rewritten = patch.apply(body)
            except PatchError as exc:
                self._fail(patch, f"补丁应用失败: {exc}")
                return body, PatchOutcome(False, patch.name, f"应用失败: {exc}")
            except Exception as exc:  # noqa: BLE001 —— 补丁 bug 绝不破坏原 JS
                self._fail(patch, f"补丁异常: {exc}")
                return body, PatchOutcome(False, patch.name, f"补丁异常: {exc}")
            if not rewritten or rewritten == body:
                self._fail(patch, "补丁未产生变化")
                return body, PatchOutcome(False, patch.name, "未产生变化")
            if self._diagnostics is not None:
                self._diagnostics.incr("patch_applied")
            self.last_reason = f"{patch.name} 已应用（{patch.target_version or '版本未标注'}）"
            return rewritten, PatchOutcome(True, patch.name, self.last_reason)
        self.last_reason = "无补丁命中（bundle 特征不匹配）"
        return body, PatchOutcome(False, reason=self.last_reason)

    def _fail(self, patch: JsBundlePatch, reason: str) -> None:
        if self._diagnostics is not None:
            self._diagnostics.incr("patch_failures")
        self.last_reason = f"{patch.name}: {reason}"

    def report(self) -> Dict[str, object]:
        """补丁框架状态（诊断面板 / 测试断言）。"""
        return {
            "registered": [p.name for p in self._patches],
            "last_reason": self.last_reason,
        }
