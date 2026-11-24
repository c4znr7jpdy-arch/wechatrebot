"""Build the complete application while keeping its environment independently reusable."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import build_runtime
import build_staging
import build_update_release
import build_distribution
import publish_full_update as full
from build_runtime_packages import build_environment, environment_inputs, read_environment
from prepare_component_update import _manager_executable
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.licensing.bundle import install_runtime_bundle
from wechatrebot_manager.payload import PAYLOAD_MANIFEST, sha256_file, verify_payload
from wechatrebot_manager.runtime_manifest import ENGINE_PATH, ENVIRONMENT_MANIFEST


def _runtime_metadata(root: Path, runtime_root: Path) -> dict:
    metadata = build_staging._validate_runtime_root(runtime_root, root, application_only=True)
    if metadata.get("requirements_sha256") != sha256_file(root / "release/runtime-requirements.lock"):
        raise ValueError("运行环境的依赖锁已过期。")
    wheels = metadata.get("wheels", [])
    if build_runtime._wheelhouse_summary(wheels) != metadata["wheelhouse"]:
        raise ValueError("运行环境 wheel 清单与已固定指纹不一致。")
    engine = [item for item in wheels if item["filename"].lower().startswith("astrbot-")]
    if len(engine) != 1:
        raise ValueError("缺少唯一的 AstrBot 引擎 wheel。")
    return {"inputs": environment_inputs(root), "engine_wheel": engine[0], "python": metadata["python"]}


def build_engine(root: Path, output: Path, environment, *, wheelhouse: Path | None, offline: bool):
    wheel = environment.manifest.get("engine_wheel", {})
    name = wheel.get("filename", "")
    if not name.lower().startswith("astrbot-") or not name.endswith(".whl") or Path(name).name != name:
        raise ValueError("签名环境清单缺少已固定的引擎 wheel。")
    cache = wheelhouse or root / "artifacts/runtime-cache/wheels-py313-win-amd64"
    source = cache / name
    if not source.is_file():
        if offline:
            raise ValueError(f"离线引擎 wheel 缺失：{name}")
        cache.mkdir(parents=True, exist_ok=True)
        version = name.split("-")[1]
        subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
                        "--dest", str(cache), f"astrbot=={version}"], check=True, cwd=root)
    if source.stat().st_size != wheel.get("size") or sha256_file(source) != wheel.get("sha256"):
        raise ValueError("AstrBot 引擎 wheel 校验失败。")
    output.mkdir(parents=True, exist_ok=False)
    build_runtime._safe_extract_zip(source, output)
    build_runtime._apply_astrbot_patch(root, output)
    helper = output / "astrbot/core/platform/wechat_group_policy.py"
    shutil.copy2(root / "src/wechatrebot_manager/group_feature_runtime.py", helper)
    install_runtime_bundle(root, output)
    build_runtime._assert_runtime_boundary(output)


def build_application(args, *, root: Path, output: Path, version: str, channel, prefix: str, live_version: str):
    private = build_update_release._signing_key(args.private_key)
    previous_path = args.environment_manifest or root / "artifacts/runtime-environment/latest.json"
    previous = read_environment(previous_path, channel) if previous_path.is_file() else None
    assets = []
    runtime_root = args.runtime_root
    if args.refresh_environment:
        runtime_root = output / "runtime-build"
        command = [sys.executable, str(root / "scripts/build_runtime.py"), "--output", str(runtime_root)]
        if args.wheelhouse:
            command.extend(["--wheelhouse", str(args.wheelhouse)])
        if args.offline:
            command.append("--offline")
        subprocess.run(command, check=True, cwd=root)
    if runtime_root:
        runtime_root = runtime_root.expanduser().resolve()
        metadata = _runtime_metadata(root, runtime_root)
        abi = "cp" + "".join(metadata["python"]["version"].split(".")[:2])
        environment, assets = build_environment(runtime_root, output / "environment", channel=channel,
            private_key=private, prefix=prefix, python_abi=abi, metadata=metadata, previous=previous)
    else:
        if previous is None:
            raise ValueError("首次拆分请指定已验证的 runtime-root；日常更新复用 runtime-environment.json。")
        environment = read_environment(previous_path, channel, root)
    environment_path = output / "runtime-environment.json"
    write_json_atomic(environment_path, environment.manifest)
    assets.append(environment_path)

    manager = _manager_executable(root, output, version, args.manager_build)
    staging = output / "staging"
    build_staging.main(["--source-root", str(args.source_root), "--manager-exe", str(manager), "--output", str(staging)])
    build_engine(root, staging / ENGINE_PATH, environment, wheelhouse=args.wheelhouse, offline=args.offline)
    shutil.copy2(environment_path, staging / ENVIRONMENT_MANIFEST)
    manifest = json.loads((staging / PAYLOAD_MANIFEST).read_text(encoding="utf-8"))
    manifest["application_package"] = 1
    manifest["files"] = [{"path": path.relative_to(staging).as_posix(), "sha256": sha256_file(path)}
                         for path in sorted(staging.rglob("*")) if path.is_file() and path != staging / PAYLOAD_MANIFEST]
    write_json_atomic(staging / PAYLOAD_MANIFEST, manifest)
    verify_payload(staging)
    from release_guard import scan_release
    findings = scan_release(staging)
    if findings:
        raise ValueError(f"应用发布检查失败：{findings[:5]}")

    update = output / "update"
    command = ["--payload", str(staging), "--output", str(update), "--private-key", str(args.private_key),
               "--package-url", f"{prefix}/wechatrebot-{version}-update.zip", "--title", args.title,
               "--minimum-supported-version", args.minimum_supported_version]
    if args.build is not None:
        command.extend(["--build", str(args.build)])
    if args.mandatory:
        command.append("--mandatory")
    for note in args.note:
        command.extend(["--note", note])
    build_update_release.main(command)
    stable = json.loads((update / "stable.json").read_text(encoding="utf-8"))
    stable["environment_size"] = sum(package.size for package in environment.packages)
    stable["runtime_environment"] = {"url": f"{prefix}/{environment_path.name}",
                                    "size": environment_path.stat().st_size, "sha256": sha256_file(environment_path)}
    stable.pop("signature")
    build_update_release._sign_manifest(stable, private)
    write_json_atomic(update / "stable.json", stable)
    release = full._release_from_output(update, version=version, channel=channel, previous_version=live_version)
    customer_root = output / "customer"
    customer_root.mkdir()
    build_distribution._write_payload_archive(staging, customer_root / "payload.wrb")
    customer = full._native_customer_package_from_output(customer_root, version=version, asset_prefix=prefix,
        manager_exe=manager, output_root=output, build_installer=True)
    assets.extend([release.archive_path, customer.archive_path, customer.checksum_path, customer.payload_path])
    return release, assets
