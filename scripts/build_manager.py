from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def _run(argv: list[str], *, cwd: Path, timeout: int | None = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True, timeout=timeout)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the frozen Windows manager.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--build-python", type=Path,
        help="reuse an existing Python 3.13 x64 build environment instead of reinstalling tools",
    )
    args = parser.parse_args(argv)

    commercial_root = Path(__file__).resolve().parents[1]
    artifacts_root = (commercial_root / "artifacts").resolve()
    output = args.output.expanduser().resolve()
    if output == artifacts_root or artifacts_root not in output.parents:
        parser.error("output must stay inside the commercial artifacts directory")
    if output.exists() and any(output.iterdir()):
        parser.error(f"output must not already contain files: {output}")
    output.mkdir(parents=True, exist_ok=True)

    host_python = args.host_python.expanduser().resolve()
    if sys.version_info[:2] != (3, 13) or platform.architecture()[0] != "64bit":
        parser.error("build_manager.py itself must run under Python 3.13 x64")
    _run(
        [
            str(host_python),
            "-c",
            "import platform,sys;"
            "assert sys.version_info[:2] == (3,13);"
            "assert platform.architecture()[0] == '64bit'",
        ],
        cwd=commercial_root,
    )

    if args.build_python is not None:
        build_python = args.build_python.expanduser().resolve()
        if not build_python.is_file():
            parser.error(f"build Python does not exist: {build_python}")
        _run(
            [
                str(build_python), "-c",
                "import platform,sys,PyInstaller;"
                "assert sys.version_info[:2] == (3,13);"
                "assert platform.architecture()[0] == '64bit';"
                "assert PyInstaller.__version__ == '6.22.2'",
            ],
            cwd=commercial_root,
        )
    else:
        build_venv = output / "build-venv"
        _run([str(host_python), "-m", "venv", str(build_venv)], cwd=commercial_root)
        build_python = build_venv / "Scripts" / "python.exe"
        requirements = commercial_root / "release" / "build-requirements.lock"
        _run(
            [
                str(build_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--requirement",
                str(requirements),
            ],
            cwd=commercial_root,
        )

    dist = output / "dist"
    _run(
        [
            str(build_python),
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--windowed",
            "--name",
            "WeChatReBot",
            "--icon",
            str(commercial_root / "src" / "wechatrebot_manager" / "app_webui" / "assets" / "app-icon.ico"),
            "--distpath",
            str(dist),
            "--workpath",
            str(output / "work"),
            "--specpath",
            str(output / "spec"),
            "--paths",
            str(commercial_root / "src"),
            "--add-data",
            f"{commercial_root / 'src' / 'wechatrebot_manager' / 'plugin_webui'}{os.pathsep}wechatrebot_manager/plugin_webui",
            "--add-data",
            f"{commercial_root / 'src' / 'wechatrebot_manager' / 'app_webui'}{os.pathsep}wechatrebot_manager/app_webui",
            "--add-data",
            f"{commercial_root / 'src' / 'wechatrebot_manager' / 'update-channel.json'}{os.pathsep}wechatrebot_manager",
            "--add-data",
            f"{commercial_root / 'src' / 'wechatrebot_manager' / 'licensing-channel.json'}{os.pathsep}wechatrebot_manager",
            "--add-data",
            f"{commercial_root / 'src' / 'wechatrebot_manager' / 'error-codes.zh-CN.json'}{os.pathsep}wechatrebot_manager",
            "--collect-all",
            "webview",
            "--collect-all",
            "tzdata",
            "--hidden-import",
            "webview.platforms.edgechromium",
            "--version-file",
            str(commercial_root / "release" / "windows-version-info.txt"),
            str(commercial_root / "scripts" / "manager_entry.py"),
        ],
        cwd=commercial_root,
    )
    executable = dist / "WeChatReBot.exe"
    if not executable.is_file():
        raise RuntimeError("PyInstaller did not produce WeChatReBot.exe")
    self_test = output / "manager-self-test.json"
    _run(
        [str(executable), "self-test", "--output", str(self_test)],
        cwd=output,
        timeout=60,
    )
    result = json.loads(self_test.read_text(encoding="utf-8"))
    if (
        not result.get("ok") or not result.get("frozen")
        or result.get("architecture") != 64 or not result.get("timezone_data")
    ):
        raise RuntimeError(f"frozen manager self-test failed: {result}")
    manifest = {
        "schema_version": 1,
        "manager_version": result["manager_version"],
        "filename": executable.name,
        "size": executable.stat().st_size,
        "sha256": _sha256(executable),
        "signed": False,
        "self_test": result,
    }
    (output / "manager-build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
