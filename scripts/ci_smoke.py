"""CI 包导入冒烟测试。

用法::

    python scripts/ci_smoke.py                          # 全量模块
    python scripts/ci_smoke.py cli core ytdlp           # 只测已安装依赖能覆盖的模块

退出码非 0 = 有模块导入失败。用于在「依赖没装全」的场景（如 CI 的 core
矩阵只装 [dev]，没有 fastapi / mitmproxy）下按需圈定冒烟范围。
"""

from __future__ import annotations

import importlib
import sys

FULL_LIST = ("cli", "core", "bilibili", "channels", "ytdlp", "server")


def main(argv: list) -> int:
    modules = tuple(argv) if argv else FULL_LIST
    failures = []
    for name in modules:
        try:
            importlib.import_module(name)
            print(f"import {name}: OK")
        except Exception as exc:  # noqa: BLE001 —— 冒烟测试要收集全部失败
            failures.append(name)
            print(f"import {name}: FAIL ({type(exc).__name__}: {exc})", file=sys.stderr)
    if failures:
        print("smoke FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
