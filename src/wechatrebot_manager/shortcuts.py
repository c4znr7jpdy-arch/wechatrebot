from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
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
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_ICON + ',0'
    $shortcut.Description = $env:WECHATREBOT_SHORTCUT_DESCRIPTION
    $shortcut.Save()
}
if (Test-Path -LiteralPath $env:WECHATREBOT_UNINSTALL_TARGET) {
    $uninstallPath = Join-Path $programs '卸载微信机器人.lnk'
    $shortcut = $shell.CreateShortcut($uninstallPath)
    $shortcut.TargetPath = $env:WECHATREBOT_UNINSTALL_TARGET
    $shortcut.WorkingDirectory = $env:WECHATREBOT_SHORTCUT_WORKDIR
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_ICON + ',0'
    $shortcut.Description = '彻底卸载微信机器人及本机数据'
    $shortcut.Save()
}
$folderIni = Join-Path $env:WECHATREBOT_SHORTCUT_WORKDIR 'desktop.ini'
if (-not (Test-Path -LiteralPath $folderIni)) {
    $folderConfig = "[.ShellClassInfo]`r`nIconFile=manager\WeChatReBot.exe`r`nIconIndex=0`r`nConfirmFileOp=0`r`n"
    [System.IO.File]::WriteAllText($folderIni, $folderConfig, [System.Text.Encoding]::Unicode)
    & attrib.exe +h +s $folderIni
    & attrib.exe +s $env:WECHATREBOT_SHORTCUT_WORKDIR
}
""".strip()


_REFRESH_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$targets = @(
    (Join-Path $desktop 'WeChat ReBot.lnk'),
    (Join-Path $programs 'WeChat ReBot.lnk'),
    (Join-Path $programs '卸载微信机器人.lnk')
)
foreach ($shortcutPath in $targets) {
    if (-not (Test-Path -LiteralPath $shortcutPath)) {
        continue
    }
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_ICON + ',0'
    $shortcut.Save()
}
$iconRefresh = Get-Command ie4uinit.exe -ErrorAction SilentlyContinue
if ($null -ne $iconRefresh) {
    & $iconRefresh.Source -show | Out-Null
}
""".strip()


_REMOVE_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$targets = @(
    (Join-Path $desktop 'WeChat ReBot.lnk'),
    (Join-Path $programs 'WeChat ReBot.lnk'),
    (Join-Path $programs '卸载微信机器人.lnk')
)
foreach ($shortcutPath in $targets) {
    if (-not (Test-Path -LiteralPath $shortcutPath)) {
        continue
    }
    Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction Stop
}
""".strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provision_shortcut_icon(root: Path, target: Path) -> Path:
    source = Path(__file__).with_name("app_webui") / "assets" / "app-icon.ico"
    if not source.is_file():
        return target
    digest = _sha256(source)
    destination = root / "manager" / f"app-icon-{digest[:12]}.ico"
    if destination.is_file() and _sha256(destination) == digest:
        return destination
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _shortcut_environment(
    root: Path,
    target: Path,
    *,
    description: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "WECHATREBOT_SHORTCUT_TARGET": str(target),
            "WECHATREBOT_SHORTCUT_ICON": str(
                _provision_shortcut_icon(root, target)
            ),
            "WECHATREBOT_SHORTCUT_WORKDIR": str(root),
            "WECHATREBOT_SHORTCUT_DESCRIPTION": description,
            "WECHATREBOT_UNINSTALL_TARGET": str(root / "卸载机器人.exe"),
        }
    )
    return environment


def _run_shortcut_script(script: str, environment: dict[str, str]) -> bool:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
        timeout=30,
    )
    return completed.returncode == 0


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
    return _run_shortcut_script(
        _SHORTCUT_SCRIPT,
        _shortcut_environment(root, target, description=description),
    )


def refresh_windows_shortcuts(
    install_root: str | Path,
    *,
    description: str,
) -> bool:
    """Refresh existing shortcut icons without recreating deleted shortcuts."""

    if os.name != "nt":
        return False
    root = Path(install_root).expanduser().resolve()
    target = root / "manager" / "WeChatReBot.exe"
    if not target.is_file():
        return False
    try:
        return _run_shortcut_script(
            _REFRESH_SHORTCUT_SCRIPT,
            _shortcut_environment(root, target, description=description),
        )
    except (OSError, subprocess.SubprocessError):
        # Icon refresh is best-effort and must never block the manager window.
        return False


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


__all__ = [
    "create_windows_shortcuts",
    "refresh_windows_shortcuts",
    "remove_windows_shortcuts",
]
