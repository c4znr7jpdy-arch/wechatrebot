from pathlib import Path

import wechatrebot_manager.shortcuts as shortcuts


def test_shortcut_creation_skips_when_manager_is_missing(tmp_path: Path) -> None:
    assert not shortcuts.create_windows_shortcuts(tmp_path, description="机器人")


def test_shortcut_script_does_not_interpolate_customer_values() -> None:
    assert "$env:WECHATREBOT_SHORTCUT_TARGET" in shortcuts._SHORTCUT_SCRIPT
    assert "$env:WECHATREBOT_SHORTCUT_ICON" in shortcuts._SHORTCUT_SCRIPT
    assert "$env:WECHATREBOT_SHORTCUT_DESCRIPTION" in shortcuts._SHORTCUT_SCRIPT
    assert "$env:WECHATREBOT_UNINSTALL_TARGET" in shortcuts._SHORTCUT_SCRIPT
    assert "卸载微信机器人.lnk" in shortcuts._SHORTCUT_SCRIPT


def test_shortcut_script_uses_versioned_icon_for_install_folder_without_overwriting_existing_ini() -> None:
    script = shortcuts._SHORTCUT_SCRIPT
    assert "$shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_ICON + ',0'" in script
    assert "IconFile=manager\\WeChatReBot.exe" in script
    assert "IconIndex=0" in script
    assert "if (-not (Test-Path -LiteralPath $folderIni))" in script
    assert "& attrib.exe +h +s $folderIni" in script


def test_shortcut_icon_is_copied_to_a_content_versioned_path(tmp_path: Path) -> None:
    manager = tmp_path / "manager"
    manager.mkdir()
    target = manager / "WeChatReBot.exe"
    target.write_bytes(b"manager")

    icon = shortcuts._provision_shortcut_icon(tmp_path, target)

    assert icon.parent == manager
    assert icon.name.startswith("app-icon-")
    assert icon.suffix == ".ico"
    assert icon.read_bytes() == (
        Path(shortcuts.__file__).with_name("app_webui") / "assets" / "app-icon.ico"
    ).read_bytes()
    assert shortcuts._provision_shortcut_icon(tmp_path, target) == icon


def test_shortcut_refresh_only_updates_existing_entries() -> None:
    script = shortcuts._REFRESH_SHORTCUT_SCRIPT
    assert "if (-not (Test-Path -LiteralPath $shortcutPath))" in script
    assert "$shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_ICON + ',0'" in script
    assert "$shell.CreateShortcut($shortcutPath)" in script
    assert "ie4uinit.exe" in script


def test_shortcut_refresh_failure_does_not_block_startup(
    monkeypatch, tmp_path: Path
) -> None:
    manager = tmp_path / "manager"
    manager.mkdir()
    (manager / "WeChatReBot.exe").write_bytes(b"manager")
    monkeypatch.setattr(
        shortcuts,
        "_run_shortcut_script",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("shell busy")),
    )

    assert not shortcuts.refresh_windows_shortcuts(tmp_path, description="机器人")


def test_shortcut_cleanup_removes_launch_and_uninstall_entries() -> None:
    assert "WeChat ReBot.lnk" in shortcuts._REMOVE_SHORTCUT_SCRIPT
    assert "卸载微信机器人.lnk" in shortcuts._REMOVE_SHORTCUT_SCRIPT
    assert "if (-not (Test-Path -LiteralPath $shortcutPath))" in shortcuts._REMOVE_SHORTCUT_SCRIPT
    assert "Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction Stop" in shortcuts._REMOVE_SHORTCUT_SCRIPT
    assert "$ErrorActionPreference = 'SilentlyContinue'" not in shortcuts._REMOVE_SHORTCUT_SCRIPT
