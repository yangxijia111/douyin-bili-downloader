"""Validate captured media and produce a seekable, compatible MP4.

Runs in a worker thread in production. No network protocols are permitted in
ffmpeg inputs: every input is a locally captured file.
"""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path
from typing import List

import imageio_ffmpeg


class MediaValidationError(RuntimeError):
    pass


def _run(args: List[str], timeout: int = 3600):
    try:
        return subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise MediaValidationError("视频校验失败：FFmpeg 不可用或处理超时") from exc


def probe_video_quality(path: Path):
    """Read actual video dimensions from a local file, without transcoding."""
    probed = _run(["-protocol_whitelist", "file,pipe", "-i", str(path)], timeout=30)
    match = re.search(r"Stream #[^\r\n]*Video:[^\r\n]*?\b(\d{2,6})x(\d{2,6})\b", probed.stderr)
    if not match:
        return {}
    width, height = (int(value) for value in match.groups())
    return {"width": width, "height": height, "label": "%dp" % min(width, height)}


def prepare_mp4(paths: List[Path], output: Path, expected_duration: float) -> float:
    """Fully decode each track, check duration, then mux/transcode to MP4.

The browser separately proves continuous buffering from zero to the expected
duration; decoding here prevents valid-looking headers from certifying junk.
"""
    if not math.isfinite(expected_duration) or expected_duration <= 0:
        raise MediaValidationError("无法确认完整时长，请从视频开头播放至缓冲完成后再保存")
    if not paths or len(paths) > 4:
        raise MediaValidationError("音视频轨数量不正确")
    inputs = []
    maps = []
    has_video = False
    has_audio = False
    needs_h264 = False
    measured = 0.0
    for index, path in enumerate(paths):
        probe = _run(["-protocol_whitelist", "file,pipe", "-i", str(path)], timeout=30)
        video = bool(re.search(r"Stream #.*Video:", probe.stderr))
        audio = bool(re.search(r"Stream #.*Audio:", probe.stderr))
        if not video and not audio:
            raise MediaValidationError("分片不是可播放的视频（缺少有效音视频轨）")
        if video and has_video:
            raise MediaValidationError("捕获到多条视频轨，请重新从开头播放")
        if audio and has_audio:
            raise MediaValidationError("捕获到重复音轨，请重新从开头播放")
        # Decode tracks independently: a long video must not hide missing audio.
        for kind, present in (("v", video), ("a", audio)):
            if not present:
                continue
            checked = _run([
                "-v", "error", "-xerror", "-err_detect", "explode",
                "-threads", "2", "-protocol_whitelist", "file,pipe", "-i", str(path),
                "-map", "0:" + kind + ":0", "-progress", "pipe:1", "-f", "null", "-",
            ])
            times = re.findall(r"^out_time_us=(\d+)$", checked.stdout, re.MULTILINE)
            duration = max((int(t) / 1000000 for t in times), default=0)
            if checked.returncode != 0 or duration <= 0:
                raise MediaValidationError("视频分片损坏或不能完整解码，请重新从开头播放")
            if abs(duration - expected_duration) > 0.75:
                raise MediaValidationError(
                    "视频时长不完整：捕获 %.2f 秒 / 目标 %.2f 秒，请继续播放后再保存"
                    % (duration, expected_duration)
                )
            measured = max(measured, duration)
            maps += ["-map", "%d:%s:0" % (index, kind)]
        has_video = has_video or video
        has_audio = has_audio or audio
        if video and not re.search(r"Video: h264\b", probe.stderr):
            needs_h264 = True
        inputs += ["-protocol_whitelist", "file,pipe", "-i", str(path)]
    if not has_video:
        raise MediaValidationError("只捕获到音频，缺少视频轨")
    codec = ["-c:v", "copy"]
    if needs_h264:
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    result = _run([
        "-y", "-v", "error", "-xerror", *inputs, *maps, *codec,
        "-c:a", "aac", "-b:a", "192k", "-threads", "2",
        "-movflags", "+faststart", "-f", "mp4", str(output),
    ])
    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        raise MediaValidationError("音视频合并失败，未登记为下载成功")
    return measured
