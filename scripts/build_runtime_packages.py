"""Content-addressed, signed environment assets, reused across application releases."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import shutil
import zipfile

from build_update_release import _sign_manifest
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.payload import _checked_target, sha256_file
from wechatrebot_manager.runtime_manifest import (
    RuntimeFile, content_id, environment_id, parse_environment, runtime_file,
)


def environment_inputs(root: Path) -> dict:
    return {name: sha256_file(root / "release" / name) for name in (
        "runtime-requirements.lock", "runtime-wheelhouse.json", "ffmpeg-runtime.json",
    )}


def read_environment(path: Path, channel, root: Path | None = None):
    environment = parse_environment(json.loads(path.read_text(encoding="utf-8")), channel)
    if root is not None and environment.manifest.get("inputs") != environment_inputs(root):
        raise ValueError("环境清单与依赖锁不一致；请显式提供重新构建的 runtime-root，不能自动重建完整环境。")
    return environment


def _group(relative: Path) -> str:
    parts = relative.parts
    if parts[1] == "ffmpeg":
        return "ffmpeg"
    if len(parts) > 4 and tuple(part.casefold() for part in parts[2:4]) == ("lib", "site-packages"):
        name = parts[4].casefold()
        # Metadata and small single-file modules share a small package. Large
        # import directories keep stable identities even when another dep changes.
        return "python-metadata" if len(parts) == 5 or name.endswith(".dist-info") else f"dependency-{name}"
    return "python-base"


def _zip_entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _write_package(runtime_root: Path, files: tuple[RuntimeFile, ...], archive: Path) -> int:
    manifest = {"schema_version": 1, "files": [{"path": f.path, "sha256": f.sha256} for f in files]}
    data = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.writestr(_zip_entry("payload/release-manifest.json"), data)
        for item in files:
            source = _checked_target(runtime_root, Path(item.path).relative_to("runtime"))
            with source.open("rb") as reader, bundle.open(_zip_entry(f"payload/{item.path}"), "w", force_zip64=True) as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            if source.stat().st_size != item.size or sha256_file(source) != item.sha256:
                raise ValueError(f"运行环境源文件在打包期间变化：{item.path}")
    return len(data) + sum(item.size for item in files)


def build_environment(runtime_root: Path, output: Path, *, channel, private_key, prefix: str,
                      python_abi: str, metadata: dict, previous=None):
    groups = defaultdict(list)
    for path in sorted(runtime_root.rglob("*")):
        relative = Path("runtime") / path.relative_to(runtime_root)
        if path.is_symlink() or path.is_junction():
            raise ValueError(f"运行环境不能包含链接：{relative}")
        if not path.is_file() or not runtime_file(relative) or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        groups[_group(relative)].append(RuntimeFile(relative.as_posix(), sha256_file(path), path.stat().st_size))
    prior = {item["id"]: item for item in previous.manifest["packages"]} if previous else {}
    packages, assets = [], []
    output.mkdir(parents=True, exist_ok=True)
    for name in sorted(groups):
        files = tuple(groups[name])
        identity = content_id(files)
        if identity in prior:
            packages.append(prior[identity])
            continue
        archive = output / f"runtime-{identity.lower()}.zip"
        expanded = _write_package(runtime_root, files, archive)
        packages.append({"id": identity, "package": {"url": f"{prefix.rstrip('/')}/{archive.name}",
                         "format": "zip", "size": archive.stat().st_size, "sha256": sha256_file(archive),
                         "uncompressed_size": expanded}, "files": [item.as_dict() for item in files]})
        assets.append(archive)
    files = tuple(item for group in groups.values() for item in group)
    manifest = {**metadata, "schema_version": 1, "product": "wechatrebot-runtime", "platform": "windows-x64",
                "python_abi": python_abi, "id": environment_id(python_abi, files), "packages": packages}
    _sign_manifest(manifest, private_key)
    environment = parse_environment(manifest, channel)
    write_json_atomic(output / "runtime-environment.json", manifest)
    return environment, assets
