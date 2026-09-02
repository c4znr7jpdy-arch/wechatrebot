from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wechatrebot_manager.payload import sha256_file


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_update_release.py"
    spec = importlib.util.spec_from_file_location("build_update_release", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_release_builder_creates_signed_archive_and_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    artifacts = commercial / "artifacts"
    artifacts.mkdir(parents=True)
    (commercial / "pyproject.toml").write_text(
        '[project]\nname="test"\nversion="0.8.0"\n', encoding="utf-8"
    )
    payload = tmp_path / "payload"
    manager = payload / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(b"manager")
    (payload / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_manifest_status": "ready",
                "files": [
                    {
                        "path": "manager/WeChatReBot.exe",
                        "sha256": sha256_file(manager),
                    }
                ],
            }
        ),
        encoding="utf-8",
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
    output = artifacts / "release"
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", commercial)

    assert (
        module.main(
            [
                "--payload",
                str(payload),
                "--output",
                str(output),
                "--private-key",
                str(key_path),
                "--package-url",
                "https://updates.example.test/wechatrebot-0.8.0-update.zip",
                "--note",
                "安全更新",
            ]
        )
        == 0
    )
    manifest = json.loads((output / "stable.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.8.0"
    assert manifest["notes"] == ["安全更新"]
    assert manifest["signature"]["algorithm"] == "ed25519"
    assert (output / "wechatrebot-0.8.0-update.zip").is_file()
