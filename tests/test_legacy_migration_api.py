"""Migration/start serialization without creating any live services."""
from __future__ import annotations

import threading
import sqlite3
from types import SimpleNamespace

import pytest

from wechatrebot_manager import app_shell
from wechatrebot_manager.events import EventStore
from wechatrebot_manager.payload import PayloadError


@pytest.fixture
def api(tmp_path):
    value = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    value._install_root = tmp_path
    value._mandatory_update = False
    value._action_lock = threading.Lock()
    value._lifecycle = SimpleNamespace(
        start=lambda: pytest.fail("unexpected startup"), events=EventStore(tmp_path),
    )
    return value


@pytest.mark.parametrize("method,args,service_method", [
    ("migrate_legacy_schedule", ("token",), "commit"),
    ("cancel_legacy_schedule_migration", ("key",), "cancel"),
    ("set_schedule_enabled", ("job", False), "set_enabled"),
    ("update_schedule_time", ("job", "09:00"), "update_time"),
    ("delete_schedule", ("job",), "delete"),
    ("migrate_group_analysis", ("token",), "migrate_group_analysis"),
    ("cancel_group_analysis_migration", ("wxid_robot",), "cancel_group_analysis_migration"),
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
    api._scheduled_tasks = service
    api._schedules = service
    with api._action_lock:
        assert getattr(api, method)(*args)["ok"] is False
    assert calls == []
    result = getattr(api, method)(*args)
    assert result["ok"] is False
    assert result["error"]["message"] == "旧任务已改变，请刷新。"
    assert calls == [args]
    assert not api._action_lock.locked()


@pytest.mark.parametrize("method,service_name,service_method", [
    ("migrate_legacy_schedule", "_legacy_migrations", "commit"),
    ("migrate_group_analysis", "_scheduled_tasks", "migrate_group_analysis"),
])
def test_in_flight_migration_blocks_other_ui_actions_until_completion(api, method, service_name, service_method):
    entered = threading.Event()
    release = threading.Event()
    results = []

    def commit(_token):
        entered.set()
        assert release.wait(3)
        return {"pending": True}

    api._legacy_migrations = SimpleNamespace(cancel=lambda _key: pytest.fail("cancel raced"))
    service = getattr(api, service_name, SimpleNamespace())
    setattr(service, service_method, commit)
    setattr(api, service_name, service)
    worker = threading.Thread(target=lambda: results.append(getattr(api, method)("token")))
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


@pytest.mark.parametrize("gate", ["has_pending_migrations", "has_pending_group_analysis_migration"])
@pytest.mark.parametrize("failure", [None, OSError("private path"), ValueError("corrupt journal"), PayloadError("unsafe path"), sqlite3.DatabaseError("private database")])
def test_pending_or_unreadable_journal_blocks_start_and_releases_lock(api, monkeypatch, gate, failure):
    def pending(root):
        assert root == api._install_root
        assert api._action_lock.locked()
        if failure is not None:
            raise failure
        return True

    monkeypatch.setattr(app_shell, gate, pending)
    result = api.start_robot()
    assert result["data"]["accepted"] is False
    assert "迁移" in result["message"]
    assert "private path" not in str(result)
    assert not api._action_lock.locked()


def test_analysis_migration_uses_the_maintenance_lifecycle(api):
    calls = []
    api._lifecycle.stop_for_maintenance = lambda: calls.append("stop") or True
    api._lifecycle.maintenance_stopped = lambda: calls.append("stopped") or True
    service = api._scheduled_task_service()
    assert service.analysis_migration.stop() is True
    assert service.analysis_migration.stopped() is True
    assert calls == ["stop", "stopped"]
    assert api._scheduled_task_service() is service


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
