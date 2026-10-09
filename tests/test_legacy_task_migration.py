"""Legacy migrations must remain recoverable without ever enabling a delivery."""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from wechatrebot_manager import legacy_task_migration as migration_module
from wechatrebot_manager.legacy_task_migration import (
    LegacyTaskMigration,
    has_pending_migrations,
)
from wechatrebot_manager.schedules import ScheduleService
from wechatrebot_manager.task_store import TaskStore


ACCOUNT = "wxid_robot_a"
OTHER_ACCOUNT = "wxid_robot_b"
GROUP = "123456@chatroom"
SESSION = f"wechat:GroupMessage:{GROUP}"


@contextmanager
def connect_database(path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


class Runtime:
    """The stop acknowledgement also removes the previously live account hint."""

    def __init__(self):
        self.account = ACCOUNT
        self.allow_stop = True
        self.is_stopped = False
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if not self.allow_stop:
            return False
        self.is_stopped = True
        self.account = ""
        return True


class Installation:
    def __init__(self, root: Path):
        self.root = root
        self.runtime = Runtime()
        self.legacy = ScheduleService(root, account_hint=lambda: self.runtime.account)
        self.store = TaskStore(root / "data/manager/scheduled-tasks.db")
        self.migration = self.reopen()
        self.legacy.database.parent.mkdir(parents=True)
        with connect_database(self.legacy.database) as connection:
            # Use the real cron model's identifying, scheduling and history fields.
            # A simplified list-page fixture cannot prove safe source conversion.
            connection.executescript("""
                CREATE TABLE cron_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT, updated_at TEXT,
                    job_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    description TEXT, job_type TEXT NOT NULL,
                    cron_expression TEXT, timezone TEXT, payload TEXT NOT NULL,
                    enabled INTEGER NOT NULL, persistent INTEGER NOT NULL,
                    run_once INTEGER NOT NULL, status TEXT,
                    last_run_at TEXT, next_run_time TEXT, last_error TEXT
                );
                CREATE TABLE preserved_data (value TEXT);
                INSERT INTO preserved_data VALUES ('unrelated account configuration');
            """)

    def reopen(self):
        return LegacyTaskMigration(
            self.legacy, self.store, stop=self.runtime.stop,
            stopped=lambda: self.runtime.is_stopped,
        )

    def seed(self, action="weather", *, session=SESSION, days=(), enabled=True):
        extra = {"city": "青岛"} if action == "weather" else {"province": "四川"} if action == "oilprice" else {}
        if days:
            extra["day_of_week"] = ",".join(days)
        source = {"type": action, "session": session, "hour": 8, "minute": 30,
                  "extra": extra, "enabled": enabled}
        payload = {"account_id": ACCOUNT, "session": session,
                   **{key: value for key, value in extra.items() if key != "day_of_week"}}
        name = f"{action}_{session}"
        path = self.legacy._schedule_tasks_path()
        if action == "short_drama":
            source = {"id": "123456abcdef", "name": "午间影视", "session": session,
                      "media_type": "电视剧", "limit": 8, "hour": 8, "minute": 30,
                      "days": list(days), "enabled": enabled}
            payload = {"account_id": ACCOUNT, "task_id": source["id"]}
            name = "short_drama_recommend_" + source["id"]
            path = self.legacy._drama_tasks_path()
        job = {
            "job_id": f"job-{action}", "name": name, "description": "原任务说明",
            "job_type": "basic", "cron_expression": "30 8 * * " + (",".join(days) or "*"),
            "timezone": "Asia/Shanghai", "payload": json.dumps(payload, ensure_ascii=False),
            "enabled": int(enabled), "persistent": 1, "run_once": 0,
            "status": "scheduled" if enabled else "paused",
            "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:00+00:00",
            "last_run_at": "2026-10-01T00:30:00+00:00", "next_run_time": None,
            "last_error": "",
        }
        self.insert_job(job)
        self.source_path = path
        self.job_id = job["job_id"]
        self.job = job
        self.source = source
        self.write_source({"version": 7, "metadata": {"keep": "用户配置"}, "tasks": [source]})
        return job["job_id"]

    def insert_job(self, job):
        with connect_database(self.legacy.database) as connection:
            connection.execute(
                "INSERT INTO cron_jobs (" + ",".join(job) + ") VALUES (" + ",".join("?" for _ in job) + ")",
                tuple(job.values()),
            )

    def change_job(self, **updates):
        with connect_database(self.legacy.database) as connection:
            connection.execute("UPDATE cron_jobs SET " + ",".join(f"{key}=?" for key in updates) + " WHERE job_id=?",
                               (*updates.values(), self.job_id))

    def read_jobs(self):
        with connect_database(self.legacy.database) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM cron_jobs ORDER BY job_id")]

    def write_source(self, payload):
        self.source_path.parent.mkdir(parents=True, exist_ok=True)
        self.source_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def read_source(self):
        return json.loads(self.source_path.read_text(encoding="utf-8"))

    def journal_path(self, preview):
        return self.root / "data/manager/legacy-task-migrations" / (preview["key"] + ".json")

    def disk_snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def recover(self, preview):
        reopened = self.reopen()
        confirmation = reopened.preview("", recovery_key=preview["key"])
        return reopened, reopened.commit(confirmation["token"])


@pytest.fixture
def installation(tmp_path, monkeypatch):
    value = Installation(tmp_path / "installation")

    def unexpected_engine_call(*args, **kwargs):
        pytest.fail("Migration must not contact a live engine or send messages")

    monkeypatch.setattr(value.legacy.engine, "request", unexpected_engine_call)
    monkeypatch.setattr(value.legacy.engine, "is_available", unexpected_engine_call)
    return value


def assert_paused_once(value, result):
    task = result["item"]
    assert task["account_id"] == ACCOUNT
    assert task["draft"] is True and task["enabled"] is False
    assert task["next_run_time"] == "" and task["revision"] == 1
    assert value.store.list(ACCOUNT) == [task]
    assert value.store.runs(ACCOUNT, task["id"]) == []
    assert value.store.claim_due(datetime.now(timezone.utc) + timedelta(days=30), ACCOUNT) == []
    assert value.runtime.is_stopped
    assert not has_pending_migrations(value.root)


@pytest.mark.parametrize("failure", ["confirm", "prepare", "source", "database", "destination", "acknowledgement"])
def test_interrupted_transfer_recovers_without_duplicate_or_enabled_tasks(installation, monkeypatch, failure):
    value = installation
    value.seed()
    preview = value.migration.preview(value.job_id)
    original_write = migration_module.write_json_atomic
    original_save = value.store.save
    journal = value.journal_path(preview)

    def failing_write(path, payload):
        phase = payload.get("phase") if isinstance(payload, dict) else None
        if ((failure == "confirm" and path == journal and phase == "confirmed")
                or (failure == "prepare" and path == journal and phase == "prepared")
                or (failure == "source" and path == value.source_path)
                or (failure == "acknowledgement" and path == journal and phase == "complete")):
            raise OSError("injected atomic write failure")
        original_write(path, payload)

    def failing_save(*args, **kwargs):
        raise sqlite3.OperationalError("injected destination transaction failure")

    monkeypatch.setattr(migration_module, "write_json_atomic", failing_write)
    if failure == "destination":
        monkeypatch.setattr(value.store, "save", failing_save)
    if failure == "database":
        with connect_database(value.legacy.database) as connection:
            connection.execute("""CREATE TRIGGER fail_legacy_delete BEFORE DELETE ON cron_jobs
                BEGIN SELECT RAISE(ABORT, 'injected legacy transaction failure'); END""")

    with pytest.raises((OSError, sqlite3.Error), match="injected"):
        value.migration.commit(preview["token"])

    tasks = value.store.list(ACCOUNT)
    assert all(not task["enabled"] for task in tasks)
    assert len(tasks) == (1 if failure == "acknowledgement" else 0)
    if failure in {"confirm", "prepare", "source"}:
        assert value.read_source()["tasks"] == [value.source]
        assert len(value.read_jobs()) == 1
    elif failure == "database":
        assert value.read_source()["tasks"] == []
        assert len(value.read_jobs()) == 1  # JSON persisted, SQLite rolled back.
    else:
        assert value.read_source()["tasks"] == [] and value.read_jobs() == []
    if failure in {"source", "database", "destination", "acknowledgement"}:
        assert has_pending_migrations(value.root)
        with pytest.raises(ValueError, match="已经开始|不能丢弃"):
            value.migration.cancel(preview["key"])

    monkeypatch.setattr(migration_module, "write_json_atomic", original_write)
    monkeypatch.setattr(value.store, "save", original_save)
    if failure == "database":
        with connect_database(value.legacy.database) as connection:
            connection.execute("DROP TRIGGER fail_legacy_delete")
    if journal.exists():
        reopened, result = value.recover(preview)
    else:
        reopened = value.reopen()
        result = reopened.commit(reopened.preview(value.job_id)["token"])
    assert_paused_once(value, result)
    assert value.read_source()["tasks"] == [] and value.read_jobs() == []
    assert reopened.pending() == []


@pytest.mark.parametrize("later_action", ["edit", "delete"])
def test_missing_final_ack_preserves_later_user_edit_or_tombstone(installation, monkeypatch, later_action):
    value = installation
    value.seed()
    preview = value.migration.preview(value.job_id)
    original_write = migration_module.write_json_atomic

    def fail_completion(path, payload):
        if payload.get("phase") == "complete":
            raise OSError("completion acknowledgement lost")
        original_write(path, payload)

    monkeypatch.setattr(migration_module, "write_json_atomic", fail_completion)
    with pytest.raises(OSError, match="acknowledgement"):
        value.migration.commit(preview["token"])
    task = value.store.list(ACCOUNT)[0]
    if later_action == "edit":
        edited = value.store.save({**task, "name": "用户修改后的名称", "revision": task["revision"]})
    else:
        value.store.delete(ACCOUNT, task["id"], task["revision"])
    database_before = value.store.path.read_bytes()

    monkeypatch.setattr(migration_module, "write_json_atomic", original_write)
    _, result = value.recover(preview)

    assert value.store.path.read_bytes() == database_before
    assert not has_pending_migrations(value.root)
    if later_action == "edit":
        assert result["item"] == edited
    else:
        assert result["deleted"] is True and value.store.list(ACCOUNT) == []
    # Repeating the acknowledgement also preserves the current user decision.
    _, repeated = value.recover(preview)
    assert repeated == result


def test_original_confirmation_token_can_resume_after_old_records_are_removed(installation, monkeypatch):
    value = installation
    value.seed()
    preview = value.migration.preview(value.job_id)
    original_save = value.store.save

    def fail_save(*args, **kwargs):
        raise sqlite3.OperationalError("destination unavailable")

    monkeypatch.setattr(value.store, "save", fail_save)
    with pytest.raises(sqlite3.OperationalError, match="destination unavailable"):
        value.migration.commit(preview["token"])
    assert value.read_source()["tasks"] == [] and value.read_jobs() == []
    monkeypatch.setattr(value.store, "save", original_save)

    result = value.migration.commit(preview["token"])

    assert_paused_once(value, result)


@pytest.mark.parametrize("action,session,days,expected", [
    ("weather", SESSION, (), {"city": "青岛", "day": "today", "output": "image"}),
    ("news", "wechat:FriendMessage:wxid_friend", (), {"count": 20}),
    ("oilprice", SESSION, (), {"province": "四川"}),
    ("kfc", SESSION, ("thu",), {}),
    ("short_drama", SESSION, ("mon", "fri"), {"media_type": "电视剧", "limit": 8}),
])
def test_preview_is_read_only_and_supported_content_keeps_target_time_and_parameters(
    installation, action, session, days, expected,
):
    value = installation
    value.seed(action, session=session, days=days)
    before = value.disk_snapshot()

    preview = value.migration.preview(value.job_id)

    assert value.disk_snapshot() == before
    assert value.runtime.stop_calls == 0
    assert preview["action"] == action and preview["arguments"] == expected
    assert preview["previously_enabled"] is True
    result = value.migration.commit(preview["token"])
    assert_paused_once(value, result)
    task = result["item"]
    assert task["arguments"] == expected
    assert task["targets"] == [{"type": "private" if "FriendMessage" in session else "group",
                                "id": session.rsplit(":", 1)[-1]}]
    assert task["schedule"]["time"] == "08:30"
    assert task["schedule"]["timezone"] == "Asia/Shanghai"
    if action == "kfc":
        assert task["schedule"]["weekdays"] == [3]
    elif action == "short_drama":
        assert task["schedule"]["weekdays"] == [0, 4]
    else:
        assert task["schedule"]["kind"] == "daily"


@pytest.mark.parametrize("problem", [
    "unassigned", "other_account", "source_owner", "target_mismatch", "target_without_umo",
    "group_with_private_id", "private_with_group_id", "timezone", "active_agent", "run_once",
    "time_mismatch", "numeric_weekday", "enabled_mismatch", "missing_city", "unsupported_subscription",
])
def test_ambiguous_ownership_target_or_schedule_never_stops_or_changes_data(installation, problem):
    value = installation
    value.seed()
    source = value.read_source()
    task = source["tasks"][0]
    payload = json.loads(value.job["payload"])
    updates = {}
    if problem == "unassigned":
        payload.pop("account_id")
    elif problem == "other_account":
        payload["account_id"] = OTHER_ACCOUNT
    elif problem == "source_owner":
        task["account_id"] = OTHER_ACCOUNT
    elif problem == "target_mismatch":
        payload["session"] = "wechat:GroupMessage:other@chatroom"
    elif problem in {"target_without_umo", "group_with_private_id", "private_with_group_id"}:
        task["session"] = {
            "target_without_umo": GROUP,
            "group_with_private_id": "wechat:GroupMessage:wxid_friend",
            "private_with_group_id": f"wechat:FriendMessage:{GROUP}",
        }[problem]
        payload["session"] = task["session"]
        updates["name"] = "weather_" + task["session"]
    elif problem == "timezone":
        updates["timezone"] = "UTC"
    elif problem == "active_agent":
        updates["job_type"] = "active_agent"
    elif problem == "run_once":
        updates["run_once"] = 1
    elif problem == "time_mismatch":
        updates["cron_expression"] = "15 9 * * *"
    elif problem == "numeric_weekday":
        task["extra"]["day_of_week"] = "0"
        updates["cron_expression"] = "30 8 * * 0"
    elif problem == "enabled_mismatch":
        updates["enabled"] = 0
    elif problem == "missing_city":
        payload["city"] = task["extra"]["city"] = ""
    else:
        task.update(type="bili_dynamic", extra={"uid": "42", "interval": 5})
        updates.update(name=f"bili_{SESSION}_42", cron_expression="*/5 * * * *")
    updates["payload"] = json.dumps(payload, ensure_ascii=False)
    value.change_job(**updates)
    value.write_source(source)
    before = value.disk_snapshot()

    with pytest.raises(ValueError):
        value.migration.preview(value.job_id)

    assert value.disk_snapshot() == before and value.runtime.stop_calls == 0


@pytest.mark.parametrize("duplicate", ["source", "same_account", "other_account", "unassigned"])
def test_shared_source_is_not_claimed_from_duplicate_jobs(installation, duplicate):
    value = installation
    value.seed()
    if duplicate == "source":
        source = value.read_source()
        source["tasks"].append(deepcopy(source["tasks"][0]))
        value.write_source(source)
    else:
        payload = json.loads(value.job["payload"])
        if duplicate == "other_account":
            payload["account_id"] = OTHER_ACCOUNT
        elif duplicate == "unassigned":
            payload.pop("account_id")
        value.insert_job({**value.job, "job_id": "duplicated-job", "payload": json.dumps(payload)})
    before = value.disk_snapshot()

    with pytest.raises(ValueError, match="多条|唯一"):
        value.migration.preview(value.job_id)

    assert value.disk_snapshot() == before and value.runtime.stop_calls == 0


def test_account_switch_invalidates_confirmation_before_any_stop_or_journal(installation):
    value = installation
    value.seed()
    preview = value.migration.preview(value.job_id)
    before = value.disk_snapshot()
    value.runtime.account = OTHER_ACCOUNT

    with pytest.raises(ValueError, match="账号"):
        value.migration.commit(preview["token"])

    assert value.disk_snapshot() == before and value.runtime.stop_calls == 0


def test_account_switch_during_stop_prevents_any_legacy_cleanup(installation, monkeypatch):
    value = installation
    value.seed()
    source_before, jobs_before = value.source_path.read_bytes(), value.read_jobs()

    def stop_with_account_switch():
        value.runtime.stop()
        value.runtime.account = OTHER_ACCOUNT
        return True

    monkeypatch.setattr(value.migration, "stop", stop_with_account_switch)
    preview = value.migration.preview(value.job_id)
    with pytest.raises(ValueError, match="账号"):
        value.migration.commit(preview["token"])

    assert value.source_path.read_bytes() == source_before and value.read_jobs() == jobs_before
    assert not value.store.path.exists() and not has_pending_migrations(value.root)


def test_stopped_evidence_is_rechecked_after_opening_the_write_transaction(installation, monkeypatch):
    value = installation
    value.seed()
    source_before, jobs_before = value.source_path.read_bytes(), value.read_jobs()
    original_connect = sqlite3.connect

    def connection_after_runtime_resumes(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if isinstance(database, str) and database.endswith("?mode=rw"):
            value.runtime.is_stopped = False
        return connection

    preview = value.migration.preview(value.job_id)
    monkeypatch.setattr(migration_module.sqlite3, "connect", connection_after_runtime_resumes)
    with pytest.raises(ValueError):
        value.migration.commit(preview["token"])

    assert value.source_path.read_bytes() == source_before and value.read_jobs() == jobs_before
    assert not value.store.path.exists() and not has_pending_migrations(value.root)
    value.runtime.is_stopped = True
    assert value.migration.cancel(preview["key"])["cancelled"]


def test_unfinished_stop_can_be_cancelled_without_changing_the_original_task(installation):
    value = installation
    value.seed(enabled=False)
    before = value.disk_snapshot()
    value.runtime.allow_stop = False
    preview = value.migration.preview(value.job_id)

    assert value.migration.commit(preview["token"])["pending"] is True
    assert value.migration.pending()[0]["can_cancel"] is True
    assert not has_pending_migrations(value.root)  # No destructive phase has begun.
    assert value.migration.cancel(preview["key"])["cancelled"] is True

    assert value.disk_snapshot() == before
    assert value.migration.pending() == []
    with pytest.raises(ValueError, match="重新预览"):
        value.migration.commit(preview["token"])


def test_new_preview_refreshes_stale_confirmation_while_stopped(installation):
    value = installation
    value.seed()
    value.runtime.allow_stop = False
    preview = value.migration.preview(value.job_id)
    assert value.migration.commit(preview["token"])["pending"]
    source = value.read_source()
    source["tasks"][0]["hour"] = 9
    value.write_source(source)
    value.change_job(cron_expression="30 9 * * *")
    value.runtime.allow_stop = True
    value.runtime.stop()

    reopened = value.reopen()
    refreshed = reopened.preview("", recovery_key=preview["key"])
    assert refreshed["schedule_text"] == "每天 09:30"
    result = reopened.commit(refreshed["token"])

    assert_paused_once(value, result)
    assert result["item"]["schedule"]["time"] == "09:30"


def test_stop_acknowledgement_without_stopped_evidence_cannot_transfer(installation, monkeypatch):
    value = installation
    value.seed()
    source_before, jobs_before = value.source_path.read_bytes(), value.read_jobs()
    monkeypatch.setattr(value.migration, "stop", lambda: True)
    preview = value.migration.preview(value.job_id)

    assert value.migration.commit(preview["token"])["pending"] is True

    assert value.source_path.read_bytes() == source_before and value.read_jobs() == jobs_before
    assert not value.store.path.exists()


@pytest.mark.parametrize("change", [{"name": "renamed_during_stop"}, {"job_id": "replaced-during-stop"}])
def test_id_or_name_change_during_stop_is_detected_before_source_removal(installation, monkeypatch, change):
    value = installation
    value.seed()
    source_before = value.source_path.read_bytes()

    def stop_with_change():
        value.change_job(**change)
        return value.runtime.stop()

    monkeypatch.setattr(value.migration, "stop", stop_with_change)
    preview = value.migration.preview(value.job_id)
    with pytest.raises(ValueError, match="改变"):
        value.migration.commit(preview["token"])

    assert value.source_path.read_bytes() == source_before and not value.store.path.exists()
    assert value.migration.cancel(preview["key"])["cancelled"]
    assert all(value.read_jobs()[0][key] == changed for key, changed in change.items())


def test_database_disappearing_during_stop_is_not_recreated_or_used_to_delete_source(installation, monkeypatch):
    value = installation
    value.seed()
    source_before = value.source_path.read_bytes()

    def stop_without_database():
        value.legacy.database.unlink()
        return value.runtime.stop()

    monkeypatch.setattr(value.migration, "stop", stop_without_database)
    preview = value.migration.preview(value.job_id)
    with pytest.raises(sqlite3.OperationalError):
        value.migration.commit(preview["token"])

    assert not value.legacy.database.exists()
    assert value.source_path.read_bytes() == source_before and not value.store.path.exists()
    assert value.migration.cancel(preview["key"])["cancelled"]


@pytest.mark.parametrize("owner", ["same_account", "other_account", "deleted"])
def test_destination_collision_is_rejected_before_legacy_cleanup(installation, owner):
    value = installation
    value.seed()
    value.runtime.allow_stop = False
    preview = value.migration.preview(value.job_id)
    assert value.migration.commit(preview["token"])["pending"]
    plan = json.loads(value.journal_path(preview).read_text(encoding="utf-8"))["plan"]
    account = OTHER_ACCOUNT if owner == "other_account" else ACCOUNT
    occupied = value.store.save({**plan["task"], "account_id": account, "name": "先前由用户保存的任务"})
    if owner == "deleted":
        value.store.delete(account, occupied["id"], occupied["revision"])
    before = value.disk_snapshot()
    value.runtime.allow_stop = True

    with pytest.raises(ValueError, match="标识已被.*使用"):
        value.migration.commit(preview["token"])

    assert value.disk_snapshot() == before
    if owner == "deleted":
        assert value.store.list(account) == []
    else:
        assert value.store.get(account, occupied["id"]) == occupied
    assert value.runtime.stop_calls == 1


def test_existing_empty_destination_database_can_receive_a_paused_task(installation):
    value = installation
    value.seed()
    value.store.path.parent.mkdir(parents=True)
    with connect_database(value.store.path) as connection:
        connection.execute("CREATE TABLE unrelated_setting (value TEXT)")
        connection.execute("INSERT INTO unrelated_setting VALUES ('keep')")

    preview = value.migration.preview(value.job_id)
    result = value.migration.commit(preview["token"])

    assert_paused_once(value, result)
    with connect_database(value.store.path) as connection:
        assert connection.execute("SELECT value FROM unrelated_setting").fetchall() == [("keep",)]


@pytest.mark.parametrize("tampering", ["owner", "enable"])
def test_self_consistent_journal_hash_cannot_change_owner_or_enable_task(installation, tampering):
    value = installation
    value.seed()
    value.runtime.allow_stop = False
    preview = value.migration.preview(value.job_id)
    value.migration.commit(preview["token"])
    record = json.loads(value.journal_path(preview).read_text(encoding="utf-8"))
    if tampering == "owner":
        record["plan"]["account_id"] = OTHER_ACCOUNT
        record["key"] = migration_module._digest([OTHER_ACCOUNT, value.job_id])
        value.runtime.account = OTHER_ACCOUNT
    else:
        record["plan"]["task"].update(enabled=True, draft=False)
    record["fingerprint"] = migration_module._digest(record["plan"])
    path = value.journal_path(record)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    before = value.disk_snapshot()

    with pytest.raises(ValueError, match="账号|任务内容"):
        value.reopen().preview("", recovery_key=record["key"])

    assert value.disk_snapshot() == before and value.runtime.stop_calls == 1


def test_complete_label_does_not_allow_a_corrupt_journal_to_bypass_startup_gate(installation):
    value = installation
    value.seed()
    value.runtime.allow_stop = False
    preview = value.migration.preview(value.job_id)
    value.migration.commit(preview["token"])
    path = value.journal_path(preview)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["phase"] = "complete"
    record["plan"]["job"] = []
    record["fingerprint"] = migration_module._digest(record["plan"])
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="校验失败"):
        has_pending_migrations(value.root)


def test_missing_cron_metadata_is_rejected_without_guessing_defaults(installation):
    value = installation
    value.seed()
    with connect_database(value.legacy.database) as connection:
        connection.execute("ALTER TABLE cron_jobs DROP COLUMN timezone")
    before = value.disk_snapshot()

    with pytest.raises(ValueError, match="缺少类型或时区"):
        value.migration.preview(value.job_id)

    assert value.disk_snapshot() == before and value.runtime.stop_calls == 0


@pytest.mark.parametrize("target", ["source", "database", "destination", "journal_key"])
def test_migration_paths_cannot_escape_the_installation(installation, tmp_path, monkeypatch, target):
    value = installation
    value.seed()
    outside = tmp_path / "outside.json"
    outside.write_text('{"keep": "outside data"}', encoding="utf-8")
    before = value.disk_snapshot()
    if target == "source":
        monkeypatch.setattr(value.legacy, "_schedule_tasks_path", lambda: outside)
    elif target == "database":
        value.legacy.database = outside
    elif target == "destination":
        value.store.path = outside

    with pytest.raises(ValueError):
        if target == "journal_key":
            value.migration.preview("", recovery_key="../../outside")
        else:
            confirmation = value.migration.preview(value.job_id)
            value.migration.commit(confirmation["token"])

    assert value.disk_snapshot() == before and value.runtime.stop_calls == 0
    assert outside.read_text(encoding="utf-8") == '{"keep": "outside data"}'


def test_exact_transfer_preserves_unrelated_source_metadata_jobs_and_tables(installation):
    value = installation
    value.seed()
    other_source = {"type": "bili_dynamic", "session": "wechat:GroupMessage:other@chatroom",
                    "hour": 0, "minute": 0, "extra": {"uid": "777", "interval": 5}}
    source = value.read_source()
    malformed_unrelated = {"type": "bili_dynamic", "session": "other:FriendMessage:wxid_old", "extra": None}
    source["tasks"].extend([other_source, malformed_unrelated])
    value.write_source(source)
    other_job = {**value.job, "job_id": "keep-unknown-job", "name": "bili_other_777",
                 "payload": "{}", "last_error": "保留原有执行记录"}
    value.insert_job(other_job)
    jobs_before = [row for row in value.read_jobs() if row["job_id"] == other_job["job_id"]]

    confirmation = value.migration.preview(value.job_id)
    result = value.migration.commit(confirmation["token"])

    assert_paused_once(value, result)
    assert value.read_source() == {**source, "tasks": [other_source, malformed_unrelated]}
    assert value.read_jobs() == jobs_before
    with connect_database(value.legacy.database) as connection:
        assert connection.execute("SELECT value FROM preserved_data").fetchall() == [("unrelated account configuration",)]
