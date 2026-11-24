"""Bounded HTTPS downloads shared by application and environment installers."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import urllib.request

from .payload import ProgressCallback, sha256_file
from .update_errors import UpdateFailure
from .update_manifest import UpdateChannel, UpdateError, _safe_https_url


def download_verified(url: str, target: Path, *, size: int, sha256: str,
                      channel: UpdateChannel, progress_callback: ProgressCallback | None = None) -> None:
    _safe_https_url(url)
    if target.is_symlink() or target.is_junction():
        raise UpdateError("下载缓存不能是链接。")
    if target.is_file() and target.stat().st_size == size and sha256_file(target) == sha256:
        if progress_callback:
            progress_callback("download", size, size)
        return
    temporary = target.with_suffix(target.suffix + ".download")
    if temporary.is_symlink() or temporary.is_junction():
        raise UpdateError("下载临时文件不能是链接。")
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"Accept": "application/zip", "User-Agent": "WeChatReBot"})
    try:
        digest, received = hashlib.sha256(), 0
        with urllib.request.urlopen(request, timeout=channel.request_timeout_seconds) as response, temporary.open("wb") as output:
            _safe_https_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != size:
                raise UpdateError("更新包大小与发布清单不一致。")
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > min(channel.max_package_bytes, size):
                    raise UpdateError("更新包下载体积超出发布清单。")
                output.write(chunk)
                digest.update(chunk)
                if progress_callback:
                    progress_callback("download", received, size)
        if received != size:
            raise UpdateError("更新包下载不完整。")
        if digest.hexdigest().upper() != sha256:
            raise UpdateFailure("UPD-1003", "下载文件摘要和签名清单不一致。")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
