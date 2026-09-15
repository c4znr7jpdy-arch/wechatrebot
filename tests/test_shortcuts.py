from pathlib import Path

import wechatrebot_manager.shortcuts as shortcuts


def test_shortcut_creation_skips_when_manager_is_missing(tmp_path: Path) -> None:
    assert not shortcuts.create_windows_shortcuts(tmp_path, description="机器人")


def test_shortcut_script_does_not_interpolate_customer_values() -> None:
    assert "$env:WECHATREBOT_SHORTCUT_TARGET" in shortcuts._SHORTCUT_SCRIPT
    assert "$env:WECHATREBOT_SHORTCUT_DESCRIPTION" in shortcuts._SHORTCUT_SCRIPT
    assert "$env:WECHATREBOT_UNINSTALL_TARGET" in shortcuts._SHORTCUT_SCRIPT
    assert "卸载微信机器人.lnk" in shortcuts._SHORTCUT_SCRIPT


def test_shortcut_script_uses_embedded_exe_icon_for_install_folder_without_overwriting_existing_ini() -> None:
    script = shortcuts._SHORTCUT_SCRIPT
    assert "$shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_TARGET + ',0'" in script
    assert "IconFile=manager\\WeChatReBot.exe" in script
    assert "IconIndex=0" in script
    assert "if (-not (Test-Path -LiteralPath $folderIni))" in script
    assert "& attrib.exe +h +s $folderIni" in script


def test_shortcut_cleanup_removes_launch_and_uninstall_entries() -> None:
    assert "WeChat ReBot.lnk" in shortcuts._REMOVE_SHORTCUT_SCRIPT
    assert "卸载微信机器人.lnk" in shortcuts._REMOVE_SHORTCUT_SCRIPT
    assert "if (-not (Test-Path -LiteralPath $shortcutPath))" in shortcuts._REMOVE_SHORTCUT_SCRIPT
    assert "Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction Stop" in shortcuts._REMOVE_SHORTCUT_SCRIPT
    assert "$ErrorActionPreference = 'SilentlyContinue'" not in shortcuts._REMOVE_SHORTCUT_SCRIPT
