from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _exclusive_private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the private signing key and embedded public update config."
    )
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--public-config", type=Path, required=True)
    parser.add_argument("--manifest-url", default="")
    parser.add_argument("--force-public-config", action="store_true")
    args = parser.parse_args(argv)

    private_path = args.private_key.expanduser().resolve()
    public_path = args.public_config.expanduser().resolve()
    if private_path.exists():
        parser.error(f"private key already exists: {private_path}")
    if public_path.exists() and not args.force_public_config:
        parser.error(f"public config already exists: {public_path}")

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public_bytes).hexdigest().upper()[:16]
    config = {
        "schema_version": 1,
        "channel": "stable",
        "manifest_url": args.manifest_url.strip(),
        "key_id": key_id,
        "public_key_base64": base64.b64encode(public_bytes).decode("ascii"),
        "check_interval_seconds": 21_600,
        "request_timeout_seconds": 20,
        "max_package_bytes": 2 * 1024**3,
    }

    _exclusive_private_write(private_path, private_pem)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "key_id": key_id,
                "private_key": str(private_path),
                "public_config": str(public_path),
                "manifest_url_configured": bool(config["manifest_url"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
