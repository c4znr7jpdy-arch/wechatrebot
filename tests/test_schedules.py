import json
import sqlite3
import pytest
from pathlib import Path

from wechatrebot_manager.schedules import ScheduleService
from wechatrebot_manager.plugin_center import PluginCenterError


@pytest.fixture(autouse=True)
def current_account(monkeypatch):
    monkeypatch.setattr(ScheduleService, "_runtime_account", lambda self: "wxid_robot_a")


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
                json.dumps({"account_id": "wxid_robot_a", "session": "source:GroupMessage:123@chatroom"}),
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
                json.dumps({"account_id": "wxid_robot_a", "session": session}),
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
                json.dumps({"account_id": "wxid_robot_a", "session": ""}),
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


@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("action", ["time", "toggle", "delete"])
@pytest.mark.parametrize("corrupt", ['{"tasks": [', '[]', '{"tasks": {}}', '{"tasks": [null]}'])
def test_invalid_source_is_not_overwritten_or_submitted(tmp_path, monkeypatch, online, action, corrupt):
    database, job_id = _database(tmp_path)
    service = ScheduleService(tmp_path)
    source = service._schedule_tasks_path()
    source.write_text(corrupt, encoding="utf-8")
    calls = []
    monkeypatch.setattr(service.engine, "is_available", lambda: online)
    monkeypatch.setattr(service.engine, "request", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="定时任务.*配置"):
        if action == "time":
            service.update_time(job_id, "09:30")
        elif action == "toggle":
            service.set_enabled(job_id, False)
        else:
            service.delete(job_id)
    assert source.read_text(encoding="utf-8") == corrupt
    assert calls == []
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT enabled, cron_expression FROM cron_jobs WHERE job_id = ?", (job_id,)
        ).fetchone() == (1, "0 8 * * *")


def test_list_with_engine_database_before_scheduler_initialization(tmp_path):
    service = ScheduleService(tmp_path)
    service.database.parent.mkdir(parents=True)
    with sqlite3.connect(service.database) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT)")
    assert service.list() == {"items": [], "running": 0, "paused": 0}


def test_offline_time_change_invalidates_old_next_run_time(tmp_path, monkeypatch):
    _database(tmp_path)
    service = ScheduleService(tmp_path)
    monkeypatch.setattr(service.engine, "is_available", lambda: False)
    service.update_time("job-weather", "09:30")
    assert service.list()["items"][0]["next_run_time"] == ""


def test_obsolete_checkin_cleanup_preserves_current_authorized_schedule(tmp_path, monkeypatch):
    database, _ = _checkin_database(tmp_path)
    service = ScheduleService(tmp_path)
    original = service._checkin_config_path().read_bytes()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cron_jobs (job_id, name, payload, enabled) VALUES (?, ?, ?, ?)",
            ("obsolete", "checkin_", '{"session": ""}', 1),
        )
    monkeypatch.setattr(service.engine, "is_available", lambda: False)
    assert {item["id"] for item in service.list()["items"]} == {"job-weather", "job-checkin"}
    assert service._checkin_config_path().read_bytes() == original


@pytest.mark.parametrize("owner", [None, "wxid_other"])
@pytest.mark.parametrize("action", ["enable", "time", "delete"])
def test_unassigned_and_other_account_jobs_cannot_be_changed(tmp_path, monkeypatch, owner, action):
    database, job_id = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        payload = {"session": "source:GroupMessage:123@chatroom"}
        if owner:
            payload["account_id"] = owner
        connection.execute("UPDATE cron_jobs SET payload=? WHERE job_id=?", (json.dumps(payload), job_id))
    service = ScheduleService(tmp_path)
    source_before = service._schedule_tasks_path().read_bytes()
    database_before = database.read_bytes()
    monkeypatch.setattr(service.engine, "is_available", lambda: pytest.fail("Unauthorized job probed engine"))
    result = service.list()
    assert result["items"] == []
    assert result["unassigned_count"] == (1 if owner is None else 0)
    with pytest.raises(ValueError, match="账号"):
        if action == "enable":
            service.set_enabled(job_id, False)
        elif action == "time":
            service.update_time(job_id, "09:30")
        else:
            service.delete(job_id)
    assert database.read_bytes() == database_before
    assert service._schedule_tasks_path().read_bytes() == source_before


def test_account_change_during_engine_probe_blocks_task_write(tmp_path, monkeypatch):
    database, job_id = _database(tmp_path)
    account = ["wxid_robot_a"]
    service = ScheduleService(tmp_path, account_hint=lambda: account[0])
    before = service._schedule_tasks_path().read_bytes()

    def available():
        account[0] = "wxid_robot_b"
        return True

    monkeypatch.setattr(service.engine, "is_available", available)
    monkeypatch.setattr(service.engine, "request", lambda *args: pytest.fail("Changed account reached engine write"))
    with pytest.raises(ValueError, match="当前微信账号"):
        service.set_enabled(job_id, False)
    assert service._schedule_tasks_path().read_bytes() == before
    assert service.list()["items"] == []
    account[0] = ""
    assert service.list()["requires_login"] is True


def test_unassigned_obsolete_job_is_not_deleted_on_read(tmp_path, monkeypatch):
    database, _ = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO cron_jobs(job_id,name,payload,enabled) VALUES(?,?,?,?)",
                           ("unknown", "checkin_", '{"session":""}', 1))
    service = ScheduleService(tmp_path)
    monkeypatch.setattr(service.engine, "is_available", lambda: pytest.fail("Unassigned cleanup reached engine"))
    assert service.list()["unassigned_count"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT enabled FROM cron_jobs WHERE job_id='unknown'").fetchone() == (1,)
