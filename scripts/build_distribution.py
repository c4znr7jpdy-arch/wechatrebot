from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import uuid
import zipfile


COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from wechatrebot_manager.payload import PAYLOAD_ARCHIVE, sha256_file, verify_payload


def _validate_pe(path: Path) -> None:
    if not path.is_file() or path.suffix.casefold() != ".exe":
        raise RuntimeError(f"manager executable is missing: {path}")
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise RuntimeError(f"manager executable is not a Windows PE file: {path}")
        handle.seek(0x3C)
        offset_bytes = handle.read(4)
        if len(offset_bytes) != 4:
            raise RuntimeError(f"manager executable has an invalid PE header: {path}")
        pe_offset = struct.unpack("<I", offset_bytes)[0]
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\x00\x00":
            raise RuntimeError(f"manager executable has an invalid PE signature: {path}")
        machine_bytes = handle.read(2)
        if len(machine_bytes) != 2 or struct.unpack("<H", machine_bytes)[0] != 0x8664:
            raise RuntimeError(f"manager executable must be Windows x64: {path}")


def _write_distribution_manifest(
    root: Path,
    *,
    payload_status: object,
    payload_file_count: int,
    development_preview: bool,
) -> Path:
    manager = root / "WeChatReBot.exe"
    payload_archive = root / PAYLOAD_ARCHIVE
    manifest = {
        "schema_version": 1,
        "product": "WeChat ReBot",
        "entrypoint": manager.name,
        "payload_file": PAYLOAD_ARCHIVE,
        "payload_format": "zip-v1",
        "payload_status": payload_status,
        "payload_file_count": payload_file_count,
        "development_preview": development_preview,
        "files": [
            {
                "path": manager.name,
                "size": manager.stat().st_size,
                "sha256": sha256_file(manager),
            },
            {
                "path": PAYLOAD_ARCHIVE,
                "size": payload_archive.stat().st_size,
                "sha256": sha256_file(payload_archive),
            },
        ],
    }
    target = root / "DISTRIBUTION-MANIFEST.json"
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _write_payload_archive(payload: Path, target: Path) -> None:
    with zipfile.ZipFile(
        target,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(payload).as_posix())


def _write_outer_archive(output: Path) -> Path:
    archive_path = output.parent / f"{output.name}.zip"
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
    ) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, f"{output.name}/{path.relative_to(output).as_posix()}")
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a customer Windows package.")
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", action="store_true")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="development verification only; never use for a paid release",
    )
    args = parser.parse_args(argv)

    artifacts_root = (COMMERCIAL_ROOT / "artifacts").resolve()
    output = args.output.expanduser().resolve()
    if output == artifacts_root or artifacts_root not in output.parents:
        parser.error("output must stay inside the commercial artifacts directory")
    if output.exists() and any(output.iterdir()):
        parser.error(f"output must not already contain files: {output}")

    payload = args.payload.expanduser().resolve()
    try:
        output.relative_to(payload)
    except ValueError:
        pass
    else:
        parser.error("output must not be placed inside the source payload")

    verified = verify_payload(payload, allow_incomplete=args.allow_incomplete)
    source_guard = subprocess.run(
        [sys.executable, str(COMMERCIAL_ROOT / "scripts" / "release_guard.py"), str(payload)],
        check=False,
    )
    if source_guard.returncode:
        raise RuntimeError("release guard rejected the source payload")
    source_manager = payload / "manager" / "WeChatReBot.exe"
    _validate_pe(source_manager)

    temp = artifacts_root / f".distribution-{uuid.uuid4().hex}"
    temp.mkdir(parents=True)
    try:
        _write_payload_archive(payload, temp / PAYLOAD_ARCHIVE)
        shutil.copy2(source_manager, temp / "WeChatReBot.exe")
        shutil.copy2(COMMERCIAL_ROOT / "release" / "START-HERE.txt", temp / "START-HERE.txt")
        _write_distribution_manifest(
            temp,
            payload_status=verified.manifest.get("source_manifest_status"),
            payload_file_count=len(verified.files),
            development_preview=(
                args.allow_incomplete
                and verified.manifest.get("source_manifest_status") != "ready"
            ),
        )
        guard = subprocess.run(
            [sys.executable, str(COMMERCIAL_ROOT / "scripts" / "release_guard.py"), str(temp)],
            check=False,
        )
        if guard.returncode:
            raise RuntimeError("release guard rejected the assembled distribution")
        if output.exists():
            output.rmdir()
        os.replace(temp, output)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise

    result: dict[str, object] = {
        "schema_version": 1,
        "output": str(output),
        "payload_status": verified.manifest.get("source_manifest_status"),
        "payload_file_count": len(verified.files),
        "archive": None,
    }
    if args.archive:
        archive = _write_outer_archive(output)
        digest = sha256_file(archive)
        checksum = archive.with_suffix(archive.suffix + ".sha256")
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
        result["archive"] = {
            "path": str(archive),
            "size": archive.stat().st_size,
            "sha256": digest,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
