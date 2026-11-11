"""Keep a restarted application's descendants inside one owned Windows job."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess


class ProcessTreeStopError(OSError):
    """The caller must retain recovery files instead of overwriting live code."""


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong),
        ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
        ("faults", wintypes.DWORD), ("total", wintypes.DWORD),
        ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
    ]


def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.ResumeThread.argtypes = [wintypes.HANDLE]
    api.ResumeThread.restype = wintypes.DWORD
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateJobObject.restype = wintypes.BOOL
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.QueryInformationJobObject.restype = wintypes.BOOL
    return api


class OwnedProcess:
    """Use kernel handles, never a PID lookup, to stop only this launch's tree."""

    def __init__(self, process: int, job: int, pid: int, api) -> None:
        self.pid = pid
        self._process = process
        self._job = job
        self._api = api

    def poll(self) -> int | None:
        import _winapi
        if _winapi.WaitForSingleObject(self._process, 0) == _winapi.WAIT_TIMEOUT:
            return None
        return _winapi.GetExitCodeProcess(self._process)

    def active_processes(self) -> int:
        accounting = _Accounting()
        if not self._api.QueryInformationJobObject(
            self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise ProcessTreeStopError("无法确认本次重启进程树是否已退出。")
        return int(accounting.active)

    def terminate_tree(self, timeout_seconds: float = 10) -> None:
        import _winapi
        # The root can already have exited while its one-file child is alive.
        # The retained job still owns that child and all of its descendants.
        if not self._api.TerminateJobObject(self._job, 1):
            raise ProcessTreeStopError("无法停止本次重启的客户端进程树。")
        # Termination clears ActiveProcesses before asynchronous process teardown
        # releases executable/file locks. The terminated job is signaled only
        # after its processes, including nested-job descendants, have exited.
        timeout_ms = max(0, min(int(timeout_seconds * 1000), 0xFFFFFFFE))
        try:
            result = _winapi.WaitForSingleObject(self._job, timeout_ms)
        except OSError as exc:
            raise ProcessTreeStopError("无法确认本次重启进程树是否已退出。") from exc
        if result != _winapi.WAIT_OBJECT_0:
            raise ProcessTreeStopError("等待本次重启的客户端进程树退出超时。")
        if self.active_processes():
            raise ProcessTreeStopError("无法确认本次重启进程树是否已退出。")

    def close(self) -> None:
        import _winapi
        # No KILL_ON_JOB_CLOSE: after successful startup the manager must survive
        # the updater's exit. Only explicit failure handling terminates the job.
        for name in ("_process", "_job"):
            handle = getattr(self, name, None)
            if handle:
                _winapi.CloseHandle(handle)
                setattr(self, name, None)

    def __del__(self) -> None:
        self.close()


def start_owned_process(
    command: list[str], *, cwd: Path, env: dict[str, str],
) -> OwnedProcess:
    """Assign a suspended process before any application code can spawn a child."""
    if os.name != "nt":
        raise OSError("当前平台不支持 Windows 进程树管理。")
    import _winapi

    api = _kernel()
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = thread = None
    owned = None
    started = False
    try:
        # CPython's Windows subprocess implementation uses this same wrapper.
        # Retaining its primary-thread handle lets us assign the job atomically
        # with respect to application execution, without enumerating live PIDs.
        process, thread, pid, _tid = _winapi.CreateProcess(
            command[0], subprocess.list2cmdline(command), None, None, False,
            subprocess.CREATE_NO_WINDOW | 0x00000004, env, str(cwd), subprocess.STARTUPINFO(),
        )
        owned = OwnedProcess(process, job, pid, api)
        if not api.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
        if api.ResumeThread(thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        started = True
        return owned
    except Exception:
        if process is not None:
            try:
                _winapi.TerminateProcess(process, 1)
                if _winapi.WaitForSingleObject(process, 5000) == _winapi.WAIT_TIMEOUT:
                    raise ProcessTreeStopError("启动失败的客户端进程未能退出，已保留更新恢复文件。")
            except OSError as exc:
                raise ProcessTreeStopError("启动失败的客户端进程无法安全停止。") from exc
        raise
    finally:
        if thread is not None:
            _winapi.CloseHandle(thread)
        if owned is not None and not started:
            owned.close()
        elif owned is None:
            _winapi.CloseHandle(job)
