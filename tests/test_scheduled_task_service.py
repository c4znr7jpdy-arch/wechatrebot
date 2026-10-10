from pathlib import Path
import json
import sqlite3

import pytest

from wechatrebot_manager.scheduled_task_service import ScheduledTaskService


ACCOUNT = "wxid_robot"
GROUP = "123456@chatroom"


class Directory:
    def __init__(self, kind: str):
        self.kind = kind

    def list(self, refresh=False):
        items = ([{"group_id": GROUP, "name": "家人群"}] if self.kind == "group"
                 else [{"user_id": "wxid_friend", "remark": "张三"}])
        return {"account_id": ACCOUNT, "items": items, "stale": False}


class Engine:
    def __init__(self, service):
        self.service = service

    def request(self, method, path, payload=None):
        if method == "GET":
            return {"version": 1, "runtime_ready": True, "account_id": ACCOUNT,
                    "capabilities": [{"id": action, "ready": True} for action in
                                     ("weather", "news", "oilprice", "kfc", "short_drama", "group_analysis", "text")]}
        if path.endswith("save"):
            return {"item": self.service.store.save(payload), "message": "任务已保存并启用。"}
        if path.endswith("test"):
            return {"status": "submitted", "message": "测试消息已提交微信发送。",
                    "target": payload["test_target"]}
        if path.endswith("preview"):
            return {"kind": "text", "text": payload["preview_target"]["id"]}
        raise AssertionError((method, path))


def service(tmp_path: Path) -> ScheduledTaskService:
    value = ScheduledTaskService(
        tmp_path, groups=Directory("group"), contacts=Directory("private"),
        account_hint=lambda: ACCOUNT,
    )
    value.engine = Engine(value)
    value.report_engine = value.engine
    return value


def test_group_analysis_drafts_and_preview_share_the_standard_service_contract(tmp_path):
    value = service(tmp_path)
    task = {**payload(draft=True), "action": "group_analysis", "arguments": {"days": 3},
            "targets": [{"type": "group", "id": GROUP}]}
    saved = value.save(task)["item"]
    assert saved["action"] == "group_analysis"
    assert value.context()["legacy_group_analysis"]["state"] == "none"
    assert value.preview(task)["text"] == GROUP
    result = value.test({**task, "test_target": {"type": "group", "id": GROUP}})
    assert result["status"] == "submitted"
    assert value.delete(ACCOUNT, saved["id"], saved["revision"])["deleted"]


def test_group_analysis_service_never_routes_a_report_to_a_private_recipient(tmp_path):
    value = service(tmp_path)
    task = {**payload(draft=True), "action": "group_analysis", "arguments": {"days": 1},
            "targets": [{"type": "private", "id": "wxid_friend"}]}
    with pytest.raises(ValueError, match="被分析的群聊"):
        value.preview(task)
    with pytest.raises(ValueError, match="被分析的群聊"):
        value.test({**task, "test_target": task["targets"][0]})


def payload(*, draft=False):
    return {
        "id": "task-1", "account_id": ACCOUNT, "name": "每日提醒",
        "action": "text", "arguments": {"text": "喝水"},
        "targets": [] if draft else [{"type": "group", "id": GROUP}],
        "schedule": {"kind": "daily", "time": "23:59", "timezone": "Asia/Shanghai"},
        "enabled": not draft, "draft": draft,
    }


def test_context_resolves_names_without_seeding(tmp_path: Path) -> None:
    value = service(tmp_path)

    context = value.context()

    assert context["account_id"] == ACCOUNT
    assert context["items"] == []
    assert context["command_definition_version"] == 1
    assert not any(item["id"] == "checkin" for item in context["command_catalog"])
    assert context["groups"] == [{"id": GROUP, "name": "家人群", "type": "group"}]


def test_draft_and_active_task_share_one_authoritative_store(tmp_path: Path) -> None:
    value = service(tmp_path)
    draft = value.save(payload(draft=True))["item"]

    active = value.save({**payload(), "revision": draft["revision"]})["item"]

    assert active["id"] == draft["id"]
    assert active["revision"] == draft["revision"] + 1
    assert active["enabled"] is True
    assert value.context()["items"][0]["targets"][0]["name"] == "家人群"


def test_stale_account_cannot_edit_or_delete_another_accounts_task(tmp_path: Path) -> None:
    value = service(tmp_path)
    draft = value.save(payload(draft=True))["item"]

    with pytest.raises(ValueError, match="账号"):
        value.delete("wxid_other", draft["id"], draft["revision"])


def test_message_test_requires_one_explicit_live_target(tmp_path: Path) -> None:
    value = service(tmp_path)
    request = {**payload(), "test_target": {"type": "group", "id": GROUP}}

    result = value.test(request)

    assert result["status"] == "submitted"
    assert value.store.list(ACCOUNT) == []


def test_active_task_rechecks_content_capability(tmp_path: Path) -> None:
    value = service(tmp_path)
    original = value.engine.request

    def request(method, path, payload=None):
        if method == "GET":
            return {
                "version": 1,
                "runtime_ready": True,
                "account_id": ACCOUNT,
                "capabilities": [
                    {"id": "weather", "ready": False, "message": "天气服务尚未配置。"},
                    {"id": "text", "ready": True},
                ],
            }
        return original(method, path, payload)

    value.engine.request = request
    weather = {
        **payload(),
        "action": "weather",
        "arguments": {"city": "青岛", "day": "today", "output": "image"},
    }

    with pytest.raises(ValueError, match="天气服务尚未配置"):
        value.save(weather)


def test_legacy_task_is_marked_for_command_review(tmp_path: Path) -> None:
    value = service(tmp_path)
    saved = value.store.save(payload())
    path = value.store.path
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT payload FROM scheduled_tasks WHERE id=?", (saved["id"],)).fetchone()
        legacy = json.loads(row[0])
        legacy.pop("definition_version", None)
        connection.execute("UPDATE scheduled_tasks SET payload=? WHERE id=?", (json.dumps(legacy), saved["id"]))

    item = value.context()["items"][0]
    assert item["status"] == "needs_review"
    assert "命令定义已更新" in item["status_message"]
