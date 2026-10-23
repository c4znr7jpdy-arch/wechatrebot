from pathlib import Path
import json
import os
import subprocess

import pytest

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


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell shortcut contract")
def test_refresh_repairs_public_targets_and_preserves_other_installations(tmp_path):
    root = tmp_path / "robot space"
    (root / "manager").mkdir(parents=True)
    manager = root / "manager" / "WeChatReBot.exe"
    manager.write_bytes(b"manager")
    (root / "卸载机器人.exe").write_bytes(b"legacy")
    (root / "unins000.exe").write_bytes(b"native")
    (root / "unins000.dat").write_bytes(b"data")
    # This contract tests target ownership, not broken-link resolution. Keep all
    # Shell targets real and isolated so saving a link cannot search the host for
    # an absent installation (which can stall on unattended Windows runners).
    other_manager = tmp_path / "other robot" / "manager" / "WeChatReBot.exe"
    other_manager.parent.mkdir(parents=True)
    other_manager.write_bytes(b"other manager")
    assert shortcuts.ensure_launcher_executable(root)
    environment = shortcuts._shortcut_environment(root, root / "启动机器人.exe", description="robot")
    environment["TEST_OTHER_MANAGER"] = str(other_manager)
    script = shortcuts._REFRESH_SHORTCUT_SCRIPT
    for name in ("Desktop", "Programs", "CommonDesktopDirectory", "CommonPrograms"):
        folder = tmp_path / name
        folder.mkdir()
        environment[f"TEST_{name}"] = str(folder)
        script = script.replace(f"[Environment]::GetFolderPath('{name}')", f"$env:TEST_{name}")
    setup = r'''
$s = New-Object UnicodeShortcutFactory
$link = $s.CreateShortcut((Join-Path $env:TEST_CommonDesktopDirectory 'WeChat ReBot.lnk'))
$link.TargetPath = Join-Path $env:WECHATREBOT_SHORTCUT_WORKDIR 'manager\WeChatReBot.exe'
$link.Arguments = 'gui --install-root "old"'
$link.Save()
$link = $s.CreateShortcut((Join-Path $env:TEST_CommonPrograms '卸载微信机器人.lnk'))
$link.TargetPath = Join-Path $env:WECHATREBOT_SHORTCUT_WORKDIR '卸载机器人.exe'
$link.Save()
$link = $s.CreateShortcut((Join-Path $env:TEST_Programs 'WeChat ReBot.lnk'))
$link.TargetPath = $env:TEST_OTHER_MANAGER
$link.Arguments = 'keep'
$link.Save()
'''
    assert shortcuts._run_shortcut_script(setup, environment)
    # Unattended Windows hosts may reject the optional Explorer icon refresh.
    script = script.replace("& $iconRefresh.Source -show | Out-Null", "& $env:ComSpec /c exit 7")
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", shortcuts._UNICODE_SHELL_SCRIPT + "\n" + script],
                               env=environment, capture_output=True)
    assert completed.returncode == 0, repr(completed.stdout) + repr(completed.stderr)
    # Deleted desktop shortcuts must stay deleted; another installation stays untouched.
    assert not (tmp_path / "Desktop" / "WeChat ReBot.lnk").exists()
    inspect = r'''
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$s = New-Object UnicodeShortcutFactory
@((Join-Path $env:TEST_CommonDesktopDirectory 'WeChat ReBot.lnk'),
  (Join-Path $env:TEST_CommonPrograms '卸载微信机器人.lnk'),
  (Join-Path $env:TEST_Programs 'WeChat ReBot.lnk')) | ForEach-Object {
    $l = $s.CreateShortcut($_)
    @{target=$l.TargetPath; args=$l.Arguments}
} | ConvertTo-Json -Compress
'''
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", shortcuts._UNICODE_SHELL_SCRIPT + "\n" + inspect],
                            env=environment, capture_output=True, check=True)
    rows = json.loads(result.stdout.decode("utf-8"))
    assert Path(rows[0]["target"]) == root / "启动机器人.exe"
    assert rows[0]["args"] == ""
    assert Path(rows[1]["target"]) == root / "unins000.exe"
    assert Path(rows[2]["target"]) == other_manager
    assert rows[2]["args"] == "keep"
