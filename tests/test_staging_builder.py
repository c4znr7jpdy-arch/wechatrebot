import importlib.util
import json
from pathlib import Path
import struct

import pytest


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_staging.py"
    spec = importlib.util.spec_from_file_location("build_staging", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_manifest(root: Path) -> dict[str, object]:
    commercial = Path(__file__).parents[1]
    wheelhouse = json.loads(
        (commercial / "release" / "runtime-wheelhouse.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        "schema_version": 1,
        "python": {
            "version": wheelhouse["python_version"],
            "architecture": "x64",
        },
        "wheelhouse": {
            key: wheelhouse[key]
            for key in ("wheel_count", "total_bytes", "aggregate_sha256")
        },
    }


def test_runtime_root_validation_accepts_matching_fingerprint(tmp_path: Path) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    (runtime / "python").mkdir(parents=True)
    (runtime / "python" / "python.exe").write_bytes(b"placeholder")
    manifest = _runtime_manifest(commercial)
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    validated = _builder()._validate_runtime_root(runtime, commercial)
    assert validated["wheelhouse"] == manifest["wheelhouse"]


def test_runtime_root_validation_rejects_wrong_fingerprint(tmp_path: Path) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    (runtime / "python").mkdir(parents=True)
    (runtime / "python" / "python.exe").write_bytes(b"placeholder")
    manifest = _runtime_manifest(commercial)
    manifest["wheelhouse"]["wheel_count"] = 1
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        _builder()._validate_runtime_root(runtime, commercial)


def test_staging_manager_pe_check_rejects_plain_file(tmp_path: Path) -> None:
    source = tmp_path / "manager.exe"
    source.write_bytes(b"not-a-pe")
    with pytest.raises(RuntimeError, match="Windows PE"):
        _builder()._validate_manager_exe(source)


def test_staging_manager_pe_check_requires_x64(tmp_path: Path) -> None:
    source = tmp_path / "manager.exe"
    payload = bytearray(256)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", payload, 0x84, 0x014C)
    source.write_bytes(payload)
    with pytest.raises(RuntimeError, match="x64"):
        _builder()._validate_manager_exe(source)


def test_staging_provenance_does_not_record_source_machine_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / "staging"
    target = output / "manager" / "WeChatReBot.exe"

    assert _builder()._copied_record(target, output) == {
        "target": "manager/WeChatReBot.exe"
    }


def test_staging_keeps_legacy_update_bootstrap_marker(tmp_path: Path) -> None:
    module = _builder()
    source_manifest = tmp_path / "source-manifest.json"
    source_manifest.write_text("{}", encoding="utf-8")

    manifest = module._release_manifest(
        version="0.8.6",
        source_manifest=source_manifest,
        copied=[],
        runtime=None,
        files=[],
    )

    assert manifest["source_manifest_status"] == "ready"
    assert manifest["release_version"] == "0.8.6"
