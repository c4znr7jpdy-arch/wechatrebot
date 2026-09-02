from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tomllib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.version import Version


COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from wechatrebot_manager.updates import PRODUCT_ID, canonical_manifest_bytes


def _project_version() -> str:
    payload = tomllib.loads(
        (COMMERCIAL_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(Version(str(payload["project"]["version"])))


def _signing_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("update signing key must be Ed25519")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a signed current-version status manifest without an update package."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--title", default="当前稳定版本")
    parser.add_argument("--note", action="append", default=[])
    args = parser.parse_args(argv)

    artifacts = (COMMERCIAL_ROOT / "artifacts").resolve()
    output = args.output.expanduser().resolve()
    if output == artifacts or artifacts not in output.parents:
        parser.error("output must stay inside the commercial artifacts directory")
    if output.exists() and any(output.iterdir()):
        parser.error(f"output must not already contain files: {output}")
    output.mkdir(parents=True, exist_ok=True)

    private_key = _signing_key(args.private_key.expanduser().resolve())
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public_bytes).hexdigest().upper()[:16]
    public_config = json.loads(
        (
            COMMERCIAL_ROOT
            / "src"
            / "wechatrebot_manager"
            / "update-channel.json"
        ).read_text(encoding="utf-8")
    )
    configured_public_key = str(public_config.get("public_key_base64") or "")
    if (
        public_config.get("key_id") != key_id
        or configured_public_key != base64.b64encode(public_bytes).decode("ascii")
    ):
        parser.error("private key does not match the public key embedded in the client")

    version = _project_version()
    manifest: dict[str, object] = {
        "schema_version": 1,
        "product": PRODUCT_ID,
        "channel": str(public_config.get("channel") or "stable"),
        "version": version,
        "minimum_supported_version": version,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": args.title.strip()[:100] or f"版本 {version}",
        "notes": [item.strip()[:500] for item in args.note if item.strip()][:20],
        "package": None,
    }
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
                "manifest": str(manifest_path),
                "key_id": key_id,
                "package_published": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
