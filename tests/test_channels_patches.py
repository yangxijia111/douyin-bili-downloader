"""Strategy D 兼容补丁框架测试（离线）。

框架铁律：只处理特征命中的 bundle；补丁异常/无变化自动 passthrough；
失败记诊断；当前默认不登记任何补丁。
"""

from __future__ import annotations

from channels.diagnostics import ChannelsDiagnostics
from channels.patches import JsBundlePatch, PatchError, PatchRegistry


class _UpperPatch(JsBundlePatch):
    """测试用补丁：把特征串改写掉（有变化即算应用成功）。"""

    def apply(self, body: str) -> str:
        return body.replace("FINDER_MARKER", "FINDER_PATCHED")


class _AppendPatch(JsBundlePatch):
    """测试用补丁：追加注释（只要有签名就一定有变化）。"""

    def apply(self, body: str) -> str:
        return body + "\n//__cuin_patched"


class _NoopPatch(JsBundlePatch):
    def apply(self, body: str) -> str:
        return body  # 无变化 → 视为未应用


class _BoomPatch(JsBundlePatch):
    def apply(self, body: str) -> str:
        raise PatchError("补丁自身缺陷")


class _RuntimeBoomPatch(JsBundlePatch):
    def apply(self, body: str) -> str:
        raise ValueError("未预期的异常类型")


class TestPatchRegistry:
    def test_empty_registry_passthrough(self):
        registry = PatchRegistry()
        body = "console.log('finder feed logic')"
        out, outcome = registry.process("res.wx.qq.com/x.js", body)
        assert out == body
        assert outcome.patched is False
        assert "无已登记补丁" in registry.last_reason

    def test_signature_match_applies(self):
        diagnostics = ChannelsDiagnostics()
        registry = PatchRegistry([_UpperPatch(name="upper", signature=("FINDER_MARKER",))], diagnostics)
        body = "var a = 'FINDER_MARKER';"
        out, outcome = registry.process("res.wx.qq.com/x.js", body)
        assert out == "var a = 'FINDER_PATCHED';"
        assert outcome.patched is True
        assert outcome.patch_name == "upper"
        assert diagnostics.get("patch_applied") == 1

    def test_signature_mismatch_passthrough(self):
        registry = PatchRegistry([_UpperPatch(name="upper", signature=("FINDER_MARKER",))])
        body = "var a = 1; // 无关脚本"
        out, outcome = registry.process("res.wx.qq.com/other.js", body)
        assert out == body
        assert outcome.patched is False
        assert "特征不匹配" in registry.last_reason

    def test_multi_signature_requires_all(self):
        registry = PatchRegistry(
            [_AppendPatch(name="append", signature=("AAA", "BBB"))]
        )
        out, outcome = registry.process("res.wx.qq.com/x.js", "AAA only")
        assert outcome.patched is False
        out, outcome = registry.process("res.wx.qq.com/x.js", "AAA and BBB")
        assert outcome.patched is True
        assert out.endswith("//__cuin_patched")

    def test_patch_error_passthrough_and_diagnostics(self):
        diagnostics = ChannelsDiagnostics()
        registry = PatchRegistry(
            [_BoomPatch(name="boom", signature=("FINDER_MARKER",))], diagnostics
        )
        body = "FINDER_MARKER"
        out, outcome = registry.process("res.wx.qq.com/x.js", body)
        assert out == body  # 原样放行
        assert outcome.patched is False
        assert diagnostics.get("patch_failures") == 1
        assert "应用失败" in registry.last_reason

    def test_unexpected_exception_passthrough(self):
        diagnostics = ChannelsDiagnostics()
        registry = PatchRegistry(
            [_RuntimeBoomPatch(name="rt", signature=("FINDER_MARKER",))], diagnostics
        )
        body = "FINDER_MARKER"
        out, outcome = registry.process("res.wx.qq.com/x.js", body)
        assert out == body
        assert diagnostics.get("patch_failures") == 1
        assert "补丁异常" in registry.last_reason

    def test_no_change_passthrough(self):
        diagnostics = ChannelsDiagnostics()
        registry = PatchRegistry(
            [_NoopPatch(name="noop", signature=("FINDER_MARKER",))], diagnostics
        )
        body = "FINDER_MARKER"
        out, outcome = registry.process("res.wx.qq.com/x.js", body)
        assert out == body
        assert outcome.patched is False
        assert diagnostics.get("patch_failures") == 1
        assert "未产生变化" in registry.last_reason

    def test_report(self):
        registry = PatchRegistry([_UpperPatch(name="upper", signature=("X",))])
        report = registry.report()
        assert report["registered"] == ["upper"]
        assert "last_reason" in report
