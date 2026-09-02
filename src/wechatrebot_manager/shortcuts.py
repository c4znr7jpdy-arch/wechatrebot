from __future__ import annotations

import os
from pathlib import Path
import subprocess


_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$targets = @(
    (Join-Path $desktop 'WeChat ReBot.lnk'),
    (Join-Path $programs 'WeChat ReBot.lnk')
)
foreach ($shortcutPath in $targets) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $env:WECHATREBOT_SHORTCUT_TARGET
    $shortcut.WorkingDirectory = $env:WECHATREBOT_SHORTCUT_WORKDIR
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_TARGET + ',0'
    $shortcut.Description = $env:WECHATREBOT_SHORTCUT_DESCRIPTION
    $shortcut.Save()
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
