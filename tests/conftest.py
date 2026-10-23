from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import hashlib

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SOURCE = ROOT / "vendor/data/plugins/jiang_image"


@pytest.fixture
def vendor_inputs():
    def create(repository):
        source = repository / "vendor"
        source.mkdir(parents=True)
        raw = b"value = 1\n"
        (source / "module.py").write_bytes(raw)
        (source / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": [
            {"path": "module.py", "sha256": hashlib.sha256(raw).hexdigest()},
        ]}), encoding="utf-8")
        return source
    return create


@pytest.fixture(scope="session")
def patched_plugins():
    """Apply the product's actual plugin assembly to pinned sources, without a runtime."""
    from build_staging import _apply_plugin_patches, _copy_source_entry

    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    # Keep deep plugin template paths below Windows' legacy 260-character limit.
    with tempfile.TemporaryDirectory(prefix="p-", dir=artifacts) as directory:
        output = Path(directory)
        entries = json.loads((ROOT / "release/source-manifest.json").read_text("utf-8"))["entries"]
        for entry in entries:
            if not entry["target"].startswith("app/astrbot/data/plugins/"):
                continue
            base = ROOT if entry["origin"] == "commercial" else ROOT / "vendor"
            _copy_source_entry(base / entry["source"], output / entry["target"], entry)
        _apply_plugin_patches(ROOT, output)
        yield output / "app/astrbot/data/plugins"


@pytest.fixture
def patched_image(tmp_path):
    for name in ("main.py", "model_manager.py", "_conf_schema.json"):
        shutil.copy2(SOURCE / name, tmp_path / name)
    shutil.copytree(SOURCE / "pages", tmp_path / "pages")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    for name in (
        "astrbot-plugin-jiang_image.patch",
        "astrbot-plugin-jiang_image--network-errors.patch",
        "astrbot-plugin-jiang_image--z-local-proxy.patch",
        "astrbot-plugin-jiang_image--zz-model-experience.patch",
        "astrbot-plugin-jiang_image--zzz-stop-propagation.patch",
        "astrbot-plugin-jiang_image--zzzz-chat-scope.patch",
        "astrbot-plugin-jiang_image--zzzzz-complete-delivery.patch",
    ):
        result = subprocess.run(
            ["git", "apply", "--check", "--ignore-space-change", str(ROOT / "patches" / name)],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        subprocess.run(["git", "apply", "--ignore-space-change", str(ROOT / "patches" / name)],
                       cwd=tmp_path, check=True, capture_output=True)
    schema = json.loads((tmp_path / "_conf_schema.json").read_text("utf-8"))
    assert schema["use_system_proxy"]["type"] == "bool"
    assert schema["use_system_proxy"]["default"] is True
    assert schema["local_proxy_port"]["default"] == ""
    assert schema["secondary_enabled"]["default"] is False
    assert "tertiary_enabled" not in schema
    spec = importlib.util.spec_from_file_location("patched_image_model_manager", tmp_path / "model_manager.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (tmp_path / "main.py").read_text("utf-8"), module
