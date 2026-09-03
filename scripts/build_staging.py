from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path


def _copy(source: Path, target: Path, exclude: tuple[str, ...] = ()) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                "*.pyc",
                ".pytest_cache",
                "*.egg-info",
                ".git",
                ".github",
                ".env",
                ".env.*",
                "*.log",
                "*.db",
                "*.sqlite",
                "*.sqlite3",
                "test_*.py",
                "tests",
                *exclude,
            ),
        )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _validate_runtime_root(runtime_root: Path, commercial_root: Path) -> dict[str, object]:
    python_exe = runtime_root / "python" / "python.exe"
    runtime_manifest_path = runtime_root / "runtime-manifest.json"
    if not python_exe.is_file():
        raise RuntimeError(f"portable Python is missing: {python_exe}")
    try:
        runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
        expected = json.loads(
            (commercial_root / "release" / "runtime-wheelhouse.json").read_text(
                encoding="utf-8"
            )
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"runtime manifest is missing: {runtime_manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"runtime manifest is invalid: {exc}") from exc
    if runtime_manifest.get("schema_version") != 1:
        raise RuntimeError("runtime manifest schema is unsupported")
    python = runtime_manifest.get("python")
    if not isinstance(python, dict) or (
        python.get("version"), python.get("architecture")
    ) != (expected["python_version"], "x64"):
        raise RuntimeError("runtime Python identity does not match the release lock")
    wheelhouse = runtime_manifest.get("wheelhouse")
    if not isinstance(wheelhouse, dict):
        raise RuntimeError("runtime wheelhouse fingerprint is missing")
    for key in ("wheel_count", "total_bytes", "aggregate_sha256"):
        if wheelhouse.get(key) != expected.get(key):
            raise RuntimeError(f"runtime wheelhouse fingerprint mismatch: {key}")
    return runtime_manifest


def _validate_staged_runtime(output: Path) -> None:
    python_exe = output / "runtime" / "python" / "python.exe"
    bridge_source = output / "app" / "bridge" / "src"
    code = (
        "import importlib.metadata as m,sys;"
        "sys.path.insert(0,sys.argv.pop(1));"
        "import aiohttp,astrbot,PIL,websockets;"
        "import hook_bridge;"
        "assert sys.maxsize > 2**32;"
        "assert sys.version_info[:2] == (3,13);"
        "assert m.version('AstrBot') == '4.26.6'"
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(
        [str(python_exe), "-B", "-I", "-c", code, str(bridge_source)],
        cwd=output,
        env=environment,
        check=True,
    )


def _validate_manager_exe(value: Path) -> Path:
    manager_exe = value.expanduser().resolve()
    if not manager_exe.is_file() or manager_exe.suffix.casefold() != ".exe":
        raise RuntimeError(f"frozen manager executable is invalid: {manager_exe}")
    with manager_exe.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise RuntimeError("frozen manager executable is not a Windows PE file")
        handle.seek(0x3C)
        offset_bytes = handle.read(4)
        if len(offset_bytes) != 4:
            raise RuntimeError("frozen manager executable has an invalid PE header")
        pe_offset = struct.unpack("<I", offset_bytes)[0]
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\x00\x00":
            raise RuntimeError("frozen manager executable has an invalid PE signature")
        machine_bytes = handle.read(2)
        if len(machine_bytes) != 2 or struct.unpack("<H", machine_bytes)[0] != 0x8664:
            raise RuntimeError("frozen manager executable must be Windows x64")
    return manager_exe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help="validated output directory created by scripts/build_runtime.py",
    )
    parser.add_argument(
        "--manager-exe",
        type=Path,
        help="frozen WeChatReBot.exe created by scripts/build_manager.py",
    )
    parser.add_argument("--allow-gate", action="append", default=[])
    args = parser.parse_args(argv)

    commercial_root = Path(__file__).resolve().parents[1]
    source_root = args.source_root.resolve()
    output = (args.output or commercial_root / "artifacts" / "staging").resolve()
    artifacts_root = (commercial_root / "artifacts").resolve()
    if output != artifacts_root and artifacts_root not in output.parents:
        parser.error("output must stay inside the commercial artifacts directory")
    if output.exists() and any(output.iterdir()):
        parser.error(f"output must not already contain files: {output}")

    manifest = json.loads(
        (commercial_root / "release" / "source-manifest.json").read_text(encoding="utf-8")
    )
    copied: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for entry in manifest["entries"]:
        gate = entry.get("gate")
        if gate and gate not in args.allow_gate:
            skipped.append({"source": entry["source"], "gate": gate})
            continue
        origin = commercial_root if entry["origin"] == "commercial" else source_root
        source = (origin / entry["source"]).resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        expected_sha256 = entry.get("sha256")
        if expected_sha256:
            if not source.is_file():
                raise RuntimeError(f"hash-pinned source is not a file: {source}")
            actual_sha256 = _sha256(source)
            if actual_sha256 != str(expected_sha256).upper():
                raise RuntimeError(
                    f"source hash mismatch: {entry['source']}; "
                    f"expected {expected_sha256}, actual {actual_sha256}"
                )
        target = output / entry["target"]
        excludes = entry.get("exclude", [])
        if not isinstance(excludes, list) or not all(
            isinstance(value, str) and value for value in excludes
        ):
            raise RuntimeError(f"invalid exclude list: {entry['source']}")
        _copy(source, target, tuple(excludes))
        copied.append({"source": str(source), "target": target.relative_to(output).as_posix()})

    runtime_summary: dict[str, object] | None = None
    if args.runtime_root:
        runtime_root = args.runtime_root.expanduser().resolve()
        runtime_manifest = _validate_runtime_root(runtime_root, commercial_root)
        _copy(runtime_root / "python", output / "runtime" / "python")
        _copy(
            runtime_root / "runtime-manifest.json",
            output / "runtime" / "runtime-manifest.json",
        )
        runtime_summary = {
            "python": runtime_manifest["python"],
            "wheelhouse": runtime_manifest["wheelhouse"],
        }
        _validate_staged_runtime(output)

    if args.manager_exe:
        manager_exe = _validate_manager_exe(args.manager_exe)
        manager_target = output / "manager" / "WeChatReBot.exe"
        _copy(manager_exe, manager_target)
        copied.append(
            {
                "source": str(manager_exe),
                "target": manager_target.relative_to(output).as_posix(),
            }
        )

    hashes = [
        {"path": path.relative_to(output).as_posix(), "sha256": _sha256(path)}
        for path in sorted(output.rglob("*"))
        if path.is_file()
    ]
    (output / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_manifest_status": manifest["status"],
                "copied": copied,
                "skipped_gated_entries": skipped,
                "runtime": runtime_summary,
                "files": hashes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    guard = subprocess.run(
        [sys.executable, str(commercial_root / "scripts" / "release_guard.py"), str(output)],
        check=False,
    )
    return guard.returncode


if __name__ == "__main__":
    raise SystemExit(main())
