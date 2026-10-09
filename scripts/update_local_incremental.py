"""One local update workflow: source assembly, delta, transactional application.

Never rebuilds or copies Python/FFmpeg, publishes files, or starts the robot.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from wechatrebot_manager.payload import _is_user_config, sha256_file, validate_incremental_path
from source_inputs import repository_sources, verify_source_inputs
from wechatrebot_manager.licensing.bundle import RUNTIME_FILES, validate_runtime_bundle
from wechatrebot_manager.licensing.config import _read_channel

# Changes to these inputs require a compatible base runtime, not a script patch.
BASE_INPUTS = {
    "release/runtime-requirements.lock": "legal/runtime-requirements.lock",
    "release/runtime-wheelhouse.json": "legal/runtime-wheelhouse.json",
    "release/ffmpeg-runtime.json": "legal/ffmpeg-runtime.json",
    "release/hook-binaries.json": "legal/hook-binaries.json",
    "patches/astrbot-4.26.6-wechat.patch": "legal/corresponding-source/astrbot-4.26.6-wechat.patch",
    "patches/astrbot-4.26.6-outbound-policy.patch": "legal/corresponding-source/astrbot-4.26.6-outbound-policy.patch",
    "patches/astrbot-4.26.6-license.patch": "legal/corresponding-source/astrbot-4.26.6-license.patch",
    **{f"src/wechatrebot_manager/{name}": f"runtime/python/Lib/site-packages/wechatrebot_manager/{name}"
       for name in RUNTIME_FILES},
}
MANAGER_INPUTS_FILE = "legal/manager-build-inputs.json"


def manager_build_inputs(root: Path) -> dict[str, str]:
    return {name: sha256_file(root / name) for name in (
        "scripts/build_manager.py", "scripts/manager_entry.py", "release/windows-version-info.txt",
        "release/build-requirements.lock", "pyproject.toml",
    )}


def remove_working_directory(path: Path, parent: Path) -> None:
    """Remove only a newly created direct child, never traverse a junction."""
    if not path.exists():
        return
    if path.is_symlink() or path.is_junction() or path.resolve().parent != parent.resolve():
        raise RuntimeError(f"临时目录不在预期位置，拒绝清理：{path}")
    shutil.rmtree(path)


def installed_root() -> Path:
    locator = Path(os.environ["LOCALAPPDATA"]) / "WeChatReBot/install-location.json"
    value = json.loads(locator.read_text("utf-8-sig"))
    root = Path(value["install_root"]).resolve()
    if not (root / ".wechatrebot-install.json").is_file():
        raise RuntimeError("未找到已安装的机器人，请先完成首次安装。")
    return root


def require_master(root: Path) -> None:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True, encoding="utf-8").strip()
    subprocess.run(["git", "fetch", "origin", "master"], cwd=root, check=True)
    if git("branch", "--show-current") != "master" or git("rev-parse", "HEAD") != git("rev-parse", "origin/master"):
        raise RuntimeError("请从已同步 origin/master 的 master 工作树执行本地更新。")
    protected = ("src/", "runtime/", "scripts/", "patches/", "release/", "assets/", "config/", "installer/", "vendor/", "pyproject.toml")
    changed = git("diff", "HEAD", "--name-only").splitlines()
    changed += git("ls-files", "--others", "--exclude-standard").splitlines()
    if any(path.startswith(protected) for path in changed):
        raise RuntimeError("构建输入存在未提交改动，请先提交并同步 master。")


def verify_base_inputs(root: Path, install: Path) -> None:
    for source, target in BASE_INPUTS.items():
        current, previous = root / source, install / target
        if not previous.is_file() or sha256_file(current) != sha256_file(previous):
            raise RuntimeError(f"底层组件基线不兼容：{source}。本次需要更新运行环境；未启动完整构建。")
    from build_runtime import PYTHON_SHA256, PYTHON_VERSION
    runtime = json.loads((install / "runtime/runtime-manifest.json").read_text("utf-8-sig"))
    python = runtime.get("python", {})
    if python.get("version") != PYTHON_VERSION or python.get("sha256") != PYTHON_SHA256:
        raise RuntimeError("底层组件基线不兼容：Python 版本或发行文件已变化；未启动完整构建。")


def verify_manager_license_base(root: Path, install: Path) -> None:
    """Check only the shared authorization files when either package enforces it."""
    site_packages = install / "runtime/python/Lib/site-packages"
    channels = (
        root / "src/wechatrebot_manager/licensing-channel.json",
        install / "manager/src/wechatrebot_manager/licensing-channel.json",
        site_packages / "wechatrebot_manager/licensing-channel.json",
    )
    if not any(path.exists() and _read_channel(path).enabled for path in channels):
        return
    try:
        runtime = json.loads((install / "runtime/runtime-manifest.json").read_text("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("授权运行时清单缺失或无效，本次需要完整载荷更新；未启动构建。") from exc
    if not isinstance(runtime, dict):
        raise RuntimeError("授权运行时清单无效，本次需要完整载荷更新；未启动构建。")
    validate_runtime_bundle(root, site_packages, runtime.get("licensing_bundle_sha256"))
    if runtime.get("astrbot_license_patch_sha256") != sha256_file(root / "patches/astrbot-4.26.6-license.patch"):
        raise RuntimeError("授权核心补丁缺失或已变更，本次需要完整载荷更新；未启动构建。")


def manager_changed(root: Path, install: Path) -> bool:
    """Compare only manager sources and its build lock, never the runtime tree."""
    # Match the source manifest's delivered source set without copying it.
    source = root / "src/wechatrebot_manager"
    files = [p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    for path in files:
        target = install / "manager/src/wechatrebot_manager" / path.relative_to(source)
        if not target.is_file() or sha256_file(path) != sha256_file(target):
            return True
    manifest = json.loads((install / "config/installed-payload.json").read_text("utf-8-sig"))
    current_names = {"manager/src/wechatrebot_manager/" + p.relative_to(source).as_posix() for p in files}
    if any(item["path"].startswith("manager/src/wechatrebot_manager/") and item["path"] not in current_names
           for item in manifest["files"]):
        return True
    for source_name, target_name in (("pyproject.toml", "manager/pyproject.toml"),
                                      ("release/build-requirements.lock", "legal/build-requirements.lock")):
        previous = install / target_name
        if not previous.is_file() or sha256_file(root / source_name) != sha256_file(previous):
            return True
    previous_inputs = install / MANAGER_INPUTS_FILE
    if not previous_inputs.is_file() or json.loads(previous_inputs.read_text("utf-8")) != manager_build_inputs(root):
        return True
    return not (install / "manager/WeChatReBot.exe").is_file()


def source_scopes(root: Path, install: Path, *, manager_only: bool = False) -> tuple[list[dict], list[str]]:
    """Choose complete owned components; never claim arbitrary user plugin dirs."""
    from wechatrebot_manager.payload import PayloadError
    manifest = json.loads((root / "release/source-manifest.json").read_text("utf-8"))
    entries, scopes = [], []
    for entry in manifest["entries"]:
        target = entry["target"]
        if manager_only and not (target.startswith("manager/") or target == "legal/build-requirements.lock"):
            continue
        if _is_user_config(Path(target)):
            continue  # New defaults come from plugin schemas; retain real user config.
        if target.startswith("app/bridge/vendor/"):
            previous = install / target
            if not previous.is_file() or sha256_file(previous) != str(entry.get("sha256", "")).upper():
                raise RuntimeError(f"桥接二进制不兼容：{target}，需要更新底层组件。")
            continue
        try:
            validate_incremental_path(target)
        except PayloadError as exc:
            raise RuntimeError(f"此构建输入尚不支持增量更新：{target}") from exc
        entries.append(entry)
        scopes.append(target)
    if not manager_only:
        scopes.append("legal/corresponding-source")  # Includes generated reviewed patches.
        old = json.loads((install / "legal/plugin-manifest.json").read_text("utf-8"))
        new = json.loads((root / "release/plugin-manifest.json").read_text("utf-8"))
        old_names = {p["name"] for p in old["plugins"] if p.get("ownership") != "excluded"}
        new_names = {p["name"] for p in new["plugins"] if p.get("ownership") != "excluded"}
        scopes.extend("app/astrbot/data/plugins/" + name for name in sorted(old_names - new_names))
    # A directory scope already covers its source overlay children.
    unique = sorted(set(scopes), key=lambda s: (len(s.split("/")), s))
    return entries, [s for i, s in enumerate(unique) if not any(s.startswith(parent + "/") for parent in unique[:i])]


def stage_sources(root: Path, source: Path, install: Path, output: Path, *, manager_only: bool = False) -> list[str]:
    from build_staging import (_copy_source_entry, _remove_embedded_checkin_accounts,
                               _sanitize_project_source_credentials, _apply_bridge_patch, _apply_plugin_patches)
    entries, scopes = source_scopes(root, install, manager_only=manager_only)
    if output.exists():
        raise RuntimeError("增量暂存目录已存在，拒绝覆盖。")
    output.mkdir(parents=True)
    for entry in entries:
        base = root if entry["origin"] == "commercial" else source
        path = base / entry["source"]
        if not path.exists():
            raise RuntimeError(f"构建输入缺失：{entry['source']}")
        _copy_source_entry(path, output / entry["target"], entry)
    if not manager_only:
        _remove_embedded_checkin_accounts(output)
        _sanitize_project_source_credentials(output)
        if (output / "app/bridge").is_dir():
            _apply_bridge_patch(root, output)
        _apply_plugin_patches(root, output)
        for scope in scopes:
            if scope.startswith("app/astrbot/data/plugins/") and not (output / scope).exists():
                (output / scope).mkdir(parents=True)  # Explicit retired-plugin scope.
    return scopes


def apply_local(payload_archive: Path, install: Path) -> None:
    from wechatrebot_manager.updates import apply_update_plan, assert_update_runtime_stopped
    from wechatrebot_manager.payload import verify_payload, validate_incremental_base
    from wechatrebot_manager.uninstaller import _iter_windows_process_ids
    from wechatrebot_manager.updates import _process_executable
    verified = verify_payload(payload_archive)
    validate_incremental_base(payload_archive, install)
    assert_update_runtime_stopped(payload_archive, install)
    # Do not forcibly terminate apps or start the robot to validate an update.
    changed_manager = any(item.relative_path.as_posix() == "manager/WeChatReBot.exe" for item in verified.files)
    if changed_manager:
        executables = {install / "manager/WeChatReBot.exe", install / "启动机器人.exe", install / "卸载机器人.exe"}
        for pid in _iter_windows_process_ids():
            executable = _process_executable(pid)
            if executable is not None and executable.resolve() in executables:
                raise RuntimeError("请先关闭管理器窗口，再重新运行本地增量更新。")
    update_id = uuid.uuid4().hex
    working = install / "updates" / ("local-" + update_id)
    payload = working / "payload"
    payload.mkdir(parents=True)
    with zipfile.ZipFile(payload_archive) as archive:
        # verify_payload already validated every member and rejected extras/links.
        archive.extractall(payload)
    version = str(verified.manifest.get("release_version") or "")
    plan_path = working / "update-plan.json"
    plan_path.write_text(json.dumps({
        "schema_version": 1, "update_id": update_id, "version": version,
        "install_root": str(install), "payload_root": str(payload),
        "target_executable": str(install / "manager/WeChatReBot.exe"),
        "parent_pid": 0, "wait_pids": [0],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if apply_update_plan(plan_path, restart=False):
        raise RuntimeError("增量安装未完成，请查看安装目录 logs/update.log；已尝试回滚。")
    remove_working_directory(working, install / "updates")
    print("本地增量更新完成。机器人保持停止，请按需手动启动验证。", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT / "vendor")
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--plan", action="store_true", help="只检查基线与更新范围，不构建或安装")
    parser.add_argument("--package-only", action="store_true", help="只生成本地增量包")
    parser.add_argument("--manager-only", action="store_true", help="只更新管理器，不扫描插件与运行环境")
    args = parser.parse_args(argv)
    try:
        source_root = repository_sources(ROOT, args.source_root)
        if not args.manager_only:
            verify_source_inputs(ROOT, source_root)
        install = (args.install_root or installed_root()).resolve()
        require_master(ROOT)
        if args.manager_only:
            verify_manager_license_base(ROOT, install)
        else:
            verify_base_inputs(ROOT, install)
        entries, scopes = source_scopes(ROOT, install, manager_only=args.manager_only)
        needs_manager = manager_changed(ROOT, install)
        print(json.dumps({"mode": "incremental", "manager_build_required": needs_manager,
                          "runtime_build_required": False, "components": scopes,
                          "note": "更新需要停止机器人；不会自动启动或上传。"}, ensure_ascii=False, indent=2), flush=True)
        if args.plan:
            return 0
        from build_incremental_update import build_incremental_update
        from build_local_package import select_build_python, run_step
        from release_guard import scan_release
        output = ROOT / "artifacts" / ("local-incremental-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        output.mkdir(parents=True)
        candidate = output / "candidate"
        scopes = stage_sources(ROOT, source_root, install, candidate, manager_only=args.manager_only)
        if needs_manager:
            python = select_build_python(ROOT)
            command = [sys.executable, str(ROOT / "scripts/build_manager.py"), "--output", str(output / "manager-build")]
            if python:
                command += ["--build-python", str(python)]
            run_step("manager", command, output)
            executable = output / "manager-build/dist/WeChatReBot.exe"
            (candidate / "manager").mkdir(exist_ok=True)
            shutil.copy2(executable, candidate / "manager/WeChatReBot.exe")
            scopes.append("manager/WeChatReBot.exe")
            (candidate / MANAGER_INPUTS_FILE).parent.mkdir(parents=True, exist_ok=True)
            (candidate / MANAGER_INPUTS_FILE).write_text(
                json.dumps(manager_build_inputs(ROOT), indent=2), encoding="utf-8"
            )
            scopes.append(MANAGER_INPUTS_FILE)
        findings = scan_release(candidate)
        if findings:
            raise RuntimeError("增量源文件检查未通过：" + json.dumps(findings[:5], ensure_ascii=False))
        version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
        archive = output / "incremental.zip"
        report = build_incremental_update(candidate, install, archive, managed_roots=scopes, version=version)
        (output / "update-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if archive.is_file() and not args.package_only:
            apply_local(archive, install)
        remove_working_directory(candidate, output)
        # Keep an original build environment for reuse; large compiler intermediates
        # and duplicate executables are no longer needed once the ZIP is verified.
        for name in ("work", "spec", "dist"):
            remove_working_directory(output / "manager-build" / name, output / "manager-build")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"本地增量更新未执行完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
