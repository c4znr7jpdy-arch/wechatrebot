import json
import sqlite3
from pathlib import Path

from wechatrebot_manager.schedules import ScheduleService


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
