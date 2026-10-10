"""Exercise delivered diagnostic UI state and error-capture behaviour."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_activity_ui_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web UI behaviour checks")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("activity_webui.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACTIVITY_WEBUI_COMPLETE" in result.stdout
