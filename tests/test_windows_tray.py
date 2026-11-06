from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from wechatrebot_manager import windows_tray
from wechatrebot_manager.windows_tray import WindowsTrayController


@pytest.fixture
def exit_jobs(monkeypatch):
    jobs = []
    real_thread = windows_tray.threading.Thread
    def thread_factory(**values):
        if values.get("name") != "wechatrebot-exit":
            return real_thread(**values)
        return SimpleNamespace(start=lambda: jobs.append(lambda: values["target"](*values.get("args", ()))))
    monkeypatch.setattr(windows_tray.threading, "Thread", thread_factory)
    return jobs


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


def test_explicit_exit_allows_window_to_close(monkeypatch, exit_jobs) -> None:
    calls: list[str] = []
    window = SimpleNamespace(
        hide=lambda: calls.append("hide"),
        evaluate_js=lambda script: False,
        destroy=lambda: calls.append("destroy"),
    )
    tray = WindowsTrayController(window, "测试机器人")
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))

    tray.request_exit()
    assert calls == []
    exit_jobs.pop()()

    assert calls == ["destroy", "stop"]
    assert tray.handle_window_closing() is None
    assert "hide" not in calls


def test_close_checks_drafts_when_tray_is_unavailable(
    monkeypatch, exit_jobs,
) -> None:
    calls: list[str] = []
    tray = WindowsTrayController(
        SimpleNamespace(hide=lambda: calls.append("hide"), evaluate_js=lambda script: False,
                        destroy=lambda: calls.append("destroy")),
        "测试机器人",
    )
    monkeypatch.setattr(tray, "_ensure_available", lambda: False)
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))

    assert tray.handle_window_closing() is False
    assert calls == []
    exit_jobs.pop()()
    assert calls == ["destroy", "stop"]


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("check", [True, None, "error"])
def test_exit_preserves_window_until_drafts_are_confirmed(monkeypatch, exit_jobs, confirm, check):
    calls = []
    def evaluate(script):
        assert script == windows_tray._BEFORE_UNLOAD_SCRIPT
        if check == "error":
            raise RuntimeError("renderer unavailable")
        return check
    window = SimpleNamespace(
        evaluate_js=evaluate, show=lambda: calls.append("show"), restore=lambda: calls.append("restore"),
        create_confirmation_dialog=lambda title, message: calls.append((title, message)) or confirm,
        destroy=lambda: calls.append("destroy"),
    )
    tray = WindowsTrayController(window, "测试机器人")
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))
    tray.request_exit()
    tray.request_exit()
    assert len(exit_jobs) == 1
    assert calls == []
    exit_jobs.pop()()
    assert calls[:2] == ["show", "restore"]
    assert calls[2][0] == "退出管理器？"
    assert ("destroy" in calls) is confirm
    assert ("stop" in calls) is confirm
    assert tray._exit_requested is confirm
    if not confirm:
        tray.request_exit()
        assert len(exit_jobs) == 1


def test_programmatic_update_close_bypasses_draft_prompt(monkeypatch, exit_jobs):
    calls = []
    tray = WindowsTrayController(SimpleNamespace(), "测试机器人")
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))
    tray.permit_programmatic_close()
    assert tray.handle_window_closing() is None
    assert calls == ["stop"]
    assert exit_jobs == []


def test_failed_exit_keeps_tray_and_allows_retry(monkeypatch, exit_jobs):
    calls = []
    def destroy():
        raise RuntimeError("native close failed")
    tray = WindowsTrayController(SimpleNamespace(
        evaluate_js=lambda script: False, destroy=destroy,
        show=lambda: calls.append("show"), restore=lambda: calls.append("restore")), "测试机器人")
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))
    tray.request_exit()
    exit_jobs.pop()()
    assert calls == ["show", "restore"]
    assert not tray._exit_requested
    tray.request_exit()
    assert len(exit_jobs) == 1


def test_renderer_timeout_allows_retry_without_spawning_more_probes(monkeypatch, exit_jobs):
    calls = []
    entered = threading.Event()
    release = threading.Event()
    answers = iter([False, False, True])

    def evaluate(script):
        calls.append("probe")
        entered.set()
        assert release.wait(2)
        return True

    def confirm(title, message):
        calls.append("confirm")
        expected = "仍有未保存" if release.is_set() else "暂时无法确认"
        assert expected in message
        return next(answers)

    tray = WindowsTrayController(SimpleNamespace(
        evaluate_js=evaluate, show=lambda: calls.append("show"), restore=lambda: calls.append("restore"),
        create_confirmation_dialog=confirm, destroy=lambda: calls.append("destroy")), "测试机器人")
    monkeypatch.setattr(windows_tray, "_DRAFT_CHECK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))
    try:
        tray.request_exit()
        exit_jobs.pop()()
        assert entered.wait(1)
        assert not tray._exit_check_pending
        assert not tray._exit_requested

        tray.request_exit()
        exit_jobs.pop()()
        assert calls.count("probe") == 1
        assert calls.count("confirm") == 2
        assert "destroy" not in calls
        assert not tray._exit_check_pending

        before_late_result = list(calls)
        release.set()
        assert tray._draft_check.wait(1)
        assert calls == before_late_result

        tray.request_exit()
        exit_jobs.pop()()
        assert calls.count("probe") == 2
        assert calls[-2:] == ["destroy", "stop"]
    finally:
        release.set()
        if tray._draft_check is not None:
            assert tray._draft_check.wait(1)


@pytest.mark.parametrize("stage", ["queued", "probe", "restore", "confirmation"])
def test_programmatic_close_invalidates_pending_user_exit(monkeypatch, exit_jobs, stage):
    calls = []

    def evaluate(script):
        calls.append("probe")
        if stage == "probe":
            tray.permit_programmatic_close()
        return True

    def confirm(title, message):
        calls.append("confirm")
        if stage == "confirmation":
            tray.permit_programmatic_close()
        return True

    def restore():
        calls.append("restore")
        if stage == "restore":
            tray.permit_programmatic_close()

    tray = WindowsTrayController(SimpleNamespace(
        evaluate_js=evaluate, show=lambda: calls.append("show"), restore=restore,
        create_confirmation_dialog=confirm, destroy=lambda: calls.append("destroy")), "测试机器人")
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))
    tray.request_exit()
    if stage == "queued":
        tray.permit_programmatic_close()
    exit_jobs.pop()()

    assert "destroy" not in calls
    assert calls.count("stop") == 1
    if stage in {"queued", "probe"}:
        assert "show" not in calls
    if stage != "confirmation":
        assert "confirm" not in calls
    assert tray._exit_requested
    assert not tray._exit_check_pending
    assert tray.handle_window_closing() is None
    tray.request_exit()
    assert exit_jobs == []


def test_stale_destroy_error_cannot_revoke_programmatic_close(monkeypatch, exit_jobs):
    calls = []

    def destroy():
        calls.append("destroy")
        tray.permit_programmatic_close()
        raise RuntimeError("native window already closing")

    tray = WindowsTrayController(SimpleNamespace(
        evaluate_js=lambda script: False, destroy=destroy,
        show=lambda: calls.append("show"), restore=lambda: calls.append("restore")), "测试机器人")
    monkeypatch.setattr(tray, "stop", lambda: calls.append("stop"))
    tray.request_exit()
    exit_jobs.pop()()

    assert calls == ["destroy", "stop"]
    assert tray._exit_requested
    assert not tray._exit_check_pending
    assert tray.handle_window_closing() is None
