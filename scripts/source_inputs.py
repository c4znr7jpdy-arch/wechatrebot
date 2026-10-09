"""Resolve the repository-owned, hash-pinned inputs shared by build tools."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath


def repository_sources(repository: Path, requested: Path | None = None) -> Path:
    root = repository.resolve()
    expected = root / "vendor"
    source = (requested or expected).expanduser().resolve()
    if source != expected or not source.is_relative_to(root):
        raise ValueError("源码输入必须使用本仓库的 vendor 目录，禁止读取其他项目。")
    return source


def verify_source_inputs(repository: Path, requested: Path | None = None) -> Path:
    source = repository_sources(repository, requested)
    if not (source / "manifest.json").is_file():
        raise ValueError("本仓库缺少 vendor/manifest.json，请恢复完整仓库源码。")
    manifest = json.loads((source / "manifest.json").read_text("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("源码输入清单格式不正确。")
    files = manifest.get("files")
    if manifest.get("schema_version") != 1 or not isinstance(files, list) or not files:
        raise ValueError("源码输入清单格式不正确。")
    seen = set()
    for entry in files:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))):
            raise ValueError("源码输入清单条目格式不正确。")
        relative = PurePosixPath(entry["path"])
        if (relative.is_absolute() or ".." in relative.parts or ":" in str(relative)
                or "\\" in str(relative) or not relative.parts):
            raise ValueError("源码输入清单包含越界路径。")
        path = (source / relative).resolve()
        if not path.is_relative_to(source) or str(relative).casefold() in seen:
            raise ValueError("源码输入清单包含越界或重复路径。")
        seen.add(str(relative).casefold())
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"源码输入缺失或哈希不匹配：vendor/{relative}")
    actual = {path.relative_to(source).as_posix().casefold() for path in source.rglob("*")
              if path.is_file() and "__pycache__" not in path.parts
              and path.suffix != ".pyc" and path != source / "manifest.json"}
    if actual != seen:
        raise ValueError("源码目录与清单不一致，请显式更新 vendor/manifest.json。")
    return source


if __name__ == "__main__":
    verify_source_inputs(Path(__file__).resolve().parents[1])
    print("Repository source inputs verified.")
