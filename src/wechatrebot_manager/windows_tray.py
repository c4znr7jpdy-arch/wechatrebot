from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import sys
import threading
from typing import Any
import uuid


_BEFORE_UNLOAD_SCRIPT = """(() => {
    const event = new Event("beforeunload", {cancelable: true});
    window.dispatchEvent(event);
    return event.defaultPrevented;
})()"""
_DRAFT_CHECK_TIMEOUT_SECONDS = 3.0


def _tray_icon_path() -> Path | None:
    candidates = [Path(__file__).with_name("app_webui") / "assets" / "app-icon.ico"]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.insert(
            0,
            Path(bundle_root) / "wechatrebot_manager" / "app_webui" / "assets" / "app-icon.ico",
        )
    return next((path for path in candidates if path.is_file()), None)


class WindowsTrayController:
    """Keep a pywebview window available from the Windows notification area."""

    def __init__(self, window: Any, title: str) -> None:
        self._window = window
        self._title = str(title).strip() or "微信机器人"
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._available = False
        self._exit_requested = False
        self._exit_check_pending = False
        self._exit_generation = 0
        self._draft_check: threading.Event | None = None
        self._notified = False
        self._notify_data: Any | None = None
        self._shell_notify_icon: Any | None = None

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> None:
        if os.name != "nt":
            self._ready.set()
            return
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._message_loop,
                daemon=True,
                name="wechatrebot-tray",
            )
            self._thread.start()

    def _ensure_available(self) -> bool:
        self.start()
        self._ready.wait(2)
        return self._available

    def handle_window_closing(self) -> bool | None:
        """Return False to cancel a normal close after hiding the window."""
        if self._exit_requested:
            return None
        if not self._ensure_available():
            self.request_exit()
            return False
        try:
            self._window.hide()
        except Exception:
            self.request_exit()
            return False
        self._show_minimized_notification_once()
        return False

    def restore_window(self) -> None:
        try:
            self._window.show()
            self._window.restore()
        except Exception:
            return

    def request_exit(self) -> None:
        # Native closing callbacks run on the UI thread; evaluating JavaScript
        # there would deadlock WebView2. Cancel that close and check off-thread.
        with self._lock:
            if self._exit_requested or self._exit_check_pending:
                return
            self._exit_check_pending = True
            self._exit_generation += 1
            generation = self._exit_generation
        try:
            threading.Thread(target=self._finish_exit, args=(generation,), daemon=True, name="wechatrebot-exit").start()
        except RuntimeError:
            with self._lock:
                if generation == self._exit_generation:
                    self._exit_check_pending = False
            logging.getLogger(__name__).exception("Could not start manager exit check")

    def _check_window_drafts(self) -> bool | None:
        with self._lock:
            # A hung renderer keeps at most one daemon probe. A later exit can
            # still use the explicit confirmation without waiting on that probe.
            if self._draft_check is not None and not self._draft_check.is_set():
                return None
            completed = threading.Event()
            self._draft_check = completed
        result: list[Any] = []

        def probe() -> None:
            try:
                result.append(self._window.evaluate_js(_BEFORE_UNLOAD_SCRIPT))
            except Exception as exc:
                logging.getLogger(__name__).warning("Could not check manager drafts: %s", type(exc).__name__)
            finally:
                completed.set()

        threading.Thread(target=probe, daemon=True, name="wechatrebot-draft-check").start()
        if not completed.wait(_DRAFT_CHECK_TIMEOUT_SECONDS):
            logging.getLogger(__name__).warning("Manager draft check timed out")
            return None
        return result[0] if result and isinstance(result[0], bool) else None

    def _exit_check_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._exit_generation and not self._exit_requested

    def _finish_exit(self, generation: int) -> None:
        try:
            if not self._exit_check_current(generation):
                return
            needs_confirmation = self._check_window_drafts()
            if not self._exit_check_current(generation):
                return
            if needs_confirmation is not False:
                self.restore_window()
                if not self._exit_check_current(generation):
                    return
                message = (
                    "仍有未保存的设置，或保存、更新等任务尚未完成。确认退出会丢失未保存修改，并可能中断任务。取消可返回管理器继续处理。"
                    if needs_confirmation is True else
                    "暂时无法确认设置是否已保存。确认退出可能丢失未保存修改，取消可返回管理器检查。"
                )
                if not self._window.create_confirmation_dialog("退出管理器？", message):
                    return
            with self._lock:
                if generation != self._exit_generation or self._exit_requested:
                    return
                self._exit_requested = True
            self._window.destroy()
            self.stop()
        except Exception as exc:
            with self._lock:
                current = generation == self._exit_generation
                if current:
                    self._exit_requested = False
            if current:
                self.restore_window()
            logging.getLogger(__name__).warning("Could not close manager window: %s", type(exc).__name__)
        finally:
            with self._lock:
                if generation == self._exit_generation:
                    self._exit_check_pending = False

    def permit_programmatic_close(self) -> None:
        with self._lock:
            self._exit_generation += 1
            self._exit_requested = True
            self._exit_check_pending = False
        self.stop()

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd and os.name == "nt":
            try:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
            except Exception:
                return

    def _show_minimized_notification_once(self) -> None:
        if self._notified:
            return
        self._notified = True
        data = self._notify_data
        notify = self._shell_notify_icon
        if data is None or notify is None:
            return
        try:
            data.uFlags = 0x00000010
            data.szInfoTitle = "已最小化到托盘"
            data.szInfo = f"{self._title}仍在后台运行，双击托盘图标可恢复窗口。"[:255]
            data.dwInfoFlags = 0x00000001
            notify(0x00000001, ctypes.byref(data))
        except Exception:
            return

    def _message_loop(self) -> None:  # pragma: no cover - exercised on Windows GUI
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        wm_destroy = 0x0002
        wm_command = 0x0111
        wm_close = 0x0010
        wm_user = 0x0400
        wm_tray = wm_user + 1
        wm_lbutton_dblclk = 0x0203
        wm_rbutton_up = 0x0205
        wm_contextmenu = 0x007B
        command_open = 1001
        command_exit = 1002
        nif_message = 0x00000001
        nif_icon = 0x00000002
        nif_tip = 0x00000004
        nim_add = 0x00000000
        nim_delete = 0x00000002
        mf_string = 0x00000000
        mf_separator = 0x00000800
        tpm_leftalign = 0x0000
        tpm_bottomalign = 0x0020
        tpm_rightbutton = 0x0002
        image_icon = 1
        lr_defaultsize = 0x0040
        lr_loadfromfile = 0x0010
        lr_shared = 0x8000

        lresult = ctypes.c_ssize_t
        wndproc_type = ctypes.WINFUNCTYPE(
            lresult,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class WindowClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NotifyIconData(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = lresult
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        ]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = lresult
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(NotifyIconData),
        ]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        def show_menu(hwnd: int) -> None:
            menu = user32.CreatePopupMenu()
            if not menu:
                return
            point = wintypes.POINT()
            try:
                user32.AppendMenuW(menu, mf_string, command_open, "打开主界面")
                user32.AppendMenuW(menu, mf_separator, 0, None)
                user32.AppendMenuW(menu, mf_string, command_exit, "退出程序")
                user32.GetCursorPos(ctypes.byref(point))
                user32.SetForegroundWindow(hwnd)
                user32.TrackPopupMenu(
                    menu,
                    tpm_leftalign | tpm_bottomalign | tpm_rightbutton,
                    point.x,
                    point.y,
                    0,
                    hwnd,
                    None,
                )
            finally:
                user32.DestroyMenu(menu)

        @wndproc_type
        def window_proc(hwnd, message, wparam, lparam):
            if message == wm_tray:
                event = int(lparam) & 0xFFFF
                if event == wm_lbutton_dblclk:
                    self.restore_window()
                    return 0
                if event in {wm_rbutton_up, wm_contextmenu}:
                    show_menu(hwnd)
                    return 0
            elif message == wm_command:
                command = int(wparam) & 0xFFFF
                if command == command_open:
                    self.restore_window()
                    return 0
                if command == command_exit:
                    self.request_exit()
                    return 0
            elif message == wm_close:
                user32.DestroyWindow(hwnd)
                return 0
            elif message == wm_destroy:
                data = self._notify_data
                if data is not None:
                    shell32.Shell_NotifyIconW(nim_delete, ctypes.byref(data))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        class_name = f"WeChatReBotTray_{uuid.uuid4().hex}"
        instance = kernel32.GetModuleHandleW(None)
        resource = ctypes.cast(32512, wintypes.LPCWSTR)
        icon_path = _tray_icon_path()
        icon = user32.LoadImageW(
            None,
            str(icon_path) if icon_path else resource,
            image_icon,
            0,
            0,
            (lr_defaultsize | lr_loadfromfile) if icon_path else (lr_defaultsize | lr_shared),
        )
        window_class = WindowClass()
        window_class.lpfnWndProc = window_proc
        window_class.hInstance = instance
        window_class.hIcon = icon
        window_class.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            self._ready.set()
            return

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            instance,
            None,
        )
        if not hwnd:
            user32.UnregisterClassW(class_name, instance)
            self._ready.set()
            return

        data = NotifyIconData()
        data.cbSize = ctypes.sizeof(NotifyIconData)
        data.hWnd = hwnd
        data.uID = 1
        data.uFlags = nif_message | nif_icon | nif_tip
        data.uCallbackMessage = wm_tray
        data.hIcon = icon
        data.szTip = self._title[:127]
        added = bool(shell32.Shell_NotifyIconW(nim_add, ctypes.byref(data)))
        self._hwnd = int(hwnd)
        self._notify_data = data
        self._shell_notify_icon = shell32.Shell_NotifyIconW
        self._available = added
        self._ready.set()
        if not added:
            user32.DestroyWindow(hwnd)

        message = wintypes.MSG()
        while added and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        self._available = False
        self._hwnd = None
        self._notify_data = None
        self._shell_notify_icon = None
        user32.UnregisterClassW(class_name, instance)


__all__ = ["WindowsTrayController"]
