from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
from urllib.parse import unquote, urlsplit
import uuid

from aiohttp import ClientSession, ClientTimeout
from PIL import Image, ImageOps, UnidentifiedImageError


_IMAGE_SUFFIXES = {
    "BMP": ".bmp",
    "GIF": ".gif",
    "JPEG": ".jpg",
    "PNG": ".png",
    "TIFF": ".tiff",
    "WEBP": ".webp",
}
_SILK_SUFFIXES = {".silk", ".slik"}
_MP3_SUFFIXES = {".mp3"}
_AUDIO_SUFFIXES = {".wav", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".bin"}


class MediaError(RuntimeError):
    """Base class for media materialization failures."""


class MediaTooLarge(MediaError):
    """Raised when media exceeds the configured byte limit."""


class UnsupportedMediaSource(MediaError):
    """Raised when a media reference cannot be handled safely."""


@dataclass(frozen=True, slots=True)
class PreparedVideo:
    path: Path
    thumbnail_path: Path
    video_length: int
    thumbnail_length: int
    play_length: int


class MediaStore:
    def __init__(
        self,
        root: Path,
        session: ClientSession,
        max_bytes: int,
        timeout: float = 30.0,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero")
        self.root = root.expanduser().resolve()
        self.session = session
        self.max_bytes = max_bytes
        self.timeout = ClientTimeout(total=timeout)

    def _existing_file(self, path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise UnsupportedMediaSource(f"media file does not exist: {resolved}")
        if resolved.stat().st_size > self.max_bytes:
            raise MediaTooLarge(f"media exceeds {self.max_bytes} bytes")
        return resolved

    def _file_uri_path(self, source: str) -> Path:
        parsed = urlsplit(source)
        raw_path = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]
        return Path(raw_path)

    def _destination(self, suffix: str | None) -> Path:
        cleaned = suffix or ".bin"
        if not cleaned.startswith("."):
            cleaned = f".{cleaned}"
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", cleaned):
            cleaned = ".bin"
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"bridge-{uuid.uuid4().hex}{cleaned.lower()}"

    @staticmethod
    def _image_suffix(content: bytes) -> str | None:
        try:
            with Image.open(BytesIO(content)) as image:
                return _IMAGE_SUFFIXES.get(str(image.format or "").upper())
        except (OSError, ValueError, UnidentifiedImageError):
            return None

    async def materialize(self, source: str, suffix: str | None = None) -> Path:
        value = str(source or "").strip()
        if not value:
            raise UnsupportedMediaSource("media source is empty")
        if value.startswith("base64://"):
            try:
                content = base64.b64decode(value[9:], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise UnsupportedMediaSource("invalid base64 media") from exc
            if len(content) > self.max_bytes:
                raise MediaTooLarge(f"media exceeds {self.max_bytes} bytes")
            destination = self._destination(suffix or self._image_suffix(content))
            destination.write_bytes(content)
            return destination
        if value.startswith("file://"):
            return self._existing_file(self._file_uri_path(value))
        if value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            remote_suffix = suffix or Path(parsed.path).suffix or ".bin"
            destination = self._destination(remote_suffix)
            try:
                async with self.session.get(value, timeout=self.timeout) as response:
                    response.raise_for_status()
                    content_length = response.content_length
                    if content_length is not None and content_length > self.max_bytes:
                        raise MediaTooLarge(f"media exceeds {self.max_bytes} bytes")
                    written = 0
                    with destination.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            written += len(chunk)
                            if written > self.max_bytes:
                                raise MediaTooLarge(
                                    f"media exceeds {self.max_bytes} bytes"
                                )
                            handle.write(chunk)
                return destination
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return self._existing_file(candidate)
        raise UnsupportedMediaSource("media source must be an absolute path or URI")

    @staticmethod
    def _convert_to_gif(source: Path, destination: Path) -> None:
        with Image.open(source) as image:
            frame = ImageOps.exif_transpose(image)
            frame.save(destination, format="GIF", optimize=True)

    @staticmethod
    def _image_format(source: Path) -> str:
        with Image.open(source) as image:
            return str(image.format or "").upper()

    async def prepare_emotion(self, path: Path) -> Path:
        source = self._existing_file(path)
        try:
            image_format = await asyncio.to_thread(self._image_format, source)
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            raise UnsupportedMediaSource(
                f"emotion image format cannot be read: {source}"
            ) from exc
        if image_format == "GIF":
            return source

        destination = self._destination(".gif")
        try:
            await asyncio.to_thread(self._convert_to_gif, source, destination)
            if destination.stat().st_size > self.max_bytes:
                raise MediaTooLarge(f"media exceeds {self.max_bytes} bytes")
            return destination
        except MediaTooLarge:
            destination.unlink(missing_ok=True)
            raise
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            destination.unlink(missing_ok=True)
            raise UnsupportedMediaSource(
                f"emotion image cannot be converted to GIF: {source}"
            ) from exc

    async def prepare_voice(self, path: Path) -> Path:
        """Normalize TTS output for the HOOK voice endpoint.

        `/api/send_mp3_voice` expects an MP3 path.  MiMo voiceclone returns
        WAV, so passing that path directly can make WeChat show a file card.
        SILK/SLIK remains untouched and continues through `/api/send_voice`.
        """
        source = self._existing_file(path)
        suffix = source.suffix.lower()
        if suffix in _SILK_SUFFIXES or suffix in _MP3_SUFFIXES:
            return source
        if suffix == ".bin":
            try:
                header = source.read_bytes()[:12]
            except OSError as exc:
                raise UnsupportedMediaSource(
                    f"voice media cannot be inspected: {source}"
                ) from exc
            if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
                suffix = ".wav"
            else:
                raise UnsupportedMediaSource(
                    f"unknown binary voice format: {source}"
                )
        if suffix not in _AUDIO_SUFFIXES:
            raise UnsupportedMediaSource(
                f"unsupported voice format for MP3 endpoint: {source.suffix or '<none>'}"
            )

        destination = self._destination(".mp3")
        try:
            await self._run_media_tool(
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(destination),
            )
            if not destination.is_file() or destination.stat().st_size <= 0:
                raise UnsupportedMediaSource("voice MP3 output is empty")
            if destination.stat().st_size > self.max_bytes:
                raise MediaTooLarge(f"media exceeds {self.max_bytes} bytes")
            return destination
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    async def _run_media_tool(executable: str, *arguments: str) -> str:
        resolved = shutil.which(executable)
        if not resolved:
            raise UnsupportedMediaSource(f"{executable} is not available on PATH")
        try:
            process = await asyncio.create_subprocess_exec(
                resolved,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except OSError as exc:
            raise UnsupportedMediaSource(f"failed to start {executable}") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise UnsupportedMediaSource(
                f"{executable} failed with code {process.returncode}: {detail}"
            )
        return stdout.decode("utf-8", errors="strict")

    async def prepare_video(self, path: Path) -> PreparedVideo:
        source = self._existing_file(path)
        probe_output = await self._run_media_tool(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(source),
        )
        try:
            duration = float(json.loads(probe_output)["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise UnsupportedMediaSource("video duration cannot be read") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise UnsupportedMediaSource("video duration must be positive")

        thumbnail = self._destination(".jpg")
        try:
            await self._run_media_tool(
                "ffmpeg",
                "-y",
                "-ss",
                "0",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-2",
                "-q:v",
                "3",
                str(thumbnail),
            )
            if not thumbnail.is_file() or thumbnail.stat().st_size <= 0:
                raise UnsupportedMediaSource("video thumbnail is empty")
            thumbnail_length = thumbnail.stat().st_size
            if thumbnail_length > self.max_bytes:
                raise MediaTooLarge(f"media exceeds {self.max_bytes} bytes")
            return PreparedVideo(
                path=source,
                thumbnail_path=thumbnail,
                video_length=source.stat().st_size,
                thumbnail_length=thumbnail_length,
                play_length=max(1, math.ceil(duration)),
            )
        except BaseException:
            thumbnail.unlink(missing_ok=True)
            raise

    def cleanup_expired(
        self, retention_seconds: float, now: float | None = None
    ) -> int:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be greater than zero")
        if not self.root.exists():
            return 0
        current = time.time() if now is None else now
        cutoff = current - retention_seconds
        removed = 0
        for candidate in self.root.iterdir():
            if not candidate.is_file():
                continue
            if not candidate.name.startswith(("bridge-", "inbound-")):
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.root):
                continue
            if candidate.stat().st_mtime <= cutoff:
                candidate.unlink()
                removed += 1
        return removed
