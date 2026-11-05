from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wechatrebot_manager.payload import sha256_file
from wechatrebot_manager.updates import UpdateChannel, parse_update_manifest


def _publisher():
    script = Path(__file__).parents[1] / "scripts" / "publish_manager_update.py"
    spec = importlib.util.spec_from_file_location("publish_manager_update", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manager_build(root: Path, version: str = "0.8.8") -> Path:
    executable = root / "dist" / "WeChatReBot.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"manager-update")
    (root / "manager-build-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manager_version": version,
                "filename": executable.name,
                "size": executable.stat().st_size,
                "sha256": sha256_file(executable),
                "self_test": {
                    "ok": True,
                    "frozen": True,
                    "architecture": 64,
                    "manager_version": version,
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _channel_and_key(tmp_path: Path) -> tuple[UpdateChannel, Path]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public).hexdigest().upper()[:16]
    key_path = tmp_path / "update-key.pem"
    key_path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return (
        UpdateChannel(
            manifest_url="https://updates.example.test/stable.json",
            channel="stable",
            key_id=key_id,
            public_key=public,
        ),
        key_path,
    )


def test_build_manager_update_creates_small_signed_delta(tmp_path: Path) -> None:
    module = _publisher()
    manager_build = _manager_build(tmp_path / "manager-build")
    channel, key_path = _channel_and_key(tmp_path)

    result = module.build_manager_update(
        manager_build=manager_build,
        output=tmp_path / "output",
        version="0.8.8",
        private_key_path=key_path,
        channel=channel,
        asset_prefix="https://github.com/o/r/releases/download/v0.8.8",
        title="插件配置更新",
        notes=("修复高级页面",),
        minimum_supported_version="0.7.0",
        build=42,
        mandatory=True,
    )

    assert result.package_size == result.archive_path.stat().st_size
    assert result.package_sha256 == sha256_file(result.archive_path)
    assert result.package_sha256[:12].lower() in result.archive_path.name
    with zipfile.ZipFile(result.archive_path) as archive:
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
            "sha256": sha256_file(manager_build / "dist" / "WeChatReBot.exe"),
        }
    ]
    assert payload_manifest["source_manifest_status"] == "ready"
    assert payload_manifest["release_build"] == 42
    stable = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    release = parse_update_manifest(stable, channel, current_version="0.8.7")
    assert release is not None
    assert release.version == "0.8.8"
    assert release.package_url == result.package_url
    assert result.package_url == f"https://github.com/o/r/releases/download/v0.8.8/{result.archive_path.name}"
    assert release.build == 42
    assert release.mandatory is True
    assert stable["package"]["payload_file_count"] == 1
    assert stable["package"]["uncompressed_size"] > 0


def test_build_manager_update_rejects_tampered_manager(tmp_path: Path) -> None:
    module = _publisher()
    manager_build = _manager_build(tmp_path / "manager-build")
    channel, key_path = _channel_and_key(tmp_path)
    (manager_build / "dist" / "WeChatReBot.exe").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="摘要不一致"):
        module.build_manager_update(
            manager_build=manager_build,
            output=tmp_path / "output",
            version="0.8.8",
            private_key_path=key_path,
            channel=channel,
            asset_prefix="https://github.com/o/r/releases/download/v0.8.8",
            title="update",
            notes=(),
            minimum_supported_version="0.7.0",
        )


def test_manager_delta_rejects_legacy_stable_base() -> None:
    module = _publisher()

    with pytest.raises(ValueError, match="完整载荷发布 0.8.7"):
        module._require_safe_delta_base("0.8.6")

    module._require_safe_delta_base("0.8.7")


def test_live_manifest_does_not_reuse_cached_stable_redirect(tmp_path, monkeypatch):
    from dataclasses import replace
    from urllib.parse import parse_qs, urlsplit
    import io

    module = _publisher()
    channel, _key = _channel_and_key(tmp_path)
    channel = replace(channel, manifest_url="https://github.com/o/r/releases/download/stable/stable.json")
    seen = []

    def cached_edge(request, **kwargs):
        seen.append(request.full_url)
        # The fixed URL still points to an old redirect, even with no-cache.
        fresh = bool(parse_qs(urlsplit(request.full_url).query).get("_wrb_check"))
        return io.BytesIO(json.dumps({"version": "1.0.1" if fresh else "1.0.0"}).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", cached_edge)
    monkeypatch.setattr(module, "parse_update_manifest", lambda *args, **kwargs: None)
    assert module._fetch_live_manifest(channel)["version"] == "1.0.1"
    assert module._fetch_live_manifest(channel)["version"] == "1.0.1"
    assert seen[0] != seen[1]
    assert channel.manifest_url.endswith("/stable/stable.json")
