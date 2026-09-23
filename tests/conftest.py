"""pytest 全局配置。

Python 3.9 / 3.10 事件循环兼容垫片：

``asyncio.Lock()`` / ``asyncio.Event()`` / ``asyncio.Queue()`` 在 3.9/3.10
**构造时**急切调用 ``get_event_loop()`` 绑定循环，且主线程无当前循环时直接
``RuntimeError``（这一「自动创建」语义自 3.12 起被移除）。仓库里有大量
同步代码路径（下载器工厂、FeedStore 等）在构造时创建这些原语——在
asyncio 运行环境中没有问题，但在 pytest 的同步测试里，前一个异步测试
结束后循环被清掉，后续构造就会炸（CI py3.9/3.10 双平台实测）。

此垫片为每个同步测试保证「存在已设置的事件循环」，恢复 3.12 之前主线程
的默认语义；创建的循环在会话内保持开启，避免跨测试复用的单例（如
FfmpegLocator）把锁绑定到已关闭的循环。asyncio 模式的测试仍由
pytest-asyncio 提供自己的循环，不受影响。3.11+ 上为空操作。
"""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture(autouse=True)
def _default_event_loop_for_legacy_pythons(request):
    if sys.version_info >= (3, 11):
        yield
        return
    if "asyncio" in request.keywords:  # pytest-asyncio 管理的异步测试自建循环
        yield
        return
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield
