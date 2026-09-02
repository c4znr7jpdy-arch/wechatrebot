from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_update_status.py"
    spec = importlib.util.spec_from_file_location("build_update_status", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_status_builder_creates_signed_manifest_without_package(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    artifacts = commercial / "artifacts"
    config_root = commercial / "src" / "wechatrebot_manager"
    artifacts.mkdir(parents=True)
    config_root.mkdir(parents=True)
    (commercial / "pyproject.toml").write_text(
        '[project]\nname="test"\nversion="0.8.0"\n', encoding="utf-8"
    )
    private = Ed25519PrivateKey.generate()
    key_path = tmp_path / "private.pem"
    key_path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public).hexdigest().upper()[:16]
    (config_root / "update-channel.json").write_text(
        json.dumps(
            {
                "channel": "stable",
                "key_id": key_id,
                "public_key_base64": base64.b64encode(public).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )
    output = artifacts / "status"
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", commercial)

    assert (
        module.main(
            [
                "--output",
                str(output),
                "--private-key",
                str(key_path),
                "--note",
                "更新服务已就绪",
            ]
        )
        == 0
    )
    manifest = json.loads((output / "stable.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.8.0"
    assert manifest["package"] is None
    assert manifest["notes"] == ["更新服务已就绪"]
    assert manifest["signature"]["key_id"] == key_id
