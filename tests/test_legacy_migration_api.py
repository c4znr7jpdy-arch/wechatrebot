"""Migration/start serialization without creating any live services."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from wechatrebot_manager import app_shell
from wechatrebot_manager.payload import PayloadError


@pytest.fixture
def api(tmp_path):
    value = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    value._install_root = tmp_path
    value._mandatory_update = False
    value._action_lock = threading.Lock()
    value._lifecycle = SimpleNamespace(start=lambda: pytest.fail("unexpected startup"))
    return value


@pytest.mark.parametrize("method,args,service_method", [
    ("migrate_legacy_schedule", ("token",), "commit"),
    ("cancel_legacy_schedule_migration", ("key",), "cancel"),
    ("set_schedule_enabled", ("job", False), "set_enabled"),
    ("update_schedule_time", ("job", "09:00"), "update_time"),
    ("delete_schedule", ("job",), "delete"),
])
def test_start_and_all_legacy_writes_share_the_action_lock(api, method, args, service_method):
    calls = []

    def write(*received):
        assert api._action_lock.locked()
        assert api.start_robot()["data"]["accepted"] is False
        calls.append(received)
        raise ValueError("旧任务已改变，请刷新。")

    service = SimpleNamespace(**{service_method: write})
    api._legacy_migrations = service
    api._schedules = service
    with api._action_lock:
        assert getattr(api, method)(*args)["ok"] is False
    assert calls == []
    result = getattr(api, method)(*args)
    assert result["ok"] is False
    assert result["error"]["message"] == "旧任务已改变，请刷新。"
    assert calls == [args]
    assert not api._action_lock.locked()


def test_in_flight_migration_blocks_other_ui_actions_until_completion(api):
    entered = threading.Event()
    release = threading.Event()
    results = []

    def commit(_token):
        entered.set()
        assert release.wait(3)
        return {"pending": True}

    api._legacy_migrations = SimpleNamespace(commit=commit, cancel=lambda _key: pytest.fail("cancel raced"))
    worker = threading.Thread(target=lambda: results.append(api.migrate_legacy_schedule("token")))
    worker.start()
    try:
        assert entered.wait(3)
        assert api.start_robot()["data"]["accepted"] is False
        assert api.cancel_legacy_schedule_migration("key")["ok"] is False
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert results[0]["data"]["pending"] is True
    assert not api._action_lock.locked()


@pytest.mark.parametrize("failure", [None, OSError("private path"), ValueError("corrupt journal"), PayloadError("unsafe path")])
def test_pending_or_unreadable_journal_blocks_start_and_releases_lock(api, monkeypatch, failure):
    def pending(root):
        assert root == api._install_root
        assert api._action_lock.locked()
        if failure is not None:
            raise failure
        return True

    monkeypatch.setattr(app_shell, "has_pending_migrations", pending)
    result = api.start_robot()
    assert result["data"]["accepted"] is False
    assert "迁移" in result["message"]
    assert "private path" not in str(result)
    assert not api._action_lock.locked()


def test_completed_migration_gate_allows_normal_start_worker(api, monkeypatch):
    started = []

    class InlineThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(app_shell.threading, "Thread", InlineThread)
    api._lifecycle.start = lambda: started.append(api._action_lock.locked())
    assert api.start_robot()["data"]["accepted"] is True
    assert started == [True]
    assert not api._action_lock.locked()


def test_migration_transport_errors_are_customer_safe(api):
    def commit(_token):
        raise PayloadError("sensitive filesystem location")

    api._legacy_migrations = SimpleNamespace(commit=commit)
    result = api.migrate_legacy_schedule("token")
    assert result["ok"] is False
    assert "连接或保存未完成" in result["error"]["message"]
    assert "sensitive" not in str(result)
    assert not api._action_lock.locked()
