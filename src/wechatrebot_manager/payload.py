from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import time
from typing import BinaryIO, Callable
import uuid
import zipfile

from .install_paths import validate_install_path
from .json_io import write_json_atomic


PAYLOAD_MANIFEST = "release-manifest.json"
PAYLOAD_ARCHIVE = "payload.wrb"
# Versions through 0.8.3 refuse an otherwise valid update payload unless this
# legacy marker is present. New readers intentionally do not use it as a
# completeness/security decision, but publishers keep emitting it so installed
# legacy managers can bootstrap to a fixed release.
LEGACY_READY_MANIFEST_STATUS = "ready"
USER_MANAGED_PATHS = {
    "config/brand.json",
    "config/bridge.json",
    "config/manager.json",
}
USER_MANAGED_ROOTS = {"backups", "cache", "data", "logs", "updates"}
ProgressCallback = Callable[[str, int, int], None]


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
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise PayloadError(f"载荷路径包含不安全片段：{value}")
    if any(os.path.isreserved(part) for part in posix.parts):
        raise PayloadError(f"载荷路径包含 Windows 保留名称：{value}")
    return Path(*posix.parts)


def _is_user_managed(relative_path: Path) -> bool:
    normalized = relative_path.as_posix().casefold()
    first = relative_path.parts[0].casefold()
    return normalized in USER_MANAGED_PATHS or first in USER_MANAGED_ROOTS


def _is_user_config(relative_path: Path) -> bool:
    """Return whether an existing configuration file belongs to the user."""
    parts = tuple(part.casefold() for part in relative_path.parts)
    if not parts:
        return False
    if parts[0] == "config":
        return len(parts) > 1 and parts[1] not in {
            "installed-payload.json",
            "templates",
        }
    if parts == ("app", "astrbot", "data", "cmd_config.json"):
        return True
    return len(parts) >= 4 and parts[:4] == (
        "app",
        "astrbot",
        "data",
        "config",
    )


def _is_runtime_user_data(relative_path: Path) -> bool:
    parts = tuple(part.casefold() for part in relative_path.parts)
    return parts[:3] == ("app", "astrbot", "data") and (
        len(parts) <= 3 or parts[3] not in {"plugins", "dist"}
    )


def validate_incremental_path(relative_path: str | Path) -> Path:
    """Validate a managed program file; incremental updates never own user data."""
    relative = _safe_relative_path(
        relative_path.as_posix() if isinstance(relative_path, Path) else relative_path
    )
    if (
        _is_user_managed(relative)
        or _is_user_config(relative)
        or _is_runtime_user_data(relative)
        or relative.as_posix().casefold() == "config/installed-payload.json"
    ):
        raise PayloadError(f"增量更新不能修改用户配置、用户数据或安装清单：{relative.as_posix()}")
    if relative.parts[0].casefold() == "runtime" or (
        relative.suffix.casefold() in {".exe", ".dll", ".pyd"}
        and not (relative.parts[0].casefold() == "manager" and relative.suffix.casefold() == ".exe")
    ):
        raise PayloadError(f"运行环境和本机二进制需要完整更新：{relative.as_posix()}")
    return relative


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )


def _fingerprint_manifest(manifest: dict[str, object]) -> str:
    entries = []
    for entry in manifest["files"]:
        relative = _safe_relative_path(entry["path"])
        if (
            _is_user_managed(relative)
            or _is_user_config(relative)
            or _is_runtime_user_data(relative)
            or relative.as_posix().casefold() == "config/installed-payload.json"
        ):
            continue
        entries.append({"path": relative.as_posix().casefold(), "sha256": entry["sha256"].upper()})
    entries.sort(key=lambda entry: entry["path"])
    canonical = json.dumps(entries, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()


def installed_payload_fingerprint(install_root: str | Path) -> str:
    """Identify installed managed files without hashing the entire runtime tree."""
    root = Path(install_root).expanduser().resolve()
    if not (root / "config" / "installed-payload.json").is_file():
        raise PayloadError("增量更新需要现有安装清单。")
    return _fingerprint_manifest(_read_installed_manifest_for_merge(root))


def _read_installed_manifest_for_merge(root: Path) -> dict[str, object]:
    _checked_target(root, Path("config/installed-payload.json"))
    manifest_path = root / "config" / "installed-payload.json"
    if manifest_path.is_symlink() or (
        hasattr(manifest_path, "is_junction") and manifest_path.is_junction()
    ):
        raise PayloadError("现有安装清单不能是符号链接或目录联接。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"files": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadError("现有安装清单无法安全保留。") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PayloadError("现有安装清单格式无效，已取消增量更新。")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise PayloadError("现有安装文件清单格式无效，已取消增量更新。")
    files: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise PayloadError("现有安装文件条目格式无效，已取消增量更新。")
        relative = _safe_relative_path(entry.get("path"))
        if _is_user_managed(relative) or relative.as_posix().casefold() == (
            "config/installed-payload.json"
        ):
            continue
        digest = entry.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            raise PayloadError("现有安装文件摘要无效，已取消增量更新。")
        key = relative.as_posix().casefold()
        if key in files:
            raise PayloadError("现有安装清单包含重复路径，已取消增量更新。")
        files[key] = {"path": relative.as_posix(), "sha256": digest.upper()}
    result: dict[str, object] = {"files": list(files.values())}
    release_version = manifest.get("release_version")
    if isinstance(release_version, str) and release_version.strip():
        result["release_version"] = release_version.strip()
    release_build = manifest.get("release_build")
    if isinstance(release_build, int) and not isinstance(release_build, bool) and release_build >= 0:
        result["release_build"] = release_build
    return result


def _manifest_specs(
    manifest: object,
) -> tuple[tuple[tuple[Path, str], ...], tuple[tuple[Path, str], ...]]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in (1, 2):
        raise PayloadError("不支持的载荷清单格式。")
    incremental = manifest["schema_version"] == 2
    if incremental and (
        manifest.get("update_kind") != "incremental"
        or not _valid_digest(manifest.get("base_fingerprint"))
    ):
        raise PayloadError("增量载荷缺少有效的更新类型或基线指纹。")
    if not incremental and (manifest.get("remove") or manifest.get("update_kind") == "incremental"):
        raise PayloadError("增量操作必须使用第二版载荷清单。")
    entries = manifest.get("files")
    removals = manifest.get("remove", []) if incremental else []
    if not isinstance(entries, list) or not isinstance(removals, list) or not (entries or removals):
        raise PayloadError("载荷清单没有可安装文件。")

    specs: list[tuple[Path, str]] = []
    remove_specs: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for entry, removing in [(entry, False) for entry in entries] + [(entry, True) for entry in removals]:
        if not isinstance(entry, dict):
            raise PayloadError("载荷文件条目格式错误。")
        relative = _safe_relative_path(entry.get("path"))
        key = relative.as_posix().casefold()
        if key in seen:
            raise PayloadError(f"载荷文件路径重复：{relative.as_posix()}")
        if _is_user_managed(relative):
            raise PayloadError(f"载荷不能覆盖用户数据：{relative.as_posix()}")
        if relative.as_posix().casefold() == "config/installed-payload.json":
            raise PayloadError("载荷不能直接覆盖安装清单。")
        if incremental:
            validate_incremental_path(relative)
        expected = entry.get("sha256")
        if not _valid_digest(expected):
            raise PayloadError(f"SHA-256 格式错误：{relative.as_posix()}")
        seen.add(key)
        (remove_specs if removing else specs).append((relative, expected.upper()))
    for key in seen:
        if any("/".join(key.split("/")[:index]) in seen for index in range(1, len(key.split("/")))):
            raise PayloadError(f"载荷路径不能同时表示文件及父目录：{key}")
    return tuple(specs), tuple(remove_specs)


def _manifest_file_specs(manifest: object) -> tuple[tuple[Path, str], ...]:
    return _manifest_specs(manifest)[0]


def _read_directory_manifest(root: Path) -> dict[str, object]:
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
    if not isinstance(manifest, dict):
        raise PayloadError("不支持的载荷清单格式。")
    return manifest


def _payload_plan(
    payload_root: str | Path,
) -> tuple[Path, dict[str, object], tuple[VerifiedPayloadFile, ...]]:
    """Validate manifest structure and file boundaries without reading file contents."""
    root = Path(payload_root).expanduser().resolve()
    manifest = _read_directory_manifest(root)
    planned: list[VerifiedPayloadFile] = []
    listed: set[str] = set()
    for relative, expected in _manifest_file_specs(manifest):
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
        listed.add(relative.as_posix())
        planned.append(VerifiedPayloadFile(relative, source, expected))

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
    return root, manifest, tuple(planned)


def _archive_plan(
    payload_archive: str | Path,
) -> tuple[Path, dict[str, object], tuple[VerifiedPayloadFile, ...]]:
    archive_path = Path(payload_archive).expanduser().resolve()
    if archive_path.is_symlink() or (
        hasattr(archive_path, "is_junction") and archive_path.is_junction()
    ):
        raise PayloadError("安装包不能是符号链接或目录联接。")
    if not archive_path.is_file():
        raise PayloadError(f"安装包不存在：{archive_path}")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = _safe_relative_path(info.filename)
                normalized = relative.as_posix()
                if normalized != info.filename:
                    raise PayloadError(f"安装包路径格式不规范：{info.filename}")
                key = normalized.casefold()
                if key in members:
                    raise PayloadError(f"安装包文件路径重复：{normalized}")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise PayloadError(f"安装包不能包含符号链接：{normalized}")
                if info.flag_bits & 0x1:
                    raise PayloadError(f"安装包不能包含加密文件：{normalized}")
                members[key] = info
            manifest_info = members.get(PAYLOAD_MANIFEST.casefold())
            if manifest_info is None or manifest_info.file_size > 64 * 1024 * 1024:
                raise PayloadError("安装包缺少有效的载荷清单。")
            try:
                manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PayloadError("安装包内的载荷清单无法读取。") from exc
            specs = _manifest_file_specs(manifest)
            expected_members = {PAYLOAD_MANIFEST.casefold()}
            planned: list[VerifiedPayloadFile] = []
            for relative, expected in specs:
                key = relative.as_posix().casefold()
                if key not in members:
                    raise PayloadError(f"安装包文件缺失：{relative.as_posix()}")
                if members[key].filename != relative.as_posix():
                    raise PayloadError(f"安装包路径大小写与清单不一致：{relative.as_posix()}")
                expected_members.add(key)
                planned.append(VerifiedPayloadFile(relative, archive_path, expected))
            extras = set(members) - expected_members
            if extras:
                preview = "、".join(sorted(extras)[:5])
                raise PayloadError(f"安装包包含未登记文件：{preview}")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PayloadError(f"安装包无法读取：{exc}") from exc
    return archive_path, manifest, tuple(planned)


def _plan_payload(
    payload_root: str | Path,
) -> tuple[Path, dict[str, object], tuple[VerifiedPayloadFile, ...]]:
    path = Path(payload_root).expanduser().resolve()
    if path.is_file():
        return _archive_plan(path)
    return _payload_plan(path)


def payload_relative_paths(
    payload_root: str | Path,
) -> tuple[Path, ...]:
    """Read safe manifest paths without hashing or traversing the full payload."""
    path = Path(payload_root).expanduser().resolve()
    if path.is_file():
        _root, _manifest, planned = _archive_plan(path)
        return tuple(item.relative_path for item in planned)
    manifest = _read_directory_manifest(path)
    return tuple(
        relative
        for relative, _expected in _manifest_file_specs(manifest)
    )


def payload_removed_paths(payload_root: str | Path) -> tuple[Path, ...]:
    """Return exact deleted files for runtime stop decisions and rollback snapshots."""
    path = Path(payload_root).expanduser().resolve()
    manifest = _archive_plan(path)[1] if path.is_file() else _read_directory_manifest(path)
    return tuple(relative for relative, _digest in _manifest_specs(manifest)[1])


def payload_requires_runtime_stop(payload_root: str | Path) -> bool:
    """Any runtime code/resource change, including deletion, requires a stopped bot."""
    paths = (*payload_relative_paths(payload_root), *payload_removed_paths(payload_root))
    return any(path.parts[0].casefold() in {"app", "runtime"} for path in paths)


def _checked_target(root: Path, relative: Path, *, allow_directory: bool = False) -> Path:
    candidate = root / relative
    for component in (candidate, *candidate.parents):
        if component == root.parent:
            break
        if component.is_symlink() or (
            hasattr(component, "is_junction") and component.is_junction()
        ):
            raise PayloadError(f"安装目标不能经过符号链接或目录联接：{relative.as_posix()}")
    target = candidate.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PayloadError(f"安装目标越过根目录：{relative.as_posix()}") from exc
    if target.exists() and not target.is_file() and not (allow_directory and target.is_dir()):
        raise PayloadError(f"安装目标不是普通文件：{relative.as_posix()}")
    return target


def _validate_incremental_base(
    manifest: dict[str, object], root: Path, previous_manifest: dict[str, object]
) -> None:
    if manifest.get("schema_version") != 2:
        return
    if not (root / "config" / "installed-payload.json").is_file():
        raise PayloadError("增量更新需要现有安装清单。")
    if _fingerprint_manifest(previous_manifest) != str(manifest["base_fingerprint"]).upper():
        raise PayloadError("增量更新基线不匹配，请使用适配当前安装的更新包。")
    existing = {
        entry["path"].casefold(): entry["sha256"]
        for entry in previous_manifest["files"]
    }
    specs, removals = _manifest_specs(manifest)
    for (relative, digest), removing in [(spec, False) for spec in specs] + [(spec, True) for spec in removals]:
        target = _checked_target(root, relative)
        expected = existing.get(relative.as_posix().casefold())
        if removing and (expected is None or expected != digest):
            raise PayloadError(f"删除目标未登记或删除摘要不匹配：{relative.as_posix()}")
        if expected is None:
            if target.exists():
                raise PayloadError(f"新增文件与现有未登记文件冲突：{relative.as_posix()}")
        elif not target.is_file() or sha256_file(target) != expected:
            raise PayloadError(f"现有文件已修改或缺失，已取消增量更新：{relative.as_posix()}")


def validate_incremental_base(payload_root: str | Path, install_root: str | Path) -> None:
    """Preflight a delta against its base without changing the installation."""
    path = Path(payload_root).expanduser().resolve()
    manifest = _archive_plan(path)[1] if path.is_file() else _read_directory_manifest(path)
    _manifest_specs(manifest)
    if manifest.get("schema_version") == 2:
        root = Path(install_root).expanduser().resolve()
        _validate_incremental_base(manifest, root, _read_installed_manifest_for_merge(root))


def _copy_stream_and_hash(reader: BinaryIO, target: Path) -> str:
    digest = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
            writer.write(chunk)
    return digest.hexdigest().upper()


def _copy_and_hash(source: Path, target: Path) -> str:
    """Copy once while hashing the same byte stream."""
    with source.open("rb") as reader:
        result = _copy_stream_and_hash(reader, target)
    shutil.copystat(source, target, follow_symlinks=False)
    return result


def _transaction_file(root: Path, index: int) -> Path:
    return root / f"{index:08X}"


def _replace_file(source: Path, target: Path, *, timeout_seconds: float = 30.0) -> None:
    """Retry transient Windows locks from process teardown and antivirus scans."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def verify_payload(
    payload_root: str | Path,
    *,
    progress_callback: ProgressCallback | None = None,
) -> VerifiedPayload:
    root, manifest, planned = _plan_payload(payload_root)
    verified: list[VerifiedPayloadFile] = []
    entry_count = len(planned)
    archive = zipfile.ZipFile(root) if root.is_file() else None
    try:
        for index, item in enumerate(planned, start=1):
            if archive is None:
                actual = sha256_file(item.source_path)
            else:
                digest = hashlib.sha256()
                with archive.open(item.relative_path.as_posix()) as reader:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        digest.update(chunk)
                actual = digest.hexdigest().upper()
            if actual != item.sha256:
                raise PayloadError(f"载荷文件校验失败：{item.relative_path.as_posix()}")
            verified.append(VerifiedPayloadFile(item.relative_path, item.source_path, actual))
            if progress_callback is not None:
                progress_callback("verify", index, entry_count)
    finally:
        if archive is not None:
            archive.close()
    return VerifiedPayload(root, manifest, tuple(verified))


def install_payload(
    payload_root: str | Path,
    install_root: str | Path,
    *,
    progress_callback: ProgressCallback | None = None,
    preserve_existing_user_config: bool = False,
) -> VerifiedPayload:
    payload_root_path, manifest, planned = _plan_payload(payload_root)
    root = Path(install_root).expanduser().resolve()
    incremental = manifest.get("schema_version") == 2
    removals = _manifest_specs(manifest)[1]
    preserve_existing_user_config = preserve_existing_user_config or incremental
    # Keep the check here as a final safety boundary for callers that bypass the
    # setup wizard or InstallRequest validation.
    path_validation = validate_install_path(
        root,
        required_bytes=0,
        payload_relative_paths=(*[item.relative_path for item in planned], *[path for path, _digest in removals]),
    )
    length_errors = tuple(error for error in path_validation.errors if "过长" in error)
    if length_errors:
        raise PayloadError("；".join(length_errors))
    previous_manifest = (
        _read_installed_manifest_for_merge(root)
        if preserve_existing_user_config
        else {"files": []}
    )
    _validate_incremental_base(manifest, root, previous_manifest)
    for relative in [item.relative_path for item in planned] + [path for path, _digest in removals]:
        _checked_target(root, relative)
    manifest_target = _checked_target(root, Path("config/installed-payload.json"))
    updates_candidate = root / "updates"
    if updates_candidate.is_symlink() or (
        hasattr(updates_candidate, "is_junction") and updates_candidate.is_junction()
    ):
        raise PayloadError("更新暂存目录不能是符号链接或目录联接。")
    updates_root = updates_candidate.resolve()
    try:
        updates_root.relative_to(root)
    except ValueError as exc:
        raise PayloadError("更新暂存目录不在安装根目录内。") from exc
    updates_root.mkdir(parents=True, exist_ok=True)

    transaction_id = uuid.uuid4().hex
    stage_root = updates_root / f".install-{transaction_id}"
    backup_root = updates_root / f".rollback-{transaction_id}"
    stage_root.mkdir(parents=True)
    backup_root.mkdir(parents=True)
    replaced: list[tuple[Path, Path | None]] = []
    rollback_failed = False
    try:
        file_count = len(planned)
        verified_files: list[VerifiedPayloadFile] = []
        archive = zipfile.ZipFile(payload_root_path) if payload_root_path.is_file() else None
        try:
            for index, item in enumerate(planned, start=1):
                staged = _transaction_file(stage_root, index)
                if archive is None:
                    actual = _copy_and_hash(item.source_path, staged)
                else:
                    with archive.open(item.relative_path.as_posix()) as reader:
                        actual = _copy_stream_and_hash(reader, staged)
                if actual != item.sha256:
                    raise PayloadError(f"安装文件校验失败：{item.relative_path.as_posix()}")
                verified_files.append(
                    VerifiedPayloadFile(item.relative_path, item.source_path, actual)
                )
                if progress_callback is not None:
                    progress_callback("prepare", index, file_count)
        finally:
            if archive is not None:
                archive.close()

        # Preparation can take time: recheck the base and every target before the
        # first installed file changes, including callback/concurrent edits.
        if incremental:
            previous_manifest = _read_installed_manifest_for_merge(root)
            _validate_incremental_base(manifest, root, previous_manifest)
        for relative in [item.relative_path for item in planned] + [path for path, _digest in removals]:
            _checked_target(root, relative)
        _checked_target(root, Path("config/installed-payload.json"))
        installed_files: list[VerifiedPayloadFile] = []
        for index, item in enumerate(verified_files, start=1):
            target = _checked_target(root, item.relative_path)
            if (
                preserve_existing_user_config
                and _is_user_config(item.relative_path)
                and target.is_file()
                and not target.is_symlink()
            ):
                installed_files.append(
                    VerifiedPayloadFile(item.relative_path, target, sha256_file(target))
                )
                if progress_callback is not None:
                    progress_callback("install", index, file_count)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            backup: Path | None = None
            if target.exists():
                if not target.is_file() or target.is_symlink():
                    raise PayloadError(f"安装目标不是普通文件：{item.relative_path.as_posix()}")
                backup = _transaction_file(backup_root, index)
                shutil.copy2(target, backup)
            _replace_file(_transaction_file(stage_root, index), target)
            replaced.append((target, backup))
            installed_files.append(item)
            if progress_callback is not None:
                progress_callback("install", index, file_count)

        for index, (relative, _digest) in enumerate(removals, start=file_count + 1):
            target = _checked_target(root, relative)
            backup = _transaction_file(backup_root, index)
            _replace_file(target, backup)
            replaced.append((target, backup))
            if progress_callback is not None:
                progress_callback("remove", index - file_count, len(removals))

        installed_by_path = {
            str(entry["path"]).casefold(): dict(entry)
            for entry in previous_manifest.get("files", [])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        for item in installed_files:
            installed_by_path[item.relative_path.as_posix().casefold()] = {
                "path": item.relative_path.as_posix(),
                "sha256": item.sha256,
            }
        for relative, _digest in removals:
            installed_by_path.pop(relative.as_posix().casefold(), None)
        installed_manifest: dict[str, object] = {
            "schema_version": 1,
            "files": sorted(
                installed_by_path.values(),
                key=lambda entry: str(entry["path"]).casefold(),
            ),
        }
        release_version = manifest.get("release_version")
        if isinstance(release_version, str) and release_version.strip():
            installed_manifest["release_version"] = release_version.strip()
        elif previous_manifest.get("release_version"):
            installed_manifest["release_version"] = previous_manifest["release_version"]
        release_build = manifest.get("release_build")
        if isinstance(release_build, int) and not isinstance(release_build, bool) and release_build >= 0:
            installed_manifest["release_build"] = release_build
        elif (
            isinstance(previous_manifest.get("release_build"), int)
            and not isinstance(previous_manifest.get("release_build"), bool)
            and previous_manifest["release_build"] >= 0
        ):
            installed_manifest["release_build"] = previous_manifest["release_build"]
        manifest_backup: Path | None = None
        if manifest_target.exists():
            manifest_backup = _transaction_file(backup_root, file_count + len(removals) + 1)
            shutil.copy2(manifest_target, manifest_backup)
        # Register the manifest before writing so even a post-write failure
        # restores both the program files and the exact previous inventory.
        replaced.append((manifest_target, manifest_backup))
        write_json_atomic(manifest_target, installed_manifest)
    except Exception as exc:
        rollback_errors: list[str] = []
        for target, backup in reversed(replaced):
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                elif backup.exists():
                    _replace_file(backup, target)
            except OSError:
                rollback_errors.append(str(target))
        if rollback_errors:
            rollback_failed = True
            raise PayloadError(f"更新失败且部分文件回滚失败，已保留备份：{backup_root}") from exc
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
        if not rollback_failed:
            shutil.rmtree(backup_root, ignore_errors=True)
    return VerifiedPayload(payload_root_path, manifest, tuple(installed_files))
