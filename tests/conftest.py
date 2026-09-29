from pathlib import Path
import importlib.util
import json
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/fixtures/patch_sources/data/plugins/jiang_image"


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
