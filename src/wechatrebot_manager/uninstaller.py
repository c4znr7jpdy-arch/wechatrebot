from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Callable

from . import __version__
from .install_paths import INSTALL_MARKER
from .installed_entries import copy_manager_executable, native_uninstaller
from .json_io import write_json_atomic
from .location import chat_history_path, locator_path
from .shortcuts import remove_windows_shortcuts
from .updates import _update_wait_pids, _wait_for_process_exit


UNINSTALL_EXECUTABLE_NAME = "卸载机器人.exe"
UNINSTALL_REGISTRY_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\WeChatReBot"
)
_UNINSTALL_WORK_PREFIX = "wechatrebot-uninstall-"
_VALID_INSTALL_STATES = {
    "installing",
    "payload-installed",
    "configured",
    "ready",
    "failed",
}


class UninstallError(RuntimeError):
    """Raised when a complete and safe uninstall cannot be performed."""


@dataclass(frozen=True, slots=True)
class UninstallPlan:
    plan_path: Path
    helper_executable: Path


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(
        os.path.realpath(str(right))
    )


def _path_within(path: str | Path, parent: str | Path) -> bool:
    try:
        Path(os.path.realpath(str(path))).relative_to(
            Path(os.path.realpath(str(parent)))
        )
        return True
    except (OSError, ValueError):
        return False


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(
        hasattr(path, "is_junction") and path.is_junction()
    )


def validate_uninstall_root(install_root: str | Path) -> Path:
    root = Path(install_root).expanduser().resolve()
    protected = {
        Path(root.anchor).resolve() if root.anchor else root,
        Path.home().resolve(),
        Path(tempfile.gettempdir()).resolve(),
    }
    for name in (
        "WINDIR",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramData",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
    ):
        value = os.environ.get(name)
        if value:
            protected.add(Path(value).expanduser().resolve())
    if root in protected or root.parent == root or _is_link_or_junction(root):
        raise UninstallError("卸载目录不安全，已停止操作。")
    marker_path = root / INSTALL_MARKER
    if _is_link_or_junction(marker_path):
        raise UninstallError("安装标记无效，已停止操作。")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise UninstallError("未找到有效的机器人安装标记，已停止操作。") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("schema_version") != 1
        or marker.get("state") not in _VALID_INSTALL_STATES
    ):
        raise UninstallError("安装标记格式无效，已停止操作。")
    return root


def is_uninstall_entrypoint(executable: str | Path | None = None) -> bool:
    path = Path(executable or sys.executable)
    return path.name.casefold() == UNINSTALL_EXECUTABLE_NAME.casefold()


def ensure_uninstall_executable(
    install_root: str | Path,
    *,
    source_executable: str | Path | None = None,
) -> bool:
    root = validate_uninstall_root(install_root)
    if native_uninstaller(root) is not None:
        (root / UNINSTALL_EXECUTABLE_NAME).unlink(missing_ok=True)
        return True
    source = Path(
        source_executable or root / "manager" / "WeChatReBot.exe"
    ).expanduser().resolve()
    return copy_manager_executable(root, UNINSTALL_EXECUTABLE_NAME, source)


def register_windows_uninstaller(
    install_root: str | Path,
    *,
    display_name: str,
) -> bool:
    if os.name != "nt":
        return False
    root = validate_uninstall_root(install_root)
    if native_uninstaller(root) is not None:
        # Do not create a second Windows Apps entry for an Inno installation.
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY) as key:
                location, _ = winreg.QueryValueEx(key, "InstallLocation")
            if _same_path(location, root):
                remove_windows_uninstaller_registration()
        except FileNotFoundError:
            pass
        return True
    executable = root / UNINSTALL_EXECUTABLE_NAME
    if not executable.is_file():
        return False
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        UNINSTALL_REGISTRY_KEY,
        0,
        winreg.KEY_WRITE,
    ) as key:
        command = f'"{executable}" uninstall --install-root "{root}"'
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name.strip())
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, __version__)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "WeChat ReBot")
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(root))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(executable))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, command)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
    return True


def remove_windows_uninstaller_registration() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY)
    except FileNotFoundError:
        return False
    return True


def provision_uninstaller(
    install_root: str | Path,
    *,
    display_name: str,
    source_executable: str | Path | None = None,
) -> bool:
    if not ensure_uninstall_executable(
        install_root,
        source_executable=source_executable,
    ):
        return False
    try:
        register_windows_uninstaller(install_root, display_name=display_name)
    except OSError:
        # The double-click executable remains usable when a customer policy
        # prevents adding the optional Windows Apps entry.
        pass
    return True


def launch_installed_uninstaller(install_root: str | Path) -> None:
    root = validate_uninstall_root(install_root)
    native = native_uninstaller(root)
    executable = native or root / UNINSTALL_EXECUTABLE_NAME
    if not executable.is_file():
        raise UninstallError("卸载程序不存在，请重新打开管理软件后重试。")
    subprocess.Popen(
        [str(executable)] if native else [str(executable), "uninstall", "--install-root", str(root)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def create_uninstall_plan(
    install_root: str | Path,
    *,
    executable: str | Path | None = None,
    parent_pid: int | None = None,
    temp_root: str | Path | None = None,
) -> UninstallPlan:
    root = validate_uninstall_root(install_root)
    if executable is None and not getattr(sys, "frozen", False):
        raise UninstallError("开发运行方式不执行卸载，请使用正式安装版。")
    source = Path(executable or sys.executable).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".exe":
        raise UninstallError("无法创建独立卸载进程。")
    base = Path(temp_root or tempfile.gettempdir()).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    working = base / f"{_UNINSTALL_WORK_PREFIX}{uuid.uuid4().hex}"
    working.mkdir()
    helper = working / "WeChatReBot-Uninstall-Helper.exe"
    try:
        shutil.copy2(source, helper)
        application_pid = int(parent_pid if parent_pid is not None else os.getpid())
        wait_pids = _update_wait_pids(
            source,
            application_pid,
            include_bootloader=parent_pid is None,
        )
        plan_path = working / "uninstall-plan.json"
        write_json_atomic(
            plan_path,
            {
                "schema_version": 1,
                "install_root": str(root),
                "parent_pid": application_pid,
                "wait_pids": list(wait_pids),
            },
        )
        return UninstallPlan(plan_path, helper)
    except Exception:
        shutil.rmtree(working, ignore_errors=True)
        raise


def launch_uninstall_helper(plan: UninstallPlan) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(
        [
            str(plan.helper_executable),
            "apply-uninstall",
            "--plan",
            str(plan.plan_path),
        ],
        cwd=plan.plan_path.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def _plan_wait_pids(payload: dict[str, object]) -> tuple[int, ...]:
    raw = payload.get("wait_pids", [payload.get("parent_pid", 0)])
    if not isinstance(raw, list) or not raw or len(raw) > 4:
        raise UninstallError("卸载计划包含无效的进程等待列表。")
    try:
        result = tuple(dict.fromkeys(int(value) for value in raw))
    except (TypeError, ValueError) as exc:
        raise UninstallError("卸载计划包含无效的进程等待列表。") from exc
    if any(pid < 0 for pid in result):
        raise UninstallError("卸载计划包含无效的进程等待列表。")
    return result


def _validated_plan(plan_path: str | Path) -> tuple[dict[str, object], Path]:
    path = Path(plan_path).expanduser().resolve()
    if not path.parent.name.startswith(_UNINSTALL_WORK_PREFIX):
        raise UninstallError("卸载计划位置无效。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise UninstallError("卸载计划无法读取。") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise UninstallError("卸载计划格式无效。")
    try:
        root = validate_uninstall_root(str(payload["install_root"]))
        parent_pid = int(payload["parent_pid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UninstallError("卸载计划缺少必要字段。") from exc
    if parent_pid < 0:
        raise UninstallError("卸载计划包含无效进程。")
    _plan_wait_pids(payload)
    return payload, root


def _iter_windows_process_ids(
    *, executable_names: frozenset[str] | None = None, strict: bool = False,
) -> tuple[int, ...]:
    """Enumerate processes, optionally requiring a complete filtered snapshot."""
    if os.name != "nt":
        if strict:
            raise OSError("当前平台无法确认 Windows 进程状态。")
        return ()
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    )
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == invalid_handle:
        if strict:
            raise OSError(ctypes.get_last_error(), "无法读取进程列表。")
        return ()
    names = None if executable_names is None else {name.casefold() for name in executable_names}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    values: list[int] = []
    try:
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if names is None or entry.szExeFile.casefold() in names:
                    values.append(int(entry.th32ProcessID))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    if strict and ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                        raise OSError(ctypes.get_last_error(), "进程列表未能完整读取。")
                    break
        elif strict and ctypes.get_last_error() != 18:
            raise OSError(ctypes.get_last_error(), "无法读取进程列表。")
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(values)


def _process_module_paths(pid: int) -> tuple[Path, ...]:
    if os.name != "nt" or pid <= 0:
        return ()
    import ctypes
    from ctypes import wintypes

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("th32ModuleID", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("GlblcntUsage", wintypes.DWORD),
            ("ProccntUsage", wintypes.DWORD),
            ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
            ("modBaseSize", wintypes.DWORD),
            ("hModule", wintypes.HMODULE),
            ("szModule", wintypes.WCHAR * 256),
            ("szExePath", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Module32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(MODULEENTRY32W),
    )
    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(MODULEENTRY32W),
    )
    kernel32.Module32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, pid)
    if snapshot == invalid_handle:
        return ()
    entry = MODULEENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    values: list[Path] = []
    try:
        if kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.szExePath:
                    values.append(Path(entry.szExePath))
                if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(values)


def _terminate_windows_process(pid: int) -> bool:
    if os.name != "nt" or pid <= 0 or pid == os.getpid():
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
    if not handle:
        return False
    try:
        if not kernel32.TerminateProcess(handle, 0):
            return False
        kernel32.WaitForSingleObject(handle, 10_000)
        return True
    finally:
        kernel32.CloseHandle(handle)


def stop_installation_processes(install_root: str | Path) -> int:
    root = validate_uninstall_root(install_root)
    if os.name != "nt":
        return 0
    from .launcher import process_identity

    stopped = 0
    for _attempt in range(2):
        candidates: list[int] = []
        for pid in _iter_windows_process_ids():
            if pid in {0, os.getpid(), os.getppid()}:
                continue
            try:
                identity = process_identity(pid)
            except OSError:
                continue
            if identity and Path(identity.executable).name.casefold().startswith("unins"):
                continue
            executable_in_root = bool(
                identity and _path_within(identity.executable, root)
            )
            try:
                module_in_root = not executable_in_root and any(
                    _path_within(module_path, root)
                    for module_path in _process_module_paths(pid)
                )
            except OSError:
                module_in_root = False
            if executable_in_root or module_in_root:
                candidates.append(pid)
        if not candidates:
            break
        for pid in candidates:
            if _terminate_windows_process(pid):
                stopped += 1
        time.sleep(0.2)
    return stopped


def _remove_tree(path: Path, *, attempts: int = 10) -> None:
    if not path.exists():
        return
    if _is_link_or_junction(path):
        raise UninstallError(f"拒绝删除链接目录：{path}")
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            for child in path.rglob("*"):
                try:
                    child.chmod(0o700)
                except OSError:
                    pass
            time.sleep(min(0.2 * (attempt + 1), 1.0))
    raise UninstallError(f"无法删除目录：{path}（{last_error}）")


def remove_local_residuals(
    local_app_data: str | Path | None = None,
) -> None:
    """Remove locator/log residue while deliberately retaining chat history."""

    residual_root = locator_path(local_app_data).parent.resolve()
    if residual_root.parent == residual_root or residual_root.name != "WeChatReBot":
        raise UninstallError("本机残留目录无效，已停止操作。")
    locator = residual_root / "install-location.json"
    if locator.is_symlink() or locator.is_file():
        locator.unlink(missing_ok=True)
    elif locator.exists():
        raise UninstallError("本机定位文件不是普通文件，已停止操作。")
    logs = residual_root / "logs"
    if logs.exists():
        _remove_tree(logs)
    # The data directory contains chat-history.db and is intentionally never
    # traversed or removed.  Remove only the now-empty product container.
    try:
        residual_root.rmdir()
    except OSError:
        pass


def remove_installation(
    install_root: str | Path,
    *,
    local_app_data: str | Path | None = None,
    process_stopper: Callable[[str | Path], int] = stop_installation_processes,
    shortcut_remover: Callable[[], bool] = remove_windows_shortcuts,
    registry_remover: Callable[[], bool] = remove_windows_uninstaller_registration,
    progress_callback: Callable[[str], None] | None = None,
) -> None:
    root = validate_uninstall_root(install_root)
    residual_root = locator_path(local_app_data).parent.resolve()
    if residual_root.parent == residual_root or residual_root.name != "WeChatReBot":
        raise UninstallError("本机残留目录无效，已停止操作。")
    progress = progress_callback or (lambda _message: None)
    progress("正在关闭机器人和与机器人连接的微信…")
    process_stopper(root)
    progress("正在清理快捷方式和卸载记录…")
    if os.name == "nt" and not shortcut_remover():
        raise UninstallError("无法清理桌面或开始菜单快捷方式。")
    registry_remover()
    progress("正在删除程序和本机数据，文件较多时需要稍候…")
    _remove_tree(root)
    progress("正在清理本机残留：定位和启动日志（聊天记录会保留）…")
    if not _same_path(residual_root, root):
        remove_local_residuals(local_app_data)


_SELF_CLEANUP_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$processId = [int]$env:WECHATREBOT_UNINSTALL_PID
$workingDirectory = $env:WECHATREBOT_UNINSTALL_WORKDIR
Wait-Process -Id $processId -Timeout 60 -ErrorAction SilentlyContinue
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Remove-Item -LiteralPath $workingDirectory -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $workingDirectory)) { break }
    Start-Sleep -Milliseconds 500
}
""".strip()


def _schedule_self_cleanup(working: Path) -> None:
    if os.name != "nt":
        return
    environment = os.environ.copy()
    environment["WECHATREBOT_UNINSTALL_PID"] = str(os.getpid())
    environment["WECHATREBOT_UNINSTALL_WORKDIR"] = str(working)
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _SELF_CLEANUP_SCRIPT,
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        pass


def _message_box(message: str, title: str, flags: int) -> int:
    if os.name == "nt":
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(
            None, message, title, flags | 0x00010000 | 0x00040000
        ))
    print(f"{title}: {message}")
    return 0


def run_uninstaller(install_root: str | Path) -> int:
    try:
        root = validate_uninstall_root(install_root)
    except UninstallError as exc:
        _message_box(str(exc), "微信机器人 · 无法卸载", 0x10)
        return 1
    answer = _message_box(
        "即将彻底卸载微信机器人。\n\n"
        "将关闭机器人相关进程及与机器人连接的微信窗口，并删除程序、运行时、插件、配置、"
        "日志、缓存、备份、更新文件、快捷方式及本机定位残留。\n"
        f"聊天记录不会被删除，会保留在本机聊天记录目录：{chat_history_path()}，重新安装后可继续使用。\n\n"
        f"安装目录：{root}\n\n此操作无法撤销，是否继续？",
        "微信机器人 · 确认彻底卸载",
        0x00000004 | 0x00000030 | 0x00000100,
    )
    if answer != 6:
        return 0
    try:
        from .uninstall_ui import run_uninstall_progress

        def prepare(progress: Callable[[str], None]) -> None:
            progress("正在准备卸载程序…")
            plan = create_uninstall_plan(root)
            launch_uninstall_helper(plan)

        run_uninstall_progress(prepare, close_on_success=True)
        return 0
    except Exception as exc:
        _message_box(
            f"卸载程序未能启动：{exc}",
            "微信机器人 · 无法卸载",
            0x10,
        )
        return 1


def apply_uninstall_plan(
    plan_path: str | Path,
    *,
    show_result: bool = True,
    schedule_cleanup: bool = True,
    process_stopper: Callable[[str | Path], int] = stop_installation_processes,
    shortcut_remover: Callable[[], bool] = remove_windows_shortcuts,
    registry_remover: Callable[[], bool] = remove_windows_uninstaller_registration,
    local_app_data: str | Path | None = None,
) -> int:
    working = Path(plan_path).expanduser().resolve().parent
    try:
        def remove(progress: Callable[[str], None]) -> None:
            payload, root = _validated_plan(plan_path)
            progress("正在等待卸载准备完成…")
            for pid in _plan_wait_pids(payload):
                _wait_for_process_exit(pid)
            remove_installation(
                root,
                local_app_data=local_app_data,
                process_stopper=process_stopper,
                shortcut_remover=shortcut_remover,
                registry_remover=registry_remover,
                progress_callback=progress,
            )

        if show_result:
            from .uninstall_ui import run_uninstall_progress

            run_uninstall_progress(remove)
        else:
            remove(lambda _message: None)
        return 0
    except Exception as exc:
        if show_result:
            _message_box(
                f"卸载未能完整完成。请关闭微信和机器人相关窗口后重试。\n\n{exc}",
                "微信机器人 · 卸载失败",
                0x10,
            )
        return 1
    finally:
        if schedule_cleanup:
            _schedule_self_cleanup(working)


__all__ = [
    "UNINSTALL_EXECUTABLE_NAME",
    "UninstallError",
    "UninstallPlan",
    "apply_uninstall_plan",
    "create_uninstall_plan",
    "ensure_uninstall_executable",
    "is_uninstall_entrypoint",
    "launch_installed_uninstaller",
    "launch_uninstall_helper",
    "provision_uninstaller",
    "register_windows_uninstaller",
    "remove_installation",
    "remove_local_residuals",
    "remove_windows_uninstaller_registration",
    "run_uninstaller",
    "stop_installation_processes",
    "validate_uninstall_root",
]
