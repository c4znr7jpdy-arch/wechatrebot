from __future__ import annotations

from pathlib import Path

from wechatrebot_manager.setup_wizard import default_install_root


def test_default_install_root_uses_local_app_data(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_install_root() == (tmp_path / "XiaomeiRobot").resolve()
