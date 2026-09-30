from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

from .installed_entries import START_EXECUTABLE_NAME, ensure_launcher_executable, native_uninstaller


# WScript.Shell can reject non-ANSI target names on English Windows. Use the
# Unicode Shell Link interface for creation, reading and updates on every host.
_UNICODE_SHELL_SCRIPT = r"""
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
class ShellLinkObject {}

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr findData, uint flags);
    void GetIDList(out IntPtr value);
    void SetIDList(IntPtr value);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int size);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
    void GetHotkey(out short value);
    void SetHotkey(short value);
    void GetShowCmd(out int value);
    void SetShowCmd(int value);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}

public class UnicodeShortcut {
    readonly IShellLinkW link;
    readonly string file;
    public UnicodeShortcut(string path) {
        file = path;
        link = (IShellLinkW)new ShellLinkObject();
        if (File.Exists(path)) ((IPersistFile)link).Load(path, 0);
    }
    public string TargetPath {
        get { var text = new StringBuilder(32768); link.GetPath(text, text.Capacity, IntPtr.Zero, 4); return text.ToString(); }
        set { link.SetPath(value); }
    }
    public string Arguments {
        get { var text = new StringBuilder(32768); link.GetArguments(text, text.Capacity); return text.ToString(); }
        set { link.SetArguments(value); }
    }
    public string WorkingDirectory { set { link.SetWorkingDirectory(value); } }
    public string Description { set { link.SetDescription(value); } }
    public string IconLocation {
        set {
            int comma = value.LastIndexOf(',');
            link.SetIconLocation(comma < 0 ? value : value.Substring(0, comma), comma < 0 ? 0 : int.Parse(value.Substring(comma + 1)));
        }
    }
    public void Save() { ((IPersistFile)link).Save(file, true); }
}

public class UnicodeShortcutFactory {
    public UnicodeShortcut CreateShortcut(string path) { return new UnicodeShortcut(path); }
}
'@
""".strip()


_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object UnicodeShortcutFactory
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$launchTargets = @(
    (Join-Path $desktop 'WeChat ReBot.lnk'),
    (Join-Path $programs 'WeChat ReBot.lnk')
)
foreach ($shortcutPath in $launchTargets) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $env:WECHATREBOT_SHORTCUT_TARGET
    $shortcut.Arguments = ''
    $shortcut.WorkingDirectory = $env:WECHATREBOT_SHORTCUT_WORKDIR
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_ICON + ',0'
    $shortcut.Description = $env:WECHATREBOT_SHORTCUT_DESCRIPTION
    $shortcut.Save()
}
if (Test-Path -LiteralPath $env:WECHATREBOT_UNINSTALL_TARGET) {
    $uninstallPath = Join-Path $programs '卸载微信机器人.lnk'
    $shortcut = $shell.CreateShortcut($uninstallPath)
    $shortcut.TargetPath = $env:WECHATREBOT_UNINSTALL_TARGET
    $shortcut.Arguments = ''
    $shortcut.WorkingDirectory = $env:WECHATREBOT_SHORTCUT_WORKDIR
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_ICON + ',0'
$shortcut.Description = '彻底卸载微信机器人（保留聊天记录）'
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
$shell = New-Object UnicodeShortcutFactory
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$commonDesktop = [Environment]::GetFolderPath('CommonDesktopDirectory')
$commonPrograms = [Environment]::GetFolderPath('CommonPrograms')
$targets = @(
    (Join-Path $desktop 'WeChat ReBot.lnk'),
    (Join-Path $programs 'WeChat ReBot.lnk'),
    (Join-Path $programs '卸载微信机器人.lnk'),
    (Join-Path $commonDesktop 'WeChat ReBot.lnk'),
    (Join-Path $commonPrograms 'WeChat ReBot.lnk'),
    (Join-Path $commonPrograms '卸载微信机器人.lnk')
)
$launchPaths = @($env:WECHATREBOT_SHORTCUT_TARGET, (Join-Path $env:WECHATREBOT_SHORTCUT_WORKDIR 'manager\WeChatReBot.exe'))
$uninstallPaths = @($env:WECHATREBOT_UNINSTALL_TARGET, (Join-Path $env:WECHATREBOT_SHORTCUT_WORKDIR '卸载机器人.exe'))
foreach ($shortcutPath in $targets) {
    if (-not (Test-Path -LiteralPath $shortcutPath)) {
        continue
    }
    $shortcut = $shell.CreateShortcut($shortcutPath)
    if ($shortcut.TargetPath -in $launchPaths) {
        $shortcut.TargetPath = $env:WECHATREBOT_SHORTCUT_TARGET
    } elseif ($shortcut.TargetPath -in $uninstallPaths) {
        if (-not (Test-Path -LiteralPath $env:WECHATREBOT_UNINSTALL_TARGET)) { continue }
        $shortcut.TargetPath = $env:WECHATREBOT_UNINSTALL_TARGET
    } else {
        continue
    }
    $shortcut.Arguments = ''
    $shortcut.WorkingDirectory = $env:WECHATREBOT_SHORTCUT_WORKDIR
    $shortcut.IconLocation = $env:WECHATREBOT_SHORTCUT_ICON + ',0'
    $shortcut.Save()
}
$iconRefresh = Get-Command ie4uinit.exe -ErrorAction SilentlyContinue
if ($null -ne $iconRefresh) {
    & $iconRefresh.Source -show | Out-Null
}
# No interactive Explorer may be present (for example during unattended updates).
# Shortcut saves above are authoritative; icon-cache refresh is best effort.
exit 0
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
            "WECHATREBOT_UNINSTALL_TARGET": str(native_uninstaller(root) or root / "卸载机器人.exe"),
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
            _UNICODE_SHELL_SCRIPT + "\n" + script,
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
    target = root / START_EXECUTABLE_NAME
    if not ensure_launcher_executable(root):
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
    """Repair this installation's existing targets without recreating deleted shortcuts."""

    if os.name != "nt":
        return False
    root = Path(install_root).expanduser().resolve()
    target = root / START_EXECUTABLE_NAME
    try:
        if not ensure_launcher_executable(root):
            return False
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
