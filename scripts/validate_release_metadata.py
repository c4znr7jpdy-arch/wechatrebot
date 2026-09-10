from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tomllib


_HASH = re.compile(r"^[0-9A-F]{64}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_FORBIDDEN_PARTS = {"py", "legacy_nonebot2", "python311-x86"}
_FEATURE_CONFIGURATION_MODES = {
    "none",
    "schema",
    "page",
    "schema-and-page",
    "page-and-global-schedule-center",
}
_REQUIRED_ACCEPTANCE_EVIDENCE = {
    "hook-redistribution",
    "hook-security",
    "real-device-wechat-4.1.8.27",
    "authenticode",
    "plugin-license-review",
    "runtime-license-review",
    "customer-agreement",
    "clean-windows",
    "antivirus-compatibility",
    "upgrade-rollback",
    "offline-signing-key-backup",
    "release-artifact-hashes",
    "customer-document-scan",
}


def _approval_time(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _load_object(path: Path, errors: list[str]) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {path.name}: {exc}")
        return {}
    if not isinstance(payload, dict):
        errors.append(f"{path.name} root must be an object")
        return {}
    return payload


def _safe_repo_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return not (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def _exact_lock(path: Path, errors: list[str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        errors.append(f"cannot read {path.name}: {exc}")
        return
    requirements = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not requirements:
        errors.append(f"{path.name} is empty")
    for requirement in requirements:
        if "==" not in requirement or any(token in requirement for token in (">=", "<=", "~=", "!=")):
            errors.append(f"{path.name} contains an unpinned requirement: {requirement}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _validate_ffmpeg_definition(
    definition: dict[str, object], errors: list[str]
) -> None:
    if definition.get("schema_version") != 1 or definition.get("status") != "pinned":
        errors.append("FFmpeg runtime definition must use schema 1 and pinned status")
    if (
        definition.get("name") != "FFmpeg"
        or definition.get("architecture") != "win64"
        or definition.get("distribution") != "lgpl-shared"
        or definition.get("license") != "LGPL-3.0-or-later"
    ):
        errors.append("FFmpeg runtime must be the win64 LGPL shared distribution")
    version = definition.get("version")
    if not isinstance(version, str) or not re.fullmatch(
        r"n\d+(?:\.\d+){1,2}-\d+-g[0-9a-f]{7,40}", version
    ):
        errors.append("FFmpeg runtime version must identify a fixed source revision")
    filename = definition.get("archive_filename")
    valid_filename = isinstance(filename, str) and bool(
        re.fullmatch(
            rf"ffmpeg-{re.escape(str(version))}-win64-lgpl-shared-\d+\.\d+\.zip",
            filename,
        )
    )
    if not valid_filename:
        errors.append("FFmpeg runtime archive filename does not match its version")
    if not valid_filename or definition.get("archive_root") != filename.removesuffix(".zip"):
        errors.append("FFmpeg runtime archive root does not match its filename")
    digest = definition.get("archive_sha256")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest.upper()) or digest == "0" * 64:
        errors.append("FFmpeg runtime archive SHA-256 is invalid")
    build_tag = definition.get("build_tag")
    if not isinstance(build_tag, str) or not re.fullmatch(
        r"autobuild-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}", build_tag
    ):
        errors.append("FFmpeg runtime build tag must be a fixed autobuild release")
    if definition.get("archive_url") != (
        f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{build_tag}/{filename}"
    ):
        errors.append("FFmpeg runtime archive URL does not match its pinned build")
    if definition.get("build_source_url") != (
        f"https://github.com/BtbN/FFmpeg-Builds/tree/{build_tag}"
    ):
        errors.append("FFmpeg runtime build source URL does not match its pinned build")
    source_commit = definition.get("source_commit")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{7,40}", source_commit):
        errors.append("FFmpeg runtime source commit is invalid")
    elif not isinstance(version, str) or not source_commit.startswith(version.rsplit("-g", 1)[-1]):
        errors.append("FFmpeg runtime source commit does not match its version")
    if definition.get("source_url") != (
        f"https://github.com/FFmpeg/FFmpeg/archive/{source_commit}.tar.gz"
    ):
        errors.append("FFmpeg runtime source URL does not match its pinned source commit")


def _version_consistency(root: Path, errors: list[str]) -> str | None:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project_version = str(project["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"cannot read project version: {exc}")
        return None
    if not _VERSION.fullmatch(project_version):
        errors.append(f"project version is not semantic x.y.z: {project_version}")
        return None
    init_text = (root / "src" / "wechatrebot_manager" / "__init__.py").read_text(
        encoding="utf-8"
    )
    windows_text = (root / "release" / "windows-version-info.txt").read_text(
        encoding="utf-8"
    )
    if f'__version__ = "{project_version}"' not in init_text:
        errors.append("package __version__ does not match pyproject.toml")
    for field in ("FileVersion", "ProductVersion"):
        if f"StringStruct(u'{field}', u'{project_version}')" not in windows_text:
            errors.append(f"Windows {field} does not match pyproject.toml")
    return project_version


def _validate_legal_review(
    root: Path,
    review: dict[str, object],
    *,
    errors: list[str],
) -> None:
    if review.get("schema_version") != 1:
        errors.append("legal review schema must be 1")
    if review.get("status") not in {"review-required", "approved", "rejected"}:
        errors.append("legal review status is invalid")
    artifacts = review.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("legal review artifacts are missing")
        return
    for name in ("sbom", "notices", "license_texts", "corresponding_source"):
        record = artifacts.get(name)
        if not isinstance(record, dict):
            errors.append(f"legal review artifact is missing: {name}")
            continue
        path_value = record.get("path")
        expected_hash = record.get("sha256")
        if not _safe_repo_path(path_value):
            errors.append(f"legal review artifact path is unsafe: {name}")
            continue
        if not isinstance(expected_hash, str) or not _HASH.fullmatch(
            expected_hash.upper()
        ):
            errors.append(f"legal review artifact hash is invalid: {name}")
            continue
        artifact = root / Path(*PurePosixPath(path_value).parts)
        if not artifact.is_file():
            errors.append(f"legal review artifact does not exist: {path_value}")
        elif _sha256(artifact) != expected_hash.upper():
            errors.append(f"legal review artifact hash mismatch: {path_value}")
def _validate_acceptance_evidence(
    root: Path,
    evidence: dict[str, object],
    *,
    project_version: str | None,
    errors: list[str],
) -> None:
    if evidence.get("schema_version") != 1:
        errors.append("acceptance evidence schema must be 1")
    if project_version and evidence.get("release_version") != project_version:
        errors.append("acceptance evidence version does not match release version")
    requirements = evidence.get("requirements")
    if not isinstance(requirements, list):
        errors.append("acceptance evidence requirements must be a list")
        return
    seen: set[str] = set()
    approved: set[str] = set()
    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            errors.append(f"acceptance evidence item {index} is not an object")
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or identifier not in _REQUIRED_ACCEPTANCE_EVIDENCE:
            errors.append(f"acceptance evidence item {index} has unknown id")
            continue
        if identifier in seen:
            errors.append(f"duplicate acceptance evidence id: {identifier}")
        seen.add(identifier)
        item_status = item.get("status")
        if item_status not in {"pending", "prepared", "approved", "rejected"}:
            errors.append(f"acceptance evidence has invalid status: {identifier}")
            continue
        if item_status != "approved":
            continue
        approved.add(identifier)
        if not item.get("reviewer") or not _approval_time(item.get("approved_at")):
            errors.append(f"approved evidence has no reviewer or time: {identifier}")
        records = item.get("evidence")
        if not isinstance(records, list) or not records:
            errors.append(f"approved evidence has no artifacts: {identifier}")
            continue
        for record in records:
            if not isinstance(record, dict):
                errors.append(f"approved evidence artifact is invalid: {identifier}")
                continue
            path_value = record.get("path")
            digest = record.get("sha256")
            if not _safe_repo_path(path_value):
                errors.append(f"approved evidence path is unsafe: {identifier}")
                continue
            if not isinstance(digest, str) or not _HASH.fullmatch(digest.upper()):
                errors.append(f"approved evidence hash is invalid: {identifier}")
                continue
            artifact = root / Path(*PurePosixPath(path_value).parts)
            if not artifact.is_file() or _sha256(artifact) != digest.upper():
                errors.append(f"approved evidence artifact is missing or changed: {identifier}")
    missing = sorted(_REQUIRED_ACCEPTANCE_EVIDENCE - seen)
    if missing:
        errors.append(f"acceptance evidence requirements are missing: {missing}")
    all_approved = approved == _REQUIRED_ACCEPTANCE_EVIDENCE
    rejected = any(
        isinstance(item, dict) and item.get("status") == "rejected"
        for item in requirements
    )
    expected_status = "rejected" if rejected else ("approved" if all_approved else "pending")
    if evidence.get("status") != expected_status:
        errors.append(f"acceptance evidence overall status must be {expected_status}")
def _validate_signing_evidence(
    evidence: dict[str, object],
    *,
    project_version: str | None,
    expected_hook_entries: dict[str, str],
    errors: list[str],
) -> None:
    if evidence.get("schema_version") != 1:
        errors.append("signing evidence schema must be 1")
    if evidence.get("status") not in {"pending", "invalid", "valid"}:
        errors.append("signing evidence status is invalid")
    if project_version and evidence.get("release_version") != project_version:
        errors.append("signing evidence version does not match release version")
    files = evidence.get("files")
    if not isinstance(files, list):
        errors.append("signing evidence files must be a list")
        files = []
    if evidence.get("status") != "valid":
        return
    if evidence.get("all_signatures_valid") is not True:
        errors.append("signing evidence says one or more Authenticode signatures are invalid")
    if not _approval_time(evidence.get("collected_at")):
        errors.append("valid signing evidence has no collection time")
    by_role: dict[str, dict[str, object]] = {}
    for item in files:
        if not isinstance(item, dict) or item.get("role") not in {
            "manager",
            "injector",
            "hook-library",
        }:
            errors.append("signing evidence contains an invalid file entry")
            continue
        role = str(item["role"])
        if role in by_role:
            errors.append(f"duplicate signing evidence role: {role}")
        by_role[role] = item
        if item.get("authenticode_status") != "Valid":
            errors.append(f"signing evidence signature is not valid: {role}")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not _HASH.fullmatch(digest.upper()):
            errors.append(f"signing evidence hash is invalid: {role}")
        if not item.get("signer_thumbprint") or not item.get("timestamp_thumbprint"):
            errors.append(f"signing evidence has no signer or timestamp: {role}")
    missing_roles = sorted({"manager", "injector", "hook-library"} - set(by_role))
    if missing_roles:
        errors.append(f"signing evidence roles are missing: {missing_roles}")
    hook_hashes_by_role = {
        "injector": next(
            (digest for path, digest in expected_hook_entries.items() if path.endswith("x64 inject.exe")),
            None,
        ),
        "hook-library": next(
            (digest for path, digest in expected_hook_entries.items() if path.endswith(".dll")),
            None,
        ),
    }
    for role, expected_hash in hook_hashes_by_role.items():
        item = by_role.get(role)
        if item is not None and expected_hash and str(item.get("sha256", "")).upper() != expected_hash:
            errors.append(f"signed file hash does not match hook metadata: {role}")


def validate_release_metadata(root: str | Path) -> list[str]:
    root = Path(root).expanduser().resolve()
    errors: list[str] = []
    release = root / "release"
    source = _load_object(release / "source-manifest.json", errors)
    hook = _load_object(release / "hook-binaries.json", errors)
    plugins = _load_object(release / "plugin-manifest.json", errors)
    feature_set = _load_object(release / "feature-set.json", errors)
    wheelhouse = _load_object(release / "runtime-wheelhouse.json", errors)
    ffmpeg = _load_object(release / "ffmpeg-runtime.json", errors)
    legal_review = _load_object(release / "legal-review.json", errors)
    acceptance_evidence = _load_object(release / "acceptance-evidence.json", errors)
    signing_evidence = _load_object(release / "signing-evidence.json", errors)

    if source.get("schema_version") != 1:
        errors.append("source manifest schema must be 1")
    entries = source.get("entries")
    if not isinstance(entries, list):
        errors.append("source manifest entries must be a list")
        entries = []

    targets: set[str] = set()
    source_hashes: dict[str, str] = {}
    commercial_targets: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"source entry {index} is not an object")
            continue
        origin = entry.get("origin")
        source_path = entry.get("source")
        target_path = entry.get("target")
        if origin not in {"commercial", "project"}:
            errors.append(f"source entry {index} has invalid origin")
        for label, value in (("source", source_path), ("target", target_path)):
            if not _safe_repo_path(value):
                errors.append(f"source entry {index} has unsafe {label} path")
        if not isinstance(target_path, str):
            continue
        target_key = target_path.casefold()
        if target_key in targets:
            errors.append(f"duplicate source target: {target_path}")
        targets.add(target_key)
        parts = {part.casefold() for part in PurePosixPath(target_path).parts}
        if parts & _FORBIDDEN_PARTS:
            errors.append(f"source target crosses a forbidden boundary: {target_path}")
        if origin == "commercial" and isinstance(source_path, str):
            commercial_targets.add(target_key)
            if not (root / Path(*PurePosixPath(source_path).parts)).exists():
                errors.append(f"commercial source does not exist: {source_path}")
        digest = entry.get("sha256")
        if digest is not None:
            if not isinstance(source_path, str) or not isinstance(digest, str) or not _HASH.fullmatch(digest.upper()):
                errors.append(f"source entry {index} has an invalid SHA-256")
            else:
                source_hashes[source_path] = digest.upper()

    required_targets = {
        "legal/corresponding-source.md",
        "legal/corresponding-source/astrbot-4.26.6-wechat.patch",
        "legal/corresponding-source/build_runtime.py",
        "legal/feature-set.json",
        "legal/ffmpeg-runtime.json",
        "legal/hook-binaries.json",
        "legal/plugin-manifest.json",
        "legal/sbom.cdx.json",
        "legal/third-party-licenses.txt",
        "legal/third-party-notices.md",
        "legal/runtime-requirements.lock",
        "legal/runtime-wheelhouse.json",
        "legal/release-checklist.md",
    }
    missing_targets = sorted(required_targets - commercial_targets)
    if missing_targets:
        errors.append(f"required commercial targets are missing: {missing_targets}")
    if not any(
        isinstance(entry, dict)
        and entry.get("origin") == "commercial"
        and entry.get("source") == "release/ffmpeg-runtime.json"
        and entry.get("target") == "legal/ffmpeg-runtime.json"
        for entry in entries
    ):
        errors.append("source manifest must ship the pinned FFmpeg runtime definition")

    hook_files = hook.get("files")
    expected_hook_entries: dict[str, str] = {}
    if not isinstance(hook_files, list):
        errors.append("hook binary files must be a list")
    else:
        for item in hook_files:
            if not isinstance(item, dict):
                errors.append("hook binary entry is not an object")
                continue
            path = item.get("path")
            digest = item.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str) or not _HASH.fullmatch(digest.upper()):
                errors.append("hook binary path or SHA-256 is invalid")
                continue
            expected_hook_entries[path] = digest.upper()
            if (item.get("architecture"), item.get("pe_machine")) != ("x64", "0x8664"):
                errors.append(f"hook binary architecture is not x64: {path}")
    source_hook_entries = {
        path: source_hashes[path]
        for path in expected_hook_entries
        if path in source_hashes
    }
    if source_hook_entries != expected_hook_entries:
        errors.append("source manifest hook hashes do not match hook-binaries.json")

    features = feature_set.get("features")
    if feature_set.get("schema_version") != 1 or not isinstance(features, list):
        errors.append("feature set schema or feature list is invalid")
        features = []
    feature_modules: set[str] = set()
    plugin_items = plugins.get("plugins")
    if plugins.get("schema_version") != 1 or not isinstance(plugin_items, list):
        errors.append("plugin manifest schema or plugin list is invalid")
        plugin_items = []
    delivered_plugins: dict[str, dict[str, object]] = {}
    for item in plugin_items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            errors.append("plugin manifest entry is invalid")
            continue
        name = item["name"]
        if name in delivered_plugins:
            errors.append(f"duplicate plugin manifest entry: {name}")
        delivered_plugins[name] = item
    source_features = {
        PurePosixPath(str(entry.get("target"))).name: entry
        for entry in entries
        if isinstance(entry, dict)
        and str(entry.get("target") or "").startswith(
            "app/astrbot/data/plugins/"
        )
    }
    for index, feature in enumerate(features):
        if not isinstance(feature, dict):
            errors.append(f"feature entry {index} is not an object")
            continue
        module = feature.get("module")
        if not isinstance(module, str) or not module:
            errors.append(f"feature entry {index} has no module")
            continue
        if module in feature_modules:
            errors.append(f"duplicate feature module: {module}")
        feature_modules.add(module)
        if feature.get("configuration") not in _FEATURE_CONFIGURATION_MODES:
            errors.append(f"feature has invalid configuration mode: {module}")
        source_entry = source_features.get(module)
        if source_entry is None:
            errors.append(f"feature is not packaged by source manifest: {module}")
        plugin_entry = delivered_plugins.get(module)
        if plugin_entry is None:
            errors.append(f"feature is missing from plugin manifest: {module}")
        else:
            for field in ("version", "license", "review_status"):
                if not isinstance(plugin_entry.get(field), str) or not plugin_entry[field]:
                    errors.append(f"delivered plugin has no {field}: {module}")
            if plugin_entry.get("ownership") != feature.get("ownership"):
                errors.append(f"plugin ownership does not match feature set: {module}")

    if wheelhouse.get("python_version") != "3.13.9" or wheelhouse.get("platform") != "win_amd64":
        errors.append("runtime wheelhouse must target Python 3.13.9 win_amd64")
    if not isinstance(wheelhouse.get("aggregate_sha256"), str) or not _HASH.fullmatch(
        str(wheelhouse.get("aggregate_sha256", "")).upper()
    ):
        errors.append("runtime wheelhouse aggregate SHA-256 is invalid")
    _validate_ffmpeg_definition(ffmpeg, errors)

    _exact_lock(release / "runtime-requirements.lock", errors)
    _exact_lock(release / "build-requirements.lock", errors)
    try:
        project_version = _version_consistency(root, errors)
    except OSError as exc:
        errors.append(f"cannot validate version metadata: {exc}")
        project_version = None
    _validate_legal_review(
        root,
        legal_review,
        errors=errors,
    )
    _validate_acceptance_evidence(
        root,
        acceptance_evidence,
        project_version=project_version,
        errors=errors,
    )
    _validate_signing_evidence(
        signing_evidence,
        project_version=project_version,
        expected_hook_entries=expected_hook_entries,
        errors=errors,
    )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate commercial release metadata.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors = validate_release_metadata(args.root)
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
