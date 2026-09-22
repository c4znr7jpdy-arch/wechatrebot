"""Build a complete local offline installer without publishing or starting services."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def build_steps(source: Path, output: Path, wheelhouse: Path | None = None,
                runtime: Path | None = None, build_python: Path | None = None) -> list[tuple[str, list[str]]]:
    manager = output / "manager-build" / "dist" / "WeChatReBot.exe"
    def command(script: str, *args: object) -> list[str]:
        return [sys.executable, str(ROOT / "scripts" / script), *map(str, args)]
    steps = [
        ("1-manager", command("build_manager.py", "--output", output / "manager-build",
                              *(["--build-python", build_python] if build_python else []))),
        ("2-runtime", command("build_runtime.py", "--output", output / "runtime-build",
                              *(["--wheelhouse", wheelhouse] if wheelhouse else []))),
        ("3-staging", command("build_staging.py", "--source-root", source,
                              "--runtime-root", runtime or output / "runtime-build",
                              "--manager-exe", manager, "--output", output / "staging")),
        ("4-payload", command("build_distribution.py", "--payload", output / "staging",
                              "--output", output / "payload-build")),
        ("5-installer", command("build_native_installer.py", "--manager-exe", manager,
                                "--payload", output / "payload-build" / "payload.wrb",
                                "--output", output / "installer")),
    ]
    return [step for step in steps if not (runtime and step[0] == "2-runtime")]


def worktree_roots(root: Path) -> list[Path]:
    roots = [root]
    result = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode == 0:
        roots.extend(Path(line.removeprefix("worktree ")) for line in result.stdout.splitlines()
                     if line.startswith("worktree "))
    return list(dict.fromkeys(roots))


def select_wheelhouse(root: Path) -> Path:
    """Use only a cache matching this checkout's lock, including linked worktrees."""
    from build_runtime import _verify_wheelhouse
    for candidate_root in worktree_roots(root):
        candidate = candidate_root / "artifacts/runtime-cache/wheels-py313-win-amd64"
        if not candidate.is_dir():
            continue
        try:
            _verify_wheelhouse(root, candidate)
        except (OSError, ValueError, RuntimeError):
            continue
        print(f"Verified dependency cache: {candidate}", flush=True)
        return candidate
    raise RuntimeError("No dependency cache matches release/runtime-wheelhouse.json. "
                       "Restore the approved wheelhouse under artifacts/runtime-cache/wheels-py313-win-amd64. "
                       "Recompiling wheels cannot reproduce their locked hashes.")


def runtime_matches(root: Path, runtime: Path) -> bool:
    from build_runtime import sha256_file
    from build_staging import _validate_runtime_root
    try:
        manifest = json.loads((runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
        inputs = {
            "requirements_sha256": "release/runtime-requirements.lock",
            "astrbot_patch_sha256": "patches/astrbot-4.26.6-wechat.patch",
            "astrbot_outbound_policy_patch_sha256": "patches/astrbot-4.26.6-outbound-policy.patch",
        }
        if any(manifest.get(key) != sha256_file(root / path) for key, path in inputs.items()):
            return False
        _validate_runtime_root(runtime, root)
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def select_runtime(root: Path) -> Path | None:
    for tree in worktree_roots(root):
        # Only completed build directories, never live installations or partial builds.
        candidates = sorted((tree / "artifacts").glob("*/runtime-build/runtime-manifest.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for manifest in candidates:
            if runtime_matches(root, manifest.parent):
                print(f"Reusing verified runtime: {manifest.parent}", flush=True)
                return manifest.parent
    return None


def select_build_python(root: Path) -> Path | None:
    probe = (
        "import sys,importlib.metadata as m;from pathlib import Path;"
        "assert sys.version_info[:2]==(3,13) and sys.maxsize>2**32;"
        "pins=[s.strip().split('==') for s in Path(sys.argv[1]).read_text().splitlines() "
        "if s.strip() and not s.lstrip().startswith('#')];"
        "assert all(m.version(name)==version for name,version in pins)"
    )
    for tree in worktree_roots(root):
        candidates = sorted((tree / "artifacts").glob("*/manager-build/build-venv/Scripts/python.exe"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for candidate in candidates:
            try:
                result = subprocess.run([str(candidate), "-c", probe,
                                         str(root / "release/build-requirements.lock")],
                                        capture_output=True, timeout=20)
                if result.returncode == 0:
                    print(f"Reusing manager build environment: {candidate}", flush=True)
                    return candidate
            except (OSError, subprocess.TimeoutExpired):
                continue
    return None


def run_step(name: str, command: list[str], output: Path) -> None:
    print(f"\n===== {name} =====", flush=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    log_path = output / f"{name}.log"
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace") as process:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            code = process.wait()
    if code:
        raise RuntimeError(f"{name} failed (exit {code}). Log: {log_path}")


def finish_package(output: Path, version: str) -> Path:
    setup = output / "installer" / f"WeChatReBot-Setup-{version}.exe"
    if not setup.is_file():
        raise RuntimeError(f"Installer missing: {setup}")
    archive = output / f"WeChatReBot-{version}-local.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as package:
        package.write(setup, setup.name)
        package.writestr("安装说明.txt", (
            f"WeChat ReBot {version} 本地完整测试包\n"
            "解压后双击安装器。完整载荷已内置，无需下载线上载荷。\n"
            "覆盖安装前请先退出机器人和管理器，再选择原安装目录。\n"
            "本流程未上传线上，也未启动机器人。\n"
        ).encode("utf-8-sig"))
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise RuntimeError("ZIP integrity check failed")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest().upper()
    archive.with_suffix(".zip.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii")
    (output / "result.json").write_text(json.dumps({
        "version": version, "archive": str(archive), "bytes": archive.stat().st_size,
        "sha256": digest, "uploaded": False, "services_started": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("E:/Project"))
    parser.add_argument("--plan", action="store_true", help="Show steps without building")
    parser.add_argument("--open-output", action="store_true")
    args = parser.parse_args(argv)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    output = ROOT / "artifacts" / f"local-full-{version}-{datetime.now():%Y%m%d-%H%M%S-%f}"
    source = args.source_root.expanduser().resolve()
    steps = build_steps(source, output)
    if args.plan:
        print(json.dumps({"output": str(output), "steps": steps}, ensure_ascii=False, indent=2))
        return 0
    if sys.version_info[:2] != (3, 13) or sys.maxsize <= 2**32:
        parser.error("Python 3.13 x64 is required")
    if not source.is_dir():
        parser.error(f"Source directory missing: {source}")
    # Fail before costly work when the installer compiler is unavailable.
    from build_native_installer import _iscc
    _iscc(None)
    output.mkdir(parents=True)
    try:
        runtime = select_runtime(ROOT)
        wheelhouse = None if runtime else select_wheelhouse(ROOT)
        build_python = select_build_python(ROOT)
        steps = build_steps(source, output, wheelhouse, runtime, build_python)
        for name, command in steps:
            run_step(name, command, output)
        archive = finish_package(output, version)
    except Exception as exc:
        (output / "failure.txt").write_text(str(exc), encoding="utf-8")
        print(f"\nBUILD FAILED: {exc}\nOutput: {output}", file=sys.stderr)
        return 1
    print(f"\nBUILD COMPLETE: {archive}", flush=True)
    if args.open_output:
        os.startfile(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
