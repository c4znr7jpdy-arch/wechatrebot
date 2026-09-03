import importlib.util
import json
from pathlib import Path
import shutil


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
    shutil.copytree(source / "legal", target / "legal")
    shutil.copytree(source / "config", target / "config")
    return target


def test_current_release_metadata_is_internally_consistent() -> None:
    root = Path(__file__).parents[1]
    assert _validator().validate_release_metadata(root) == []


def test_ready_status_requires_closed_hook_and_plugin_reviews(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    source_path = root / "release" / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["status"] = "ready"
    source_path.write_text(json.dumps(source), encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert any("redistribution approval" in item for item in errors)
    assert any("plugin manifest" in item for item in errors)


def test_hook_hash_must_match_source_manifest_gate(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    source_path = root / "release" / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    hook_entry = next(
        item
        for item in source["entries"]
        if item.get("gate") == "hook-binary-redistribution"
    )
    hook_entry["sha256"] = "0" * 64
    source_path.write_text(json.dumps(source), encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert "source manifest hook gates do not match hook-binaries.json" in errors
