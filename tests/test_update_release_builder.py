from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wechatrebot_manager.payload import sha256_file, verify_payload
from wechatrebot_manager.updates import UpdateChannel, extract_update_archive, parse_update_manifest


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
                "--build",
                "5",
                "--mandatory",
            ]
        )
        == 0
    )
    manifest = json.loads((output / "stable.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.8.0"
    assert manifest["notes"] == ["安全更新"]
    assert manifest["build"] == 5
    assert manifest["mandatory"] is True
    assert manifest["package"]["uncompressed_size"] > 0
    assert manifest["signature"]["algorithm"] == "ed25519"
    archive_path = output / "wechatrebot-0.8.0-update.zip"
    assert archive_path.is_file()
    with zipfile.ZipFile(archive_path) as archive:
        payload_manifest = json.loads(
            archive.read("payload/release-manifest.json").decode("utf-8")
        )
    assert payload_manifest["source_manifest_status"] == "ready"
    assert payload_manifest["release_build"] == 5


def _archive_release_fixture(tmp_path: Path, monkeypatch, *, schema: int, removal_only=False):
    module = _builder()
    commercial = tmp_path / "commercial"
    commercial.mkdir()
    (commercial / "pyproject.toml").write_text(
        '[project]\nname="test"\nversion="0.9.9"\n', encoding="utf-8"
    )
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", commercial)
    private = Ed25519PrivateKey.generate()
    key_path = tmp_path / "private.pem"
    key_path.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    source = tmp_path / "incremental.zip"
    relative = "app/astrbot/data/plugins/fixture_plugin/main.py"
    content = b"print('new plugin content')\n"
    manifest = {
        "schema_version": schema,
        "files": [] if removal_only else [{
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest().upper(),
        }],
    }
    if schema == 2:
        manifest.update({
            "update_kind": "incremental",
            "base_fingerprint": "A" * 64,
            "remove": [{
                "path": "app/astrbot/data/plugins/fixture_plugin/obsolete.py",
                "sha256": "B" * 64,
            }],
        })
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        if not removal_only:
            archive.writestr(relative, content)
    output = commercial / "artifacts" / "release"
    arguments = [
        "--payload", str(source), "--output", str(output),
        "--private-key", str(key_path),
        "--package-url", "https://updates.example.test/incremental.zip",
        "--build", "18",
    ]
    return module, arguments, output, private, manifest, relative, content


@pytest.mark.parametrize("schema,removal_only", [(1, False), (2, False), (2, True)])
def test_update_release_builder_streams_archive_members_and_preserves_incremental_operations(
    tmp_path, monkeypatch, schema, removal_only
):
    module, arguments, output, private, expected, relative, content = _archive_release_fixture(
        tmp_path, monkeypatch, schema=schema, removal_only=removal_only,
    )
    assert module.main(arguments) == 0
    manifest = json.loads((output / "stable.json").read_text(encoding="utf-8"))
    assert manifest["package"]["payload_file_count"] == (0 if removal_only else 1)
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    channel = UpdateChannel(
        manifest_url="https://updates.example.test/stable.json", channel="stable",
        key_id=hashlib.sha256(public).hexdigest().upper()[:16], public_key=public,
    )
    release = parse_update_manifest(manifest, channel, current_version="0.9.9", current_build=17)
    assert release and release.build == 18
    archive_path = output / "wechatrebot-0.9.9-update.zip"
    assert release.package_sha256 == sha256_file(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert len(archive.namelist()) == (1 if removal_only else 2)
        if not removal_only:
            assert archive.read(f"payload/{relative}") == content
    extracted = extract_update_archive(archive_path, tmp_path / "extracted")
    verified = verify_payload(extracted)
    assert verified.manifest == {
        **expected, "source_manifest_status": "ready",
        "release_version": "0.9.9", "release_build": 18,
    }


def test_update_release_builder_refuses_filtering_incremental_payload(tmp_path, monkeypatch):
    module, arguments, output, _, _, relative, _ = _archive_release_fixture(
        tmp_path, monkeypatch, schema=2,
    )
    with pytest.raises(SystemExit) as error:
        module.main([*arguments, "--include", relative])
    assert error.value.code == 2
    assert not list(output.glob("*.zip"))


def test_update_release_builder_can_create_selected_payload_archive(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    artifacts = commercial / "artifacts"
    artifacts.mkdir(parents=True)
    (commercial / "pyproject.toml").write_text(
        '[project]\nname="test"\nversion="0.8.8"\n', encoding="utf-8"
    )
    payload = tmp_path / "payload"
    manager = payload / "manager" / "WeChatReBot.exe"
    runtime = payload / "runtime" / "deep" / "dependency.py"
    manager.parent.mkdir(parents=True)
    runtime.parent.mkdir(parents=True)
    manager.write_bytes(b"manager")
    runtime.write_bytes(b"runtime")
    (payload / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [
                    {
                        "path": "manager/WeChatReBot.exe",
                        "sha256": sha256_file(manager),
                    },
                    {
                        "path": "runtime/deep/dependency.py",
                        "sha256": sha256_file(runtime),
                    },
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
                "https://updates.example.test/wechatrebot-0.8.8-manager.zip",
                "--include",
                "manager/WeChatReBot.exe",
            ]
        )
        == 0
    )
    manifest = json.loads((output / "stable.json").read_text(encoding="utf-8"))
    assert manifest["package"]["payload_file_count"] == 1
    archive_path = output / "wechatrebot-0.8.8-update.zip"
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [
            "payload/release-manifest.json",
            "payload/manager/WeChatReBot.exe",
        ]
        payload_manifest = json.loads(
            archive.read("payload/release-manifest.json").decode("utf-8")
        )
    assert payload_manifest["files"] == [
        {
            "path": "manager/WeChatReBot.exe",
            "sha256": sha256_file(manager),
        }
    ]
    assert payload_manifest["source_manifest_status"] == "ready"
