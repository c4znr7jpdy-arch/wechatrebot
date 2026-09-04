from __future__ import annotations

from types import SimpleNamespace

from wechatrebot_manager.windows_tray import WindowsTrayController


def test_close_hides_window_when_tray_is_available(monkeypatch) -> None:
    calls: list[str] = []
    window = SimpleNamespace(
        hide=lambda: calls.append("hide"),
        show=lambda: calls.append("show"),
        restore=lambda: calls.append("restore"),
        destroy=lambda: calls.append("destroy"),
    )
    tray = WindowsTrayController(window, "测试机器人")
    monkeypatch.setattr(tray, "_ensure_available", lambda: True)
    monkeypatch.setattr(
        tray,
        "_show_minimized_notification_once",
        lambda: calls.append("notify"),
    )

    assert tray.handle_window_closing() is False
    assert calls == ["hide", "notify"]

    tray.restore_window()
    assert calls[-2:] == ["show", "restore"]


def test_explicit_exit_allows_window_to_close(monkeypatch) -> None:
    calls: list[str] = []
    window = SimpleNamespace(
        hide=lambda: calls.append("hide"),
        destroy=lambda: calls.append("destroy"),
    )
    tray = WindowsTrayController(window, "测试机器人")
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))

    tray.request_exit()

    assert calls == ["stop", "destroy"]
    assert tray.handle_window_closing() is None
    assert "hide" not in calls


def test_close_falls_back_to_normal_exit_when_tray_is_unavailable(
    monkeypatch,
) -> None:
    calls: list[str] = []
    tray = WindowsTrayController(
        SimpleNamespace(hide=lambda: calls.append("hide")),
        "测试机器人",
    )
    monkeypatch.setattr(tray, "_ensure_available", lambda: False)

    assert tray.handle_window_closing() is None
    assert calls == []
