"""FFmpeg based preprocessing helpers for video understanding."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class VideoProcessingError(RuntimeError):
    """Raised when ffprobe/ffmpeg cannot process a video."""


@dataclass(slots=True)
class VideoInfo:
    duration: float
    size_bytes: int
    width: int
    height: int
    has_audio: bool


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


async def _run_process(*args: str, timeout: float) -> tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_creation_flags(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise VideoProcessingError(f"命令执行超时: {args[0]}") from None
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise VideoProcessingError(detail[-1200:] or f"{args[0]} 执行失败")
    return stdout, stderr


async def probe_video(
    video_path: str | Path,
    *,
    ffprobe_path: str = "ffprobe",
    timeout: float = 20,
) -> VideoInfo:
    path = Path(video_path)
    if not path.is_file():
        raise VideoProcessingError("视频文件不存在")
    stdout, _ = await _run_process(
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=codec_type,width,height",
        "-of",
        "json",
        str(path),
        timeout=timeout,
    )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        streams = payload.get("streams") or []
        fmt = payload.get("format") or {}
        video_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            {},
        )
        duration = float(fmt.get("duration") or 0)
        size_bytes = int(fmt.get("size") or path.stat().st_size)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoProcessingError("无法读取视频元数据") from exc
    if duration <= 0 or not video_stream:
        raise VideoProcessingError("文件中没有可解析的视频流")
    return VideoInfo(
        duration=duration,
        size_bytes=size_bytes,
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
    )


def frame_timestamps(duration: float, frame_count: int) -> list[float]:
    """Return approximate timestamps matching ffmpeg's uniform FPS extraction."""
    if duration <= 0 or frame_count <= 0:
        return []
    actual_count = max(1, min(frame_count, int(math.ceil(duration))))
    interval = duration / actual_count
    return [min(duration, index * interval) for index in range(actual_count)]


async def extract_frames(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    duration: float,
    frame_count: int = 10,
    max_width: int = 1280,
    ffmpeg_path: str = "ffmpeg",
    timeout: float = 90,
) -> list[Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for old_frame in output.glob("frame_*.jpg"):
        old_frame.unlink(missing_ok=True)

    timestamps = frame_timestamps(duration, frame_count)
    interval = max(duration / max(len(timestamps), 1), 0.1)
    filter_expr = f"fps=1/{interval:.6f},scale='min({max_width},iw)':-2"
    await _run_process(
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        filter_expr,
        "-frames:v",
        str(len(timestamps)),
        "-q:v",
        "3",
        str(output / "frame_%03d.jpg"),
        timeout=timeout,
    )
    frames = sorted(output.glob("frame_*.jpg"))
    if not frames:
        raise VideoProcessingError("未能从视频中提取画面")
    return frames


async def extract_audio(
    video_path: str | Path,
    output_path: str | Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    timeout: float = 90,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    await _run_process(
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output),
        timeout=timeout,
    )
    if not output.is_file() or output.stat().st_size <= 44:
        raise VideoProcessingError("视频音轨为空")
    return output


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"
