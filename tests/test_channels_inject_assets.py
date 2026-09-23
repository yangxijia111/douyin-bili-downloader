"""页面注入包（channels/inject/assets）一致性与安全性测试。

保证 Python 侧与前端资源的契约不漂移：

* 六个资源文件存在且非空；
* 注入器引用的虚拟路径与 assets 目录一一对应；
* bootstrap 的桥接端点常量与 Python virtual_host 一致；
* 前端策略标签与 pipeline 白名单一致；
* 页面类型覆盖与 injector 的路径正则一致；
* JS 不含外部 URL（不引入第三方依赖 / 不外泄数据）；
* node 可用时对每个 JS 做语法检查（CI 双平台预装 node）。
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from channels.inject import ASSET_FILES, ASSETS_DIR
from channels.injector import (
    CHANNELS_PAGE_PATH_RE,
    VIRTUAL_CORE_JS_PATH,
    VIRTUAL_CSS_PATH,
    VIRTUAL_JS_PATH,
)
from channels.pipeline import ALLOWED_PAGE_STRATEGIES
from channels.virtual_host import VIRTUAL_ASSETS_DIR, VIRTUAL_PREFIX

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node 不可用（CI 双平台预装）")


def _read(name: str) -> str:
    return (ASSETS_DIR / name).read_text(encoding="utf-8")


class TestAssetsExist:
    def test_all_files_present_and_non_empty(self):
        for name in ASSET_FILES:
            path = ASSETS_DIR / name
            assert path.exists(), f"缺少注入资源 {name}"
            assert path.stat().st_size > 0, f"注入资源为空 {name}"

    def test_injector_paths_map_to_assets(self):
        for virtual_path in (VIRTUAL_CSS_PATH, VIRTUAL_CORE_JS_PATH, VIRTUAL_JS_PATH):
            name = virtual_path.rsplit("/", 1)[-1]
            assert (ASSETS_DIR / name).exists(), f"{virtual_path} 没有对应资源文件"

    def test_virtual_host_assets_dir_matches(self):
        assert VIRTUAL_ASSETS_DIR == ASSETS_DIR


class TestBootstrapContract:
    def test_bridge_endpoints_match_python(self):
        js = _read("bootstrap.js")
        assert f'"{VIRTUAL_PREFIX}/feed"' in js
        assert f'"{VIRTUAL_PREFIX}/heartbeat"' in js
        assert f'"{VIRTUAL_PREFIX}/task"' in js

    def test_idempotency_guard(self):
        js = _read("bootstrap.js")
        assert "window.__CUIN__" in js
        # 重复执行直接返回（页面刷新 / 重复注入场景）。
        assert re.search(r"if \(window\.__CUIN__[^)]*\)\s*return", js)

    def test_core_loaded_before_bootstrap(self):
        """cuin_core.js 必须先于 bootstrap.js（注入顺序在 injector 里保证）。"""
        js = _read("bootstrap.js")
        assert "window.CUIN_CORE" in js

    def test_no_external_urls(self):
        """注入 JS 不得引用任何外部 URL（同源虚拟资源除外）。"""
        for name in ASSET_FILES:
            if not name.endswith(".js"):
                continue
            js = _read(name)
            # 允许 /__cuin/ 同源路径；禁止 http(s) 绝对地址。
            external = re.findall(r"""["'(]https?://[^"')]+""", js)
            assert not external, f"{name} 引用了外部 URL: {external}"

    def test_page_type_coverage_aligned(self):
        """JS detectPageType 与 Python 路径正则覆盖同一组页面。"""
        core = _read("cuin_core.js")
        for page in ("home", "feed", "live", "profile"):
            assert CHANNELS_PAGE_PATH_RE.match(f"/web/pages/{page}")
            assert page in core

    def test_strategy_labels_aligned(self):
        js = _read("bootstrap.js")
        for strategy in ALLOWED_PAGE_STRATEGIES:
            assert strategy in js, f"bootstrap 缺少策略标签 {strategy}"

    def test_module_base_dir_has_virtual_fallback(self):
        """页面模块基址必须兜底到 /__cuin/assets/（currentScript 在异步
        加载时为 null，没有兜底会解析成页面相对路径）。"""
        js = _read("bootstrap.js")
        assert '"/__cuin/assets/"' in js
        assert "document.currentScript" in js
        # 不再有依赖执行期后调用 currentScript 的旧函数。
        assert "function baseDir" not in js

    def test_module_files_referenced(self):
        js = _read("bootstrap.js")
        for module in ("channels_home.js", "channels_feed.js", "channels_live.js"):
            assert module in js
            assert (ASSETS_DIR / module).exists()

    def test_css_has_button_classes(self):
        css = _read("channels.css")
        for cls in ("__cuin-btn", "__cuin-menu", "__cuin-toast", "__cuin-float"):
            assert cls in css

    def test_hook_factories_exported(self):
        core = _read("cuin_core.js")
        for factory in ("installFetchHook", "installXhrHook", "installRuntimeHooks"):
            assert factory in core

    def test_runtime_hook_names_present(self):
        core = _read("cuin_core.js")
        for name in (
            "finderPcFlow",
            "finderGetRecommend",
            "finderUserPage",
            "finderGetCommentDetail",
            "finderLiveUserPage",
            "joinLive",
        ):
            assert name in core, f"运行时 hook 名单缺少 {name}"

    def test_button_states_covered(self):
        """按钮必须覆盖：未识别提示 / 菜单 / 忙态 / 完成 / 失败。"""
        js = _read("bootstrap.js")
        assert "正在获取当前视频信息" in js
        assert "尚未识别当前视频" in js
        assert "准备中" in js
        assert "已完成" in js
        assert "失败" in js


@requires_node
class TestJsSyntax:
    @pytest.mark.parametrize("name", [n for n in ASSET_FILES if n.endswith(".js")])
    def test_node_syntax_check(self, name):
        proc = subprocess.run(
            [NODE, "--check", str(ASSETS_DIR / name)],
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="ignore")
