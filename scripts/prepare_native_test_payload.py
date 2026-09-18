"""Overlay a current manager on an existing payload for local installer tests only.

This intentionally skips full staging and must never be used as a release payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PREFIX = "manager/src/wechatrebot_manager/"
MANAGER_ENTRY = "manager/WeChatReBot.exe"
MANIFEST_ENTRY = "release-manifest.json"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from wechatrebot_manager.payload import verify_payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _current_source_files() -> dict[str, Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "src/wechatrebot_manager"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    files: dict[str, Path] = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        source = (ROOT / relative).resolve()
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"管理器源码文件无效：{source}")
        archive_path = SOURCE_PREFIX + relative.relative_to(
            "src/wechatrebot_manager"
        ).as_posix()
        files[archive_path] = source
    if not files:
        raise ValueError("未找到受版本控制的管理器源码。")
    return files


def prepare(base: Path, manager: Path, output: Path) -> dict[str, object]:
    base = base.expanduser().resolve()
    manager = manager.expanduser().resolve()
    output = output.expanduser().resolve()
    artifacts = (ROOT / "artifacts").resolve()
    if output == artifacts or artifacts not in output.parents or output.suffix != ".wrb":
        raise ValueError("测试载荷必须是当前工作树 artifacts 下的 .wrb 文件。")
    if output.exists() or output == base or not base.is_file():
        raise ValueError("输出必须是一个尚不存在的文件，且基础载荷必须存在。")
    if not manager.is_file() or manager.suffix.casefold() != ".exe":
        raise ValueError("缺少管理器 EXE。")
    with manager.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("管理器不是 Windows EXE。")

    source_files = _current_source_files()
    replacements = {MANAGER_ENTRY: manager, **source_files}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}-{uuid.uuid4().hex}.wrb")
    try:
        with zipfile.ZipFile(base) as original, zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
            allowZip64=True,
        ) as target:
            names = set(original.namelist())
            if MANAGER_ENTRY not in names or MANIFEST_ENTRY not in names:
                raise ValueError("基础载荷缺少管理器或清单。")
            manifest = json.loads(original.read(MANIFEST_ENTRY).decode("utf-8"))
            for info in original.infolist():
                name = info.filename
                if name == MANIFEST_ENTRY or name == MANAGER_ENTRY or name.startswith(SOURCE_PREFIX):
                    continue
                with original.open(info) as reader, target.open(info, "w") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)

            for name, path in sorted(replacements.items()):
                target.write(path, name)

            retained = [
                item for item in manifest["files"]
                if item["path"] != MANAGER_ENTRY
                and not item["path"].startswith(SOURCE_PREFIX)
            ]
            retained.extend(
                {"path": name, "sha256": _sha256(path)}
                for name, path in replacements.items()
            )
            manifest["files"] = sorted(retained, key=lambda item: item["path"])
            manifest["test_only_native_installer_overlay"] = True
            target.writestr(
                MANIFEST_ENTRY,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
        verified = verify_payload(temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "output": str(output),
        "size": output.stat().st_size,
        "sha256": _sha256(output),
        "file_count": len(verified.files),
        "manager_sha256": _sha256(manager),
        "test_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-payload", type=Path, required=True)
    parser.add_argument("--manager-exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(
        prepare(args.base_payload, args.manager_exe, args.output),
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
