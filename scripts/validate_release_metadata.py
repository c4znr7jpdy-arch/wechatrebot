from __future__ import annotations

import argparse
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


def _version_consistency(root: Path, errors: list[str]) -> None:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project_version = str(project["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"cannot read project version: {exc}")
        return
    if not _VERSION.fullmatch(project_version):
        errors.append(f"project version is not semantic x.y.z: {project_version}")
        return
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


def validate_release_metadata(root: str | Path) -> list[str]:
    root = Path(root).expanduser().resolve()
    errors: list[str] = []
    release = root / "release"
    source = _load_object(release / "source-manifest.json", errors)
    hook = _load_object(release / "hook-binaries.json", errors)
    plugins = _load_object(release / "plugin-manifest.json", errors)
    feature_set = _load_object(release / "feature-set.json", errors)
    wheelhouse = _load_object(release / "runtime-wheelhouse.json", errors)

    if source.get("schema_version") != 1:
        errors.append("source manifest schema must be 1")
    status = source.get("status")
    if not isinstance(status, str) or not status:
        errors.append("source manifest status is missing")
    entries = source.get("entries")
    if not isinstance(entries, list):
        errors.append("source manifest entries must be a list")
        entries = []

    targets: set[str] = set()
    gated_hook_entries: dict[str, str] = {}
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
        if entry.get("gate") == "hook-binary-redistribution":
            digest = entry.get("sha256")
            if not isinstance(source_path, str) or not isinstance(digest, str):
                errors.append(f"hook-gated entry {index} is incomplete")
            else:
                gated_hook_entries[source_path] = digest.upper()

    required_targets = {
        "legal/feature-set.json",
        "legal/hook-binaries.json",
        "legal/plugin-manifest.json",
        "legal/runtime-requirements.lock",
        "legal/runtime-wheelhouse.json",
        "legal/release-checklist.md",
    }
    missing_targets = sorted(required_targets - commercial_targets)
    if missing_targets:
        errors.append(f"required commercial targets are missing: {missing_targets}")

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
    if gated_hook_entries != expected_hook_entries:
        errors.append("source manifest hook gates do not match hook-binaries.json")

    features = feature_set.get("features")
    if feature_set.get("schema_version") != 1 or not isinstance(features, list):
        errors.append("feature set schema or feature list is invalid")
        features = []
    feature_modules: set[str] = set()
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
        elif source_entry.get("gate") != feature.get("gate"):
            errors.append(f"feature gate does not match source manifest: {module}")

    if status == "ready":
        if hook.get("redistribution_approved") is not True:
            errors.append("ready release requires hook redistribution approval")
        if hook.get("security_review") != "approved":
            errors.append("ready release requires approved hook security review")
        if hook.get("real_device_validation") != "approved":
            errors.append("ready release requires approved hook real-device validation")
        if plugins.get("status") != "approved":
            errors.append("ready release requires an approved plugin manifest")
        if feature_set.get("status") != "approved":
            errors.append("ready release requires an approved feature set")

    if wheelhouse.get("python_version") != "3.13.9" or wheelhouse.get("platform") != "win_amd64":
        errors.append("runtime wheelhouse must target Python 3.13.9 win_amd64")
    if not isinstance(wheelhouse.get("aggregate_sha256"), str) or not _HASH.fullmatch(
        str(wheelhouse.get("aggregate_sha256", "")).upper()
    ):
        errors.append("runtime wheelhouse aggregate SHA-256 is invalid")

    _exact_lock(release / "runtime-requirements.lock", errors)
    _exact_lock(release / "build-requirements.lock", errors)
    try:
        _version_consistency(root, errors)
    except OSError as exc:
        errors.append(f"cannot validate version metadata: {exc}")
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
