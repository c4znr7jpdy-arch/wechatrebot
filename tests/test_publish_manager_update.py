from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
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
        base_url="https://updates.example.test/product",
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
            base_url="https://updates.example.test/product",
            title="update",
            notes=(),
            minimum_supported_version="0.7.0",
        )


@pytest.mark.parametrize(
    ("host", "remote_root"),
    [
        ("root@example.test;whoami", "/srv/updates"),
        ("root@example.test", "/"),
        ("root@example.test", "relative/path"),
    ],
)
def test_remote_publish_inputs_reject_shell_metacharacters(
    host: str, remote_root: str
) -> None:
    module = _publisher()
    with pytest.raises(ValueError):
        module._safe_remote_inputs(host, remote_root)


def test_manager_delta_rejects_legacy_stable_base() -> None:
    module = _publisher()

    with pytest.raises(ValueError, match="完整载荷发布 0.8.7"):
        module._require_safe_delta_base("0.8.6")

    module._require_safe_delta_base("0.8.7")


def test_publish_runs_after_switch_inside_manifest_rollback_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    module = _publisher()
    archive = tmp_path / "update.zip"
    archive.write_bytes(b"update")
    manifest = tmp_path / "stable.json"
    manifest.write_text("{}", encoding="utf-8")
    release = module.BuiltManagerUpdate(
        version="0.8.8",
        archive_path=archive,
        manifest_path=manifest,
        package_url="https://updates.example.test/releases/update.zip",
        package_size=archive.stat().st_size,
        package_sha256=sha256_file(archive),
    )
    channel = UpdateChannel(
        manifest_url="https://updates.example.test/stable.json",
        channel="stable",
        key_id="test",
        public_key=b"0" * 32,
    )
    ssh_commands: list[str] = []
    callbacks: list[str] = []
    parsed = SimpleNamespace(
        version=release.version,
        package_sha256=release.package_sha256,
        package_size=release.package_size,
        package_url=release.package_url,
    )
    monkeypatch.setattr(module, "_ssh", lambda _host, command: ssh_commands.append(command))
    monkeypatch.setattr(module, "_scp", lambda *_args: None)
    monkeypatch.setattr(module, "_public_package_head", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_fetch_live_manifest", lambda _channel: {})
    monkeypatch.setattr(module, "parse_update_manifest", lambda *_args, **_kwargs: parsed)

    module._publish(
        release,
        channel=channel,
        previous_version="0.8.7",
        host="root@example.test",
        remote_root="/srv/updates",
        smoke_root=tmp_path / "smoke",
        skip_remote_smoke=True,
        after_switch=lambda: callbacks.append("activated"),
    )

    assert callbacks == ["activated"]
    assert not any("rollback-" in command for command in ssh_commands[-1:])


def test_publish_rolls_back_stable_manifest_when_after_switch_fails(
    tmp_path: Path, monkeypatch
) -> None:
    module = _publisher()
    archive = tmp_path / "update.zip"
    archive.write_bytes(b"update")
    manifest = tmp_path / "stable.json"
    manifest.write_text("{}", encoding="utf-8")
    release = module.BuiltManagerUpdate(
        version="0.8.8",
        archive_path=archive,
        manifest_path=manifest,
        package_url="https://updates.example.test/releases/update.zip",
        package_size=archive.stat().st_size,
        package_sha256=sha256_file(archive),
    )
    channel = UpdateChannel(
        manifest_url="https://updates.example.test/stable.json",
        channel="stable",
        key_id="test",
        public_key=b"0" * 32,
    )
    ssh_commands: list[str] = []
    parsed = SimpleNamespace(
        version=release.version,
        package_sha256=release.package_sha256,
        package_size=release.package_size,
        package_url=release.package_url,
    )
    monkeypatch.setattr(module, "_ssh", lambda _host, command: ssh_commands.append(command))
    monkeypatch.setattr(module, "_scp", lambda *_args: None)
    monkeypatch.setattr(module, "_public_package_head", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_fetch_live_manifest", lambda _channel: {})
    monkeypatch.setattr(module, "parse_update_manifest", lambda *_args, **_kwargs: parsed)

    with pytest.raises(RuntimeError, match="download metadata failed"):
        module._publish(
            release,
            channel=channel,
            previous_version="0.8.7",
            host="root@example.test",
            remote_root="/srv/updates",
            smoke_root=tmp_path / "smoke",
            skip_remote_smoke=True,
            after_switch=lambda: (_ for _ in ()).throw(
                RuntimeError("download metadata failed")
            ),
        )

    assert "stable.json.rollback-" in ssh_commands[-1]


def test_publish_with_oss_only_copies_manifest_to_server(
    tmp_path: Path, monkeypatch
) -> None:
    module = _publisher()
    archive = tmp_path / "update.zip"
    archive.write_bytes(b"update")
    manifest = tmp_path / "stable.json"
    manifest.write_text("{}", encoding="utf-8")
    release = module.BuiltManagerUpdate(
        version="0.8.8",
        archive_path=archive,
        manifest_path=manifest,
        package_url="https://download.example.test/releases/update.zip",
        package_size=archive.stat().st_size,
        package_sha256=sha256_file(archive),
    )
    channel = UpdateChannel(
        manifest_url="https://updates.example.test/stable.json",
        channel="stable",
        key_id="test",
        public_key=b"0" * 32,
    )
    copied: list[Path] = []
    parsed = SimpleNamespace(
        version=release.version,
        package_sha256=release.package_sha256,
        package_size=release.package_size,
        package_url=release.package_url,
    )
    monkeypatch.setattr(module, "_ssh", lambda *_args: None)
    monkeypatch.setattr(module, "_scp", lambda path, *_args: copied.append(path))
    monkeypatch.setattr(module, "_public_package_head", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_fetch_live_manifest", lambda _channel: {})
    monkeypatch.setattr(module, "parse_update_manifest", lambda *_args, **_kwargs: parsed)

    module._publish(
        release,
        channel=channel,
        previous_version="0.8.7",
        host="root@example.test",
        remote_root="/srv/updates",
        smoke_root=tmp_path / "smoke",
        skip_remote_smoke=True,
        upload_archive_to_server=False,
    )

    assert copied == [manifest]
