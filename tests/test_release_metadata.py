import importlib.util
import json
from pathlib import Path
import shutil

import pytest


def _validator():
    script = Path(__file__).parents[1] / "scripts" / "validate_release_metadata.py"
    spec = importlib.util.spec_from_file_location("validate_release_metadata", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metadata_copy(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1]
    target = tmp_path / "repo"
    shutil.copytree(source / "release", target / "release")
    shutil.copy2(source / "pyproject.toml", target / "pyproject.toml")
    package = target / "src" / "wechatrebot_manager"
    package.mkdir(parents=True)
    shutil.copy2(source / "src" / "wechatrebot_manager" / "__init__.py", package)
    shutil.copy2(source / "src" / "wechatrebot_manager" / "task_store.py", package)
    shutil.copytree(source / "runtime", target / "runtime")
    shutil.copytree(source / "legal", target / "legal")
    shutil.copytree(source / "config", target / "config")
    shutil.copytree(source / "assets", target / "assets")
    shutil.copytree(source / "patches", target / "patches")
    (target / "scripts").mkdir()
    shutil.copy2(source / "scripts" / "build_runtime.py", target / "scripts")
    return target


def test_current_release_metadata_is_internally_consistent() -> None:
    root = Path(__file__).parents[1]
    assert _validator().validate_release_metadata(root) == []


def test_legacy_ready_status_does_not_enable_approval_gates(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    source_path = root / "release" / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["status"] = "ready"
    source_path.write_text(json.dumps(source), encoding="utf-8")

    assert _validator().validate_release_metadata(root) == []


def test_hook_hash_must_match_source_manifest(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    source_path = root / "release" / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    hook = json.loads((root / "release" / "hook-binaries.json").read_text(encoding="utf-8"))
    hook_paths = {item["path"] for item in hook["files"]}
    hook_entry = next(
        item
        for item in source["entries"]
        if item.get("source") in hook_paths
    )
    hook_entry["sha256"] = "0" * 64
    source_path.write_text(json.dumps(source), encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert "source manifest hook hashes do not match hook-binaries.json" in errors


def test_legal_artifact_hash_change_is_rejected(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    notices = root / "legal" / "THIRD-PARTY-NOTICES.md"
    notices.write_text(notices.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert any("legal review artifact hash mismatch" in item for item in errors)


def test_approved_evidence_requires_accountable_artifacts(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    evidence_path = root / "release" / "acceptance-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["requirements"][0]["status"] = "approved"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert any("no reviewer or time" in item for item in errors)
    assert any("has no artifacts" in item for item in errors)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("archive_sha256", "Z" * 64, "archive SHA-256 is invalid"),
        ("archive_root", "../ffmpeg", "archive root does not match"),
        ("archive_url", "https://example.com/latest.zip", "archive URL does not match"),
        ("license", "GPL-3.0-only", "win64 LGPL shared distribution"),
        ("source_commit", "0123456789", "source commit does not match its version"),
        ("build_source_url", "https://github.com/BtbN/FFmpeg-Builds", "build source URL does not match"),
    ],
)
def test_ffmpeg_pin_rejects_inconsistent_metadata(field: str, value: str, message: str) -> None:
    root = Path(__file__).parents[1]
    definition = json.loads((root / "release" / "ffmpeg-runtime.json").read_text(encoding="utf-8"))
    definition[field] = value
    errors: list[str] = []

    _validator()._validate_ffmpeg_definition(definition, errors)

    assert any(message in error for error in errors)


def test_ffmpeg_pin_must_be_shipped_with_payload(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    source_path = root / "release" / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["entries"] = [
        item for item in source["entries"] if item["target"] != "legal/ffmpeg-runtime.json"
    ]
    source_path.write_text(json.dumps(source), encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert "source manifest must ship the pinned FFmpeg runtime definition" in errors
