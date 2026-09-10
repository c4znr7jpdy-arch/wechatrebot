from __future__ import annotations

import os
from pathlib import Path
import subprocess


_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$launchTargets = @(
    (Join-Path $desktop 'WeChat ReBot.lnk'),
    (Join-Path $programs 'WeChat ReBot.lnk')
)
foreach ($shortcutPath in $launchTargets) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $env:WECHATREBOT_SHORTCUT_TARGET
    $shortcut.WorkingDirectory = $env:WECHATREBOT_SHORTCUT_WORKDIR
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_TARGET + ',0'
    $shortcut.Description = $env:WECHATREBOT_SHORTCUT_DESCRIPTION
    $shortcut.Save()
}
if (Test-Path -LiteralPath $env:WECHATREBOT_UNINSTALL_TARGET) {
    $uninstallPath = Join-Path $programs '卸载微信机器人.lnk'
    $shortcut = $shell.CreateShortcut($uninstallPath)
    $shortcut.TargetPath = $env:WECHATREBOT_UNINSTALL_TARGET
    $shortcut.WorkingDirectory = $env:WECHATREBOT_SHORTCUT_WORKDIR
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_TARGET + ',0'
    $shortcut.Description = '彻底卸载微信机器人及本机数据'
    $shortcut.Save()
}
""".strip()


_REMOVE_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$targets = @(
    (Join-Path $desktop 'WeChat ReBot.lnk'),
    (Join-Path $programs 'WeChat ReBot.lnk'),
    (Join-Path $programs '卸载微信机器人.lnk')
)
foreach ($shortcutPath in $targets) {
    Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction SilentlyContinue
}
""".strip()


def create_windows_shortcuts(
    install_root: str | Path,
    *,
    description: str,
) -> bool:
    if os.name != "nt":
        return False
    root = Path(install_root).expanduser().resolve()
    target = root / "manager" / "WeChatReBot.exe"
    if not target.is_file():
        return False
    environment = os.environ.copy()
    environment.update(
        {
            "WECHATREBOT_SHORTCUT_TARGET": str(target),
            "WECHATREBOT_SHORTCUT_WORKDIR": str(root),
            "WECHATREBOT_SHORTCUT_DESCRIPTION": description,
            "WECHATREBOT_UNINSTALL_TARGET": str(root / "卸载机器人.exe"),
        }
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _SHORTCUT_SCRIPT,
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        check=False,
        timeout=30,
    )
    return completed.returncode == 0


def remove_windows_shortcuts() -> bool:
    if os.name != "nt":
        return False
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _REMOVE_SHORTCUT_SCRIPT,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
        timeout=30,
    )
    return completed.returncode == 0


__all__ = ["create_windows_shortcuts", "remove_windows_shortcuts"]
