#!/usr/bin/env python3
"""Private, atomic backups run as the activation database owner; Python 3.10+ stdlib."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid

DEFAULT_DATA_DIR = Path("/var/lib/wechatrebot-activation")
DEFAULT_BACKUP_DIR = Path("/var/backups/wechatrebot-activation")
RETAIN_BACKUPS = 14
DATABASE = "activation.sqlite3"
PRIVATE_KEY = "licensing-private.pem"
CONTACT = "wechat-contact.json"
MANIFEST = "manifest.json"
FORMAT = "wechatrebot-activation-backup-v1"
_NAME = re.compile(r"backup-(\d{8}T\d{12}Z)-[0-9a-f]{12}")
_CONTENT = {DATABASE, PRIVATE_KEY, CONTACT}
_STAGING_FILES = _CONTENT | {MANIFEST, *(DATABASE + suffix for suffix in ("-journal", "-wal", "-shm"))}


class BackupError(RuntimeError):
    """The requested backup could not be completed safely."""


def _checked_stat(path: Path, *, directory: bool = False):
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or reparse:
        raise BackupError("Symbolic links and directory junctions are not allowed")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise BackupError("Expected an ordinary directory or file")
    if not directory and info.st_nlink != 1:
        raise BackupError("Hard-linked backup material is not allowed")
    return info


def _checked_path(value: Path) -> Path:
    path = value.expanduser().absolute()
    if ".." in path.parts or path.parent == path:
        raise BackupError("An explicit, non-root path is required")
    for parent in reversed((path, *path.parents)):
        try:
            _checked_stat(parent, directory=True)
        except FileNotFoundError:
            continue
    return path


@contextmanager
def _regular_file(path: Path, flags: int = os.O_RDONLY):
    # NOFOLLOW closes the last-component race on Linux. Every parent is checked
    # separately, and the backup root is private to this script's user.
    before = _checked_stat(path) if path.exists() or path.is_symlink() else None
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise BackupError("Expected an ordinary, unlinked file")
        if before is not None and (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise BackupError("File changed while being opened")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _backup_lock(root: Path):
    with _regular_file(root / ".backup.lock", os.O_RDWR | os.O_CREAT) as descriptor:
        os.chmod(root / ".backup.lock", 0o600)
        if os.name == "posix":
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            # Windows locking needs a byte; the existing file is never truncated.
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        yield


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _copy_file(source: Path, target: Path) -> None:
    with _regular_file(source) as source_fd, _regular_file(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as target_fd:
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise BackupError("Incomplete backup file write")
                view = view[written:]
        os.fsync(target_fd)
    os.chmod(target, 0o600)


def _file_info(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with _regular_file(path) as descriptor:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def _snapshot_database(source: Path, target: Path) -> None:
    if _checked_stat(source).st_size == 0:
        # SQLite accepts a zero-byte input as a new empty database. It must never
        # displace useful snapshots after a source-file truncation.
        raise BackupError("Source database must not be empty")
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            _checked_stat(Path(str(source) + suffix))
        except FileNotFoundError:
            continue
    with _regular_file(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL):
        pass
    deadline = time.monotonic() + 120

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise BackupError("SQLite online backup timed out")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as origin:
        with closing(sqlite3.connect(target)) as destination:
            origin.backup(destination, pages=128, progress=progress, sleep=0.05)
            # A backup of a WAL database must be independently restorable without
            # a copied WAL/SHM file. Switching the destination does not touch source.
            destination.execute("PRAGMA journal_mode=DELETE").fetchone()
            if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise BackupError("SQLite snapshot integrity check failed")
    os.chmod(target, 0o600)
    with _regular_file(target, os.O_RDWR) as descriptor:
        os.fsync(descriptor)


def _complete_files(root: Path, path: Path) -> set[str] | None:
    """Recognize only our complete, flat, hash-verified snapshots for rotation."""
    match = _NAME.fullmatch(path.name)
    if path.parent != root or match is None:
        return None
    try:
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ")
        _checked_stat(root, directory=True)
        _checked_stat(path, directory=True)
        names = {child.name for child in path.iterdir()}
        if not {DATABASE, PRIVATE_KEY, MANIFEST} <= names or names - (_CONTENT | {MANIFEST}):
            return None
        for name in names:
            _checked_stat(path / name)
        with _regular_file(path / MANIFEST) as descriptor:
            raw = os.read(descriptor, 16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            return None
        manifest = json.loads(raw)
        if (not isinstance(manifest, dict) or set(manifest) != {"format", "id", "files"}
                or manifest["format"] != FORMAT or manifest["id"] != path.name
                or not isinstance(manifest["files"], dict) or set(manifest["files"]) != names - {MANIFEST}):
            return None
        if any(manifest["files"][name] != _file_info(path / name) for name in names - {MANIFEST}):
            return None
        if any(manifest["files"][name]["size"] == 0 for name in (DATABASE, PRIVATE_KEY)):
            return None
        return names
    except (BackupError, OSError, ValueError, RecursionError):
        # Foreign, damaged or linked material is never a rotation target.
        return None


def _remove_flat(root: Path, path: Path, allowed: set[str]) -> None:
    name = path.name[1:-8] if path.name.startswith(".") and path.name.endswith(".partial") else path.name
    if path.parent != root or path == root or not _NAME.fullmatch(name):
        raise BackupError("Cleanup escaped the backup root")
    _checked_path(root)
    _checked_stat(path, directory=True)
    children = list(path.iterdir())
    if any(child.name not in allowed for child in children):
        raise BackupError("Unexpected content prevents backup cleanup")
    for child in children:
        _checked_stat(child)
    for child in children:
        child.unlink()
    path.rmdir()


def _rotate(root: Path, current: Path) -> int:
    if _complete_files(root, current) is None:
        raise BackupError("Published backup failed verification")
    backups = sorted(path for path in root.iterdir() if path != current and _complete_files(root, path) is not None)
    removed = 0
    # Always keep this successful run, including after a server clock correction.
    for path in backups[:-(RETAIN_BACKUPS - 1)]:
        files = _complete_files(root, path)
        if files is None:
            raise BackupError("Backup changed before rotation")
        _remove_flat(root, path, files)
        removed += 1
    return removed


def create_backup(data_dir: Path = DEFAULT_DATA_DIR, backup_dir: Path = DEFAULT_BACKUP_DIR) -> dict:
    data, root = _checked_path(data_dir), _checked_path(backup_dir)
    _checked_stat(data, directory=True)
    if data == root or data in root.parents or root in data.parents:
        raise BackupError("Source and backup directories must be separate")
    source_info = _checked_stat(data / DATABASE)
    if os.name == "posix" and source_info.st_uid != os.geteuid():
        # SQLite mode=ro may still create missing WAL/SHM sidecars. They must
        # remain owned by the service user so the next service start can write.
        raise BackupError("Run the backup as the activation database owner")
    previous_umask = os.umask(0o077)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = _checked_stat(root, directory=True)
        if os.name == "posix" and info.st_uid != os.geteuid():
            raise BackupError("Backup directory must be owned by the current user")
        os.chmod(root, 0o700)
        with _backup_lock(root):
            name = "backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:12]
            pending, completed = root / ("." + name + ".partial"), root / name
            pending.mkdir(mode=0o700)
            published = False
            try:
                _copy_file(data / PRIVATE_KEY, pending / PRIVATE_KEY)
                _snapshot_database(data / DATABASE, pending / DATABASE)
                if (data / CONTACT).exists() or (data / CONTACT).is_symlink():
                    _copy_file(data / CONTACT, pending / CONTACT)
                names = {child.name for child in pending.iterdir()}
                if names - _CONTENT or not {DATABASE, PRIVATE_KEY} <= names:
                    raise BackupError("Unexpected SQLite snapshot files")
                files = {name: _file_info(pending / name) for name in sorted(names)}
                if any(files[name]["size"] == 0 for name in (DATABASE, PRIVATE_KEY)):
                    raise BackupError("Database and private key must not be empty")
                # A key changed during the snapshot cannot be paired reliably.
                if files[PRIVATE_KEY] != _file_info(data / PRIVATE_KEY):
                    raise BackupError("Private key changed while creating the backup")
                manifest = {"format": FORMAT, "id": name, "files": files}
                with _regular_file(pending / MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as descriptor:
                    encoded = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
                    if os.write(descriptor, encoded) != len(encoded):
                        raise BackupError("Incomplete backup manifest write")
                    os.fsync(descriptor)
                os.chmod(pending / MANIFEST, 0o600)
                _sync_directory(pending)
                pending.rename(completed)
                published = True
                _sync_directory(root)
                removed = _rotate(root, completed)
                _sync_directory(root)
            except BaseException:
                # Even a failed rotation must not leave this run labeled complete.
                if published:
                    completed.rename(pending)
                _remove_flat(root, pending, _STAGING_FILES)
                raise
            return {"path": str(completed), "retained_limit": RETAIN_BACKUPS, "removed_backups": removed}
    finally:
        os.umask(previous_umask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    args = parser.parse_args(argv)
    try:
        result = create_backup(args.data_dir, args.backup_dir)
    except (BackupError, OSError, sqlite3.Error, ValueError) as exc:
        # Never log exception messages or the contents of keys, contact or ledger.
        print(json.dumps({"ok": False, "error": {"code": "backup_failed", "type": type(exc).__name__}}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "data": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
