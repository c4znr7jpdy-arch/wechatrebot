"""Build a scoped code/resource update without rebuilding or scanning the runtime.

The candidate contains the complete desired contents of every managed scope.
An existing empty candidate directory explicitly removes a component; a missing
scope is an error. Only baseline-manifest-owned files can become removals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tomllib
from typing import Iterable
import uuid
import zipfile

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from wechatrebot_manager.payload import (
    PAYLOAD_MANIFEST,
    PayloadError,
    _read_installed_manifest_for_merge,
    _safe_relative_path,
    installed_payload_fingerprint,
    sha256_file,
    validate_incremental_path,
    verify_payload,
)


NATIVE_SUFFIXES = {".dll", ".pyd", ".so", ".dylib", ".whl", ".exe"}
RUNTIME_CONTRACTS = {
    "legal/runtime-requirements.lock",
    "legal/runtime-wheelhouse.json",
    "legal/ffmpeg-runtime.json",
    "legal/hook-binaries.json",
}
COMPONENT_ROOTS = (
    "manager", "app/astrbot/data/plugins", "app/astrbot/data/dist",
    "app/astrbot/ai_plugin", "app/astrbot/nonebot-plugin-mystool",
    "app/astrbot/shared", "app/shared", "app/bridge/src",
    "app/bridge/pyproject.toml", "legal", "config/templates",
)


def _validate_candidate_path(relative: Path) -> None:
    # Unchanged native files may be present in a complete component candidate.
    # They will never enter the delta: changes/removals are refused below.
    if relative.suffix.casefold() in NATIVE_SUFFIXES:
        validate_incremental_path(relative.parent)
    else:
        validate_incremental_path(relative)


def _checked_path(root: Path, relative: Path) -> Path:
    candidate = root / relative
    for component in (candidate, *candidate.parents):
        if component.is_symlink() or (
            hasattr(component, "is_junction") and component.is_junction()
        ):
            raise PayloadError(f"增量文件不能经过符号链接或目录联接：{relative.as_posix()}")
        if component == root:
            break
    if not candidate.resolve().is_relative_to(root):
        raise PayloadError(f"增量文件越过根目录：{relative.as_posix()}")
    return candidate


def _in_scope(path: str, scopes: tuple[str, ...]) -> bool:
    key = path.casefold()
    return any(key == scope or key.startswith(scope + "/") for scope in scopes)


def _scopes(values: Iterable[str]) -> tuple[str, ...]:
    normalized = []
    for value in values:
        relative = _safe_relative_path(value)
        # The installer owns the shared code/data boundary; the builder also
        # rejects broad parents which could silently include unmanaged data.
        validate_incremental_path(relative)
        key = relative.as_posix().casefold()
        if not _in_scope(key, COMPONENT_ROOTS):
            raise PayloadError(f"请指定支持的完整组件范围：{value}")
        if key in normalized:
            raise PayloadError(f"增量组件范围重复：{value}")
        normalized.append(key)
    if not normalized:
        raise PayloadError("增量更新必须明确指定 managed_roots。")
    ordered = tuple(sorted(normalized))
    if any(_in_scope(value, ordered[:index]) for index, value in enumerate(ordered)):
        raise PayloadError("增量组件范围不能互相包含。")
    return ordered


def _candidate_files(root: Path, scopes: tuple[str, ...]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for scope in scopes:
        selected = _checked_path(root, _safe_relative_path(scope))
        if not selected.exists():
            raise PayloadError(
                f"候选组件缺失：{scope}；如需删除整个组件，请显式保留同名空目录。"
            )
        pending = [selected]
        while pending:
            path = pending.pop()
            relative = path.relative_to(root)
            _checked_path(root, relative)
            if path.is_dir():
                pending.extend(sorted(path.iterdir(), reverse=True))
                continue
            if not path.is_file():
                raise PayloadError(f"候选组件包含非普通文件：{relative.as_posix()}")
            _validate_candidate_path(relative)
            key = relative.as_posix().casefold()
            if key in result:
                raise PayloadError(f"候选组件包含大小写冲突路径：{relative.as_posix()}")
            result[key] = path
    return result


def _requirements(text: str) -> list[Requirement]:
    requirements = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        value = value.split(" #", 1)[0].strip()
        try:
            requirements.append(Requirement(value))
        except InvalidRequirement as exc:
            raise PayloadError(f"增量更新无法验证依赖声明：{value}") from exc
    return requirements


def _declared_dependencies(path: Path) -> tuple[list[Requirement], SpecifierSet | None] | None:
    name = path.name.casefold()
    if name.startswith("requirements") and name.endswith((".txt", ".in")):
        return _requirements(path.read_text(encoding="utf-8-sig")), None
    if name == "pyproject.toml":
        try:
            metadata = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as exc:
            raise PayloadError(f"插件元数据无法读取：{path.name}") from exc
        project = metadata.get("project", {})
        if not isinstance(project, dict):
            raise PayloadError("插件 project 元数据格式无效。")
        if "dependencies" in project.get("dynamic", []):
            raise PayloadError("增量更新暂不支持动态依赖声明。")
        if metadata.get("tool", {}).get("poetry", {}).get("dependencies"):
            raise PayloadError("增量更新暂不支持 Poetry 依赖变更，请先转换为 requirements.txt。")
        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise PayloadError("插件 dependencies 必须是字符串列表。")
        requires_python = project.get("requires-python")
        if requires_python is not None and not isinstance(requires_python, str):
            raise PayloadError("插件 requires-python 必须是版本约束字符串。")
        try:
            python_specifier = SpecifierSet(requires_python) if requires_python is not None else None
        except InvalidSpecifier as exc:
            raise PayloadError(f"插件 requires-python 版本约束无效：{requires_python}") from exc
        return _requirements("\n".join(dependencies)), python_specifier
    if name in {"setup.py", "setup.cfg", "pipfile", "pipfile.lock", "poetry.lock", "uv.lock"}:
        raise PayloadError(f"增量更新暂不支持此依赖元数据变更：{path.name}")
    return None


def _runtime_environment(
    installed: Path, baseline: dict[str, dict[str, str]]
) -> dict[str, str]:
    """Use the installed, inventoried runtime rather than the builder's Python."""
    entry = baseline.get("runtime/runtime-manifest.json")
    if not entry:
        raise PayloadError("安装基线缺少受信运行时元信息，不能确认插件 Python 兼容性。")
    path = _checked_path(installed, _safe_relative_path(entry["path"]))
    if not path.is_file() or sha256_file(path) != entry["sha256"].upper():
        raise PayloadError("安装基线的运行时元信息已变化，请先修复基线。")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8-sig"))
        python = metadata["python"]
        version = Version(python["version"])
        machine = {"x64": "AMD64", "x86": "x86", "arm64": "ARM64"}[python["architecture"]]
        if metadata.get("schema_version") != 1 or len(version.release) != 3:
            raise ValueError("运行时版本格式无效")
    except (KeyError, TypeError, ValueError, InvalidVersion) as exc:
        raise PayloadError("安装基线的 Python 运行时元信息无效。") from exc
    return {
        "python_version": ".".join(map(str, version.release[:2])),
        "python_full_version": str(version),
        "implementation_version": str(version),
        "implementation_name": "cpython", "platform_python_implementation": "CPython",
        "sys_platform": "win32", "platform_system": "Windows",
        "os_name": "nt", "platform_machine": machine, "extra": "",
    }


def _check_dependencies(
    changed: list[tuple[str, Path, str]],
    installed: Path,
    baseline: dict[str, dict[str, str]],
) -> None:
    requested: list[Requirement] = []
    python_requirements: list[tuple[str, SpecifierSet]] = []
    for name, path, _digest in changed:
        if name.casefold() in RUNTIME_CONTRACTS:
            raise PayloadError(f"底层运行环境声明已变化，需要组件或完整载荷更新：{name}")
        # The delivered manager runs from its frozen executable. Its source
        # metadata describes the separate manager build environment, not the bot.
        if name.casefold().startswith("manager/"):
            continue
        declared = _declared_dependencies(path)
        if declared is not None:
            dependencies, requires_python = declared
            requested.extend(dependencies)
            if requires_python is not None:
                python_requirements.append((name, requires_python))
    if not requested and not python_requirements:
        return
    environment = _runtime_environment(installed, baseline)
    for name, requirement in python_requirements:
        if not requirement.contains(environment["python_full_version"]):
            raise PayloadError(
                f"插件 Python 版本要求不兼容：{name} 需要 {requirement}，"
                f"安装基线为 {environment['python_full_version']}。"
            )
    requested = [
        requirement for requirement in requested
        if not requirement.marker or requirement.marker.evaluate(environment)
    ]
    if not requested:
        return
    key = "legal/runtime-requirements.lock"
    entry = baseline.get(key)
    if not entry:
        raise PayloadError("安装基线缺少受信依赖锁，不能确认新插件依赖。")
    lock = _checked_path(installed, _safe_relative_path(entry["path"]))
    if not lock.is_file() or sha256_file(lock) != entry["sha256"].upper():
        raise PayloadError("安装基线的运行时依赖锁已变化，请先修复基线。")
    pins: dict[str, str] = {}
    for requirement in _requirements(lock.read_text(encoding="utf-8-sig")):
        specs = tuple(requirement.specifier)
        if requirement.url or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            raise PayloadError("安装基线的运行时依赖锁必须使用精确版本。")
        pins[canonicalize_name(requirement.name)] = specs[0].version
    for requirement in requested:
        version = pins.get(canonicalize_name(requirement.name))
        if requirement.url or requirement.extras or version is None or not requirement.specifier.contains(version):
            raise PayloadError(
                f"插件需要安装基线未满足的新依赖：{requirement}；请使用依赖组件或完整载荷更新。"
            )


def build_incremental_update(
    candidate_root: str | Path,
    install_root: str | Path,
    output: str | Path,
    *,
    managed_roots: Iterable[str],
    version: str | None = None,
    build: int | None = None,
    plan: bool = False,
) -> dict[str, object]:
    """Create a root-layout payload ZIP, or report its exact changes with plan=True.

    Only candidate scopes and corresponding installed files are hashed. Runtime
    files outside these scopes are represented by their installed manifest hashes.
    No process is started, stopped, or otherwise changed by this function.
    """
    candidate = Path(candidate_root).expanduser().resolve()
    installed = Path(install_root).expanduser().resolve()
    archive_path = Path(output).expanduser().resolve()
    if not candidate.is_dir() or not installed.is_dir():
        raise PayloadError("候选目录和安装基线目录必须存在。")
    if candidate.is_relative_to(installed) or installed.is_relative_to(candidate):
        raise PayloadError("候选目录必须与现有安装目录完全分离，不能使用运行中的安装目录作为来源。")
    if archive_path.is_relative_to(candidate) or archive_path.is_relative_to(installed):
        raise PayloadError("增量包输出不能位于候选目录或安装目录中。")
    if build is not None and (isinstance(build, bool) or not isinstance(build, int) or build < 0):
        raise PayloadError("release_build 必须是非负整数。")
    if version is not None:
        Version(version)
    scopes = _scopes(managed_roots)
    fingerprint = installed_payload_fingerprint(installed)
    manifest = _read_installed_manifest_for_merge(installed)
    baseline = {entry["path"].casefold(): entry for entry in manifest["files"]}
    candidates = _candidate_files(candidate, scopes)
    owned = {key: entry for key, entry in baseline.items() if _in_scope(key, scopes)}
    installed_sizes: dict[str, int] = {}
    for key, entry in owned.items():
        relative = _safe_relative_path(entry["path"])
        _validate_candidate_path(relative)
        old = _checked_path(installed, relative)
        if not old.is_file() or sha256_file(old) != entry["sha256"].upper():
            raise PayloadError(f"安装基线文件已变化或缺失，不能生成增量更新：{entry['path']}")
        installed_sizes[key] = old.stat().st_size

    changed: list[tuple[str, Path, str]] = []
    unchanged_count = 0
    candidate_bytes = 0
    added_count = 0
    for key, source in sorted(candidates.items()):
        name = source.relative_to(candidate).as_posix()
        digest = sha256_file(source)
        candidate_bytes += source.stat().st_size
        old = owned.get(key)
        if old is not None and old["sha256"].upper() == digest:
            unchanged_count += 1
            continue
        if source.suffix.casefold() in NATIVE_SUFFIXES and not (
            key.startswith("manager/") and source.suffix.casefold() == ".exe"
        ):
            raise PayloadError(f"增量更新暂不包含新的底层二进制：{name}")
        if old is None:
            target = _checked_path(installed, _safe_relative_path(name))
            if target.exists():
                raise PayloadError(f"新增文件与未受管的现有文件冲突：{name}")
            added_count += 1
        changed.append((name, source, digest))
    removed = [entry for key, entry in sorted(owned.items()) if key not in candidates]
    for entry in removed:
        key = entry["path"].casefold()
        if key in RUNTIME_CONTRACTS or (
            Path(key).suffix in NATIVE_SUFFIXES
            and not (key.startswith("manager/") and Path(key).suffix == ".exe")
        ):
            raise PayloadError(f"增量更新暂不支持删除底层运行环境文件：{entry['path']}")
    _check_dependencies(changed, installed, baseline)

    package_manifest: dict[str, object] = {
        "schema_version": 2,
        "update_kind": "incremental",
        "base_fingerprint": fingerprint,
        "managed_roots": list(scopes),
        "files": [{"path": name, "sha256": digest} for name, _, digest in changed],
        "remove": [{"path": item["path"], "sha256": item["sha256"].upper()} for item in removed],
    }
    if version is not None:
        package_manifest["release_version"] = version
    if build is not None:
        package_manifest["release_build"] = build
    report: dict[str, object] = {
        "schema_version": 1,
        "update_kind": "incremental",
        "plan": plan,
        "candidate_root": str(candidate),
        "install_root": str(installed),
        "output": None,
        "base_fingerprint": fingerprint,
        "managed_roots": list(scopes),
        "files": [name for name, _, _ in changed],
        "remove": [item["path"] for item in removed],
        "added_count": added_count,
        "changed_count": len(changed) - added_count,
        "removed_count": len(removed),
        "unchanged_count": unchanged_count,
        "candidate_bytes": candidate_bytes,
        "update_bytes": sum(path.stat().st_size for _, path, _ in changed),
        "removed_bytes": sum(installed_sizes[item["path"].casefold()] for item in removed),
        "archive_bytes": 0,
        "manifest": package_manifest,
    }
    if plan or not (changed or removed):
        return report
    if archive_path.exists():
        raise PayloadError(f"输出已存在，不能覆盖：{archive_path}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_name(archive_path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(PAYLOAD_MANIFEST, json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n")
            for name, source, _digest in changed:
                archive.write(source, name)
        verify_payload(temporary)
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    report["output"] = str(archive_path)
    report["archive_bytes"] = archive_path.stat().st_size
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", action="append", required=True)
    parser.add_argument("--version")
    parser.add_argument("--build", type=int)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = build_incremental_update(
            args.candidate, args.install_root, args.output,
            managed_roots=args.scope, version=args.version, build=args.build, plan=args.plan,
        )
    except (PayloadError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
