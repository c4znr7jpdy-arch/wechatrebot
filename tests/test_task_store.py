from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wechatrebot_manager.task_store import (
    TaskStore,
    inspect_scheduled_command,
    parse_command,
    scheduled_command_catalog,
)


ACCOUNT = "wxid_robot"


def task(*, task_id="task-1", targets=None, enabled=True, draft=False):
    return {
        "id": task_id,
        "account_id": ACCOUNT,
        "name": "早间提醒",
        "action": "text",
        "arguments": {"text": "记得带伞"},
        "targets": targets or [{"type": "group", "id": "123456@chatroom"}],
        "schedule": {"kind": "daily", "time": "08:30", "timezone": "Asia/Shanghai"},
        "enabled": enabled,
        "draft": draft,
    }


def test_empty_store_never_creates_or_seeds_tasks(tmp_path: Path) -> None:
    path = tmp_path / "scheduled-tasks.db"
    store = TaskStore(path)

    assert store.list(ACCOUNT) == []
    assert not path.exists()


def test_commands_are_parsed_to_typed_arguments() -> None:
    assert parse_command("/青岛天气 明天") == {
        "action": "weather",
        "arguments": {"city": "青岛", "day": "tomorrow", "output": "image"},
    }
    assert parse_command("资讯 6") == {"action": "news", "arguments": {"count": 6}}
    assert parse_command("提醒 下午三点交周报") == {
        "action": "text", "arguments": {"text": "下午三点交周报"}
    }
    assert parse_command("短剧推荐") == {
        "action": "short_drama",
        "arguments": {"media_type": "短剧", "limit": 12},
    }


def test_scheduled_command_inspection_explains_supported_and_blocked_commands() -> None:
    supported = inspect_scheduled_command("/青岛天气 明天")
    assert supported["status"] == "supported"
    assert supported["action"] == "weather"
    assert supported["definition"]["recipient_types"] == ["group", "private"]
    assert supported["definition"]["external_write"] is False

    checkin = inspect_scheduled_command("/签到")
    assert checkin["status"] == "needs_condition"
    assert "授权" in checkin["reason"]
    assert checkin["alternative"]

    contextual = inspect_scheduled_command("查询上次会议")
    assert contextual["status"] == "unsupported"
    assert "上下文" in contextual["reason"]
    assert contextual["alternative"]


def test_scheduled_command_catalog_is_json_safe_and_does_not_expose_internal_modules() -> None:
    catalog = scheduled_command_catalog()
    assert {entry["status"] for entry in catalog} == {"supported", "needs_condition", "unsupported"}
    assert all(isinstance(entry["parameters"], list) for entry in catalog)
    serialized = str(catalog)
    assert "jiang_" not in serialized
    catalog[0]["parameters"].append("测试")
    assert "测试" not in scheduled_command_catalog()[0]["parameters"]


def test_task_snapshot_keeps_command_definition_version(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "scheduled-tasks.db")
    saved = store.save(task())

    assert saved["definition_version"] == 1

    legacy = store.get(ACCOUNT, saved["id"])
    legacy["definition_version"] = 0
    legacy["revision"] = saved["revision"]
    with pytest.raises(ValueError, match="命令定义版本"):
        store.save(legacy)


def test_delete_is_a_persistent_tombstone_and_cannot_be_resurrected(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "scheduled-tasks.db")
    saved = store.save(task())

    store.delete(ACCOUNT, saved["id"], saved["revision"])

    assert store.list(ACCOUNT) == []
    with pytest.raises(ValueError, match="已删除|不属于"):
        store.save({**task(), "revision": saved["revision"]})


def test_test_task_namespace_is_reserved_and_recovered_without_future_send(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    store = TaskStore(tmp_path / "scheduled-tasks.db")
    test_task = task(task_id="test_interrupted")

    with pytest.raises(ValueError, match="保留前缀"):
        store.save(test_task, now=now)

    saved = store.save(test_task, now=now, _allow_test=True)
    run = store.create_run_now(ACCOUNT, saved["id"], test_task["targets"][0], now=now)
    assert store.list(ACCOUNT) == []
    assert store.get(ACCOUNT, saved["id"])["next_run_time"] == ""

    store.recover_interrupted()

    assert store.claim_due(now + timedelta(days=2), ACCOUNT) == []
    with pytest.raises(ValueError, match="已删除|没有找到"):
        store.get(ACCOUNT, saved["id"])
    assert store.finish_run(run["id"])["status"] == "skipped"


def test_due_run_is_account_scoped_and_authorized_only_while_sending(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    store = TaskStore(tmp_path / "scheduled-tasks.db")
    saved = store.save(task(targets=[
        {"type": "group", "id": "123456@chatroom"},
        {"type": "private", "id": "wxid_friend"},
    ]), now=now)
    due = datetime.fromisoformat(saved["next_run_time"])

    runs = store.claim_due(due + timedelta(seconds=10), ACCOUNT)

    assert len(runs) == 1
    run = runs[0]
    assert store.get(ACCOUNT, saved["id"])["status"] == "running"
    assert not store.authorize_delivery(ACCOUNT, saved["id"], run["id"], "group", "123456@chatroom")
    store.set_delivery(run["id"], "group", "123456@chatroom", "sending")
    assert store.authorize_delivery(ACCOUNT, saved["id"], run["id"], "group", "123456@chatroom")
    assert not store.authorize_delivery("wxid_other", saved["id"], run["id"], "group", "123456@chatroom")
    store.set_delivery(run["id"], "group", "123456@chatroom", "submitted", "已提交微信发送。")
    store.set_delivery(run["id"], "private", "wxid_friend", "failed", "发送被拒绝。")

    finished = store.finish_run(run["id"])

    assert finished["status"] == "partial"
    assert store.get(ACCOUNT, saved["id"])["status"] == "enabled"
    assert store.finish_run(run["id"])["status"] == "partial"


def test_missed_occurrence_is_skipped_without_catch_up(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    store = TaskStore(tmp_path / "scheduled-tasks.db")
    saved = store.save(task(), now=now)
    due = datetime.fromisoformat(saved["next_run_time"])

    assert store.claim_due(due + timedelta(seconds=91), ACCOUNT) == []
    item = store.get(ACCOUNT, saved["id"])
    assert item["status"] == "enabled"
    assert "错过" in item["last_result"]
    assert datetime.fromisoformat(item["next_run_time"]) > due + timedelta(seconds=91)
    runs = store.runs(ACCOUNT, saved["id"])
    assert runs[0]["status"] == "skipped"
    assert all(delivery["status"] == "skipped" for delivery in runs[0]["deliveries"])
