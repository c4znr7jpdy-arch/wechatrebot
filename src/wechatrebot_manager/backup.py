from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tempfile
import uuid
import zipfile

from . import __version__
from .install_paths import INSTALL_MARKER
from .payload import sha256_file


BACKUP_ROOTS = ("config", "data")
MAX_RESTORE_BYTES = 10 * 1024**3
MAX_RESTORE_FILES = 100_000


class BackupError(RuntimeError):
    """Raised when a customer backup cannot be created safely."""


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def backup_source_files(install_root: str | Path) -> tuple[tuple[Path, Path], ...]:
    root = Path(install_root).expanduser().resolve()
    try:
        marker = json.loads((root / INSTALL_MARKER).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise BackupError("安装状态无法读取，不能创建备份。") from exc
    if not isinstance(marker, dict) or marker.get("state") not in {
        "ready",
        "configured",
    }:
        raise BackupError("当前安装未处于可备份状态。")

    files: list[tuple[Path, Path]] = []
    for name in BACKUP_ROOTS:
        source_root = root / name
        if not source_root.exists():
            continue
        if not source_root.is_dir() or _is_link(source_root):
            raise BackupError(f"备份目录不是安全的普通目录：{name}")
        for path in sorted(source_root.rglob("*")):
            if _is_link(path):
                raise BackupError(
                    f"备份数据不能包含符号链接或目录联接：{path.relative_to(root)}"
                )
            if path.is_file():
                files.append((path, path.relative_to(root)))
    return tuple(files)


def create_customer_backup(
    install_root: str | Path,
    destination: str | Path,
) -> dict[str, object]:
    root = Path(install_root).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if target.suffix.casefold() != ".zip":
        raise BackupError("备份文件必须使用 .zip 扩展名。")
    for managed_root in (root / "config", root / "data"):
        try:
            target.relative_to(managed_root)
        except ValueError:
            continue
        raise BackupError("备份文件不能保存在被备份的 config 或 data 目录内。")

    sources = backup_source_files(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".partial", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    manifest_files: list[dict[str, object]] = []
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            strict_timestamps=False,
        ) as archive:
            for source, relative in sources:
                archive.write(source, relative.as_posix())
                manifest_files.append(
                    {
                        "path": relative.as_posix(),
                        "size": source.stat().st_size,
                        "sha256": sha256_file(source),
                    }
                )
            manifest: dict[str, object] = {
                "schema_version": 1,
                "product": "WeChat ReBot",
                "manager_version": __version__,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "contains_sensitive_configuration": True,
                "roots": list(BACKUP_ROOTS),
                "files": manifest_files,
            }
            archive.writestr(
                "backup-manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
        with zipfile.ZipFile(temporary) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise BackupError(f"备份压缩包校验失败：{bad}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema_version": 1,
        "path": str(target),
        "file_count": len(manifest_files),
        "size": target.stat().st_size,
        "sha256": sha256_file(target),
        "contains_sensitive_configuration": True,
    }


def _safe_restore_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise BackupError("备份清单包含无效文件路径。")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
        or any(os.path.isreserved(part) for part in posix.parts)
        or posix.parts[0].casefold() not in BACKUP_ROOTS
    ):
        raise BackupError(f"备份清单包含不安全路径：{value}")
    return Path(*posix.parts)


def _validated_restore_entries(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, object], tuple[tuple[zipfile.ZipInfo, Path, str], ...]]:
    infos = archive.infolist()
    if len(infos) > MAX_RESTORE_FILES + 1:
        raise BackupError("备份文件数量超过安全上限。")
    names: set[str] = set()
    info_by_name: dict[str, zipfile.ZipInfo] = {}
    total_bytes = 0
    for info in infos:
        key = info.filename.casefold()
        if key in names:
            raise BackupError(f"备份包含重复路径：{info.filename}")
        names.add(key)
        if info.is_dir():
            raise BackupError(f"备份包含未登记目录条目：{info.filename}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise BackupError(f"备份不能包含符号链接：{info.filename}")
        total_bytes += info.file_size
        if total_bytes > MAX_RESTORE_BYTES:
            raise BackupError("备份解压后大小超过安全上限。")
        info_by_name[key] = info
    manifest_info = info_by_name.get("backup-manifest.json")
    if manifest_info is None or manifest_info.file_size > 4 * 1024 * 1024:
        raise BackupError("备份清单缺失或过大。")
    try:
        manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("备份清单无法读取。") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("product") != "WeChat ReBot"
        or not isinstance(files, list)
    ):
        raise BackupError("备份清单格式不受支持。")

    entries: list[tuple[zipfile.ZipInfo, Path, str]] = []
    expected_names = {"backup-manifest.json"}
    manifest_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BackupError("备份文件条目格式错误。")
        relative = _safe_restore_path(item.get("path"))
        digest = item.get("sha256")
        size = item.get("size")
        if not isinstance(digest, str) or len(digest) != 64:
            raise BackupError(f"备份 SHA-256 格式错误：{relative.as_posix()}")
        if not isinstance(size, int) or size < 0:
            raise BackupError(f"备份文件大小格式错误：{relative.as_posix()}")
        key = relative.as_posix().casefold()
        if key in manifest_paths:
            raise BackupError(f"备份清单包含重复路径：{relative.as_posix()}")
        manifest_paths.add(key)
        info = info_by_name.get(key)
        if info is None or info.file_size != size:
            raise BackupError(f"备份文件缺失或大小不匹配：{relative.as_posix()}")
        expected_names.add(key)
        entries.append((info, relative, digest.upper()))
    extras = names - expected_names
    if extras:
        raise BackupError(f"备份包含未登记文件：{sorted(extras)[0]}")
    return manifest, tuple(entries)


def restore_customer_backup(
    install_root: str | Path,
    backup_path: str | Path,
) -> dict[str, object]:
    root = Path(install_root).expanduser().resolve()
    source = Path(backup_path).expanduser().resolve()
    if not source.is_file():
        raise BackupError("所选备份文件不存在。")
    for managed_root in (root / "config", root / "data"):
        try:
            source.relative_to(managed_root)
        except ValueError:
            continue
        raise BackupError("恢复文件不能存放在将被恢复的 config 或 data 目录内。")
    backup_source_files(root)

    updates = root / "updates"
    backups = root / "backups"
    updates.mkdir(parents=True, exist_ok=True)
    backups.mkdir(parents=True, exist_ok=True)
    transaction_id = uuid.uuid4().hex
    stage = updates / f".restore-{transaction_id}"
    rollback = updates / f".restore-rollback-{transaction_id}"
    stage.mkdir()
    rollback.mkdir()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    recovery_point = backups / f"pre-restore-{timestamp}.zip"
    replaced: list[tuple[Path, Path | None]] = []
    restored_count = 0
    try:
        with zipfile.ZipFile(source) as archive:
            _, entries = _validated_restore_entries(archive)
            for info, relative, expected in entries:
                staged = stage / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source_handle, staged.open("wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, 1024 * 1024)
                if sha256_file(staged) != expected:
                    raise BackupError(f"备份文件校验失败：{relative.as_posix()}")

        create_customer_backup(root, recovery_point)
        for staged in sorted(path for path in stage.rglob("*") if path.is_file()):
            relative = staged.relative_to(stage)
            target = root / relative
            lineage = [target, *target.parents]
            for component in lineage:
                if component == root.parent:
                    break
                if component.exists() and _is_link(component):
                    raise BackupError(f"恢复目标经过符号链接：{relative.as_posix()}")
            target.parent.mkdir(parents=True, exist_ok=True)
            previous: Path | None = None
            if target.exists():
                if not target.is_file():
                    raise BackupError(f"恢复目标不是普通文件：{relative.as_posix()}")
                previous = rollback / relative
                previous.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, previous)
            os.replace(staged, target)
            replaced.append((target, previous))
            restored_count += 1
    except Exception:
        for target, previous in reversed(replaced):
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                elif previous.exists():
                    os.replace(previous, target)
            except OSError:
                pass
        recovery_point.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(rollback, ignore_errors=True)
    return {
        "schema_version": 1,
        "restored_file_count": restored_count,
        "recovery_point": str(recovery_point),
        "backup_sha256": sha256_file(source),
    }


__all__ = [
    "BACKUP_ROOTS",
    "BackupError",
    "backup_source_files",
    "create_customer_backup",
    "restore_customer_backup",
]
