from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import tempfile


ERROR_ALREADY_EXISTS = 183


def instance_name(key: str | Path) -> str:
    normalized = str(key).strip().replace("\\", "/").casefold().encode("utf-8")
    digest = hashlib.sha256(normalized).hexdigest()[:24]
    return f"Local\\WeChatReBot-{digest}"


class SingleInstanceLock:
    def __init__(self, key: str | Path) -> None:
        self.key = str(key)
        self._handle: int | None = None
        self._lock_path: Path | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None or self._lock_path is not None

    def acquire(self) -> bool:
        if self.acquired:
            return True
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
            create_mutex.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_bool
            ctypes.set_last_error(0)
            handle = create_mutex(None, False, instance_name(self.key))
            if not handle:
                raise OSError(ctypes.get_last_error(), "无法创建单实例锁")
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                close_handle(handle)
                return False
            self._handle = int(handle)
            return True

        digest = instance_name(self.key).rsplit("-", 1)[-1]
        target = Path(tempfile.gettempdir()) / f"wechatrebot-{digest}.lock"
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        self._lock_path = target
        return True

    def release(self) -> None:
        if self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_bool
            close_handle(ctypes.c_void_p(self._handle))
            self._handle = None
        if self._lock_path is not None:
            self._lock_path.unlink(missing_ok=True)
            self._lock_path = None

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise RuntimeError("微信机器人管理器已经在运行。")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


__all__ = ["SingleInstanceLock", "instance_name"]
