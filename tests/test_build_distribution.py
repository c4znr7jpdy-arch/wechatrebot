import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import zipfile

def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_distribution.py"
    spec = importlib.util.spec_from_file_location("build_distribution", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(root: Path, *, legacy_status: str | None = None) -> None:
    manager = root / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir(parents=True)
    executable = bytearray(256)
    executable[:2] = b"MZ"
    struct.pack_into("<I", executable, 0x3C, 0x80)
    executable[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", executable, 0x84, 0x8664)
    manager.write_bytes(executable)
    digest = hashlib.sha256(manager.read_bytes()).hexdigest().upper()
    manifest: dict[str, object] = {
        "schema_version": 1,
        "files": [{"path": "manager/WeChatReBot.exe", "sha256": digest}],
    }
    if legacy_status is not None:
        manifest["source_manifest_status"] = legacy_status
    (root / "release-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def test_distribution_does_not_gate_on_legacy_payload_status(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    artifacts = commercial / "artifacts"
    release = commercial / "release"
    release.mkdir(parents=True)
    (release / "START-HERE.txt").write_text("start\n", encoding="utf-8")
    payload = tmp_path / "payload"
    _payload(payload, legacy_status="phase-incomplete")
    output = artifacts / "customer-package"
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", commercial)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0})(),
    )

    assert module.main(["--payload", str(payload), "--output", str(output)]) == 0
    manifest = json.loads(
        (output / "DISTRIBUTION-MANIFEST.json").read_text(encoding="utf-8")
    )
    assert "payload_status" not in manifest
    assert "development_preview" not in manifest


def test_distribution_contains_one_click_entry_and_nested_payload(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    artifacts = commercial / "artifacts"
    release = commercial / "release"
    release.mkdir(parents=True)
    (release / "START-HERE.txt").write_text("start\n", encoding="utf-8")
    payload = tmp_path / "payload"
    _payload(payload)
    output = artifacts / "customer-package"
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", commercial)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0})(),
    )

    assert module.main(["--payload", str(payload), "--output", str(output)]) == 0
    assert (output / "WeChatReBot.exe").read_bytes().startswith(b"MZ")
    assert (output / "payload.wrb").is_file()
    with zipfile.ZipFile(output / "payload.wrb") as archive:
        assert "release-manifest.json" in archive.namelist()
        assert "manager/WeChatReBot.exe" in archive.namelist()
    manifest = json.loads(
        (output / "DISTRIBUTION-MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["entrypoint"] == "WeChatReBot.exe"
    assert manifest["payload_file"] == "payload.wrb"
    assert manifest["payload_format"] == "zip-v1"


def test_outer_archive_preserves_dotted_release_name(tmp_path: Path) -> None:
    module = _builder()
    output = tmp_path / "WeChatReBot-0.8.1-Audit"
    output.mkdir()
    (output / "WeChatReBot.exe").write_bytes(b"exe")

    archive = module._write_outer_archive(output)

    assert archive.name == "WeChatReBot-0.8.1-Audit.zip"
    assert archive.is_file()
