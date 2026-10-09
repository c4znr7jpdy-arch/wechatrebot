from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from wechatrebot_manager.group_policy import GroupPolicyService
from wechatrebot_manager.private_policy import PrivateChatPolicyService, PrivatePolicyError
from wechatrebot_manager.task_store import TaskStore


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vendor/hook_41827_astrbot_bridge"
ACCOUNT = "wxid_bot"
GROUP = "room@chatroom"
CONTACT = "wxid_friend"
CREATED = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 9, 13, 0, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    if not (SOURCE / "src/hook_bridge/onebot_client.py").is_file():
        pytest.fail("pinned bridge checkout unavailable")
    repository = tmp_path_factory.mktemp("scheduled-delivery-bridge")
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True, capture_output=True)
    package_root = repository / "hook_41827_astrbot_bridge/src/hook_bridge"
    shutil.copytree(SOURCE / "src", repository / "hook_41827_astrbot_bridge/src")
    # Exercise the complete ordered bridge patch stack without touching SOURCE.
    for patch in sorted((ROOT / "patches").glob("hook-bridge-*.patch")):
        applied = subprocess.run(
            ["git", "apply", "--whitespace=error-all", str(patch)],
            cwd=repository, capture_output=True, text=True, encoding="utf-8",
        )
        assert applied.returncode == 0, applied.stderr
    shutil.copy2(ROOT / "src/wechatrebot_manager/task_store.py", package_root / "task_store.py")
    for source in package_root.glob("*.py"):
        ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    name = "_scheduled_delivery_bridge"
    spec = importlib.util.spec_from_file_location(name, package_root / "__init__.py", submodule_search_locations=[str(package_root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    try:
        yield SimpleNamespace(
            onebot=importlib.import_module(f"{name}.onebot_client"),
            models=importlib.import_module(f"{name}.models"),
            state=importlib.import_module(f"{name}.state"),
        )
    finally:
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def _store(root, *, target=GROUP, task_id="task", action="text", sending=True):
    kind = "group" if target.endswith("@chatroom") else "private"
    arguments = {
        "text": {"text": "提醒"},
        "weather": {"city": "青岛", "day": "today", "output": "image"},
        "short_drama": {"media_type": "短剧", "limit": 12},
    }.get(action, {})
    store = TaskStore(root / "data/manager/scheduled-tasks.db")
    task = store.save({
        "id": task_id, "account_id": ACCOUNT, "action": action,
        "arguments": arguments,
        "targets": [{"type": kind, "id": target}], "enabled": True,
        "schedule": {"kind": "daily", "time": "08:01"},
    }, now=CREATED)
    run = store.claim_due(DUE, ACCOUNT)[0]
    if sending:
        store.set_delivery(run["id"], kind, target, "sending")
    return store, task, run


def _policy(root, *, target=GROUP, account=ACCOUNT, **values):
    group = target.endswith("@chatroom")
    path = root / "data/manager" / ("group-policies.json" if group else "private-chat-policies.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "accounts": {account: {
        "groups" if group else "contacts": {target: values}
    }}}), encoding="utf-8")
    return path


def _router(bridge, root, *, on_send=None, on_materialize=None):
    class Hook:
        calls = None

        def __init__(self):
            self.calls = []

        async def send_text(self, target, text):
            self.calls.append((target, text))
            if on_send:
                on_send()
            return bridge.models.SendResult(ok=True)

        async def send_image(self, target, path):
            self.calls.append((target, str(path)))
            if on_send:
                on_send()
            return bridge.models.SendResult(ok=True)

        async def send_link_card(self, target, title, description, thumb_url, url):
            self.calls.append((target, title, description, thumb_url, url))
            if on_send:
                on_send()
            return bridge.models.SendResult(ok=True)

    class Media:
        async def materialize(self, source):
            if on_materialize:
                on_materialize()
            return root / "preview.png"

    state = bridge.state.BridgeState(60, 100)
    state.set_identity(ACCOUNT, "机器人")
    router = bridge.onebot.OneBotActionRouter(Hook(), Media(), state, policy_path=root / "data/manager/group-policies.json")
    router.outbound_policy._now = lambda zone: DUE.astimezone(zone)
    return router


def _send(router, task, run, *, target=GROUP, message="提醒", **extra):
    group = target.endswith("@chatroom")
    return asyncio.run(router.handle({
        "action": "send_group_msg" if group else "send_private_msg",
        "params": {
            "group_id" if group else "user_id": target, "message": message,
            "_wechatrebot_delivery": "scheduled", "_wechatrebot_account": ACCOUNT,
            "_wechatrebot_task_id": task["id"], "_wechatrebot_run_id": run["id"], **extra,
        },
    }))


def test_explicit_schedule_and_other_group_cards_are_independent(bridge, tmp_path):
    _, task, run = _store(tmp_path, action="weather")
    _policy(tmp_path, allowed_features=["jiang_commands"], proactive_enabled=False)
    router = _router(bridge, tmp_path)
    assert _send(router, task, run)["status"] == "ok"
    decision = asyncio.run(router.outbound_policy.authorize(ACCOUNT, GROUP, delivery="proactive"))
    assert decision.allowed
    assert len(router.hook.calls) == 1


def test_new_group_default_allows_weather_schedule_and_group_cards(bridge, tmp_path):
    _, task, run = _store(tmp_path, action="weather")
    router = _router(bridge, tmp_path)
    assert not (tmp_path / "data/manager/group-policies.json").exists()
    assert _send(router, task, run)["status"] == "ok"
    assert len(router.hook.calls) == 1
    decision = asyncio.run(router.outbound_policy.authorize(ACCOUNT, GROUP, delivery="proactive"))
    assert decision.allowed


def test_null_group_feature_list_is_not_treated_as_new_group_defaults(bridge, tmp_path):
    _, task, run = _store(tmp_path, action="weather")
    _policy(tmp_path, allowed_features=None)
    router = _router(bridge, tmp_path)
    assert _send(router, task, run)["status"] == "failed"
    assert router.hook.calls == []


@pytest.mark.parametrize("target", [GROUP, CONTACT])
@pytest.mark.parametrize("mutation", ["no_run", "pending", "wrong_account", "wrong_target", "deleted", "paused", "revised", "finished"])
def test_scheduled_tag_needs_live_task_run_target_and_account_authorization(bridge, tmp_path, target, mutation):
    store, task, run = _store(tmp_path, target=target, sending=mutation != "pending")
    router = _router(bridge, tmp_path)
    extra = {}
    if mutation == "no_run":
        extra["_wechatrebot_run_id"] = "missing"
    elif mutation == "wrong_account":
        router.state.set_identity("wxid_other", "其他账号")
    elif mutation == "wrong_target":
        target = "other@chatroom" if target == GROUP else "wxid_other_friend"
    elif mutation == "deleted":
        store.delete(ACCOUNT, task["id"], task["revision"])
    elif mutation in {"paused", "revised"}:
        task["enabled"] = mutation != "paused"
        task["name"] = "已修改"
        store.save(task, now=CREATED)
    elif mutation == "finished":
        store.finish_run(run["id"])
    result = _send(router, task, run, target=target, **extra)
    assert result["status"] == "failed"
    assert result["retcode"] == 1403
    assert router.hook.calls == []


@pytest.mark.parametrize("target", [GROUP, CONTACT])
@pytest.mark.parametrize("values,reason", [
    ({"bot_enabled": False}, "机器人功能已关闭"),
    ({"scheduled_enabled": False}, "暂停全部定时推送"),
    ({"daily_limit": 0}, "上限为零"),
    ({"quiet_hours": {"enabled": True, "start": "08:00", "end": "09:00"}}, "安静时段"),
    ({"daily_limit": "3"}, "无法确认"),
])
def test_group_and_private_delivery_respect_hot_read_controls(bridge, tmp_path, target, values, reason):
    _, task, run = _store(tmp_path, target=target)
    router = _router(bridge, tmp_path)
    _policy(tmp_path, target=target, **values)
    result = _send(router, task, run, target=target)
    assert result["status"] == "failed"
    assert reason in result["message"]
    assert router.hook.calls == []


@pytest.mark.parametrize("target", [GROUP, CONTACT])
def test_feature_scope_cannot_be_bypassed_by_scheduled_tag(bridge, tmp_path, target):
    _, task, run = _store(tmp_path, target=target, action="weather")
    router = _router(bridge, tmp_path)
    _policy(tmp_path, target=target, allowed_features=[])
    assert "未允许" in _send(router, task, run, target=target)["message"]
    _policy(tmp_path, target=target, allowed_features=["jiang_commands"])
    assert _send(router, task, run, target=target)["status"] == "ok"


@pytest.mark.parametrize("target", [GROUP, CONTACT])
def test_scheduled_short_drama_requires_feature_and_sends_link_card(bridge, tmp_path, target):
    _, task, run = _store(tmp_path, target=target, action="short_drama")
    router = _router(bridge, tmp_path)
    card = [{"type": "wechat_link_card", "data": {
        "title": "最新短剧推荐｜12部",
        "desc": "定时推荐 · 点击查看封面并选择播放",
        "url": "https://player.example/recommendations/token",
        "image_url": "https://player.example/cover.jpg",
    }}]
    _policy(tmp_path, target=target, allowed_features=["jiang_commands"])
    assert "未允许" in _send(router, task, run, target=target, message=card)["message"]
    _policy(tmp_path, target=target, allowed_features=["jiang_short_drama"])
    assert _send(router, task, run, target=target, message=card)["status"] == "ok"
    assert router.hook.calls[-1][1:] == (
        "最新短剧推荐｜12部",
        "定时推荐 · 点击查看封面并选择播放",
        "https://player.example/cover.jpg",
        "https://player.example/recommendations/token",
    )


@pytest.mark.parametrize("target", [GROUP, CONTACT])
def test_multisegment_task_counts_once_per_target_and_survives_router_restart(bridge, tmp_path, target):
    _, task, run = _store(tmp_path, target=target)
    _policy(tmp_path, target=target, daily_limit=1)
    router = _router(bridge, tmp_path)
    message = [{"type": "text", "data": {"text": "前缀"}}, {"type": "image", "data": {"file": "preview.png"}}, {"type": "text", "data": {"text": "后缀"}}]
    assert _send(router, task, run, target=target, message=message)["status"] == "ok"
    assert len(router.hook.calls) == 3
    restarted = _router(bridge, tmp_path)
    assert _send(restarted, task, run, target=target)["status"] == "ok"
    _, task2, run2 = _store(tmp_path, target=target, task_id="task2")
    blocked = _send(restarted, task2, run2, target=target)
    assert "上限" in blocked["message"]
    usage = tmp_path / "data/manager" / ("group-policy-usage.json" if target == GROUP else "private-policy-usage.json")
    assert json.loads(usage.read_text(encoding="utf-8"))["counts"][ACCOUNT][target]["2026-09-13"] == 1


@pytest.mark.parametrize("mutation", ["account", "deleted", "policy"])
def test_actual_send_rechecks_after_awaiting_image_materialization(bridge, tmp_path, mutation):
    store, task, run = _store(tmp_path)

    def change():
        if mutation == "account":
            router.state.set_identity("wxid_other", "其他账号")
        elif mutation == "deleted":
            store.delete(ACCOUNT, task["id"], task["revision"])
        else:
            _policy(tmp_path, scheduled_enabled=False)

    router = _router(bridge, tmp_path, on_materialize=change)
    result = _send(router, task, run, message=[{"type": "image", "data": {"file": "preview.png"}}])
    assert result["status"] == "failed"
    assert not router.hook.calls


def test_each_segment_checks_live_account_before_sending(bridge, tmp_path):
    _, task, run = _store(tmp_path)
    router = _router(bridge, tmp_path, on_send=lambda: router.state.set_identity("wxid_other", "其他账号"))
    result = _send(router, task, run, message=[{"type": "text", "data": {"text": "第一个片段"}}, {"type": "image", "data": {"file": "preview.png"}}])
    assert result["status"] == "failed"
    assert len(router.hook.calls) == 1


def test_existing_reply_and_private_message_behavior_is_preserved(bridge, tmp_path):
    router = _router(bridge, tmp_path)
    _policy(tmp_path, scheduled_enabled=False)
    reply = asyncio.run(router.handle({"action": "send_group_msg", "params": {
        "group_id": GROUP, "message": "回复", "_wechatrebot_delivery": "reply", "_wechatrebot_wake": True,
    }}))
    assert reply["status"] == "ok"
    proactive = asyncio.run(router.handle({"action": "send_group_msg", "params": {
        "group_id": GROUP, "message": "主动聊天", "_wechatrebot_delivery": "proactive",
    }}))
    assert proactive["status"] == "ok"
    _policy(tmp_path, target=CONTACT, bot_enabled=False, scheduled_enabled=False)
    private = asyncio.run(router.handle({"action": "send_private_msg", "params": {
        "user_id": CONTACT, "message": "普通私聊回复",
    }}))
    assert private["status"] == "ok"


@pytest.mark.parametrize("action", ["send_raw_xml", "send_voice"])
def test_scheduled_delivery_cannot_bypass_guard_via_unsupported_send_route(bridge, tmp_path, action):
    router = _router(bridge, tmp_path)
    result = asyncio.run(router.handle({"action": action, "params": {
        "user_id": CONTACT, "content": "<appmsg />", "file": "sound.mp3",
        "_wechatrebot_delivery": "Scheduled",
    }}))
    assert result["status"] == "failed"
    assert result["retcode"] == 1403
    assert router.hook.calls == []


def test_scheduled_permission_is_account_scoped_and_does_not_enable_ai(tmp_path):
    groups = GroupPolicyService(tmp_path)
    saved = groups.save(ACCOUNT, GROUP, {"scheduled_enabled": False})
    assert saved["scheduled_enabled"] is False
    assert "proactive_enabled" not in saved
    assert groups.get("wxid_other", GROUP)["scheduled_enabled"] is True
    contacts = PrivateChatPolicyService(tmp_path)
    contacts.save(ACCOUNT, CONTACT, {"scheduled_enabled": False, "daily_limit": 5, "quiet_hours": {"enabled": True}})
    contacts.save(ACCOUNT, CONTACT, {"allowed_features": []})
    policy = contacts.overview(ACCOUNT, CONTACT)["policy"]
    assert policy["scheduled_enabled"] is False
    assert policy["quiet_hours"]["enabled"] is True
    assert policy["daily_limit"] == 5
    other = contacts.overview("wxid_other", CONTACT)["policy"]
    assert other["scheduled_enabled"] is True
    assert other["daily_limit"] == 3


@pytest.mark.parametrize("values", [
    {"scheduled_enabled": "true"}, {"bot_enabled": 1}, {"daily_limit": True},
    {"daily_limit": 101}, {"quiet_hours": {"start": "25:00"}},
    {"quiet_hours": {"timezone": "Invalid/Zone"}}, {"quiet_hours": {"enabled": "false"}},
])
def test_private_scheduled_controls_validate_without_changing_saved_policy(tmp_path, values):
    service = PrivateChatPolicyService(tmp_path)
    service.save(ACCOUNT, CONTACT, {"scheduled_enabled": False})
    before = service.path.read_bytes()
    with pytest.raises(PrivatePolicyError):
        service.save(ACCOUNT, CONTACT, values)
    assert service.path.read_bytes() == before
