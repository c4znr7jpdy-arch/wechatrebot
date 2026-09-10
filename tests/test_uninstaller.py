from __future__ import annotations

import json
from pathlib import Path

import pytest

from wechatrebot_manager.install_paths import INSTALL_MARKER
from wechatrebot_manager import uninstaller


def _installation(root: Path) -> Path:
    manager = root / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(b"manager-v1")
    (root / INSTALL_MARKER).write_text(
        json.dumps({"schema_version": 1, "state": "ready"}),
        encoding="utf-8",
    )
    (root / "data").mkdir()
    (root / "data" / "messages.db").write_bytes(b"customer data")
    return root


def test_uninstall_root_requires_product_marker(tmp_path: Path) -> None:
    root = tmp_path / "ordinary-folder"
    root.mkdir()

    with pytest.raises(uninstaller.UninstallError, match="安装标记"):
        uninstaller.validate_uninstall_root(root)


def test_uninstall_executable_is_created_and_refreshed(tmp_path: Path) -> None:
    root = _installation(tmp_path / "install")

    assert uninstaller.ensure_uninstall_executable(root)
    target = root / uninstaller.UNINSTALL_EXECUTABLE_NAME
    assert target.read_bytes() == b"manager-v1"

    source = root / "manager" / "WeChatReBot.exe"
    source.write_bytes(b"manager-v2-updated")
    assert uninstaller.ensure_uninstall_executable(root)
    assert target.read_bytes() == b"manager-v2-updated"


def test_remove_installation_cleans_product_and_local_residuals(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path / "install")
    local = tmp_path / "local-app-data"
    residual = local / "WeChatReBot"
    residual.mkdir(parents=True)
    (residual / "install-location.json").write_text("{}", encoding="utf-8")
    calls: list[str] = []

    uninstaller.remove_installation(
        root,
        local_app_data=local,
        process_stopper=lambda value: calls.append(f"stop:{Path(value).name}") or 0,
        shortcut_remover=lambda: calls.append("shortcuts") or True,
        registry_remover=lambda: calls.append("registry") or True,
    )

    assert not root.exists()
    assert not residual.exists()
    assert calls == ["stop:install", "shortcuts", "registry"]


def test_apply_uninstall_plan_removes_everything_without_ui(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path / "install")
    helper_source = tmp_path / "manager.exe"
    helper_source.write_bytes(b"exe")
    plan = uninstaller.create_uninstall_plan(
        root,
        executable=helper_source,
        parent_pid=0,
        temp_root=tmp_path / "temporary",
    )

    result = uninstaller.apply_uninstall_plan(
        plan.plan_path,
        show_result=False,
        schedule_cleanup=False,
        process_stopper=lambda _root: 0,
        shortcut_remover=lambda: True,
        registry_remover=lambda: True,
        local_app_data=tmp_path / "local-app-data",
    )

    assert result == 0
    assert not root.exists()
    assert plan.helper_executable.is_file()


def test_chinese_uninstaller_filename_is_double_click_entry() -> None:
    assert uninstaller.is_uninstall_entrypoint(r"D:\Robot\卸载机器人.exe")
    assert not uninstaller.is_uninstall_entrypoint(r"D:\Robot\WeChatReBot.exe")
