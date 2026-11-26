"""Prepare complete application/environment transitions using verified reusable files."""
from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from .install_paths import extended_windows_path
from .json_io import write_json_atomic
from .payload import (
    PAYLOAD_MANIFEST, PayloadError, VerifiedPayload, VerifiedPayloadFile, _checked_target,
    _copy_and_hash, _is_runtime_user_data, _is_user_config, _is_user_managed, _manifest_specs,
    _plan_payload, _read_installed_manifest_for_merge, _reuse_verified_payload_plan,
    _safe_relative_path, sha256_file, verify_payload,
)
from .runtime_manifest import (
    ENVIRONMENT_JOURNAL, ENVIRONMENT_MANIFEST, ENVIRONMENT_RECEIPT, RuntimeEnvironment,
    RuntimePackage, load_environment, runtime_path,
)
from .update_download import download_verified
from .update_manifest import UpdateChannel, UpdateError


def validate_deployment_path(relative: Path, *, kind: str, removing: bool = False) -> None:
    if relative == ENVIRONMENT_RECEIPT:
        return
    if kind == "application" and not removing and _is_user_config(relative) and not _is_user_managed(relative):
        # Full applications may seed defaults. install_payload preserves every
        # existing user configuration, and removal never uses this exception.
        return
    if relative.parts[0].casefold() == "runtime":
        if removing:
            # Legacy releases also owned engine code and runtime build metadata.
            if relative.as_posix().casefold() != "runtime/runtime-manifest.json" and (
                    len(relative.parts) < 3 or relative.parts[1].casefold() not in {"python", "ffmpeg"}):
                raise PayloadError("运行环境删除路径无效。")
        else:
            runtime_path(relative)
        return
    if (kind != "application" or _is_user_managed(relative) or _is_user_config(relative)
            or _is_runtime_user_data(relative)
            or relative.parts[0].casefold() not in {"app", "manager", "legal", "config"}):
        raise PayloadError(f"文件不属于此安装范围：{relative.as_posix()}")


def package_installed(package: RuntimePackage, root: Path) -> bool:
    for item in package.files:
        path = _checked_target(root, Path(item.path))
        if not path.is_file() or path.stat().st_size != item.size or sha256_file(path) != item.sha256:
            return False
    return True


def missing_packages(environment: RuntimeEnvironment, root: Path, progress_callback=None) -> tuple[RuntimePackage, ...]:
    missing = []
    total = len(environment.packages)
    if progress_callback:
        progress_callback("runtime_check", 0, total)
    for index, package in enumerate(environment.packages, 1):
        if not package_installed(package, root):
            missing.append(package)
        if progress_callback:
            progress_callback("runtime_check", index, total)
    return tuple(missing)


def _download_packages(missing: tuple[RuntimePackage, ...], root: Path, staging: Path,
                       channel: UpdateChannel, progress_callback=None) -> list[VerifiedPayloadFile]:
    from .updates import extract_update_archive

    staged_files = []
    if not missing:
        return staged_files
    total, completed = sum(package.size for package in missing), 0
    cache = _checked_target(root, Path("cache/runtime-packages"), allow_directory=True)
    cache.mkdir(parents=True, exist_ok=True)
    for package in missing:
        def progress(_phase, received, _size):
            if progress_callback:
                progress_callback("runtime_download", completed + received, total)
        archive = _checked_target(root, Path(f"cache/runtime-packages/{package.sha256}.zip"))
        download_verified(package.url, archive, size=package.size, sha256=package.sha256,
                          channel=channel, progress_callback=progress)
        extracted = extract_update_archive(archive, staging.parent / f"package-{package.identity}")
        _, manifest, files = _plan_payload(extracted)
        expected = {item.path: item.sha256 for item in package.files}
        actual = {item.relative_path.as_posix(): item.sha256 for item in files}
        if expected != actual or manifest.get("schema_version") != 1:
            raise UpdateError("运行环境包文件与签名清单不一致。")
        if package.uncompressed_size and sum(p.stat().st_size for p in extracted.rglob("*") if p.is_file()) != package.uncompressed_size:
            raise UpdateError("运行环境包展开体积不一致。")
        for item in package.files:
            relative = Path(item.path)
            source = _checked_target(extracted, relative)
            if source.stat().st_size != item.size:
                raise UpdateError("运行环境文件大小不一致。")
            target = staging / relative
            if _copy_and_hash(source, target) != item.sha256:
                raise UpdateError(f"运行环境包文件与签名清单不一致：{item.path}")
            staged_files.append(VerifiedPayloadFile(relative, target, item.sha256))
        completed += package.size
    return staged_files


def _removals(root: Path, targets: set[str], *, kind: str) -> list[dict]:
    result = []
    for item in _read_installed_manifest_for_merge(root).get("files", []):
        relative = _safe_relative_path(item["path"])
        if relative.as_posix().casefold() in targets:
            continue
        try:
            validate_deployment_path(relative, kind=kind, removing=True)
        except PayloadError:
            continue
        target = _checked_target(root, relative)
        if target.exists() and sha256_file(target) != item["sha256"]:
            raise PayloadError(f"待删除的旧程序文件已被修改：{relative.as_posix()}")
        result.append(item)
    return result


def prepare_deployment(payload_root: Path | None, root: Path, working: Path,
                       channel: UpdateChannel, *, progress_callback=None,
                       verified_payload: VerifiedPayload | None = None) -> Path:
    """Stage only changed environment packages; final inventory also removes obsolete files."""
    if (root / ENVIRONMENT_JOURNAL).exists():
        raise UpdateError("上次环境安装未完成，请先在运行中心重试安装。")
    environment = load_environment(payload_root or root, channel)
    if environment is None:
        raise UpdateError("应用缺少签名运行环境清单。")
    missing = missing_packages(environment, root, progress_callback)
    verified = verified_payload
    if verified is not None:
        if payload_root is None:
            raise PayloadError("已校验载荷与当前安装来源不匹配。")
        _reuse_verified_payload_plan(payload_root, verified)
    elif payload_root is not None:
        verified = verify_payload(payload_root)
    application_bytes = sum(item.source_path.stat().st_size for item in verified.files) if verified else 0
    required_space = (sum(package.size + 3 * sum(item.size for item in package.files) for package in missing)
                      + application_bytes + 64 * 1024**2)
    if shutil.disk_usage(root).free < required_space:
        raise UpdateError("运行环境安装所需磁盘空间不足。")
    staging = extended_windows_path(working / f"deployment-{uuid.uuid4().hex}").resolve()
    staging.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 3, "update_kind": "environment", "files": []}
    app_paths = set()
    staged_files = []
    if verified is not None:
        if verified.manifest.get("application_package") != 1:
            raise UpdateError("载荷不是完整应用包。")
        total = len(verified.files)
        if progress_callback:
            progress_callback("prepare", 0, total)
        for index, item in enumerate(verified.files, 1):
            if item.relative_path.parts[0].casefold() == "runtime":
                raise UpdateError("应用包不能内嵌运行环境。")
            validate_deployment_path(item.relative_path, kind="application")
            target = staging / item.relative_path
            if _copy_and_hash(item.source_path, target) != item.sha256:
                raise PayloadError(f"应用文件在准备期间已改变：{item.relative_path.as_posix()}")
            staged_files.append(VerifiedPayloadFile(item.relative_path, target, item.sha256))
            app_paths.add(item.relative_path.as_posix().casefold())
            if progress_callback:
                progress_callback("prepare", index, total)
        manifest.update(update_kind="application", release_version=verified.manifest["release_version"])
        if "release_build" in verified.manifest:
            manifest["release_build"] = verified.manifest["release_build"]
    staged_files.extend(_download_packages(missing, root, staging, channel, progress_callback))
    receipt = staging / ENVIRONMENT_RECEIPT
    write_json_atomic(receipt, {"schema_version": 1, "id": environment.identity})
    staged_files.append(VerifiedPayloadFile(ENVIRONMENT_RECEIPT, receipt, sha256_file(receipt)))
    targets = app_paths | {item.path.casefold() for item in environment.files} | {ENVIRONMENT_RECEIPT.as_posix()}
    manifest["remove"] = _removals(root, targets, kind=manifest["update_kind"])
    staged_files.sort(key=lambda item: item.relative_path)
    manifest["files"] = [{"path": item.relative_path.as_posix(), "sha256": item.sha256} for item in staged_files]
    staged_paths = {item["path"].casefold() for item in manifest["files"]}
    manifest["reuse"] = [{"path": item.path, "sha256": item.sha256}
                         for item in environment.files if item.path.casefold() not in staged_paths]
    _manifest_specs(manifest)
    write_json_atomic(staging / PAYLOAD_MANIFEST, manifest)
    verify_deployment_environment(staging, root, channel,
                                  verified_payload=VerifiedPayload(staging, manifest, tuple(staged_files)),
                                  check_installed=False)
    return staging


def verify_deployment_environment(staging: Path, root: Path, channel: UpdateChannel, *,
                                  verified_payload: VerifiedPayload | None = None,
                                  check_installed: bool = True) -> None:
    """Bind the staged and reused inventories to the signed environment."""
    environment = load_environment(staging if (staging / ENVIRONMENT_MANIFEST).exists() else root, channel)
    if environment is None:
        raise UpdateError("安装事务缺少签名环境清单。")
    verified = verified_payload or verify_payload(staging)
    if verified.root != staging.resolve():
        raise PayloadError("已校验载荷与当前安装来源不匹配。")
    staged_files = {item.relative_path.as_posix().casefold(): item for item in verified.files}
    for item in environment.files:
        staged = staged_files.get(item.path.casefold())
        if staged is not None:
            if staged.sha256 != item.sha256 or staged.source_path.stat().st_size != item.size:
                raise UpdateError(f"运行环境文件在准备后改变：{item.path}")
        elif check_installed:
            source = _checked_target(root, Path(item.path))
            if not source.is_file() or source.stat().st_size != item.size or sha256_file(source) != item.sha256:
                raise UpdateError(f"运行环境文件在准备后改变：{item.path}")
    expected = {item.path.casefold() for item in environment.files}
    expected_reuse = {item.path.casefold(): item.sha256 for item in environment.files if item.path.casefold() not in staged_files}
    actual_reuse = {item["path"].casefold(): item["sha256"] for item in verified.manifest.get("reuse", [])}
    if actual_reuse != expected_reuse:
        raise UpdateError("复用文件清单与目标环境不一致。")
    for item in verified.files:
        key = item.relative_path.as_posix().casefold()
        if item.relative_path.parts[0].casefold() == "runtime" and item.relative_path != ENVIRONMENT_RECEIPT and key not in expected:
            raise UpdateError("安装事务包含环境清单之外的文件。")
    removals = {item["path"].casefold() for item in verified.manifest.get("remove", [])}
    if removals & expected:
        raise UpdateError("安装事务不能删除目标环境需要的文件。")

    if check_installed:
        for entry in verified.manifest.get("remove", []):
            path = _checked_target(root, Path(entry["path"]))
            if path.exists() and (not path.is_file() or sha256_file(path) != entry["sha256"]):
                raise UpdateError(f"待清理的旧文件在准备后改变：{entry['path']}")
