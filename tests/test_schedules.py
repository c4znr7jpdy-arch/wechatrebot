import json
import sqlite3
import pytest
from pathlib import Path

from wechatrebot_manager.schedules import ScheduleService
from wechatrebot_manager.plugin_center import PluginCenterError


def _database(root: Path) -> tuple[Path, str]:
    data = root / "app" / "astrbot" / "data"
    data.mkdir(parents=True)
    path = data / "data_v4.db"
    job_id = "job-weather"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE cron_jobs (
                job_id TEXT PRIMARY KEY, name TEXT, description TEXT,
                cron_expression TEXT, payload TEXT, enabled INTEGER,
                status TEXT, last_run_at TEXT, next_run_time TEXT, last_error TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO cron_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                "weather_source:GroupMessage:123@chatroom",
                "天气预报 定时推送",
                "0 8 * * *",
                json.dumps({"session": "source:GroupMessage:123@chatroom"}),
                1,
                "scheduled",
                None,
                "2026-09-04 00:00:00",
                None,
            ),
        )
    tasks = data / "jiang_schedule" / "tasks.json"
    tasks.parent.mkdir()
    tasks.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "type": "weather",
                        "session": "source:GroupMessage:123@chatroom",
                        "hour": 8,
                        "minute": 0,
                        "extra": {"city": "青岛"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path, job_id


def test_schedule_list_and_offline_pause_persist_to_plugin_source(
    tmp_path: Path, monkeypatch
) -> None:
    _, job_id = _database(tmp_path)
    service = ScheduleService(tmp_path)
    monkeypatch.setattr(service.engine, "is_available", lambda: False)

    listed = service.list()
    result = service.set_enabled(job_id, False)

    assert listed["items"][0]["name"] == "天气预报"
    assert listed["items"][0]["schedule_text"] == "每天 08:00"
    assert result["enabled"] is False
    tasks = json.loads(service._schedule_tasks_path().read_text(encoding="utf-8"))
    assert tasks["tasks"][0]["enabled"] is False
    assert service.list()["paused"] == 1


def test_schedule_update_time_and_delete_offline(
    tmp_path: Path, monkeypatch
) -> None:
    _, job_id = _database(tmp_path)
    service = ScheduleService(tmp_path)
    monkeypatch.setattr(service.engine, "is_available", lambda: False)

    service.update_time(job_id, "09:35")
    assert service.list()["items"][0]["cron_expression"] == "35 9 * * *"
    service.delete(job_id)

    assert service.list()["items"] == []
    tasks = json.loads(service._schedule_tasks_path().read_text(encoding="utf-8"))
    assert tasks["tasks"] == []


def _checkin_database(root: Path) -> tuple[Path, str]:
    database, _ = _database(root)
    job_id = "job-checkin"
    session = "source:GroupMessage:456@chatroom"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cron_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                f"checkin_{session}",
                "每日签到 定时推送",
                "30 8 * * *",
                json.dumps({"session": session}),
                1,
                "scheduled",
                None,
                "2026-09-11 00:30:00",
                None,
            ),
        )
    tasks_path = root / "app" / "astrbot" / "data" / "jiang_schedule" / "tasks.json"
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    tasks["tasks"].append(
        {
            "type": "checkin",
            "session": session,
            "hour": 8,
            "minute": 30,
            "extra": {},
        }
    )
    tasks_path.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
    checkin_config = tasks_path.with_name("checkin_config.json")
    checkin_config.write_text(
        json.dumps(
            {
                "version": 1,
                "schedule": {
                    "enabled": True,
                    "time": "08:30",
                    "session": session,
                },
                "accounts": [{"id": "site-1", "name": "站点一"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return database, job_id


def test_checkin_changes_persist_to_own_config_across_restart(
    tmp_path: Path, monkeypatch
) -> None:
    _, job_id = _checkin_database(tmp_path)
    service = ScheduleService(tmp_path)
    monkeypatch.setattr(service.engine, "is_available", lambda: False)

    service.update_time(job_id, "09:35")
    service.set_enabled(job_id, False)

    config = json.loads(service._checkin_config_path().read_text(encoding="utf-8"))
    assert config["schedule"]["time"] == "09:35"
    assert config["schedule"]["enabled"] is False
    assert config["accounts"] == [{"id": "site-1", "name": "站点一"}]


def test_checkin_delete_disables_source_so_restart_does_not_restore_it(
    tmp_path: Path, monkeypatch
) -> None:
    _, job_id = _checkin_database(tmp_path)
    service = ScheduleService(tmp_path)
    monkeypatch.setattr(service.engine, "is_available", lambda: False)

    service.delete(job_id)

    config = json.loads(service._checkin_config_path().read_text(encoding="utf-8"))
    tasks = json.loads(service._schedule_tasks_path().read_text(encoding="utf-8"))
    assert config["schedule"]["enabled"] is False
    assert not any(task.get("type") == "checkin" for task in tasks["tasks"])
    assert service.list()["items"][0]["id"] == "job-weather"


def test_obsolete_targetless_default_checkin_is_removed_permanently(
    tmp_path: Path, monkeypatch
) -> None:
    database, _ = _database(tmp_path)
    data = database.parent
    tasks_path = data / "jiang_schedule" / "tasks.json"
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    tasks["tasks"].append(
        {
            "type": "checkin",
            "session": "",
            "hour": 8,
            "minute": 30,
            "extra": {},
        }
    )
    tasks_path.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
    checkin_config = tasks_path.with_name("checkin_config.json")
    checkin_config.write_text(
        json.dumps(
            {
                "version": 1,
                "schedule": {"enabled": True, "time": "08:30", "session": ""},
                "accounts": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cron_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-default-checkin",
                "checkin_",
                "每日签到 定时推送",
                "30 8 * * *",
                json.dumps({"session": ""}),
                1,
                "scheduled",
                None,
                "2026-09-12 00:30:00",
                None,
            ),
        )

    service = ScheduleService(tmp_path)
    monkeypatch.setattr(service.engine, "is_available", lambda: False)

    listed = service.list()

    assert [item["id"] for item in listed["items"]] == ["job-weather"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM cron_jobs WHERE job_id = 'job-default-checkin'"
        ).fetchone() is None
    config = json.loads(checkin_config.read_text(encoding="utf-8"))
    persisted_tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert config["schedule"]["enabled"] is False
    assert not any(task.get("type") == "checkin" for task in persisted_tasks["tasks"])


def test_removed_plugin_schedule_is_hidden_from_updated_install(tmp_path: Path) -> None:
    database, _ = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cron_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-legacy-checkin",
                "yanyun_checkin_daily",
                "旧版本签到任务",
                "0 7 * * *",
                "{}",
                1,
                "scheduled",
                None,
                "2026-09-10 07:00:00",
                None,
            ),
        )

    listed = ScheduleService(tmp_path).list()

    assert [item["id"] for item in listed["items"]] == ["job-weather"]
    assert listed["running"] == 1


@pytest.mark.parametrize("action", ["time", "toggle", "delete"])
def test_online_schedule_error_never_falls_back_to_silent_disk_edit(tmp_path, monkeypatch, action):
    _, job_id = _database(tmp_path)
    service = ScheduleService(tmp_path)
    before = service._schedule_tasks_path().read_bytes()
    monkeypatch.setattr(service.engine, "is_available", lambda: True)
    def fail(*_args, **_kwargs):
        raise PluginCenterError("调度器未接受修改")
    monkeypatch.setattr(service.engine, "request", fail)
    with pytest.raises(PluginCenterError):
        if action == "time":
            service.update_time(job_id, "09:30")
        elif action == "toggle":
            service.set_enabled(job_id, False)
        else:
            service.delete(job_id)
    assert service._schedule_tasks_path().read_bytes() == before
    assert service.list()["items"][0]["cron_expression"] == "0 8 * * *"
