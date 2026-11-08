"""Retry native window closure without launching another update helper."""
from types import SimpleNamespace

import pytest

from test_app_shell import _download_ready, _downloadable_update, _wait_update_worker
from wechatrebot_manager import update_api, windows_file_holders
from wechatrebot_manager.windows_tray import WindowsTrayController


def _waiting_for_close(monkeypatch, tmp_path, *, timer_fails=False):
    api, prepared, events = _downloadable_update(monkeypatch, tmp_path)
    callbacks = []
    helper = SimpleNamespace(poll=lambda: None, args=[str(tmp_path / "helper.exe")])
    def launch(_plan):
        events.append("launch")
        return helper
    def timer(_delay, callback):
        def start():
            if timer_fails:
                raise RuntimeError("synthetic timer startup failure")
            callbacks.append(callback)
        return SimpleNamespace(start=start)
    monkeypatch.setattr(update_api, "launch_update_helper", launch)
    monkeypatch.setattr(update_api.threading, "Timer", timer)
    api._window = SimpleNamespace(destroy=lambda: (_ for _ in ()).throw(RuntimeError("synthetic native failure")))
    status = _download_ready(api)
    assert api.install_update(prepared.release.version, status["confirmation_token"])["data"]["accepted"]
    _wait_update_worker(api)
    if not timer_fails:
        callbacks.pop()()
    failed = api.get_update_status()["data"]
    assert failed["state"] == "ready" and failed["close_pending"]
    assert "未能自动关闭" in failed["message"]
    assert failed["confirmation_token"] != status["confirmation_token"]
    return api, prepared, events, helper, callbacks, failed


@pytest.mark.parametrize("timer_fails", [False, True])
def test_close_failure_preserves_helper_and_retries_only_window_close(monkeypatch, tmp_path, timer_fails):
    api, prepared, events, _helper, callbacks, failed = _waiting_for_close(
        monkeypatch, tmp_path, timer_fails=timer_fails,
    )
    assert api._update_installing
    assert not api.download_update(prepared.release.version)["data"]["accepted"]
    assert not api.install_update(prepared.release.version, "old-token")["ok"]
    assert api._update_installing
    if timer_fails:
        monkeypatch.setattr(update_api.threading, "Timer", lambda _delay, fn:
                            SimpleNamespace(start=lambda: callbacks.append(fn)))
    closed = []
    api._window.destroy = lambda: closed.append(True)
    assert api.install_update(prepared.release.version, failed["confirmation_token"])["data"]["accepted"]
    callbacks.pop()()
    assert closed == [True]
    assert events.count("launch") == events.count("download") == events.count("stop_all") == 1
    assert api._update_installing
    assert api.get_update_status()["data"]["state"] == "restarting"


def test_exited_helper_releases_installation_for_a_normal_retry(monkeypatch, tmp_path):
    api, prepared, events, helper, _callbacks, failed = _waiting_for_close(monkeypatch, tmp_path)
    helper.poll = lambda: 1
    monkeypatch.setattr(windows_file_holders, "file_holders", lambda _paths: ())
    expired = api.get_update_status()["data"]
    assert expired["state"] == "failed" and not expired["close_pending"]
    assert not expired["confirmation_token"] and not api._update_installing
    assert api._prepared_update is prepared
    ready = _download_ready(api)
    assert ready["confirmation_token"] != failed["confirmation_token"]
    assert not ready["close_pending"]
    assert events.count("download") == events.count("launch") == 1


def test_unverified_helper_blocks_a_second_helper(monkeypatch, tmp_path):
    api, prepared, events, helper, _callbacks, _failed = _waiting_for_close(monkeypatch, tmp_path)
    helper.poll = lambda: (_ for _ in ()).throw(OSError("synthetic query failure"))
    assert api.get_update_status()["data"]["close_pending"]
    assert not api.download_update(prepared.release.version)["data"]["accepted"]
    assert api._update_installing and events.count("launch") == 1


@pytest.mark.parametrize("query_fails", [False, True])
def test_exited_helper_root_does_not_allow_overlap_with_owned_file_holders(monkeypatch, tmp_path, query_fails):
    api, prepared, events, helper, _callbacks, _failed = _waiting_for_close(monkeypatch, tmp_path)
    helper.poll = lambda: 1
    def holders(paths):
        assert list(paths) == [tmp_path / "helper.exe"]
        if query_fails:
            raise OSError("synthetic holder query failure")
        return ((123, object()),)
    monkeypatch.setattr(windows_file_holders, "file_holders", holders)
    assert api.get_update_status()["data"]["close_pending"]
    assert not api.download_update(prepared.release.version)["data"]["accepted"]
    assert api._update_installing and events.count("launch") == 1


def test_repeated_close_failure_rotates_consent_without_launching_helper(monkeypatch, tmp_path):
    api, prepared, events, _helper, callbacks, failed = _waiting_for_close(monkeypatch, tmp_path)
    assert api.install_update(prepared.release.version, failed["confirmation_token"])["data"]["accepted"]
    callbacks.pop()()
    again = api.get_update_status()["data"]
    assert again["close_pending"] and again["confirmation_token"] != failed["confirmation_token"]
    assert api._update_installing and events.count("launch") == 1


def test_native_close_failure_keeps_tray_and_normal_close_protection(monkeypatch, tmp_path):
    api, _prepared, _events, _helper, _callbacks, _failed = _waiting_for_close(monkeypatch, tmp_path)
    calls = []
    api._tray = WindowsTrayController(api._window, "test")
    monkeypatch.setattr(api._tray, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(api._tray, "_ensure_available", lambda: True)
    api._window.hide = lambda: calls.append("hide")
    api._close_window_for_update(api._update_helper, api._update_close_generation)
    assert calls == []
    assert not api._tray._exit_requested
    assert api._tray.handle_window_closing() is False
    assert calls == ["hide"]


def _scheduled_close(monkeypatch, tmp_path, attempt):
    if attempt == "retry":
        api, prepared, events, helper, callbacks, failed = _waiting_for_close(monkeypatch, tmp_path)
        assert api.install_update(prepared.release.version, failed["confirmation_token"])["data"]["accepted"]
    else:
        api, prepared, events = _downloadable_update(monkeypatch, tmp_path)
        callbacks = []
        monkeypatch.setattr(update_api.threading, "Timer", lambda _delay, callback:
                            SimpleNamespace(start=lambda: callbacks.append(callback)))
        api._window = SimpleNamespace(destroy=lambda: None)
        ready = _download_ready(api)
        assert api.install_update(prepared.release.version, ready["confirmation_token"])["data"]["accepted"]
        _wait_update_worker(api)
        helper = api._update_helper
    calls = []
    api._tray = SimpleNamespace(
        permit_programmatic_close=lambda: calls.append("permit"),
        cancel_programmatic_close=lambda: calls.append("cancel"),
    )
    api._window.destroy = lambda: calls.append("destroy")
    api._close_feature_page_server = lambda: calls.append("server_close")
    return api, prepared, events, helper, callbacks, calls


@pytest.mark.parametrize("attempt", ["initial", "retry"])
@pytest.mark.parametrize("poll_before_callback", [False, True])
def test_exited_helper_invalidates_pending_close_before_native_side_effects(
    monkeypatch, tmp_path, attempt, poll_before_callback,
):
    api, _prepared, _events, helper, callbacks, calls = _scheduled_close(monkeypatch, tmp_path, attempt)
    helper.poll = lambda: 1
    monkeypatch.setattr(windows_file_holders, "file_holders", lambda _paths: ())
    if poll_before_callback:
        assert api.get_update_status()["data"]["state"] == "failed"
    callbacks.pop()()
    assert calls == []
    assert not api._update_installing and api._update_helper is None
    assert api.get_update_status()["data"]["state"] == "failed"


@pytest.mark.parametrize("attempt", ["initial", "retry"])
def test_old_close_cannot_close_or_reset_a_new_helper_handoff(monkeypatch, tmp_path, attempt):
    api, prepared, events, helper, callbacks, calls = _scheduled_close(monkeypatch, tmp_path, attempt)
    old_callback = callbacks.pop()
    helper.poll = lambda: 1
    monkeypatch.setattr(windows_file_holders, "file_holders", lambda _paths: ())
    assert api.get_update_status()["data"]["state"] == "failed"
    ready = _download_ready(api)
    replacement = SimpleNamespace(poll=lambda: None, args=[str(tmp_path / "replacement-helper.exe")])
    monkeypatch.setattr(update_api, "launch_update_helper", lambda _plan:
                        events.append("launch") or replacement)
    assert api.install_update(prepared.release.version, ready["confirmation_token"])["data"]["accepted"]
    assert _wait_update_worker(api)["state"] == "restarting"
    before = api.get_update_status()["data"]
    old_callback()
    assert calls == []
    assert api._update_helper is replacement and api._update_installing
    assert api.get_update_status()["data"] == before
    current_callback = callbacks.pop()
    current_callback()
    current_callback()  # An already consumed timer cannot close twice.
    old_callback()
    assert calls == ["permit", "destroy", "server_close"]
    assert events.count("launch") == 2 and events.count("download") == 1


def test_new_close_attempt_invalidates_older_attempt_for_same_helper(monkeypatch, tmp_path):
    api, _prepared, _events, helper, callbacks, calls = _scheduled_close(monkeypatch, tmp_path, "retry")
    old_callback = callbacks.pop()
    with api._update_lock:
        api._schedule_update_close()
    current_callback = callbacks.pop()
    before = api.get_update_status()["data"]
    old_callback()
    assert calls == [] and api._update_helper is helper
    assert api.get_update_status()["data"] == before
    current_callback()
    assert calls == ["permit", "destroy", "server_close"]
