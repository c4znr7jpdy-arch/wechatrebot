from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SOURCE = Path("E:/Project/data/plugins/astrbot_plugin_self_learning")
PATCH = ROOT / "patches" / "astrbot-plugin-astrbot_plugin_self_learning.patch"


def test_conversation_core_hot_update_includes_explicit_memory_phrases(
    tmp_path: Path,
) -> None:
    if not SOURCE.is_dir():
        pytest.skip("emotion plugin source is unavailable")
    target = tmp_path / "astrbot_plugin_self_learning"
    shutil.copytree(SOURCE, target)
    # The host user profile can itself be a Git worktree. Make the copied plugin
    # its own repository so git apply never resolves paths against that parent.
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "apply", "--check", "-p1", str(PATCH)],
        cwd=target,
        check=True,
    )
    subprocess.run(
        ["git", "apply", "-p1", str(PATCH)],
        cwd=target,
        check=True,
    )

    source = (target / "core" / "page_api.py").read_text(encoding="utf-8")
    assert '"memory_explicit_phrases"' in source
    assert '"explicit_recall_phrases": phrases' in source
    assert "if len(phrases) >= 20" in source
    assert 'errors.append(f"长期记忆: {exc}")' in source
    assert 'errors.append(f"情感状态: {exc}")' in source
