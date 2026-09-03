import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import zipfile

import pytest


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_distribution.py"
    spec = importlib.util.spec_from_file_location("build_distribution", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(root: Path, *, status: str) -> None:
    manager = root / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir(parents=True)
    executable = bytearray(256)
    executable[:2] = b"MZ"
    struct.pack_into("<I", executable, 0x3C, 0x80)
    executable[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", executable, 0x84, 0x8664)
    manager.write_bytes(executable)
    digest = hashlib.sha256(manager.read_bytes()).hexdigest().upper()
    (root / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_manifest_status": status,
                "files": [
                    {"path": "manager/WeChatReBot.exe", "sha256": digest}
                ],
            }
        ),
        encoding="utf-8",
    )


def test_distribution_requires_ready_payload_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    (commercial / "artifacts").mkdir(parents=True)
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", commercial)
    payload = tmp_path / "payload"
    _payload(payload, status="phase-incomplete")
    with pytest.raises(Exception, match="尚未达到"):
        module.main(
            [
                "--payload",
                str(payload),
                "--output",
                str(commercial / "artifacts" / "customer-package"),
            ]
        )


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
    _payload(payload, status="ready")
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
    assert manifest["payload_status"] == "ready"
    assert manifest["payload_file"] == "payload.wrb"
    assert manifest["payload_format"] == "zip-v1"
    assert manifest["development_preview"] is False


def test_incomplete_distribution_is_explicitly_marked_as_development_preview(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    artifacts = commercial / "artifacts"
    release = commercial / "release"
    release.mkdir(parents=True)
    (release / "START-HERE.txt").write_text("start\n", encoding="utf-8")
    payload = tmp_path / "payload"
    _payload(payload, status="phase-incomplete")
    output = artifacts / "customer-package"
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", commercial)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0})(),
    )

    assert (
        module.main(
            [
                "--payload",
                str(payload),
                "--output",
                str(output),
                "--allow-incomplete",
            ]
        )
        == 0
    )
    manifest = json.loads(
        (output / "DISTRIBUTION-MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["development_preview"] is True


def test_outer_archive_preserves_dotted_release_name(tmp_path: Path) -> None:
    module = _builder()
    output = tmp_path / "WeChatReBot-0.8.1-Audit"
    output.mkdir()
    (output / "WeChatReBot.exe").write_bytes(b"exe")

    archive = module._write_outer_archive(output)

    assert archive.name == "WeChatReBot-0.8.1-Audit.zip"
    assert archive.is_file()
