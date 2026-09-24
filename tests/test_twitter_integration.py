"""Run vendored plugin adapter tests separately from the manager's AstrBot stubs."""

from pathlib import Path
import subprocess
import sys


def test_twitter_replacement_contracts():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short",
         "runtime/astrbot_plugin_twitter/tests/test_wechat_integration.py",
         "runtime/astrbot_plugin_twitter/tests/test_twitter_webui.py"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
