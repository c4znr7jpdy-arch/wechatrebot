from __future__ import annotations

from pathlib import Path

from .setup_wizard import run_setup_wizard


def run_bootstrap(payload_root: Path | None = None) -> int:
    """Run the single-window Windows setup wizard."""

    return run_setup_wizard(payload_root)
