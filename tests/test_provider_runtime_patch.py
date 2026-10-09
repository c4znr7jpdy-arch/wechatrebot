from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "vendor/engine"
PATCH = ROOT / "patches" / "astrbot-4.26.6-wechat.patch"


def test_provider_runtime_uses_one_bounded_attempt(tmp_path: Path) -> None:
    relatives = (
        Path("astrbot/core/provider/sources/openai_source.py"),
        Path("astrbot/core/provider/sources/request_retry.py"),
    )
    if not all((SOURCE / relative).is_file() for relative in relatives):
        pytest.fail("Pinned AstrBot source unavailable")
    for relative in relatives:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / relative, target)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "apply",
            "-p2",
            *[f"--include={relative.as_posix()}" for relative in relatives],
            str(PATCH),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    openai = (tmp_path / relatives[0]).read_text(encoding="utf-8-sig")
    retry = (tmp_path / relatives[1]).read_text(encoding="utf-8-sig")
    assert openai.count("max_retries=0") == 2
    assert "REQUEST_RETRY_ATTEMPTS = 1" in retry
