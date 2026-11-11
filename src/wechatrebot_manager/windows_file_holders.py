"""Identify users of exact Windows files without closing any application."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .payload import PayloadError, _checked_target

if TYPE_CHECKING:
    from .launcher import ProcessIdentity


class _UniqueProcess(ctypes.Structure):
    _fields_ = [("pid", wintypes.DWORD), ("started", wintypes.FILETIME)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", _UniqueProcess),
        ("app_name", wintypes.WCHAR * 256),
        ("service_name", wintypes.WCHAR * 64),
        ("app_type", ctypes.c_int),
        ("status", wintypes.ULONG),
        ("session_id", wintypes.DWORD),
        ("restartable", wintypes.BOOL),
    ]


def _check_status(status: int, operation: str) -> None:
    if status:
        raise OSError(status, f"{operation} failed ({status})")


def _restart_manager_processes(files: tuple[Path, ...]) -> tuple[tuple[int, int], ...]:
    if os.name != "nt":
        raise OSError("当前平台无法查询 Windows 文件占用。")
    # Layouts/signatures match Microsoft's restartmanager.h. We only register
    # exact filenames and query the list; never call RmShutdown or RmRestart.
    api = ctypes.WinDLL("Rstrtmgr.dll", use_last_error=True)
    api.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    api.RmStartSession.restype = wintypes.DWORD
    api.RmRegisterResources.argtypes = [
        wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT, ctypes.POINTER(_UniqueProcess),
        wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
    ]
    api.RmRegisterResources.restype = wintypes.DWORD
    api.RmGetList.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(_ProcessInfo), ctypes.POINTER(wintypes.DWORD),
    ]
    api.RmGetList.restype = wintypes.DWORD
    api.RmEndSession.argtypes = [wintypes.DWORD]
    api.RmEndSession.restype = wintypes.DWORD
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    _check_status(api.RmStartSession(ctypes.byref(session), 0, key), "RmStartSession")
    try:
        names = (wintypes.LPCWSTR * len(files))(*(str(path) for path in files))
        _check_status(api.RmRegisterResources(session, len(files), names, 0, None, 0, None), "RmRegisterResources")
        needed, count, reboot = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
        processes = None
        for _ in range(5):
            status = api.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), processes, ctypes.byref(reboot))
            if status == 0:
                if count.value > (len(processes) if processes is not None else 0):
                    raise OSError("Windows 文件占用查询返回不完整列表。")
                # Reboot reasons constrain Restart Manager's shutdown ability,
                # not the reported file ownership. Keep complete holder evidence
                # (including self/critical apps) so callers can block the update.
                if reboot.value and not count.value:
                    raise OSError("Windows 无法确认文件是否可释放，请稍后重试。")
                return tuple(
                    (int(item.process.pid),
                     (int(item.process.started.dwHighDateTime) << 32) | int(item.process.started.dwLowDateTime))
                    for item in (processes or ())[:count.value]
                )
            if status != 234:  # ERROR_MORE_DATA: the process list may grow.
                _check_status(status, "RmGetList")
            if not 0 < needed.value <= 100_000:
                raise OSError("Windows 文件占用查询返回无效数量。")
            count.value = needed.value
            processes = (_ProcessInfo * count.value)()
        raise OSError("Windows 文件占用列表持续变化，无法确认进程身份。")
    finally:
        _check_status(api.RmEndSession(session), "RmEndSession")


def file_holders(files: Iterable[str | Path]) -> tuple[tuple[int, ProcessIdentity], ...]:
    """Return current identities proven to hold one of these exact files.

    Restart Manager includes image/data mappings absent from ToolHelp's loader
    list. Its PID/start time must still match a freshly queried executable.
    Any inaccessible, expired, or reused identity fails closed.
    """
    from .launcher import process_identity

    paths: dict[str, Path] = {}
    for value in files:
        path = Path(value).expanduser()
        if not path.is_absolute() or path.is_dir():
            raise OSError("文件占用查询必须指定精确文件的绝对路径。")
        try:
            path = _checked_target(Path(path.anchor), path.relative_to(path.anchor))
        except PayloadError as exc:
            raise OSError("文件占用查询不能经过符号链接或目录联接。") from exc
        paths[os.path.normcase(str(path))] = path
    if not paths:
        return ()
    holders: dict[int, ProcessIdentity] = {}
    for pid, created in _restart_manager_processes(tuple(paths.values())):
        identity = process_identity(pid)
        if (
            pid <= 0 or created <= 0 or identity is None
            or not identity.executable or identity.creation_time != created
            or (pid in holders and holders[pid] != identity)
        ):
            raise OSError("文件占用进程身份无法确认，请稍后重试。")
        holders[pid] = identity
    return tuple(holders.items())


__all__ = ["file_holders"]
