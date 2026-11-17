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
    VerifiedPayload,
    sha256_file,
    verify_payload,
)
from wechatrebot_manager.updates import (
    PRODUCT_ID, UpdateChannel, _manifest_package_fields, canonical_manifest_bytes, parse_update_manifest,
)


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


def _sign_manifest(manifest: dict, private_key: Ed25519PrivateKey) -> str:
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public).hexdigest().upper()[:16]
    manifest["signature"] = {
        "algorithm": "ed25519", "key_id": key_id,
        "value": base64.b64encode(private_key.sign(canonical_manifest_bytes(manifest))).decode("ascii"),
    }
    return key_id


def _write_payload_archive(verified, selected, archive: Path, *, version: str,
                           build: int | None, filtered: bool = False) -> None:
    manifest = ({"schema_version": 1, "files": [
        {"path": item.relative_path.as_posix(), "sha256": item.sha256} for item in selected
    ]} if filtered else dict(verified.manifest))
    manifest.update(source_manifest_status=LEGACY_READY_MANIFEST_STATUS, release_version=version)
    if build is not None:
        if build < 0:
            raise ValueError("build must be a non-negative integer")
        manifest["release_build"] = build
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as package, ExitStack() as sources:
        package.writestr("payload/release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        source_archive = sources.enter_context(zipfile.ZipFile(verified.root)) if verified.root.is_file() else None
        members = ({info.filename.casefold(): info for info in source_archive.infolist()}
                   if source_archive is not None else {})
        for item in selected:
            relative = item.relative_path.as_posix()
            if source_archive is None:
                package.write(item.source_path, f"payload/{relative}")
            else:
                with source_archive.open(members[relative.casefold()]) as source:
                    with package.open(f"payload/{relative}", "w", force_zip64=True) as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)


def _verified_incremental_payloads(payloads: list[Path], *, version: str,
                                   build: int | None, require_target: bool = False) -> list[tuple[VerifiedPayload, str]]:
    if len(payloads) > 64:
        raise ValueError("at most 64 incremental payloads are supported")
    verified_payloads, bases = [], set()
    for path in payloads:
        verified = verify_payload(path)
        base = str(verified.manifest.get("base_fingerprint", "")).upper()
        if verified.manifest.get("schema_version") != 2 or verified.manifest.get("update_kind") != "incremental":
            raise ValueError("additional payloads must be complete schema 2 incremental payloads")
        target_version = verified.manifest.get("release_version", None if require_target else version)
        target_build = verified.manifest.get("release_build", build)
        if target_version != version:
            raise ValueError("incremental payload target version differs from the release")
        if target_build != build or (target_build is not None and type(target_build) is not int):
            raise ValueError("incremental payload target build differs from the release")
        if require_target and build is not None and "release_build" not in verified.manifest:
            raise ValueError("incremental payload is missing its target build")
        if base in bases:
            raise ValueError("duplicate incremental base fingerprint")
        bases.add(base)
        verified_payloads.append((verified, base))
    return verified_payloads


def _write_incremental_archives(verified_payloads: list[tuple[VerifiedPayload, str]], output: Path, *, version: str,
                                build: int | None, asset_prefix: str) -> tuple[list[dict], list[Path]]:
    packages, assets = [], []
    for verified, base in verified_payloads:
        archive = output / f"wechatrebot-{version}-delta-{base[:12].lower()}.zip"
        if archive.exists():
            raise ValueError("incremental output already exists")
        _write_payload_archive(verified, verified.files, archive, version=version, build=build)
        digest = sha256_file(archive)
        final = archive.with_name(f"wechatrebot-{version}-delta-{base[:12].lower()}-{digest[:12].lower()}.zip")
        if final.exists():
            raise ValueError("incremental output already exists")
        archive.rename(final)
        with zipfile.ZipFile(final) as package:
            expanded = sum(item.file_size for item in package.infolist())
        packages.append({"base_fingerprint": base, "package": {
            "url": f"{asset_prefix.rstrip('/')}/{final.name}", "format": "zip",
            "size": final.stat().st_size, "sha256": digest, "uncompressed_size": expanded,
        }})
        assets.append(final)
    return packages, assets


def attach_incremental_payloads(manifest_path: Path, payloads: list[Path], *,
                               private_key_path: Path) -> list[Path]:
    """Attach verified deltas without rebuilding or scanning the default runtime."""
    if not payloads:
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    private = _signing_key(private_key_path)
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    channel = UpdateChannel(
        manifest_url="https://updates.invalid/stable.json", channel="stable",
        key_id=hashlib.sha256(public).hexdigest().upper()[:16], public_key=public,
    )
    release = parse_update_manifest(manifest, channel, current_version="0.0.0")
    if release is None or manifest.get("incremental_packages"):
        raise ValueError("a new signed default release is required before adding incremental payloads")
    verified = _verified_incremental_payloads(payloads, version=release.version, build=release.build)
    prefix = release.package_url.rsplit("/", 1)[0]
    packages, assets = _write_incremental_archives(
        verified, manifest_path.parent, version=release.version, build=release.build, asset_prefix=prefix,
    )
    manifest["incremental_packages"] = packages
    _sign_manifest(manifest, private)
    parse_update_manifest(manifest, channel, current_version="0.0.0")
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return assets


def build_component_release(payloads: list[Path], output: Path, *, version: str,
                            private_key_path: Path, channel: UpdateChannel, asset_prefix: str,
                            title: str, notes: tuple[str, ...] = (),
                            minimum_supported_version: str = "0.0.0", build: int | None = None,
                            mandatory: bool = False) -> tuple[Path, list[Path]]:
    """Sign complete per-baseline component operations without a full default package."""
    if not payloads:
        raise ValueError("a component release requires at least one incremental payload")
    if str(Version(version)) != version:
        raise ValueError("version must use its canonical form")
    minimum = str(Version(minimum_supported_version))
    if build is not None and (type(build) is not int or build < 0):
        raise ValueError("build must be a non-negative integer")
    if type(mandatory) is not bool:
        raise ValueError("mandatory must be a boolean")
    prefix = _https_url(asset_prefix).rstrip("/")
    if urllib.parse.urlsplit(prefix).query:
        raise ValueError("asset prefix must not contain a query")
    private = _signing_key(private_key_path)
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if public != channel.public_key or hashlib.sha256(public).hexdigest().upper()[:16] != channel.key_id:
        raise ValueError("signing key does not match the update channel")
    verified = _verified_incremental_payloads(payloads, version=version, build=build, require_target=True)
    if output.is_symlink() or output.is_junction():
        raise ValueError("component output must not be a link")
    output = output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"output must not already contain files: {output}")
    output.mkdir(parents=True, exist_ok=True)
    packages, assets = _write_incremental_archives(
        verified, output, version=version, build=build, asset_prefix=prefix,
    )
    for package in packages:
        _manifest_package_fields(package["package"], channel)
    manifest = {
        "schema_version": 1, "product": PRODUCT_ID, "channel": channel.channel,
        "component_update_protocol": 1, "version": version,
        "minimum_supported_version": minimum,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": title.strip()[:100] or f"版本 {version}",
        "notes": [note.strip()[:500] for note in notes if note.strip()][:20],
        "mandatory": mandatory, "incremental_packages": packages,
    }
    if build is not None:
        manifest["build"] = build
    _sign_manifest(manifest, private)
    manifest_path = output / "stable.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path, assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and sign a full or selected-payload remote update release."
    )
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--package-url")
    parser.add_argument("--components-only", action="store_true",
                        help="sign only per-baseline component packages, without a default full package")
    parser.add_argument("--asset-prefix", help="HTTPS Release directory for --components-only assets")
    parser.add_argument("--incremental-payload", type=Path, action="append", default=[],
                        help="attach an existing schema 2 component payload (repeatable, one per base)")
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
    if args.components_only:
        if args.payload or args.include or args.package_url or not args.asset_prefix:
            parser.error("--components-only requires --asset-prefix and cannot use --payload, --include or --package-url")
        if not args.incremental_payload:
            parser.error("--components-only requires at least one --incremental-payload")
    elif not args.payload or not args.package_url or args.asset_prefix:
        parser.error("default releases require --payload and --package-url; --asset-prefix requires --components-only")

    artifacts = (COMMERCIAL_ROOT / "artifacts").resolve()
    output = args.output.expanduser().resolve()
    if output == artifacts or artifacts not in output.parents:
        parser.error("output must stay inside the commercial artifacts directory")
    if output.exists() and any(output.iterdir()):
        parser.error(f"output must not already contain files: {output}")
    output.mkdir(parents=True, exist_ok=True)

    if args.components_only:
        private = _signing_key(args.private_key.expanduser().resolve())
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        channel = UpdateChannel(
            "https://updates.invalid/stable.json", "stable",
            hashlib.sha256(public).hexdigest().upper()[:16], public,
        )
        manifest, assets = build_component_release(
            args.incremental_payload, output, version=_project_version(), private_key_path=args.private_key,
            channel=channel, asset_prefix=args.asset_prefix, title=args.title, notes=tuple(args.note),
            minimum_supported_version=args.minimum_supported_version, build=args.build, mandatory=args.mandatory,
        )
        print(json.dumps({"manifest": str(manifest), "component_packages": [str(path) for path in assets],
                          "upload_order": [*[path.name for path in assets], manifest.name]}, ensure_ascii=False, indent=2))
        return 0

    payload = args.payload.expanduser().resolve()
    verified = verify_payload(payload)
    if args.incremental_payload and verified.manifest.get("schema_version") == 2:
        parser.error("multiple-base releases require an unbound default payload")
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
    _write_payload_archive(verified, selected, archive, version=version,
                           build=args.build, filtered=bool(args.include))
    with zipfile.ZipFile(archive, "r") as package:
        uncompressed_size = sum(item.file_size for item in package.infolist())

    private_key = _signing_key(args.private_key.expanduser().resolve())
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
    key_id = _sign_manifest(manifest, private_key)
    manifest_path = output / "stable.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    incremental_assets = attach_incremental_payloads(
        manifest_path, args.incremental_payload, private_key_path=args.private_key.expanduser().resolve(),
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
                "upload_order": [archive.name, *[path.name for path in incremental_assets], manifest_path.name],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
