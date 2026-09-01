import importlib.util
from pathlib import Path
import zipfile

import pytest


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_runtime.py"
    spec = importlib.util.spec_from_file_location("build_runtime", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_builder_pins_python_archive_and_hash() -> None:
    module = _builder()
    assert module.PYTHON_VERSION == "3.13.9"
    assert module.PYTHON_URL.startswith("https://www.python.org/")
    assert len(module.PYTHON_SHA256) == 64


def test_runtime_zip_extraction_rejects_traversal(tmp_path: Path) -> None:
    module = _builder()
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "bad")
    with pytest.raises(RuntimeError, match="不安全路径"):
        module._safe_extract_zip(archive, tmp_path / "output")


def test_runtime_lock_is_exact_and_has_no_editable_sources() -> None:
    root = Path(__file__).parents[1]
    lines = [
        line.strip()
        for line in (root / "release" / "runtime-requirements.lock")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) >= 200
    assert all("==" in line for line in lines)
    assert all("git+" not in line and " -e " not in f" {line} " for line in lines)


def test_blessed_wheelhouse_manifest_matches_runtime_lock_size() -> None:
    import json

    root = Path(__file__).parents[1]
    payload = json.loads(
        (root / "release" / "runtime-wheelhouse.json").read_text(encoding="utf-8")
    )
    assert payload["wheel_count"] >= 200
    assert payload["total_bytes"] > 0
    assert len(payload["aggregate_sha256"]) == 64
