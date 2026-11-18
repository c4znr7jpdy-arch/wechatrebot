from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wechatrebot_manager.payload import sha256_file, verify_payload
from wechatrebot_manager.updates import UpdateChannel, UpdateError, canonical_manifest_bytes, extract_update_archive


def _builder():
    script = Path(__file__).parents[1] / "scripts/build_update_release.py"
    spec = importlib.util.spec_from_file_location("component_release_builder", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release_inputs(tmp_path):
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "test-key.pem"
    private.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    channel = UpdateChannel("https://example.test/stable.json", "stable",
                            hashlib.sha256(public).hexdigest().upper()[:16], public)
    return key, {"version": "1.0.23", "private_key_path": private, "channel": channel,
                 "asset_prefix": "https://github.com/example/robot/releases/download/v1.0.23",
                 "title": "组件修复", "notes": ("媒体接收修复",), "build": 7}


def _delta(path, base="A" * 64, *, removal_only=False, **fields):
    relative = "app/bridge/src/media.py"
    content = b"def media_ready(): return True\n"
    manifest = {
        "schema_version": 2, "update_kind": "incremental", "base_fingerprint": base,
        "release_version": "1.0.23", "release_build": 7,
        "managed_roots": ["app/bridge/src"],
        "files": [] if removal_only else [{"path": relative, "sha256": hashlib.sha256(content).hexdigest()}],
        "remove": [{"path": "app/bridge/src/retired.py", "sha256": "D" * 64}], **fields,
    }
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("release-manifest.json", json.dumps(manifest))
        if not removal_only:
            package.writestr(relative, content)
    return path, manifest


def test_component_release_signs_all_complete_operations_without_a_default_package(tmp_path, release_inputs):
    key, options = release_inputs
    payloads = [_delta(tmp_path / "a.zip"), _delta(tmp_path / "b.zip", "B" * 64, removal_only=True)]
    manifest_path, assets = _builder().build_component_release(
        [entry[0] for entry in payloads], tmp_path / "release", mandatory=True, **options,
    )
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert manifest["component_update_protocol"] == 1
    assert manifest["version"] == "1.0.23" and manifest["build"] == 7
    assert manifest["mandatory"] is True and manifest["notes"] == ["媒体接收修复"]
    assert "package" not in manifest and len(assets) == 2
    key.public_key().verify(base64.b64decode(manifest["signature"]["value"]), canonical_manifest_bytes(manifest))
    assert [entry["base_fingerprint"] for entry in manifest["incremental_packages"]] == ["A" * 64, "B" * 64]
    for index, (entry, archive) in enumerate(zip(manifest["incremental_packages"], assets, strict=True)):
        package = entry["package"]
        assert package["url"] == f"{options['asset_prefix']}/{archive.name}"
        assert package["size"] == archive.stat().st_size
        assert package["sha256"] == sha256_file(archive)
        with zipfile.ZipFile(archive) as zipped:
            assert package["uncompressed_size"] == sum(item.file_size for item in zipped.infolist())
        extracted = extract_update_archive(archive, tmp_path / f"expanded-{index}")
        verified = verify_payload(extracted)
        assert verified.manifest == {**payloads[index][1], "source_manifest_status": "ready"}
    manifest["incremental_packages"][0]["package"]["size"] += 1
    with pytest.raises(InvalidSignature):
        key.public_key().verify(base64.b64decode(manifest["signature"]["value"]), canonical_manifest_bytes(manifest))


@pytest.mark.parametrize("case", ["empty", "duplicate", "limit", "version", "build", "schema", "missing_version", "missing_build"])
def test_component_release_rejects_invalid_operations_before_writing_outputs(tmp_path, release_inputs, case):
    _, options = release_inputs
    fields = {"release_version": "1.0.24"} if case == "version" else (
        {"release_build": 8} if case == "build" else ({"schema_version": 1} if case == "schema" else {}))
    payload, manifest = _delta(tmp_path / "delta.zip", **fields)
    if case.startswith("missing_"):
        del manifest["release_" + case.removeprefix("missing_")]
        with zipfile.ZipFile(payload, "w") as package:
            package.writestr("release-manifest.json", json.dumps(manifest))
            package.writestr("app/bridge/src/media.py", b"def media_ready(): return True\n")
    payloads = [] if case == "empty" else ([payload] * 2 if case == "duplicate" else [payload] * 65 if case == "limit" else [payload])
    output = tmp_path / "release"
    with pytest.raises((ValueError, RuntimeError)):
        _builder().build_component_release(payloads, output, **options)
    assert not output.exists()


@pytest.mark.parametrize("changes", [{"build": True}, {"build": -1}, {"mandatory": 1},
                                     {"version": "v1.0.23"}, {"asset_prefix": "http://example.test/release"},
                                     {"asset_prefix": "https://example.test/release?token=unsafe"}])
def test_component_release_rejects_invalid_metadata_before_writing_outputs(tmp_path, release_inputs, changes):
    _, options = release_inputs
    delta, _ = _delta(tmp_path / "delta.zip")
    with pytest.raises(ValueError):
        _builder().build_component_release([delta], tmp_path / "release", **{**options, **changes})
    assert not (tmp_path / "release").exists()


def test_component_release_checks_signing_channel_and_preserves_existing_outputs(tmp_path, release_inputs):
    _, options = release_inputs
    delta, _ = _delta(tmp_path / "delta.zip")
    wrong_channel = UpdateChannel("https://example.test/stable.json", "stable", "0" * 16, b"\0" * 32)
    with pytest.raises(ValueError, match="signing key"):
        _builder().build_component_release([delta], tmp_path / "wrong", **{**options, "channel": wrong_channel})
    output = tmp_path / "release"
    output.mkdir()
    marker = output / "preserve.txt"
    marker.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="output"):
        _builder().build_component_release([delta], output, **options)
    assert marker.read_text("utf-8") == "existing" and list(output.iterdir()) == [marker]


def test_component_release_respects_the_channels_package_limit(tmp_path, release_inputs):
    _, options = release_inputs
    delta, _ = _delta(tmp_path / "delta.zip")
    channel = replace(options["channel"], max_package_bytes=1)
    output = tmp_path / "release"
    with pytest.raises(UpdateError, match="大小"):
        _builder().build_component_release([delta], output, **{**options, "channel": channel})
    assert not (output / "stable.json").exists()


def test_component_release_cli_refuses_filtering_or_a_default_payload(tmp_path, release_inputs, monkeypatch):
    module = _builder()
    _, options = release_inputs
    delta, _ = _delta(tmp_path / "delta.zip")
    arguments = ["--components-only", "--incremental-payload", str(delta), "--output", str(tmp_path / "out"),
                 "--private-key", str(options["private_key_path"]), "--asset-prefix", options["asset_prefix"]]
    for incompatible in (["--include", "app/bridge/src/media.py"], ["--payload", str(delta)]):
        with pytest.raises(SystemExit) as error:
            module.main([*arguments, *incompatible])
        assert error.value.code == 2
    assert not (tmp_path / "out").exists()


def test_component_release_cli_builds_only_component_archives(tmp_path, release_inputs, monkeypatch):
    module = _builder()
    _, options = release_inputs
    delta, _ = _delta(tmp_path / "delta.zip")
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", tmp_path)
    monkeypatch.setattr(module, "_project_version", lambda: "1.0.23")
    output = tmp_path / "artifacts/components"
    assert module.main(["--components-only", "--incremental-payload", str(delta), "--output", str(output),
                        "--private-key", str(options["private_key_path"]), "--asset-prefix", options["asset_prefix"],
                        "--build", "7"]) == 0
    manifest = json.loads((output / "stable.json").read_text("utf-8"))
    assert "package" not in manifest and len(list(output.glob("*.zip"))) == 1
