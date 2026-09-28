from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tomllib
import urllib.parse
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.version import Version


COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from wechatrebot_manager.payload import (
    LEGACY_READY_MANIFEST_STATUS,
    sha256_file,
    verify_payload,
)
from wechatrebot_manager.updates import PRODUCT_ID, canonical_manifest_bytes


def _https_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("package URL must be an HTTPS URL without credentials")
    return value.strip()


def _project_version() -> str:
    payload = tomllib.loads((COMMERCIAL_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def _signing_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("update signing key must be Ed25519")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and sign a full or selected-payload remote update release."
    )
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--package-url", required=True)
    parser.add_argument("--title", default="稳定性与功能更新")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="PAYLOAD_PATH",
        help="include only this manifest-listed payload path (repeatable)",
    )
    parser.add_argument("--minimum-supported-version", default="0.7.0")
    parser.add_argument("--build", type=int)
    parser.add_argument(
        "--mandatory",
        action="store_true",
        help="mark this release as required for all older client versions",
    )
    args = parser.parse_args(argv)

    artifacts = (COMMERCIAL_ROOT / "artifacts").resolve()
    output = args.output.expanduser().resolve()
    if output == artifacts or artifacts not in output.parents:
        parser.error("output must stay inside the commercial artifacts directory")
    if output.exists() and any(output.iterdir()):
        parser.error(f"output must not already contain files: {output}")
    output.mkdir(parents=True, exist_ok=True)

    payload = args.payload.expanduser().resolve()
    verified = verify_payload(payload)
    if args.include and verified.manifest.get("schema_version") == 2:
        parser.error("incremental payloads must be published intact; --include is not supported")
    verified_by_path = {
        item.relative_path.as_posix(): item for item in verified.files
    }
    if args.include:
        requested = tuple(dict.fromkeys(item.strip() for item in args.include if item.strip()))
        missing = tuple(item for item in requested if item not in verified_by_path)
        if missing:
            parser.error(
                "included payload paths are not present in the verified manifest: "
                + ", ".join(missing)
            )
        selected = tuple(verified_by_path[item] for item in requested)
    else:
        selected = verified.files
    version = _project_version()
    Version(version)
    package_url = _https_url(args.package_url)
    archive = output / f"wechatrebot-{version}-update.zip"
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as package:
        if args.include:
            package_manifest = {
                "schema_version": 1,
                "source_manifest_status": LEGACY_READY_MANIFEST_STATUS,
                "release_version": version,
                "files": [
                    {
                        "path": item.relative_path.as_posix(),
                        "sha256": item.sha256,
                    }
                    for item in selected
                ],
            }
        else:
            package_manifest = {
                **verified.manifest,
                "source_manifest_status": LEGACY_READY_MANIFEST_STATUS,
                "release_version": version,
            }
        if args.build is not None:
            if args.build < 0:
                parser.error("build must be a non-negative integer")
            package_manifest["release_build"] = args.build
        package.writestr(
            "payload/release-manifest.json",
            json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n",
        )
        with ExitStack() as sources:
            source_archive = (
                sources.enter_context(zipfile.ZipFile(verified.root))
                if verified.root.is_file()
                else None
            )
            members = (
                {info.filename.casefold(): info for info in source_archive.infolist()}
                if source_archive is not None
                else {}
            )
            for item in selected:
                relative = item.relative_path.as_posix()
                if source_archive is None:
                    package.write(item.source_path, f"payload/{relative}")
                else:
                    # Archive payload files share source_path=the source ZIP;
                    # copy each verified member, never the ZIP itself.
                    with source_archive.open(members[relative.casefold()]) as source:
                        with package.open(f"payload/{relative}", "w", force_zip64=True) as target:
                            shutil.copyfileobj(source, target, length=1024 * 1024)
    with zipfile.ZipFile(archive, "r") as package:
        uncompressed_size = sum(item.file_size for item in package.infolist())

    private_key = _signing_key(args.private_key.expanduser().resolve())
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public_bytes).hexdigest().upper()[:16]
    manifest: dict[str, object] = {
        "schema_version": 1,
        "product": PRODUCT_ID,
        "channel": "stable",
        "version": version,
        "minimum_supported_version": str(Version(args.minimum_supported_version)),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": args.title.strip()[:100] or f"版本 {version}",
        "notes": [item.strip()[:500] for item in args.note if item.strip()][:20],
        "mandatory": args.mandatory,
        "package": {
            "url": package_url,
            "format": "zip",
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
            "payload_file_count": len(selected),
            "uncompressed_size": uncompressed_size,
        },
    }
    if args.build is not None:
        manifest["build"] = args.build
    signature = private_key.sign(canonical_manifest_bytes(manifest))
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    manifest_path = output / "stable.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "archive": str(archive),
                "archive_size": archive.stat().st_size,
                "archive_sha256": sha256_file(archive),
                "manifest": str(manifest_path),
                "key_id": key_id,
                "upload_order": [archive.name, manifest_path.name],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
