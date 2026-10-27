"""Apply the WebUI palette to the native Windows caption when supported."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import re
from typing import Any


_LOGGER = logging.getLogger(__name__)
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_DARK_MODE = 20
_CAPTION_COLOR = 35
_TEXT_COLOR = 36


def _colorref(value: str) -> int:
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise ValueError("标题栏颜色必须为六位十六进制颜色。")
    red, green, blue = (int(value[index:index + 2], 16) for index in (1, 3, 5))
    return red | (green << 8) | (blue << 16)


class _CaptionTheme:
    def __init__(self, native: Any) -> None:
        self.native = native
        self.palette: tuple[int, int, int] | None = None
        self._system_events: Any | None = None
        self._closed = False
        # Retain the same callbacks for .NET event subscription and removal.
        self._changed_handler = self._on_system_changed
        self._disposed_handler = self._on_disposed

    def _subscribe(self) -> None:
        if self._system_events is not None:
            return
        from Microsoft.Win32 import SystemEvents  # type: ignore[import-not-found]

        self.native.Disposed += self._disposed_handler
        try:
            # pywebview subscribed during native window construction. Running
            # afterwards restores our application palette over its system theme.
            SystemEvents.UserPreferenceChanged += self._changed_handler
        except Exception:
            self.native.Disposed -= self._disposed_handler
            raise
        self._system_events = SystemEvents

    def _on_system_changed(self, *_args: Any) -> None:
        self.refresh()

    def _on_disposed(self, *_args: Any) -> None:
        self._closed = True
        events, self._system_events = self._system_events, None
        try:
            if events is not None:
                events.UserPreferenceChanged -= self._changed_handler
            self.native.Disposed -= self._disposed_handler
        except Exception as exc:
            _LOGGER.debug("Native caption listeners were not removed: %s", type(exc).__name__)

    def _update(self) -> bool:
        native = self.native
        if self._closed or native.IsDisposed or not native.IsHandleCreated or self.palette is None:
            return False
        self._subscribe()
        # IntPtr must remain pointer-sized on 64-bit Windows.
        hwnd = native.Handle.ToInt64()
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        set_attribute = dwm.DwmSetWindowAttribute
        set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        set_attribute.restype = ctypes.c_long
        applied = False
        for attribute, color in zip((_DARK_MODE, _CAPTION_COLOR, _TEXT_COLOR), self.palette):
            value = ctypes.c_uint32(color)
            result = set_attribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
            # Caption/text colors need Windows 11; dark mode can still work on
            # earlier Windows versions that reject the two color attributes.
            applied = result == 0 or applied
            if result != 0:
                _LOGGER.debug("Native caption attribute %s is unavailable (%s)", attribute, result)
        return applied

    def refresh(self) -> bool:
        if self._closed:
            return False
        applied = False

        def update() -> None:
            nonlocal applied
            applied = self._update()

        try:
            if self.native.InvokeRequired:
                from System import Action  # type: ignore[import-not-found]

                self.native.Invoke(Action(update))
            else:
                update()
        except Exception as exc:  # Native window may be closing or lack DWM support.
            _LOGGER.debug("Native caption theme was not applied: %s", type(exc).__name__)
            return False
        return applied


def apply_window_theme(window: Any, theme: str, background: str, foreground: str) -> bool:
    """Use only this window's handle; unsupported Windows attributes are optional."""
    if theme not in ("light", "dark"):
        raise ValueError("请选择浅色或深色模式。")
    caption, text = _colorref(background), _colorref(foreground)
    if os.name != "nt" or window is None:
        return False
    native = getattr(window, "native", None)
    if native is None:
        return False
    controller = getattr(window, "_caption_theme", None)
    if controller is None:
        controller = window._caption_theme = _CaptionTheme(native)
    controller.palette = (int(theme == "dark"), caption, text)
    return controller.refresh()
