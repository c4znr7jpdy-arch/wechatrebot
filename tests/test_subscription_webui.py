"""Run the delivered subscription UI's activation and expiry regressions."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_subscription_confirmation_expiry_and_recovery():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web UI behaviour checks")
    script = Path(__file__).with_name("subscription_webui.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
