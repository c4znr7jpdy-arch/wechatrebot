"""Run vendored plugin adapter tests separately from the manager's AstrBot stubs."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


TEST_ROOT = Path(__file__).resolve().parents[1] / "runtime/astrbot_plugin_twitter/tests"


@pytest.mark.parametrize("test_file", sorted(TEST_ROOT.glob("test_*.py")), ids=lambda path: path.stem)
def test_twitter_replacement_contracts(test_file):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short",
         str(test_file)],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("test_file", sorted(TEST_ROOT.glob("test_*.mjs")), ids=lambda path: path.stem)
def test_twitter_webui_contracts(test_file):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the Twitter Web UI")
    result = subprocess.run(
        [node, "--test", str(test_file)], cwd=TEST_ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
