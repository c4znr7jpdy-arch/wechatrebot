from pathlib import Path

import wechatrebot_manager.shortcuts as shortcuts


def test_shortcut_creation_skips_when_manager_is_missing(tmp_path: Path) -> None:
    assert not shortcuts.create_windows_shortcuts(tmp_path, description="机器人")


def test_shortcut_script_does_not_interpolate_customer_values() -> None:
    assert "$env:WECHATREBOT_SHORTCUT_TARGET" in shortcuts._SHORTCUT_SCRIPT
    assert "$env:WECHATREBOT_SHORTCUT_DESCRIPTION" in shortcuts._SHORTCUT_SCRIPT
