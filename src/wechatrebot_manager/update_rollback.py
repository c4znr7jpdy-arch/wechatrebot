"""Durable update rollback: retain old program files by same-volume rename."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import uuid

from .install_paths import INSTALL_MARKER
from .json_io import write_json_atomic
from .payload import (
    COPY_BACKUP_PATHS, PayloadError, VerifiedPayload, ProgressCallback, _checked_target, _is_user_config,
    _manifest_specs, _replace_file, _valid_digest, payload_relative_paths, payload_removed_paths,
    sha256_file,
)
from .update_errors import UpdateFailure
from .update_manifest import UpdateError


# These control files must remain readable until their atomic replacement.
# In particular, recovery identifies the installation by INSTALL_MARKER.
CONTROL_FILES = frozenset({Path(INSTALL_MARKER), Path("config/installed-payload.json")})


def update_target_paths(payload_root: Path, verified: VerifiedPayload | None = None) -> set[Path]:
    if verified is None:
        paths = set(payload_relative_paths(payload_root))
        paths.update(payload_removed_paths(payload_root))
    else:
        if verified.root != payload_root.resolve():
            raise UpdateError("已校验载荷与回滚来源不匹配。")
        paths = {item.relative_path for item in verified.files}
        paths.update(relative for relative, _digest in _manifest_specs(verified.manifest)[1])
    if Path("manager/WeChatReBot.exe") in paths:
        paths.update({Path("启动机器人.exe"), Path("卸载机器人.exe")})
    paths.update(CONTROL_FILES)
    return paths


def snapshot_update_targets(
    payload_root: Path,
    install_root: Path,
    rollback_root: Path,
    *,
    update_id: str,
    version: str,
    verified_payload: VerifiedPayload | None = None,
    move_existing: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, object]:
    """Plan before mutation; the caller must persist this journal before retain."""
    incoming_hashes = (
        {item.relative_path: item.sha256 for item in verified_payload.files}
        if verified_payload is not None else {}
    )
    (rollback_root / "files").mkdir(parents=True, exist_ok=True)
    snapshots: list[dict[str, object]] = []
    unchanged: dict[str, str] = {}
    paths = update_target_paths(payload_root, verified_payload)
    if progress_callback:
        progress_callback("backup", 0, len(paths))
    for index, relative in enumerate(sorted(paths, key=lambda item: item.as_posix().casefold())):
        if progress_callback:
            progress_callback("backup", index, len(paths))
        target = _checked_target(install_root, relative)
        if _is_user_config(relative) and target.is_file():
            continue
        if target.exists() and not target.is_file():
            raise UpdateError(f"更新目标不是普通文件：{relative.as_posix()}")
        exists = target.is_file()
        digest = sha256_file(target) if exists else ""
        if exists and digest == incoming_hashes.get(relative):
            unchanged[relative.as_posix()] = digest
            continue
        backup_relative = f"files/{index:08X}"
        mode = "copy" if not move_existing or relative in COPY_BACKUP_PATHS else "move"
        if exists and mode == "copy":
            backup = rollback_root / backup_relative
            shutil.copy2(target, backup)
            if sha256_file(backup) != digest:
                raise UpdateError(f"更新控制文件在准备期间已改变：{relative.as_posix()}")
        snapshots.append({
            "path": relative.as_posix(), "existed": exists,
            "backup": backup_relative, "sha256": digest, "backup_mode": mode,
        })
    if progress_callback:
        progress_callback("backup", len(paths), len(paths))
    return {
        "schema_version": 2 if move_existing else 1,
        "update_id": update_id, "version": version,
        "state": "prepared",
        "rollback_root": rollback_root.relative_to(install_root).as_posix(),
        "files": snapshots, "unchanged_files": unchanged,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class UpdateFileBackup:
    """One rollback owner shared by payload installation and generated entries."""

    def __init__(self, journal: dict[str, object], install_root: Path):
        if journal.get("schema_version") not in {1, 2}:
            raise UpdateError("更新回滚记录版本不支持文件移动。")
        self.movable = journal["schema_version"] == 2
        self.root = install_root
        self.rollback_root = _checked_target(
            install_root, Path(str(journal["rollback_root"])), allow_directory=True,
        )
        self.entries = {Path(entry["path"]): entry for entry in journal["files"]}
        self.retained: set[Path] = set()

    def retain(self, target: Path, incoming: Path | None = None) -> None:
        relative = target.relative_to(self.root)
        target = _checked_target(self.root, relative)
        entry = self.entries.get(relative)
        if entry is None or relative in self.retained:
            raise UpdateError(f"更新目标不在待替换记录中：{relative.as_posix()}")
        backup = _checked_target(self.rollback_root, Path(entry["backup"]))
        if entry["existed"]:
            if not target.is_file() or sha256_file(target) != entry["sha256"]:
                raise UpdateError(f"更新目标在准备后已改变：{relative.as_posix()}")
            if entry["backup_mode"] == "move":
                if backup.exists():
                    raise UpdateError(f"更新回滚文件已存在：{relative.as_posix()}")
                # os.replace refuses a cross-volume move; never fall back to a
                # copy/delete operation that leaves an ambiguous crash window.
                _replace_file(target, backup)
        else:
            if target.exists():
                raise UpdateError(f"更新目标在准备后已出现：{relative.as_posix()}")
            # Only new targets need a separate intent. Persist the exact new
            # digest before replacement so recovery cannot delete a later edit.
            if backup.exists():
                raise UpdateError(f"更新创建记录已存在：{relative.as_posix()}")
            if self.movable:
                write_json_atomic(backup, {
                    "path": relative.as_posix(),
                    "sha256": sha256_file(incoming) if incoming else "",
                })
        self.retained.add(relative)


def _restore_entry(entry: dict, rollback_root: Path, install_root: Path, *, movable: bool) -> None:
    relative = Path(*PurePosixPath(str(entry.get("path") or "")).parts)
    target = _checked_target(install_root, relative)
    if entry.get("existed") is not True:
        if not movable:
            target.unlink(missing_ok=True)
            return
        backup = _checked_target(rollback_root, Path(str(entry.get("backup") or "")))
        if not target.exists():
            return
        if not backup.is_file():
            raise OSError(f"新建文件缺少更新创建记录：{relative.as_posix()}")
        try:
            intent = json.loads(backup.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OSError(f"更新创建记录无效：{relative.as_posix()}") from exc
        expected = (
            intent.get("sha256")
            if isinstance(intent, dict) and intent.get("path") == relative.as_posix() else None
        )
        if not _valid_digest(expected) or not target.is_file() or sha256_file(target) != expected:
            raise OSError(f"新建文件在更新后已改变：{relative.as_posix()}")
        target.unlink()
        return
    backup = _checked_target(rollback_root, Path(str(entry.get("backup") or "")))
    original = entry.get("sha256")
    if movable and (not _valid_digest(original) or entry.get("backup_mode") not in {"move", "copy"}):
        raise OSError(f"回滚文件记录无效：{relative.as_posix()}")
    if not backup.is_file():
        if (
            movable and entry["backup_mode"] == "move"
            and target.is_file() and sha256_file(target) == original
        ):
            # Not touched yet, or restored before an interrupted recovery.
            return
        raise OSError(f"回滚备份缺失：{relative.as_posix()}")
    backup_digest = sha256_file(backup)
    if movable and backup_digest != original:
        raise OSError(f"回滚备份校验失败：{relative.as_posix()}")
    if target.is_file() and sha256_file(target) == backup_digest:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if movable and entry["backup_mode"] == "move":
        _replace_file(backup, target)
    else:
        # Keep control files and legacy copies until the whole rollback commits.
        staged = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
        try:
            shutil.copy2(backup, staged)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)


def restore_update_targets(journal: dict[str, object], install_root: Path) -> None:
    rollback_relative = Path(*PurePosixPath(str(journal.get("rollback_root") or "")).parts)
    try:
        rollback_root = _checked_target(install_root, rollback_relative, allow_directory=True)
    except PayloadError as exc:
        raise UpdateFailure("UPD-1010", str(exc)) from exc
    files = journal.get("files")
    if not isinstance(files, list) or journal.get("schema_version", 1) not in {1, 2}:
        raise UpdateFailure("UPD-1010", "回滚清单格式无效。")
    failures: list[str] = []
    for entry in reversed(files):
        try:
            if not isinstance(entry, dict):
                raise OSError("回滚文件条目无效")
            _restore_entry(entry, rollback_root, install_root, movable=journal.get("schema_version") == 2)
        except (OSError, ValueError, PayloadError) as exc:
            failures.append(str(exc))
    if failures:
        raise UpdateFailure("UPD-1010", "；".join(failures[:5]))
