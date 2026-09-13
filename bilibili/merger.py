"""DASH 音视频合并。

B 站 ``fnval=4048`` 下发的视频流与音频流是两个独立的 fMP4（``.m4s``）文件，
播放器拿到单个视频流只会静音。合并是纯封装转换（``-c copy``，不重编码），
秒级完成，但要处理三件事：

* Windows 上必须用 ``ProactorEventLoop`` 才能起子进程——``asyncio`` 在
  Python 3.8+ 的 Windows 默认策略即为该事件循环，无需额外处理；
* 无音频轨的稿件（纯音乐 MV、部分搬运）不能调用双输入合并，得退回单输入封装；
* 合并失败必须显式失败，不能留下只有画面没有声音的「成功」产物。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional, Sequence

from utils.logger import setup_logger

logger = setup_logger("BiliMerger")

# 合并是 ``-c copy``，不涉及转码；300s 对几 GB 的封装也绰绰有余，超时基本
# 只可能是 ffmpeg 卡在坏输入上。
MERGE_TIMEOUT_SECONDS = 300

# 输出临时文件的后缀是 ``.part``（见 merge_dash_streams），ffmpeg 无法从它
# 推断封装格式，会报 ``Unable to choose an output format ... Invalid argument``
# 然后整个合并失败。所以按最终文件的后缀显式指定 muxer：m4a 的正确 muxer
# 是 ipod（mp4 muxer 的音频专用变体）。
OUTPUT_FORMAT_BY_SUFFIX = {
    ".mp4": "mp4",
    ".m4a": "ipod",
    ".mkv": "matroska",
    ".flv": "flv",
}


def _output_format(output_path: Path) -> Optional[str]:
    return OUTPUT_FORMAT_BY_SUFFIX.get(output_path.suffix.lower())


class FFmpegMissingError(RuntimeError):
    """找不到可用的 ffmpeg 可执行文件。"""


def resolve_ffmpeg_path(configured: Optional[str] = None) -> str:
    """解析 ffmpeg 路径：显式配置 > 打包内置 > PATH。"""
    candidate = str(configured or "").strip()
    if candidate:
        return candidate
    try:
        from core.ffmpeg import resolve_ffmpeg_path as resolve_bundled

        return resolve_bundled()
    except Exception as exc:  # pragma: no cover — 仅在异常环境触发
        logger.debug("Failed to resolve bundled ffmpeg: %s", exc)
        return ""


async def merge_dash_streams(
    video_path: Path,
    audio_path: Optional[Path],
    output_path: Path,
    *,
    ffmpeg_path: Optional[str] = None,
) -> bool:
    """把视频轨（与可选音频轨）封装成 ``output_path``。

    成功返回 ``True`` 并保证输出文件非空；失败返回 ``False``，由调用方决定
    是丢弃整条作品还是保留单独的视频轨。不删除输入文件——输入通常是共享的
    临时文件，生命周期由调用方管理。
    """
    executable = resolve_ffmpeg_path(ffmpeg_path)
    if not executable:
        raise FFmpegMissingError(
            "ffmpeg not found. Install ffmpeg and put it on PATH, or set "
            "bilibili.ffmpeg_path in config.yml"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_path.with_suffix(output_path.suffix + ".part")
    if tmp_output.exists():
        tmp_output.unlink(missing_ok=True)

    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
    ]
    if audio_path is not None:
        command += ["-i", str(audio_path)]
    command += [
        "-c",
        "copy",
        # 缺 faststart 时 moov 落在文件尾部，边下载边播的体验会变成必须下完
        # 才能拖进度条。
        "-movflags",
        "+faststart",
    ]
    # 输出写成 ``.part`` 临时名，ffmpeg 推断不出封装格式，必须显式指定。
    output_format = _output_format(output_path)
    if output_format:
        command += ["-f", output_format]
    command += [str(tmp_output)]

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=MERGE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await _terminate(process)
        logger.error("ffmpeg merge timed out after %ss for %s", MERGE_TIMEOUT_SECONDS, output_path.name)
        tmp_output.unlink(missing_ok=True)
        return False
    except FileNotFoundError:
        raise FFmpegMissingError(f"ffmpeg executable not found: {executable}")
    except Exception as exc:
        await _terminate(process)
        logger.error("ffmpeg merge failed for %s: %s", output_path.name, exc)
        tmp_output.unlink(missing_ok=True)
        return False

    if process.returncode != 0:
        logger.error(
            "ffmpeg merge exited with %s for %s: %s",
            process.returncode,
            output_path.name,
            _tail(stderr),
        )
        tmp_output.unlink(missing_ok=True)
        return False

    if not tmp_output.exists() or tmp_output.stat().st_size <= 0:
        logger.error("ffmpeg produced an empty file for %s", output_path.name)
        tmp_output.unlink(missing_ok=True)
        return False

    os.replace(str(tmp_output), str(output_path))
    return True


async def _terminate(process: Optional[asyncio.subprocess.Process]) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        await process.wait()
    except Exception as exc:  # pragma: no cover — 防御
        logger.debug("Failed waiting for ffmpeg termination: %s", exc)


def _tail(stderr: Optional[bytes], limit: int = 400) -> str:
    if not stderr:
        return ""
    text = stderr.decode("utf-8", errors="replace").strip()
    return text[-limit:]


async def remux_single_track(
    source_path: Path, output_path: Path, *, ffmpeg_path: Optional[str] = None
) -> bool:
    """只有一条轨时的封装：把 ``.m4s`` 变成标准 ``.mp4``。

    直接改扩展名在大多数播放器上也能播，但索引结构（``moov`` 位置）与
    ``-movflags +faststart`` 的处理不同，统一走一次 ffmpeg 更可控。
    """
    return await merge_dash_streams(
        source_path, None, output_path, ffmpeg_path=ffmpeg_path
    )


def cleanup_temp_files(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Failed to remove temp file %s: %s", path, exc)
