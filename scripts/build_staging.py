from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath


COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from wechatrebot_manager.payload import LEGACY_READY_MANIFEST_STATUS


_SENSITIVE_FIELD = re.compile(
    r"(?i)(?:.*[_-])?(?:api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|"
    r"secret|cookie|authorization|session[_-]?token)"
)
_DOCUMENTED_PLACEHOLDER = re.compile(
    r"(?i)^(?:<|\$\{|your[-_]|example[-_]|sample[-_]|placeholder|xxx|test[-_])"
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_COMMENT_SECRET = re.compile(
    r"(?i)([\"']?(?:.*[_-])?(?:api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|"
    r"secret|cookie|authorization|session[_-]?token)[\"']?\s*[=:]\s*[\"'])"
    r"((?!<|\$\{|your[-_]|example[-_]|sample[-_]|placeholder|xxx|test[-_])[^\"'\r\n]{16,})"
    r"([\"'])"
)
_DOCUMENT_SECRET = re.compile(
    r"(?i)([\"']?(?:.*[_-])?(?:api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|"
    r"secret|cookie|authorization|session(?:[_-]?(?:id|token))?)[\"']?\s*[=:]\s*[\"'])"
    r"((?!<|\$\{|your[-_]|example[-_]|sample[-_]|placeholder|xxx|test[-_])[^\"'\r\n]{16,})"
    r"([\"'])"
)


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


def _copied_record(target: Path, output: Path) -> dict[str, str]:
    """Record package provenance without exposing developer-machine paths."""
    return {"target": target.relative_to(output).as_posix()}


def _apply_plugin_patches(commercial_root: Path, output: Path) -> None:
    prefix = "astrbot-plugin-"
    plugin_manifest_path = commercial_root / "release" / "plugin-manifest.json"
    excluded_modules: set[str] = set()
    if plugin_manifest_path.is_file():
        try:
            plugin_manifest = json.loads(plugin_manifest_path.read_text(encoding="utf-8"))
            excluded_modules = {
                str(item["name"])
                for item in plugin_manifest.get("plugins", [])
                if isinstance(item, dict) and item.get("ownership") == "excluded"
            }
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            excluded_modules = set()
    def patch_order(path: Path) -> tuple[str, int, str]:
        raw = path.stem.removeprefix(prefix)
        module, separator, variant = raw.partition("--")
        return module.casefold(), 1 if separator else 0, variant.casefold()

    for patch in sorted(
        (commercial_root / "patches").glob(f"{prefix}*.patch"),
        key=patch_order,
    ):
        module = patch.stem.removeprefix(prefix).split("--", 1)[0]
        plugin_root = output / "app" / "astrbot" / "data" / "plugins" / module
        if not plugin_root.is_dir():
            if module in excluded_modules:
                continue
            raise RuntimeError(f"plugin patch target is missing: {module}")
        plugin_relative = plugin_root.relative_to(commercial_root).as_posix()
        command = [
            "git",
            "apply",
            "--ignore-space-change",
            "-p1",
            f"--directory={plugin_relative}",
            str(patch),
        ]
        # A source checkout may already include the reviewed change.  Keep
        # the expected failed forward check quiet; the reverse check below
        # decides whether this is an idempotent no-op or a real mismatch.
        check_command = [*command[:2], "--quiet", "--check", *command[2:]]
        try:
            subprocess.run(
                check_command,
                cwd=commercial_root,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            # Some source checkouts already contain the reviewed patch. Keep
            # the patch in corresponding-source without applying it twice.
            try:
                subprocess.run(
                    [
                        *command[:2],
                        "--quiet",
                        "--reverse",
                        "--check",
                        *command[2:],
                    ],
                    cwd=commercial_root,
                    check=True,
                )
            except subprocess.CalledProcessError:
                raise exc
            else:
                print(
                    f"Plugin patch already present; skipped: {patch.name}",
                    flush=True,
                )
        else:
            subprocess.run(command, cwd=commercial_root, check=True)
        patch_target = output / "legal" / "corresponding-source" / patch.name
        patch_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(patch, patch_target)


def _apply_bridge_patch(commercial_root: Path, output: Path) -> None:
    patches = sorted((commercial_root / "patches").glob("hook-bridge-*.patch"))
    bridge_root = output / "app" / "bridge"
    if not patches:
        raise RuntimeError("bridge patches are missing")
    if not bridge_root.is_dir():
        raise RuntimeError("bridge patch target is missing")
    bridge_relative = bridge_root.relative_to(commercial_root).as_posix()
    for patch in patches:
        command = [
            "git",
            "apply",
            "--ignore-space-change",
            "-p2",
            f"--directory={bridge_relative}",
            str(patch),
        ]
        subprocess.run(
            [*command[:2], "--check", *command[2:]],
            cwd=commercial_root,
            check=True,
        )
        subprocess.run(command, cwd=commercial_root, check=True)
        patch_target = output / "legal" / "corresponding-source" / patch.name
        patch_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(patch, patch_target)


def _remove_embedded_checkin_accounts(output: Path) -> None:
    """Remove developer account seeds without copying their values into a patch."""
    source_path = output / "app" / "astrbot" / "ai_plugin" / "ai_plugin" / "checkin.py"
    if not source_path.is_file():
        return
    source = source_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(source_path))
    except SyntaxError as exc:
        raise RuntimeError("shared check-in module is not valid Python") from exc
    assignments = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "ACCOUNTS"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    if len(assignments) != 1:
        raise RuntimeError("shared check-in module must declare exactly one ACCOUNTS seed")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def span(node: ast.AST) -> tuple[int, int]:
        if (
            not hasattr(node, "lineno")
            or node.end_lineno is None
            or node.end_col_offset is None
        ):
            raise RuntimeError("shared check-in credential location is unavailable")
        return (
            offsets[node.lineno - 1] + node.col_offset,
            offsets[node.end_lineno - 1] + node.end_col_offset,
        )

    replacements = [
        (*span(assignments[0]), "ACCOUNTS = []  # Customers add their own credentials after installation.")
    ]
    seed_functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_seed_config"
    ]
    if len(seed_functions) != 1:
        raise RuntimeError("shared check-in module must declare exactly one _seed_config")
    schedule_mappings = [
        value
        for mapping in ast.walk(seed_functions[0])
        if isinstance(mapping, ast.Dict)
        for key, value in zip(mapping.keys, mapping.values)
        if isinstance(key, ast.Constant)
        and key.value == "schedule"
        and isinstance(value, ast.Dict)
    ]
    if len(schedule_mappings) != 1:
        raise RuntimeError("shared check-in seed must declare exactly one schedule mapping")
    schedule_fields = {
        key.value: value
        for key, value in zip(
            schedule_mappings[0].keys, schedule_mappings[0].values
        )
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    enabled_value = schedule_fields.get("enabled")
    session_value = schedule_fields.get("session")
    if not isinstance(enabled_value, ast.Constant) or not isinstance(
        enabled_value.value, bool
    ):
        raise RuntimeError("shared check-in seed must declare a boolean enabled field")
    if not isinstance(session_value, ast.Constant) or not isinstance(
        session_value.value, str
    ):
        raise RuntimeError("shared check-in seed must declare a string session field")
    replacements.extend(
        (
            (*span(enabled_value), "False"),
            (*span(session_value), '""'),
        )
    )

    sanitized = source
    for start, end, replacement in sorted(replacements, reverse=True):
        sanitized = sanitized[:start] + replacement + sanitized[end:]
    try:
        compile(sanitized, str(source_path), "exec")
    except SyntaxError as exc:
        raise RuntimeError("sanitized shared check-in module is not valid Python") from exc
    source_path.write_text(sanitized, encoding="utf-8")


def _sanitize_project_source_credentials(output: Path) -> None:
    """Blank active credentials and redact examples without logging their values."""
    roots = (
        output / "app" / "astrbot" / "data" / "plugins",
        output / "app" / "astrbot" / "ai_plugin",
        output / "app" / "bridge",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8-sig")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                raise RuntimeError(f"project Python source is invalid: {path}") from exc
            lines = source.splitlines(keepends=True)
            offsets = [0]
            for line in lines:
                offsets.append(offsets[-1] + len(line))
            replacements: dict[tuple[int, int], str] = {}

            def add_string(node: ast.AST, field: str) -> None:
                if not _SENSITIVE_FIELD.fullmatch(field):
                    return
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    return
                if (
                    len(node.value) < 16
                    or _DOCUMENTED_PLACEHOLDER.match(node.value)
                    or _ENVIRONMENT_NAME.fullmatch(node.value)
                ):
                    return
                if node.end_lineno is None or node.end_col_offset is None:
                    raise RuntimeError(f"credential location is unavailable: {path}")
                start = offsets[node.lineno - 1] + node.col_offset
                end = offsets[node.end_lineno - 1] + node.end_col_offset
                replacements[(start, end)] = '""'

            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        field = target.id if isinstance(target, ast.Name) else (
                            target.attr if isinstance(target, ast.Attribute) else ""
                        )
                        add_string(node.value, field)
                elif isinstance(node, ast.AnnAssign):
                    field = node.target.id if isinstance(node.target, ast.Name) else (
                        node.target.attr if isinstance(node.target, ast.Attribute) else ""
                    )
                    add_string(node.value, field)
                elif isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            add_string(value, key.value)
            sanitized = source
            for (start, end), replacement in sorted(replacements.items(), reverse=True):
                sanitized = sanitized[:start] + replacement + sanitized[end:]
            sanitized = "".join(
                _COMMENT_SECRET.sub(r"\1your-value-here\3", line)
                if line.lstrip().startswith("#") else line
                for line in sanitized.splitlines(keepends=True)
            )
            if sanitized != source:
                compile(sanitized, str(path), "exec")
                path.write_text(sanitized, encoding="utf-8")
        for suffix in ("*.md", "*.txt"):
            for path in sorted(root.rglob(suffix)):
                source = path.read_text(encoding="utf-8-sig")
                sanitized = _DOCUMENT_SECRET.sub(r"\1your-value-here\3", source)
                if sanitized != source:
                    path.write_text(sanitized, encoding="utf-8")

    config_root = output / "app" / "astrbot" / "data" / "config"
    if config_root.is_dir():
        for path in sorted(config_root.glob("*_config.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"project JSON configuration is invalid: {path}") from exc

            def sanitize(value: object, field: str = "") -> object:
                if isinstance(value, dict):
                    return {
                        str(key): sanitize(child, str(key))
                        for key, child in value.items()
                    }
                if isinstance(value, list):
                    return [sanitize(child, field) for child in value]
                if (
                    isinstance(value, str)
                    and value
                    and _SENSITIVE_FIELD.fullmatch(field)
                    and not _DOCUMENTED_PLACEHOLDER.match(value)
                    and not _ENVIRONMENT_NAME.fullmatch(value)
                ):
                    return ""
                return value

            sanitized_payload = sanitize(payload)
            if path.name == "jiang_short_drama_config.json" and isinstance(
                sanitized_payload, dict
            ):
                # Commercial clients use the hosted page and never expose a local
                # player listener, even if a developer checkout configured one.
                sanitized_payload["episode_player_public_base_url"] = ""
            if sanitized_payload != payload:
                path.write_text(
                    json.dumps(sanitized_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )


def _release_manifest(
    *,
    version: str,
    source_manifest: Path,
    copied: list[dict[str, str]],
    runtime: dict[str, object] | None,
    files: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_manifest_status": LEGACY_READY_MANIFEST_STATUS,
        "release_version": version,
        "source_manifest_build_sha256": _sha256(source_manifest),
        "copied": copied,
        "runtime": runtime,
        "files": files,
    }


def _validate_runtime_root(runtime_root: Path, commercial_root: Path) -> dict[str, object]:
    helper = runtime_root / "python/Lib/site-packages/astrbot/core/platform/wechat_group_policy.py"
    expected_helper = commercial_root / "src/wechatrebot_manager/group_feature_runtime.py"
    if not helper.is_file() or helper.read_text(encoding="utf-8") != expected_helper.read_text(encoding="utf-8"):
        raise RuntimeError("Runtime group command filter is incomplete or stale; rebuild the runtime")
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
    _validate_ffmpeg_runtime(runtime_root, commercial_root, runtime_manifest.get("ffmpeg"))
    return runtime_manifest


def _validate_ffmpeg_runtime(
    runtime_root: Path, commercial_root: Path, metadata: object
) -> None:
    expected_path = commercial_root / "release" / "ffmpeg-runtime.json"
    try:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"FFmpeg release lock is missing or invalid: {expected_path}") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError("runtime FFmpeg fingerprint is missing")
    for key in (
        "version", "architecture", "archive_url", "archive_sha256",
        "license", "source_url", "build_source_url",
    ):
        if metadata.get(key) != expected.get(key):
            raise RuntimeError(f"runtime FFmpeg identity mismatch: {key}")
    files = metadata.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("runtime FFmpeg file fingerprint is missing")
    ffmpeg_root = (runtime_root / "ffmpeg").resolve()
    names: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise RuntimeError("runtime FFmpeg file record is invalid")
        name = item["path"]
        relative = PurePosixPath(name)
        if (
            not relative.parts or relative.is_absolute()
            or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or name.casefold() in names
        ):
            raise RuntimeError(f"runtime FFmpeg file path is invalid: {name}")
        target = (ffmpeg_root / Path(*relative.parts)).resolve()
        if ffmpeg_root not in target.parents:
            raise RuntimeError(f"runtime FFmpeg file path escapes its directory: {name}")
        if not target.is_file():
            raise RuntimeError(f"runtime FFmpeg required file is missing: {name}")
        if item.get("size") != target.stat().st_size or item.get("sha256") != _sha256(target):
            raise RuntimeError(f"runtime FFmpeg file fingerprint mismatch: {name}")
        names.add(name.casefold())
    required = {"license.txt", "bin/ffmpeg.exe", "bin/ffprobe.exe"}
    if not required.issubset(names) or not any(name.endswith(".dll") for name in names):
        raise RuntimeError("runtime FFmpeg manifest omits required executable, DLL or license files")
    actual = {
        path.relative_to(ffmpeg_root).as_posix().casefold()
        for path in ffmpeg_root.rglob("*") if path.is_file()
    }
    if actual != names:
        raise RuntimeError("runtime FFmpeg contains files outside its fingerprint")


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
    for executable in ("ffmpeg.exe", "ffprobe.exe"):
        subprocess.run(
            [str(output / "runtime" / "ffmpeg" / "bin" / executable), "-version"],
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
    args = parser.parse_args(argv)

    commercial_root = COMMERCIAL_ROOT
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
    project = tomllib.loads((commercial_root / "pyproject.toml").read_text(encoding="utf-8"))
    copied: list[dict[str, str]] = []
    for entry in manifest["entries"]:
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
        copied.append(_copied_record(target, output))

    _remove_embedded_checkin_accounts(output)
    _sanitize_project_source_credentials(output)
    if (output / "app" / "bridge").is_dir():
        _apply_bridge_patch(commercial_root, output)
    _apply_plugin_patches(commercial_root, output)

    runtime_summary: dict[str, object] | None = None
    if args.runtime_root:
        runtime_root = args.runtime_root.expanduser().resolve()
        runtime_manifest = _validate_runtime_root(runtime_root, commercial_root)
        _copy(runtime_root / "python", output / "runtime" / "python")
        _copy(runtime_root / "ffmpeg", output / "runtime" / "ffmpeg")
        _copy(
            runtime_root / "runtime-manifest.json",
            output / "runtime" / "runtime-manifest.json",
        )
        runtime_summary = {
            "python": runtime_manifest["python"],
            "wheelhouse": runtime_manifest["wheelhouse"],
            "ffmpeg": runtime_manifest["ffmpeg"],
        }
        _validate_staged_runtime(output)

    if args.manager_exe:
        manager_exe = _validate_manager_exe(args.manager_exe)
        manager_target = output / "manager" / "WeChatReBot.exe"
        _copy(manager_exe, manager_target)
        copied.append(_copied_record(manager_target, output))

    hashes = [
        {"path": path.relative_to(output).as_posix(), "sha256": _sha256(path)}
        for path in sorted(output.rglob("*"))
        if path.is_file()
    ]
    (output / "release-manifest.json").write_text(
        json.dumps(
            _release_manifest(
                version=project["project"]["version"],
                source_manifest=commercial_root / "release" / "source-manifest.json",
                copied=copied,
                runtime=runtime_summary,
                files=hashes,
            ),
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
