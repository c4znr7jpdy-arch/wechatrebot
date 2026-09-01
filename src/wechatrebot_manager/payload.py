from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import uuid

from .json_io import write_json_atomic


PAYLOAD_MANIFEST = "release-manifest.json"
READY_MANIFEST_STATUS = "ready"
USER_MANAGED_PATHS = {
    "config/brand.json",
    "config/bridge.json",
    "config/manager.json",
}
USER_MANAGED_ROOTS = {"backups", "cache", "data", "logs", "updates"}


class PayloadError(RuntimeError):
    """Raised when a payload cannot be trusted or installed safely."""


@dataclass(frozen=True, slots=True)
class VerifiedPayloadFile:
    relative_path: Path
    source_path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedPayload:
    root: Path
    manifest: dict[str, object]
    files: tuple[VerifiedPayloadFile, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _safe_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError("载荷清单包含空文件路径。")
    if "\\" in value:
        raise PayloadError(f"载荷路径必须使用正斜杠：{value}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise PayloadError(f"载荷路径不能是绝对路径：{value}")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise PayloadError(f"载荷路径包含不安全片段：{value}")
    if any(os.path.isreserved(part) for part in posix.parts):
        raise PayloadError(f"载荷路径包含 Windows 保留名称：{value}")
    return Path(*posix.parts)


def _is_user_managed(relative_path: Path) -> bool:
    normalized = relative_path.as_posix().casefold()
    first = relative_path.parts[0].casefold()
    return normalized in USER_MANAGED_PATHS or first in USER_MANAGED_ROOTS


def verify_payload(
    payload_root: str | Path,
    *,
    allow_incomplete: bool = False,
) -> VerifiedPayload:
    root = Path(payload_root).expanduser().resolve()
    manifest_path = root / PAYLOAD_MANIFEST
    if manifest_path.is_symlink() or (
        hasattr(manifest_path, "is_junction") and manifest_path.is_junction()
    ):
        raise PayloadError("载荷清单不能是符号链接或目录联接。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PayloadError(f"缺少载荷清单：{manifest_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadError(f"载荷清单无法读取：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PayloadError("不支持的载荷清单格式。")
    status = manifest.get("source_manifest_status")
    if status != READY_MANIFEST_STATUS and not allow_incomplete:
        raise PayloadError(f"载荷尚未达到可安装状态：{status!r}")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise PayloadError("载荷清单没有可安装文件。")

    verified: list[VerifiedPayloadFile] = []
    seen: set[str] = set()
    listed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise PayloadError("载荷文件条目格式错误。")
        relative = _safe_relative_path(entry.get("path"))
        key = relative.as_posix().casefold()
        if key in seen:
            raise PayloadError(f"载荷文件路径重复：{relative.as_posix()}")
        if _is_user_managed(relative):
            raise PayloadError(f"载荷不能覆盖用户数据：{relative.as_posix()}")
        expected = entry.get("sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise PayloadError(f"SHA-256 格式错误：{relative.as_posix()}")
        candidate = root / relative
        lineage = [candidate, *candidate.parents]
        for component in lineage:
            if component == root.parent:
                break
            if component.is_symlink() or (
                hasattr(component, "is_junction") and component.is_junction()
            ):
                raise PayloadError(
                    f"载荷文件不能经过符号链接或目录联接：{relative.as_posix()}"
                )
        source = candidate.resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise PayloadError(f"载荷文件越过根目录：{relative.as_posix()}") from exc
        if not source.is_file():
            raise PayloadError(f"载荷文件缺失或不是普通文件：{relative.as_posix()}")
        actual = sha256_file(source)
        if actual.casefold() != expected.casefold():
            raise PayloadError(f"载荷文件校验失败：{relative.as_posix()}")
        seen.add(key)
        listed.add(relative.as_posix())
        verified.append(VerifiedPayloadFile(relative, source, actual))

    payload_paths = tuple(root.rglob("*"))
    for path in payload_paths:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise PayloadError(
                f"载荷不能包含符号链接或目录联接：{path.relative_to(root).as_posix()}"
            )
    extras = {
        path.relative_to(root).as_posix()
        for path in payload_paths
        if path.is_file() and path.name != PAYLOAD_MANIFEST
    } - listed
    if extras:
        preview = "、".join(sorted(extras)[:5])
        raise PayloadError(f"载荷包含未登记文件：{preview}")
    return VerifiedPayload(root, manifest, tuple(verified))


def install_payload(
    payload_root: str | Path,
    install_root: str | Path,
    *,
    allow_incomplete: bool = False,
) -> VerifiedPayload:
    verified = verify_payload(payload_root, allow_incomplete=allow_incomplete)
    root = Path(install_root).expanduser().resolve()
    updates_root = (root / "updates").resolve()
    updates_root.mkdir(parents=True, exist_ok=True)
    try:
        updates_root.relative_to(root)
    except ValueError as exc:
        raise PayloadError("更新暂存目录不在安装根目录内。") from exc

    transaction_id = uuid.uuid4().hex
    stage_root = updates_root / f".install-{transaction_id}"
    backup_root = updates_root / f".rollback-{transaction_id}"
    stage_root.mkdir(parents=True)
    backup_root.mkdir(parents=True)
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for item in verified.files:
            staged = stage_root / item.relative_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.source_path, staged)
            if sha256_file(staged) != item.sha256:
                raise PayloadError(f"暂存文件校验失败：{item.relative_path.as_posix()}")

        for item in verified.files:
            target = (root / item.relative_path).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise PayloadError(
                    f"安装目标越过根目录：{item.relative_path.as_posix()}"
                ) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            backup: Path | None = None
            if target.exists():
                if not target.is_file() or target.is_symlink():
                    raise PayloadError(f"安装目标不是普通文件：{item.relative_path.as_posix()}")
                backup = backup_root / item.relative_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            os.replace(stage_root / item.relative_path, target)
            replaced.append((target, backup))

        write_json_atomic(
            root / "config" / "installed-payload.json",
            {
                "schema_version": 1,
                "source_manifest_status": verified.manifest.get(
                    "source_manifest_status"
                ),
                "files": [
                    {"path": item.relative_path.as_posix(), "sha256": item.sha256}
                    for item in verified.files
                ],
            },
        )
    except Exception:
        for target, backup in reversed(replaced):
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, target)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)
    return verified
