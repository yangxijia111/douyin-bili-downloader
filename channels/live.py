"""视频号实时直播录制（ffmpeg 拉流）。

直播是**不加密**的 FLV 流（``liveInfo.streamUrl``），处理方式与 wx_channels_
download 一致：ffmpeg 直接拉流封装，``-c copy`` 不转码。直播回放则是普通的
加密 MP4（mediaType=4），走 :mod:`channels.downloader` 的视频链路。

录制随直播持续，没有固定时长；调用方（CLI 会话 / Server 任务）通过取消
asyncio 任务停止录制，ffmpeg 收到 terminate 后写出文件尾部。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from core.ffmpeg import resolve_ffmpeg_path
from utils.logger import setup_logger

logger = setup_logger("ChannelsLiveRecorder")

__all__ = ["LiveRecordError", "LiveRecorder"]


class LiveRecordError(RuntimeError):
    pass


class LiveRecorder:
    def __init__(self, *, ffmpeg_path: str = ""):
        self.ffmpeg_path = ffmpeg_path or resolve_ffmpeg_path()
        if not self.ffmpeg_path:
            raise LiveRecordError(
                "未找到 ffmpeg：直播录制需要 ffmpeg（普通视频下载不需要）。"
                "请安装 ffmpeg 并加入 PATH。"
            )

    async def record(self, url: str, output_path: Path) -> int:
        """拉流录制到 ``output_path``，返回 ffmpeg 退出码。

        正常停止（直播结束 / 任务取消）返回 0 或 233（ffmpeg 被终止）。
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # -flv_global_header 让本地播放器直接可放；-copyts 不用，保持默认时间轴。
        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            url,
            "-c",
            "copy",
            "-y",
            str(output_path),
        ]
        logger.info("开始录制直播: %s", output_path.name)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await proc.communicate()
        except asyncio.CancelledError:
            # 取消录制：优雅终止（给 ffmpeg 写文件尾的时间），再强杀兜底。
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover - ffmpeg 卡死
                proc.kill()
                await proc.wait()
            logger.info("录制已手动停止: %s", output_path.name)
            raise
        finally:
            if proc.returncode is None:  # pragma: no cover - 防御
                proc.kill()
                await proc.wait()
        if proc.returncode != 0 and not output_path.exists():
            detail = (stderr or b"").decode("utf-8", errors="ignore")[-500:]
            raise LiveRecordError(f"ffmpeg 录制失败（exit {proc.returncode}）: {detail}")
        logger.info("录制结束: %s", output_path.name)
        return proc.returncode or 0


async def suggest_live_output_path(save_dir: Path, feed_title: str) -> Path:
    """直播录制文件的落盘路径（``live_时间戳.flv``，标题进目录名）。"""
    # 本地时间仅用于文件名，时区语义无意义。
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return save_dir / f"live_{stamp}.flv"
