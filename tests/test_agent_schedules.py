"""Native AI plans stay separate from fixed-content tasks and robot accounts."""
import json
import sqlite3

from test_schedules import _database
from wechatrebot_manager.schedules import ScheduleService


ACCOUNT = "wxid_robot_a"


def service(tmp_path, monkeypatch):
    database, _ = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE cron_jobs ADD COLUMN job_type TEXT DEFAULT 'basic'")
        connection.execute("ALTER TABLE cron_jobs ADD COLUMN timezone TEXT")
        connection.execute("ALTER TABLE cron_jobs ADD COLUMN run_once INTEGER DEFAULT 0")
    value = ScheduleService(tmp_path, account_hint=lambda: ACCOUNT)
    monkeypatch.setattr(value.engine, "is_available", lambda: False)
    return value


def add(value, job_id="native", owner=ACCOUNT):
    payload = {"session": "source:GroupMessage:123@chatroom", "sender_id": "wxid_user",
               "note": "查询青岛当天的天气，再发到本群。", "origin": "tool"}
    if owner:
        payload["account_id"] = owner
    with value._connect() as connection:
        connection.execute("""INSERT INTO cron_jobs
            (job_id,name,description,cron_expression,payload,enabled,status,next_run_time,job_type,run_once)
            VALUES(?,?,?,?,?,1,'scheduled',?,'active_agent',0)""",
            (job_id, "weather_青岛每日天气预报", payload["note"], "30 8 * * *",
             json.dumps(payload), "2026-11-14 00:30:00.000000"))


def test_native_list_only_shows_current_account_and_hides_missing_owner(tmp_path, monkeypatch):
    value = service(tmp_path, monkeypatch)
    add(value)
    add(value, "other", "wxid_robot_b")
    add(value, "old", None)
    result = value.list()
    assert result["account_id"] == ACCOUNT
    assert [item["id"] for item in result["items"]] == ["job-weather"]
    assert [item["id"] for item in result["agent_items"]] == ["native"]
    assert result["unassigned_count"] == 0 and not result["message"]
    agent = result["agent_items"][0]
    assert agent["action"] == "agent"
    assert agent["schedule_text"] == "每天 08:30"
    assert agent["next_run_time"] == "2026-11-14T00:30:00+00:00"
    with value._connect() as connection:
        assert connection.execute("SELECT enabled FROM cron_jobs WHERE job_id='old'").fetchone()[0] == 1
    value._account_hint = lambda: "wxid_robot_b"
    assert [item["id"] for item in value.list()["agent_items"]] == ["other"]
    value._account_hint = lambda: ""
    empty = value.list()
    assert empty["items"] == [] and not empty.get("agent_items")


def test_native_label_matching_plugin_prefix_never_modifies_plugin_source(tmp_path, monkeypatch):
    value = service(tmp_path, monkeypatch)
    add(value)
    path = value._schedule_tasks_path()
    before = path.read_bytes()
    value.set_enabled("native", False)
    value.update_time("native", "09:25")
    assert value.list()["agent_items"][0]["cron_expression"] == "25 9 * * *"
    value.delete("native")
    assert path.read_bytes() == before
    assert value.list()["agent_items"] == []
