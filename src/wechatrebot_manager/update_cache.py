"""Recover downloaded archives without trusting old plans or extracted files."""
from __future__ import annotations

import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Callable, Iterator

from .json_io import write_json_atomic
from .payload import PayloadError, _checked_target, _copy_stream_and_hash, _valid_digest

if TYPE_CHECKING:
    from .updates import UpdateRelease


UPDATE_CACHE_RECEIPT = "prepared-update.json"
UPDATE_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
UPDATE_CACHE_MAX_SCAN_ENTRIES = 128
UPDATE_CACHE_MAX_COPY_ATTEMPTS = 3


def update_release_id(release: UpdateRelease) -> str:
    """Bind UI confirmation and disk-cache lookup to the same signed package."""
    identity = (
        release.version, release.build, release.package_url,
        release.package_sha256, release.package_size, release.package_kind,
        release.base_fingerprint, release.uncompressed_size,
    )
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


def _read_receipt(working: Path) -> dict[str, object] | None:
    try:
        path = _checked_target(working, Path(UPDATE_CACHE_RECEIPT))
        if not path.is_file():
            return None
        with path.open("rb") as source:
            content = source.read(1025)
        if len(content) > 1024:
            return None
        payload = json.loads(content.decode("utf-8"))
        if (isinstance(payload, dict) and payload.get("schema_version") == 1
                and _valid_digest(payload.get("release_id"))):
            return payload
    except (OSError, PayloadError, UnicodeError, ValueError):
        pass
    return None


def has_update_cache_receipt(working: Path) -> bool:
    """A small receipt extends cleanup retention; it never authorizes installation."""
    return _read_receipt(working) is not None


def _cache_candidates(updates_root: Path) -> Iterator[Path]:
    try:
        with os.scandir(updates_root) as entries:
            for entry in islice(entries, UPDATE_CACHE_MAX_SCAN_ENTRIES):
                if entry.name.startswith(".pending-"):
                    yield Path(entry.path)
    except OSError:
        return


def restore_update_archive(
    updates_root: Path,
    release: UpdateRelease,
    destination: Path,
    *,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> Path | None:
    """Copy an eligible ZIP and hash the copied bytes against current metadata."""
    identity = update_release_id(release)
    cutoff = time.time() - UPDATE_CACHE_MAX_AGE_SECONDS
    attempts = 0
    for candidate in _cache_candidates(updates_root):
        if attempts >= UPDATE_CACHE_MAX_COPY_ATTEMPTS:
            break
        if candidate == destination.parent:
            continue
        try:
            working = _checked_target(updates_root, Path(candidate.name), allow_directory=True)
            if not working.is_dir() or working.stat().st_mtime <= cutoff:
                continue
            receipt = _read_receipt(working)
            if receipt is None or receipt["release_id"] != identity:
                continue
            archive = _checked_target(working, Path("update.zip"))
            if not archive.is_file() or archive.stat().st_size != release.package_size:
                continue
            if progress_callback is not None:
                progress_callback("verify", 0, release.package_size)
            attempts += 1
            with archive.open("rb") as source:
                actual = _copy_stream_and_hash(source, destination, max_bytes=release.package_size)
            if destination.stat().st_size != release.package_size or actual != release.package_sha256:
                destination.unlink(missing_ok=True)
                continue
        except (OSError, PayloadError, ValueError):
            # Missing, damaged, or concurrently cleaned cache entries are only
            # an optimization miss. The normal download remains authoritative.
            continue
        if progress_callback is not None:
            progress_callback("verify", release.package_size, release.package_size)
        return working
    return None


def record_update_archive(
    working: Path, release: UpdateRelease, *, previous: Path | None = None,
) -> None:
    write_json_atomic(working / UPDATE_CACHE_RECEIPT, {
        "schema_version": 1, "release_id": update_release_id(release),
    })
    if previous is not None:
        try:
            # Keep only the new receipt eligible for extended retention. Do not
            # remove any old files that a detached updater may still be using.
            _checked_target(previous, Path(UPDATE_CACHE_RECEIPT)).unlink(missing_ok=True)
        except (OSError, PayloadError):
            pass
