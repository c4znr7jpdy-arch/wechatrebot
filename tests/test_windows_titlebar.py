import ctypes
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wechatrebot_manager import windows_titlebar


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self):
        for handler in tuple(self.handlers):
            handler(None, None)


@pytest.fixture(autouse=True)
def system_events(monkeypatch):
    events = SimpleNamespace(UserPreferenceChanged=_Event())
    monkeypatch.setitem(sys.modules, "Microsoft.Win32", SimpleNamespace(SystemEvents=events))
    return events


def _native_window():
    return SimpleNamespace(native=SimpleNamespace(
        IsDisposed=False, IsHandleCreated=True, InvokeRequired=False,
        Handle=SimpleNamespace(ToInt64=lambda: 0x123456789),
        Disposed=_Event(),
    ))


def _dwm(monkeypatch, results):
    calls = []

    def set_attribute(hwnd, attribute, pointer, size):
        value = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value
        calls.append((hwnd, attribute, value, size))
        return results.get(attribute, 0)

    monkeypatch.setattr(windows_titlebar, "os", SimpleNamespace(name="nt"))
    library = SimpleNamespace(DwmSetWindowAttribute=Mock(side_effect=set_attribute))
    monkeypatch.setattr(windows_titlebar.ctypes, "WinDLL", lambda *_args, **_kw: library, raising=False)
    return calls


@pytest.mark.parametrize("theme,dark", [("light", 0), ("dark", 1)])
def test_caption_uses_app_palette_and_pointer_sized_native_handle(monkeypatch, theme, dark):
    calls = _dwm(monkeypatch, {})
    assert windows_titlebar.apply_window_theme(_native_window(), theme, "#162B40", "#eef2f7")
    assert calls == [(0x123456789, 20, dark, 4),
                     (0x123456789, 35, 0x402B16, 4), (0x123456789, 36, 0xF7F2EE, 4)]


def test_caption_updates_are_marshaled_to_native_ui_thread(monkeypatch):
    calls = _dwm(monkeypatch, {})
    window = _native_window()
    window.native.InvokeRequired = True
    window.native.Invoke = Mock(side_effect=lambda action: action())
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda callback: callback))
    assert windows_titlebar.apply_window_theme(window, "dark", "#161b22", "#eef2f7")
    window.native.Invoke.assert_called_once()
    assert len(calls) == 3


def test_older_windows_retains_dark_mode_when_explicit_colors_are_unsupported(monkeypatch):
    calls = _dwm(monkeypatch, {35: -2147024809, 36: -2147024809})
    assert windows_titlebar.apply_window_theme(_native_window(), "dark", "#161b22", "#eef2f7")
    assert [item[1] for item in calls] == [20, 35, 36]


def test_system_theme_change_restores_latest_app_palette_without_duplicate_subscriptions(monkeypatch, system_events):
    calls = _dwm(monkeypatch, {35: -2147024809, 36: -2147024809})
    # pywebview registers first and overwrites DWM dark mode on a system change.
    system_events.UserPreferenceChanged += lambda *_args: calls.append((0x123456789, 20, 1, 4))
    window = _native_window()
    assert windows_titlebar.apply_window_theme(window, "dark", "#161b22", "#eef2f7")
    assert windows_titlebar.apply_window_theme(window, "light", "#ffffff", "#14181f")
    assert len(system_events.UserPreferenceChanged.handlers) == 2
    assert len(window.native.Disposed.handlers) == 1
    calls.clear()

    system_events.UserPreferenceChanged.emit()

    assert calls == [(0x123456789, 20, 1, 4), (0x123456789, 20, 0, 4),
                     (0x123456789, 35, 0xFFFFFF, 4), (0x123456789, 36, 0x1F1814, 4)]


def test_disposed_window_unsubscribes_and_cannot_reapply_theme(monkeypatch, system_events):
    calls = _dwm(monkeypatch, {})
    window = _native_window()
    assert windows_titlebar.apply_window_theme(window, "light", "#ffffff", "#14181f")
    window.native.IsDisposed = True
    window.native.Disposed.emit()
    assert system_events.UserPreferenceChanged.handlers == []
    assert window.native.Disposed.handlers == []
    calls.clear()

    system_events.UserPreferenceChanged.emit()
    assert not windows_titlebar.apply_window_theme(window, "dark", "#161b22", "#eef2f7")
    assert calls == []


@pytest.mark.parametrize("condition", ["unavailable", "disposed", "not-created", "unsupported"])
def test_optional_caption_support_does_not_prevent_application_theme_changes(monkeypatch, condition):
    calls = _dwm(monkeypatch, {20: -1, 35: -1, 36: -1})
    window = _native_window()
    if condition == "unavailable":
        window.native = None
    elif condition == "disposed":
        window.native.IsDisposed = True
    elif condition == "not-created":
        window.native.IsHandleCreated = False
    assert not windows_titlebar.apply_window_theme(window, "light", "#ffffff", "#14181f")
    assert bool(calls) == (condition == "unsupported")


@pytest.mark.parametrize("theme,background,foreground", [
    ("system", "#ffffff", "#14181f"), ("light", "#fff", "#14181f"),
    ("light", "#ffffff", "red"), ("dark", None, "#eef2f7"),
])
def test_native_theme_rejects_non_palette_inputs(theme, background, foreground):
    with pytest.raises(ValueError):
        windows_titlebar.apply_window_theme(None, theme, background, foreground)
