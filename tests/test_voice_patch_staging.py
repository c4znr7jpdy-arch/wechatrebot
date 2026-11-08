"""Assemble the delivered voice patch stack with the real staging checks."""
from pathlib import Path
import shutil
import subprocess

import pytest

from test_staging_builder import _builder


ROOT = Path(__file__).parents[1]
PLUGIN = "astrbot_plugin_mimo_tts_clone"


def _assemble(tmp_path):
    output = tmp_path / "artifacts/staging"
    plugin = output / "app/astrbot/data/plugins" / PLUGIN
    shutil.copytree(ROOT / "vendor/data/plugins" / PLUGIN, plugin,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    patches = tmp_path / "patches"
    patches.mkdir()
    for source in (ROOT / "patches").glob(f"astrbot-plugin-{PLUGIN}*.patch"):
        shutil.copy2(source, patches / source.name)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    builder = _builder()
    builder._apply_plugin_patches(tmp_path, output)
    return builder, output, plugin


def test_voice_patch_stack_reapplication_keeps_reviewed_output_and_source(tmp_path):
    builder, output, plugin = _assemble(tmp_path)
    before = {path.relative_to(output): path.read_bytes()
              for path in output.rglob("*") if path.is_file()}

    builder._apply_plugin_patches(tmp_path, output)

    after = {path.relative_to(output): path.read_bytes()
             for path in output.rglob("*") if path.is_file()}
    assert after == before
    assert "_voice_allowed" in (plugin / "core/tool_delivery.py").read_text("utf-8")
    assert "check_send_response(await" in (plugin / "main.py").read_text("utf-8")
    assert {path.name for path in (output / "legal/corresponding-source").iterdir()} == {
        path.name for path in (tmp_path / "patches").iterdir()}


def test_voice_staging_still_rejects_modified_delivery_safety_code(tmp_path):
    builder, output, plugin = _assemble(tmp_path)
    delivery = plugin / "core/tool_delivery.py"
    text = delivery.read_text("utf-8")
    delivery.write_text(text.replace("_STATE_KEY =", "_CHANGED_STATE_KEY =", 1), encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        builder._apply_plugin_patches(tmp_path, output)
