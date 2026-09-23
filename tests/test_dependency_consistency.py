"""依赖声明一致性测试（P1 可复现性）。

保证三份依赖声明互不漂移：
* ``pyproject.toml``（安装范围声明，yt-dlp 刻意不设上界）
* ``requirements.txt``（与 pyproject 运行时依赖同义）
* ``requirements.lock``（CI pinned 作业的完整快照，须满足 pyproject 约束）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - 3.9/3.10
    tomllib = pytest.importorskip("tomli")

ROOT = Path(__file__).resolve().parent.parent


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _req_name(requirement: str) -> str:
    """``yt-dlp[curl]>=2025.1.1; python_version >= '3.10'`` → ``yt-dlp``。"""
    head = requirement.split(";", 1)[0]
    head = re.split(r"[<>=!~\[ ]", head.strip(), 1)[0]
    return _norm(head)


def _load_pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _load_lock() -> dict:
    """requirements.lock → {规范名: pinned 版本}。"""
    pins = {}
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ;]+)", line)
        assert match, f"requirements.lock 只允许精确 pin，发现: {line!r}"
        pins[_norm(match.group(1))] = match.group(2)
    return pins


def _version_key(version: str):
    parts = []
    for chunk in re.split(r"[.+\-]", version):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts)


def _satisfies(version: str, spec: str) -> bool:
    """极简约束检查，只支持 pyproject 里实际用到的 >= / == / < 。"""
    for clause in re.split(r",", spec):
        clause = clause.strip()
        match = re.fullmatch(r"(>=|==|<)\s*([A-Za-z0-9_.+!-]+)", clause)
        if not match:
            continue
        op, target = match.group(1), match.group(2)
        left, right = _version_key(version), _version_key(target)
        if op == ">=" and not left >= right:
            return False
        if op == "==" and left != right:
            return False
        if op == "<" and not left < right:
            return False
    return True


def test_requirements_txt_covers_pyproject_runtime_deps():
    pyproject = _load_pyproject()
    declared = [_req_name(r) for r in pyproject["project"]["dependencies"]]
    txt = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    txt_names = {_req_name(line) for line in txt.splitlines() if line.strip() and not line.strip().startswith("#")}
    for name in declared:
        assert name in txt_names, f"pyproject 运行时依赖 {name} 未在 requirements.txt 声明"


def test_lock_pins_satisfy_pyproject_constraints():
    pyproject = _load_pyproject()
    pins = _load_lock()
    deps = list(pyproject["project"]["dependencies"])
    for extra in ("channels", "server", "dev"):
        deps.extend(pyproject["project"]["optional-dependencies"].get(extra, ()))
    checked = 0
    for requirement in deps:
        marker = requirement.split(";", 1)
        # lock 是固定解释器（Python 3.13）的快照：带 python_version 标记的
        # 依赖（如 tomli<3.11、mitmproxy>=3.10）是否在快照里取决于快照解释器
        # 而非运行本测试的解释器，此类依赖一律不强制要求出现在 lock 中
        # （出现时仍校验版本约束）。
        if len(marker) == 2 and "python_version" in marker[1]:
            spec = marker[0].strip()
            name = _req_name(requirement)
            constraint = spec[len(name):].replace("(", "").replace(")", "").strip()
            if name in pins and constraint and "*" not in constraint:
                assert _satisfies(pins[name], constraint), (
                    f"lock 中 {name}=={pins[name]} 不满足 pyproject 约束 {constraint}"
                )
                checked += 1
            continue
        spec = marker[0].strip()
        name = _req_name(requirement)
        constraint = spec[len(name):].replace("(", "").replace(")", "").strip()
        if not constraint or "*" in constraint:
            continue
        assert name in pins, f"requirements.lock 缺少 {name}（pyproject 要求 {constraint}）"
        assert _satisfies(pins[name], constraint), (
            f"lock 中 {name}=={pins[name]} 不满足 pyproject 约束 {constraint}"
        )
        checked += 1
    assert checked >= 10, f"约束检查覆盖异常偏少：{checked}"


def test_lock_has_no_self_reference_or_non_pin_lines():
    pins = _load_lock()
    assert _norm("douyin-downloader") not in pins, "lock 不得包含项目自身"


def test_mitmproxy_has_major_upper_bound():
    """mitmproxy 大版本升级曾破坏过嗅探（内部 API 变化），必须带上界。"""
    pyproject = _load_pyproject()
    channels_spec = pyproject["project"]["optional-dependencies"]["channels"]
    assert any("<" in item and "mitmproxy" in item for item in channels_spec), channels_spec


def test_ytdlp_keeps_no_upper_bound():
    """yt-dlp 刻意不设上界：站方解析器要能跟着 pip install -U 升级。"""
    pyproject = _load_pyproject()
    for dep in pyproject["project"]["dependencies"]:
        if _req_name(dep) == "yt-dlp":
            spec = dep.split(";", 1)[0]
            assert "<" not in spec, f"yt-dlp 不应设上界: {dep}"
            return
    raise AssertionError("pyproject 缺少 yt-dlp 依赖")


def test_version_is_consistent_across_surfaces():
    """__init__.__version__ 与 pyproject 版本必须一致（API 显示走 __init__）。"""
    import re as _re

    pyproject = _load_pyproject()
    declared = pyproject["project"]["version"]
    init_text = (ROOT / "__init__.py").read_text(encoding="utf-8")
    match = _re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert match, "__init__.py 缺少 __version__"
    assert match.group(1) == declared, (
        f"__init__.py {match.group(1)} != pyproject {declared}"
    )
    # server.app 的 ImportError 兜底版本也保持同步，避免打包场景显示旧版本。
    app_text = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
    fallback = _re.search(r'_VERSION = "([^"]+)"', app_text)
    assert fallback and fallback.group(1) == declared
