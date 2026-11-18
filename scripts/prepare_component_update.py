"""Prepare component payloads from explicit, inventoried release baselines.

Snapshots retain the complete managed inventory, but copy only component files
and compatibility metadata. Python and FFmpeg are never scanned or rebuilt.
This module never discovers a local installation, starts apps, or publishes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tomllib

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import update_local_incremental as local
from build_incremental_update import COMPONENT_ROOTS, _checked_path, _in_scope, build_incremental_update
from release_guard import scan_release
from source_inputs import verify_source_inputs
from wechatrebot_manager.payload import (
    PayloadError, _fingerprint_manifest, _is_user_config, _manifest_specs,
    _safe_relative_path, install_payload, installed_payload_fingerprint, sha256_file,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _absolute(path: Path) -> Path:
    path = path.expanduser().absolute()
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise PayloadError(f"发布基线和输出路径不能经过链接：{path}")
    return path.resolve()


def _new_output(output: Path, sources: list[Path]) -> Path:
    output = _absolute(output)
    if output.exists():
        raise ValueError(f"输出已存在，拒绝覆盖：{output}")
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in sources):
        raise ValueError("输出目录必须与输入基线和源码目录完全分离。")
    output.mkdir(parents=True)
    return output


def _baseline_manifest(source: Path) -> dict:
    manifest_path = _checked_path(source, Path("config/installed-payload.json"))
    if not manifest_path.exists():
        manifest_path = _checked_path(source, Path("release-manifest.json"))
    raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise PayloadError("发布基线必须提供完整的 schema 1 受管清单。")
    specs, _ = _manifest_specs(raw)
    manifest = dict(raw)
    manifest["files"] = [
        {"path": relative.as_posix(), "sha256": digest} for relative, digest in specs
    ]
    if "release_version" in manifest:
        version = manifest["release_version"]
        if not isinstance(version, str) or str(Version(version)) != version:
            raise ValueError("发布基线版本必须使用规范版本号。")
    build = manifest.get("release_build")
    if build is not None and (isinstance(build, bool) or not isinstance(build, int) or build < 0):
        raise ValueError("发布基线构建号必须为非负整数。")
    return manifest


def _snapshot_file(name: str) -> bool:
    relative = _safe_relative_path(name)
    if _is_user_config(relative):
        return False
    key = relative.as_posix().casefold()
    return (
        _in_scope(key, COMPONENT_ROOTS)
        or key in {value.casefold() for value in local.BASE_INPUTS.values()}
        or key == "runtime/runtime-manifest.json"
        or key.startswith("app/bridge/vendor/")
    )


def _copy_verified(source: Path, output: Path, entry: dict) -> None:
    relative = _safe_relative_path(entry["path"])
    original = _checked_path(source, relative)
    if not original.is_file() or sha256_file(original) != entry["sha256"]:
        raise PayloadError(f"发布基线文件缺失或摘要不符：{entry['path']}")
    target = _checked_path(output, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original, target)
    if sha256_file(target) != entry["sha256"]:
        raise PayloadError(f"发布基线文件复制校验失败：{entry['path']}")


def snapshot_baseline(source: Path, output: Path, *, version: str | None = None) -> Path:
    """Copy an explicit staging/snapshot inventory without traversing its runtime."""
    source = _absolute(source)
    if not source.is_dir():
        raise ValueError(f"发布基线目录不存在：{source}")
    manifest = _baseline_manifest(source)
    if version is not None:
        if str(Version(version)) != version:
            raise ValueError("发布基线版本必须使用规范版本号。")
        if manifest.get("release_version", version) != version:
            raise ValueError("指定基线版本与受管清单不同。")
        manifest["release_version"] = version
    output = _new_output(output, [source])
    copied = []
    for entry in manifest["files"]:
        if _snapshot_file(entry["path"]):
            _copy_verified(source, output, entry)
            copied.append(entry["path"])
    _write_json(output / "config/installed-payload.json", manifest)
    _write_json(output / "component-baseline.json", {
        "schema_version": 1, "fingerprint": _fingerprint_manifest(manifest),
        "release_version": manifest.get("release_version"), "copied_files": copied,
    })
    return output


def _manager_executable(root: Path, output: Path, version: str, manager_build: Path | None) -> Path:
    from build_local_package import run_step, select_build_python
    from publish_manager_update import _validated_manager

    if manager_build is None:
        manager_build = output / "manager-build"
        command = [sys.executable, str(root / "scripts/build_manager.py"), "--output", str(manager_build)]
        python = select_build_python(root)
        if python:
            command += ["--build-python", str(python)]
        run_step("manager", command, output)
    return _validated_manager(_absolute(manager_build), version)


def prepare_component_payloads(
    root: Path, source_root: Path, baselines: list[Path], output: Path, *,
    version: str, build: int | None = None, manager_build: Path | None = None,
) -> dict:
    """Prepare one exact-base payload and reusable target snapshot per baseline.

    All writes, including transactional target-inventory merging, are confined
    to a new preparation directory. Input baselines are never installed into.
    """
    root, source_root = _absolute(root), _absolute(source_root)
    if str(Version(version)) != version:
        raise ValueError("目标版本必须使用规范版本号。")
    if build is not None and (isinstance(build, bool) or not isinstance(build, int) or build < 0):
        raise ValueError("构建号必须为非负整数。")
    if not 1 <= len(baselines) <= 64:
        raise ValueError("组件发布需要 1 到 64 个显式发布基线。")
    project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    if project["project"]["version"] != version:
        raise ValueError("目标版本必须与项目版本一致。")
    source_root = verify_source_inputs(root, source_root)
    if not _absolute(output).is_relative_to(root / "artifacts"):
        raise ValueError("组件准备输出必须位于当前仓库的 artifacts 目录内。")
    inputs = [_absolute(path) for path in baselines]
    fingerprints = [_fingerprint_manifest(_baseline_manifest(path)) for path in inputs]
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("组件发布包含重复基线指纹。")
    output = _new_output(output, [source_root, *inputs])
    prepared, manager_needed = [], False
    for index, (source, fingerprint) in enumerate(zip(inputs, fingerprints)):
        baseline = snapshot_baseline(source, output / "baselines" / f"{index:02d}")
        local.verify_base_inputs(root, baseline)
        local.source_scopes(root, baseline)  # Also validates bridge vendor hashes.
        changed = local.manager_changed(root, baseline)
        manager_needed = manager_needed or changed
        prepared.append((baseline, fingerprint, changed))
    manager = _manager_executable(root, output, version, manager_build) if manager_needed else None
    payloads, targets, reports = [], [], []
    for index, (baseline, fingerprint, needs_manager) in enumerate(prepared):
        candidate = output / "candidates" / f"{index:02d}"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        scopes = local.stage_sources(root, source_root, baseline, candidate)
        if needs_manager:
            destination = candidate / "manager/WeChatReBot.exe"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manager, destination)
            _write_json(candidate / local.MANAGER_INPUTS_FILE, local.manager_build_inputs(root))
            scopes.extend(["manager/WeChatReBot.exe", local.MANAGER_INPUTS_FILE])
        findings = scan_release(candidate)
        if findings:
            raise RuntimeError("组件候选检查未通过：" + json.dumps(findings[:5], ensure_ascii=False))
        archive = output / "payloads" / f"component-{fingerprint.lower()}.zip"
        report = build_incremental_update(
            candidate, baseline, archive, managed_roots=scopes, version=version, build=build,
        )
        if not archive.is_file():
            raise ValueError(f"基线 {fingerprint} 没有组件变化，未生成空更新包。")
        target = snapshot_baseline(baseline, output / "targets" / f"{index:02d}")
        # Reuse the installer's exact inventory merge/removal transaction inside
        # this isolated publication cache, never an actual application install.
        install_payload(archive, target)
        target_fingerprint = installed_payload_fingerprint(target)
        record = json.loads((target / "component-baseline.json").read_text("utf-8"))
        inventory = _baseline_manifest(target)
        record.update(fingerprint=target_fingerprint, release_version=version, copied_files=[
            entry["path"] for entry in inventory["files"] if _snapshot_file(entry["path"])
        ])
        _write_json(target / "component-baseline.json", record)
        report.update(target_baseline=str(target), target_fingerprint=target_fingerprint,
                      manager_build_required=needs_manager)
        _write_json(output / "reports" / f"{fingerprint.lower()}.json", report)
        payloads.append(archive)
        targets.append(target)
        reports.append(report)
    result = {"payloads": payloads, "target_baselines": targets, "reports": reports,
              "manager_build_required": manager_needed, "runtime_build_required": False}
    _write_json(output / "preparation-report.json", {
        **result, "payloads": [str(path) for path in payloads],
        "target_baselines": [str(path) for path in targets], "version": version, "build": build,
    })
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot-source", type=Path)
    mode.add_argument("--baseline", type=Path, action="append")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--build", type=int)
    parser.add_argument("--manager-build", type=Path)
    args = parser.parse_args(argv)
    if args.snapshot_source:
        snapshot_baseline(args.snapshot_source, args.output, version=args.version)
    else:
        version = args.version or tomllib.loads((args.root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
        prepare_component_payloads(
            args.root, args.source_root or args.root / "vendor", args.baseline, args.output,
            version=version, build=args.build, manager_build=args.manager_build,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
