"""Run message-center state regressions against the delivered JavaScript."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_message_center_date_cache_and_paging_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web UI behaviour checks")
    script = Path(__file__).with_name("history_webui.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "history date, cache, pagination, refresh and account isolation: passed" in result.stdout
