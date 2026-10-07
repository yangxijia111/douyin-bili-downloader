"""视频号直播子进程的网络环境边界（不启动 FFmpeg，不请求真网）。"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from channels import live


async def test_live_record_ignores_inherited_proxies_without_changing_parent(
    tmp_path, monkeypatch
):
    parent_env = {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "https_proxy": "http://127.0.0.1:7890",
        "All_Proxy": "socks5://127.0.0.1:7891",
        "PATH": "existing-path",
        "TEMP": "existing-temp",
        "NO_PROXY": "localhost,127.0.0.1",
        "CHANNELS_HTTP_PROXY_HINT": "keep-this-unrelated-variable",
    }
    before = dict(parent_env)
    # 用普通字典模拟父进程环境，不写 Windows 全局或实际 os.environ。
    monkeypatch.setattr(os, "environ", parent_env)
    process = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(None, b""))
    )
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(live.asyncio, "create_subprocess_exec", launch)
    output = tmp_path / "recordings" / "live.flv"

    assert await live.LiveRecorder(ffmpeg_path="ffmpeg-test").record(
        "https://live.example/stream.flv", output
    ) == 0

    args, kwargs = launch.call_args
    assert args[0] == "ffmpeg-test"
    assert args[args.index("-i") + 1] == "https://live.example/stream.flv"
    child_env = kwargs["env"]
    assert child_env == {
        "PATH": "existing-path",
        "TEMP": "existing-temp",
        "NO_PROXY": "localhost,127.0.0.1",
        "CHANNELS_HTTP_PROXY_HINT": "keep-this-unrelated-variable",
    }
    assert child_env is not parent_env
    assert parent_env == before
    assert os.environ is parent_env
