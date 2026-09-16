import json
from pathlib import Path

import pytest

from wechatrebot_manager import cli, installed_entries
from wechatrebot_manager.desktop import install_root_from_executable
from wechatrebot_manager.install_paths import INSTALL_MARKER


def test_root_launcher_is_created_refreshed_and_locates_install(tmp_path: Path) -> None:
    manager = tmp_path / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir()
    manager.write_bytes(b"version-one")
    (tmp_path / INSTALL_MARKER).write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    assert installed_entries.ensure_launcher_executable(tmp_path)
    launcher = tmp_path / installed_entries.START_EXECUTABLE_NAME
    assert launcher.read_bytes() == manager.read_bytes()
    assert install_root_from_executable(launcher) == tmp_path
    manager.write_bytes(b"version-two-new")
    assert installed_entries.ensure_launcher_executable(tmp_path)
    assert launcher.read_bytes() == b"version-two-new"


def test_double_click_launcher_hands_off_to_canonical_manager(monkeypatch, tmp_path: Path) -> None:
    manager = tmp_path / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir()
    manager.write_bytes(b"exe")
    (tmp_path / INSTALL_MARKER).write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / installed_entries.START_EXECUTABLE_NAME))
    calls = []
    monkeypatch.setattr(installed_entries.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    assert cli.main([]) == 0
    argv, options = calls[0]
    assert argv == [str(manager), "gui", "--install-root", str(tmp_path)]
    assert options["cwd"] == tmp_path
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_launcher_missing_installation_reports_visible_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / installed_entries.START_EXECUTABLE_NAME))
    errors = []
    monkeypatch.setattr(cli, "report_startup_error", errors.append)
    assert cli.main([]) == 1
    assert len(errors) == 1
    assert "安装目录" in str(errors[0])


def test_missing_manager_does_not_create_or_launch_an_entry(tmp_path: Path) -> None:
    assert not installed_entries.ensure_launcher_executable(tmp_path)
    with pytest.raises(FileNotFoundError, match="启动程序缺失"):
        installed_entries.launch_installed_manager(tmp_path)
