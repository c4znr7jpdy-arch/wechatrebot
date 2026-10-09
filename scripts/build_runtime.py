from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wechatrebot_manager.licensing.bundle import install_runtime_bundle

PYTHON_VERSION = "3.13.9"
PYTHON_ARCHIVE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ARCHIVE}"
PYTHON_SHA256 = "91D828C2DA3A029B41699E918674A0CB379C02CF20DAB9C501306885F837402A"
FFMPEG_DEFINITION = "ffmpeg-runtime.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _run(argv: list[str], *, cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True)


def _download_verified_archive(
    target: Path,
    *,
    url: str,
    expected_sha256: str,
    display_name: str,
    offline: bool,
) -> None:
    expected_sha256 = expected_sha256.upper()
    if target.is_file() and sha256_file(target) == expected_sha256:
        return
    if offline:
        raise RuntimeError(
            f"离线构建缺少通过指纹校验的 {display_name}：{target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".download", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        urllib.request.urlretrieve(url, temporary)
        actual = sha256_file(temporary)
        if actual != expected_sha256:
            raise RuntimeError(
                f"{display_name} 哈希错误，期望 {expected_sha256}，实际 {actual}。"
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _download_python(target: Path, *, offline: bool = False) -> None:
    _download_verified_archive(
        target,
        url=PYTHON_URL,
        expected_sha256=PYTHON_SHA256,
        display_name="Python 嵌入包",
        offline=offline,
    )


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in _validated_zip_members(bundle):
            relative = PurePosixPath(info.filename)
            target = (destination / Path(*relative.parts)).resolve()
            try:
                target.relative_to(destination_resolved)
            except ValueError as exc:
                raise RuntimeError(f"压缩包路径越界：{info.filename}") from exc
        bundle.extractall(destination)


def _validated_zip_members(bundle: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    safe: list[zipfile.ZipInfo] = []
    for info in bundle.infolist():
        relative = PurePosixPath(info.filename)
        if (
            not relative.parts
            or relative.is_absolute()
            or "\\" in info.filename
            or ":" in info.filename
            or any(part in {"", ".", ".."} for part in info.filename.rstrip("/").split("/"))
        ):
            raise RuntimeError(f"压缩包包含不安全路径：{info.filename}")
        unix_mode = (info.external_attr >> 16) & 0xF000
        if unix_mode == stat.S_IFLNK:
            raise RuntimeError(f"压缩包包含不允许的符号链接：{info.filename}")
        safe.append(info)
    return tuple(safe)


def _load_ffmpeg_definition(commercial_root: Path) -> dict[str, object]:
    path = commercial_root / "release" / FFMPEG_DEFINITION
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"FFmpeg 固定版本清单不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FFmpeg 固定版本清单不是有效 JSON：{exc}") from exc
    required_strings = (
        "version",
        "architecture",
        "distribution",
        "archive_filename",
        "archive_root",
        "archive_url",
        "archive_sha256",
        "license",
        "source_url",
        "source_commit",
        "build_source_url",
        "build_tag",
    )
    if definition.get("schema_version") != 1 or any(
        not isinstance(definition.get(key), str) or not definition[key]
        for key in required_strings
    ):
        raise RuntimeError("FFmpeg 固定版本清单字段不完整。")
    if len(str(definition["archive_sha256"])) != 64:
        raise RuntimeError("FFmpeg 固定版本清单的 SHA-256 无效。")
    return definition


def _install_ffmpeg(
    archive: Path,
    destination: Path,
    definition: dict[str, object],
) -> dict[str, object]:
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(f"FFmpeg 目标目录必须为空：{destination}")
    archive_root = PurePosixPath(str(definition["archive_root"]))
    expected_bin = archive_root / "bin"
    selected: dict[str, zipfile.ZipInfo] = {}
    with zipfile.ZipFile(archive) as bundle:
        for info in _validated_zip_members(bundle):
            if info.is_dir():
                continue
            relative = PurePosixPath(info.filename)
            target_name: str | None = None
            if relative == archive_root / "LICENSE.txt":
                target_name = "LICENSE.txt"
            elif relative.parent == expected_bin and (
                relative.name.casefold() in {"ffmpeg.exe", "ffprobe.exe"}
                or relative.suffix.casefold() == ".dll"
            ):
                target_name = f"bin/{relative.name}"
            if target_name is None:
                continue
            if target_name.casefold() in {key.casefold() for key in selected}:
                raise RuntimeError(f"FFmpeg 压缩包包含重复文件：{target_name}")
            selected[target_name] = info

        required = {"LICENSE.txt", "bin/ffmpeg.exe", "bin/ffprobe.exe"}
        missing = sorted(required - set(selected))
        dlls = [name for name in selected if name.casefold().endswith(".dll")]
        if missing or not dlls:
            detail = missing or ["bin/*.dll"]
            raise RuntimeError(f"FFmpeg 压缩包缺少必要文件：{detail}")

        for relative_name, info in selected.items():
            target = destination / Path(*PurePosixPath(relative_name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)

    files = [
        {
            "path": path.relative_to(destination).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    ]
    return {
        key: definition[key]
        for key in (
            "version",
            "architecture",
            "distribution",
            "archive_filename",
            "archive_url",
            "archive_sha256",
            "license",
            "source_url",
            "source_commit",
            "build_source_url",
            "build_tag",
        )
    } | {"files": files}


def _validate_ffmpeg(ffmpeg_root: Path, output: Path) -> None:
    for executable in ("ffmpeg.exe", "ffprobe.exe"):
        _run([str(ffmpeg_root / "bin" / executable), "-version"], cwd=output)


def _enable_site_packages(python_root: Path) -> None:
    candidates = tuple(python_root.glob("python*._pth"))
    if len(candidates) != 1:
        raise RuntimeError("无法唯一确定嵌入式 Python 的 _pth 文件。")
    pth_path = candidates[0]
    zip_name = f"python{sys.version_info.major}{sys.version_info.minor}.zip"
    pth_path.write_text(
        f"{zip_name}\n.\nLib/site-packages\nimport site\n",
        encoding="utf-8",
        newline="\n",
    )


def _wheel_manifest(wheelhouse: Path) -> list[dict[str, object]]:
    return [
        {
            "filename": path.name,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(wheelhouse.glob("*.whl"), key=lambda item: item.name.casefold())
    ]


def _wheelhouse_summary(items: list[dict[str, object]]) -> dict[str, object]:
    canonical = "".join(
        f"{item['sha256']}  {item['filename']}\n" for item in items
    ).encode("utf-8")
    return {
        "wheel_count": len(items),
        "total_bytes": sum(int(item["size"]) for item in items),
        "aggregate_sha256": hashlib.sha256(canonical).hexdigest().upper(),
    }


def _verify_wheelhouse(commercial_root: Path, wheelhouse: Path) -> dict[str, object]:
    expected_path = commercial_root / "release" / "runtime-wheelhouse.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    items = _wheel_manifest(wheelhouse)
    summary = _wheelhouse_summary(items)
    for key in ("wheel_count", "total_bytes", "aggregate_sha256"):
        if summary[key] != expected.get(key):
            raise RuntimeError(
                f"离线 wheelhouse 指纹不匹配：{key}，"
                f"期望 {expected.get(key)!r}，实际 {summary[key]!r}。"
            )
    return {**summary, "wheels": items}


def _download_wheels(
    host_python: Path,
    requirements: Path,
    wheelhouse: Path,
    *,
    offline: bool,
    commercial_root: Path,
) -> None:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if not offline:
        try:
            _verify_wheelhouse(commercial_root, wheelhouse)
        except (OSError, ValueError, RuntimeError):
            _run(
                [
                    str(host_python),
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-deps",
                    "--prefer-binary",
                    "--wheel-dir",
                    str(wheelhouse),
                    "--requirement",
                    str(requirements),
                ],
                cwd=commercial_root,
            )
        else:
            print("已复用通过指纹校验的离线 wheelhouse。", flush=True)
    if not tuple(wheelhouse.glob("*.whl")):
        raise RuntimeError("离线 wheelhouse 为空。")
    unexpected = [path.name for path in wheelhouse.iterdir() if path.suffix != ".whl"]
    if unexpected:
        raise RuntimeError(f"wheelhouse 包含非 wheel 文件：{unexpected[:5]}")


def _install_wheels(
    host_python: Path,
    requirements: Path,
    wheelhouse: Path,
    site_packages: Path,
    *,
    commercial_root: Path,
) -> None:
    site_packages.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(host_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--find-links",
            str(wheelhouse),
            "--target",
            str(site_packages),
            "--requirement",
            str(requirements),
        ],
        cwd=commercial_root,
    )


def _apply_astrbot_patch(commercial_root: Path, site_packages: Path) -> None:
    patches = (
        commercial_root / "patches" / "astrbot-4.26.6-wechat.patch",
        commercial_root / "patches" / "astrbot-4.26.6-outbound-policy.patch",
        commercial_root / "patches" / "astrbot-4.26.6-license.patch",
    )
    site_relative = site_packages.relative_to(commercial_root).as_posix()
    for patch in patches:
        if not patch.is_file():
            raise RuntimeError(f"AstrBot runtime patch is missing: {patch.name}")
        command = [
            "git",
            "apply",
            "--ignore-space-change",
            "-p2",
            f"--directory={site_relative}",
            str(patch),
        ]
        _run([*command[:2], "--check", *command[2:]], cwd=commercial_root)
        _run(command, cwd=commercial_root)


def _assert_runtime_boundary(site_packages: Path) -> None:
    astrbot_root = site_packages / "astrbot"
    if not astrbot_root.is_dir():
        raise RuntimeError("AstrBot 未安装到便携运行时。")
    forbidden = "6199"
    findings: list[str] = []
    for path in astrbot_root.rglob("*.py"):
        try:
            if forbidden in path.read_text(encoding="utf-8"):
                findings.append(path.relative_to(site_packages).as_posix())
        except (OSError, UnicodeDecodeError):
            continue
    if findings:
        raise RuntimeError(f"AstrBot 运行时仍包含历史端口：{findings[:5]}")


def _validate_runtime(python_exe: Path, output: Path) -> None:
    code = (
        "import importlib.metadata as m,sys;"
        "import aiohttp,astrbot,PIL,websockets;"
        "assert sys.maxsize > 2**32;"
        "assert sys.version_info[:2] == (3,13);"
        "assert m.version('AstrBot') == '4.26.6';"
        "print(sys.version.split()[0], m.version('AstrBot'))"
    )
    _run([str(python_exe), "-B", "-I", "-c", code], cwd=output)
    node_exe = python_exe.parent / "Lib" / "site-packages" / "playwright" / "driver" / "node.exe"
    _run([str(node_exe), "--version"], cwd=output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the pinned Python 3.13 x64 commercial runtime."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--offline", action="store_true")
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
    check = subprocess.run(
        [
            str(host_python),
            "-c",
            "import platform,sys;"
            "assert sys.version_info[:2] == (3,13);"
            "assert platform.architecture()[0] == '64bit'",
        ],
        check=False,
    )
    if check.returncode:
        parser.error("host-python must be Python 3.13 x64")

    cache_root = artifacts_root / "runtime-cache"
    python_archive = cache_root / PYTHON_ARCHIVE
    requirements = commercial_root / "release" / "runtime-requirements.lock"
    wheelhouse = (
        args.wheelhouse.expanduser().resolve()
        if args.wheelhouse
        else cache_root / "wheels-py313-win-amd64"
    )
    ffmpeg_definition = _load_ffmpeg_definition(commercial_root)
    ffmpeg_archive = cache_root / str(ffmpeg_definition["archive_filename"])

    _download_python(python_archive, offline=args.offline)
    _download_verified_archive(
        ffmpeg_archive,
        url=str(ffmpeg_definition["archive_url"]),
        expected_sha256=str(ffmpeg_definition["archive_sha256"]),
        display_name="FFmpeg 便携运行时",
        offline=args.offline,
    )
    python_root = output / "python"
    python_root.mkdir()
    _safe_extract_zip(python_archive, python_root)
    _enable_site_packages(python_root)
    _download_wheels(
        host_python,
        requirements,
        wheelhouse,
        offline=args.offline,
        commercial_root=commercial_root,
    )
    wheelhouse_verification = _verify_wheelhouse(commercial_root, wheelhouse)
    site_packages = python_root / "Lib" / "site-packages"
    _install_wheels(
        host_python,
        requirements,
        wheelhouse,
        site_packages,
        commercial_root=commercial_root,
    )
    _apply_astrbot_patch(commercial_root, site_packages)
    licensing_fingerprint = install_runtime_bundle(commercial_root, site_packages)
    _assert_runtime_boundary(site_packages)
    _validate_runtime(python_root / "python.exe", output)
    ffmpeg_root = output / "ffmpeg"
    ffmpeg_manifest = _install_ffmpeg(
        ffmpeg_archive,
        ffmpeg_root,
        ffmpeg_definition,
    )
    _validate_ffmpeg(ffmpeg_root, output)

    manifest = {
        "schema_version": 1,
        "python": {
            "version": PYTHON_VERSION,
            "architecture": "x64",
            "source_url": PYTHON_URL,
            "sha256": PYTHON_SHA256,
        },
        "requirements_sha256": sha256_file(requirements),
        "astrbot_patch_sha256": sha256_file(
            commercial_root / "patches" / "astrbot-4.26.6-wechat.patch"
        ),
        "astrbot_outbound_policy_patch_sha256": sha256_file(
            commercial_root / "patches" / "astrbot-4.26.6-outbound-policy.patch"
        ),
        "astrbot_license_patch_sha256": sha256_file(
            commercial_root / "patches" / "astrbot-4.26.6-license.patch"
        ),
        "licensing_bundle_sha256": licensing_fingerprint,
        "wheelhouse": {
            key: value
            for key, value in wheelhouse_verification.items()
            if key != "wheels"
        },
        "wheels": wheelhouse_verification["wheels"],
        "ffmpeg": ffmpeg_manifest,
    }
    (output / "runtime-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "wheel_count": len(manifest["wheels"]),
                "ffmpeg_version": ffmpeg_manifest["version"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
