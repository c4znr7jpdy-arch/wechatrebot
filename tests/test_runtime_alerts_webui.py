"""Focused reminder interactions without a robot or model request."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_runtime_alert_preferences_ui():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web UI behaviour checks")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("runtime_alerts_webui.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RUNTIME_ALERTS_WEBUI_COMPLETE" in result.stdout
