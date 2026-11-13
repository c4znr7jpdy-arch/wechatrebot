"""Apply the production engine patches and exercise native reminder isolation."""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).parents[1]
SESSION = "wechat:GroupMessage:room@chatroom"


@pytest.fixture(scope="module")
def patched_cron(tmp_path_factory):
    target = tmp_path_factory.mktemp("cron-account-engine")
    shutil.copytree(ROOT / "vendor/engine/astrbot", target / "astrbot")
    subprocess.run(["git", "init", "--quiet"], cwd=target, check=True, capture_output=True)
    for name in ("wechat", "outbound-policy", "background-permissions", "license"):
        subprocess.run(["git", "apply", "-p2", "--whitespace=error-all",
                        str(ROOT / f"patches/astrbot-4.26.6-{name}.patch")],
                       cwd=target, check=True, capture_output=True)
    for source in target.rglob("*.py"):
        ast.parse(source.read_text("utf-8"))
    return target


def _load_method(root, relative, owner, method, namespace):
    tree = ast.parse((root / relative).read_text("utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
    node = next(node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method)
    namespace = {"__package__": "astrbot.core.cron", **namespace}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), relative, "exec"), namespace)
    return namespace[method]


@pytest.fixture
def runtime(patched_cron, monkeypatch):
    name = "astrbot.core.cron.wechat_accounts"
    spec = importlib.util.spec_from_file_location(name, patched_cron / "astrbot/core/cron/wechat_accounts.py")
    helper = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, helper)
    spec.loader.exec_module(helper)
    account = ["wxid_a"]
    bot = NS(get_login_info=AsyncMock(side_effect=lambda **kwargs: {"user_id": account[0]}),
             send_group_msg=AsyncMock(return_value={"message_id": 1}),
             send_private_msg=AsyncMock(return_value={"message_id": 2}), call_action=AsyncMock())
    platform = NS(bot=bot, meta=lambda: NS(name="aiocqhttp", id="wechat"))
    context = NS(get_platform_inst=lambda _id: platform, send_message=AsyncMock())
    event = NS(unified_msg_origin=SESSION, get_self_id=lambda: "wxid_a", get_sender_id=lambda: "admin",
               get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "room@chatroom")
    return NS(root=patched_cron, helper=helper, account=account, bot=bot, platform=platform,
              context=context, event=event)


def _future_call(root):
    path = root / "astrbot/core/tools/cron_tools.py"
    tree = ast.parse(path.read_text("utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    tool = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FutureTaskTool")
    call = next(node for node in tool.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "call")
    namespace = {"Any": Any, "datetime": datetime, "CronJobSchedulingError": ValueError}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *functions, call], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace["call"]


def _job(identity="job", account="wxid_a"):
    payload = {"session": SESSION, "sender_id": "admin", "note": "weather", "origin": "tool"}
    if account is not None:
        payload["account_id"] = account
    return NS(job_id=identity, name=identity, job_type="active_agent", payload=payload, enabled=True,
              next_run_time=None, run_once=False, cron_expression="30 8 * * *", timezone=None,
              description="weather")


def _tool_context(runtime, manager):
    runtime.context.cron_manager = manager
    return NS(context=NS(context=runtime.context, event=runtime.event))


def test_native_creation_binds_event_account_after_live_verification(runtime):
    manager = NS(add_active_job=AsyncMock(return_value=_job()))
    result = asyncio.run(_future_call(runtime.root)(None, _tool_context(runtime, manager),
                        action="create", cron_expression="30 8 * * *", note="weather"))
    assert result.startswith("Scheduled future task")
    assert manager.add_active_job.call_args.kwargs["payload"]["account_id"] == "wxid_a"
    runtime.account[0] = "wxid_b"
    result = asyncio.run(_future_call(runtime.root)(None, _tool_context(runtime, manager),
                        action="create", cron_expression="30 8 * * *", note="weather"))
    assert result.startswith("error:")
    assert manager.add_active_job.await_count == 1


@pytest.mark.parametrize("action", ["list", "edit", "delete"])
def test_native_tasks_hide_and_reject_other_accounts_and_unassigned(runtime, action):
    own, other, unassigned = _job("own"), _job("other", "wxid_b"), _job("unassigned", None)
    manager = NS(list_jobs=AsyncMock(return_value=[own, other, unassigned]),
                 db=NS(get_cron_job=AsyncMock(return_value=other)),
                 update_job=AsyncMock(), delete_job=AsyncMock())
    call = _future_call(runtime.root)
    context = _tool_context(runtime, manager)
    result = asyncio.run(call(None, context, action=action, job_id="other", name="edited"))
    if action == "list":
        assert "own | own" in result and "other |" not in result and "unassigned |" not in result
    else:
        assert result.startswith("error:")
        manager.db.get_cron_job.return_value = unassigned
        assert asyncio.run(call(None, context, action=action, job_id="unassigned", name="edited")).startswith("error:")
    manager.update_job.assert_not_called()
    manager.delete_job.assert_not_called()


def test_native_non_wechat_platform_preserves_existing_ownership(runtime):
    runtime.platform.meta = lambda: NS(name="discord", id="discord")
    manager = NS(list_jobs=AsyncMock(return_value=[_job("legacy", None)]))
    result = asyncio.run(_future_call(runtime.root)(None, _tool_context(runtime, manager), action="list"))
    assert "legacy | legacy" in result
    runtime.bot.get_login_info.assert_not_called()


def test_qq_aiocqhttp_keeps_numeric_account_behavior(runtime):
    runtime.event.unified_msg_origin = "qq:GroupMessage:12345"
    runtime.event.get_self_id = lambda: "67890"
    assert asyncio.run(runtime.helper.event_account(runtime.context, runtime.event)) is None
    runtime.bot.get_login_info.assert_not_called()


@pytest.mark.parametrize("account", [None, "wxid_b"])
def test_execution_never_wakes_agent_for_unassigned_or_switched_account(runtime, account):
    method = _load_method(runtime.root, "astrbot/core/cron/manager.py", "CronJobManager", "_run_active_agent_job", {})
    manager = NS(ctx=runtime.context, _woke_main_agent=AsyncMock())
    with pytest.raises(PermissionError):
        asyncio.run(method(manager, _job(account=account), datetime.now(timezone.utc)))
    manager._woke_main_agent.assert_not_called()


def test_execution_scope_covers_agent_and_is_restored(runtime):
    method = _load_method(runtime.root, "astrbot/core/cron/manager.py", "CronJobManager", "_run_active_agent_job", {})
    observed = []
    async def wake(**kwargs):
        observed.append(runtime.helper._scope.get())
    manager = NS(ctx=runtime.context, _woke_main_agent=wake)
    asyncio.run(method(manager, _job(), datetime.now(timezone.utc)))
    assert observed == [(SESSION, "wxid_a", runtime.context)]
    assert runtime.helper._scope.get() is None


def test_scoped_send_rechecks_after_media_and_carries_account(runtime, monkeypatch):
    conversions = []
    async def convert(**kwargs):
        for callback in conversions:
            callback()
        await kwargs["bot"].send_group_msg(group_id=kwargs["session_id"], message=kwargs["message_chain"])
    monkeypatch.setitem(sys.modules, "aiocqhttp", NS(Event=dict))
    monkeypatch.setitem(sys.modules, "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
                        NS(AiocqhttpMessageEvent=NS(send_message=convert)))
    with runtime.helper.account_scope(SESSION, "wxid_a", runtime.context):
        assert asyncio.run(runtime.helper.send_scoped_message(runtime.context, SESSION, ["weather"]))
    assert runtime.bot.send_group_msg.call_args.kwargs["self_id"] == "wxid_a"
    conversions.append(lambda: runtime.account.__setitem__(0, "wxid_b"))
    with runtime.helper.account_scope(SESSION, "wxid_a", runtime.context), pytest.raises(PermissionError):
        asyncio.run(runtime.helper.send_scoped_message(runtime.context, SESSION, ["weather"]))
    assert runtime.bot.send_group_msg.await_count == 1


def test_context_send_rejects_cross_target_even_for_admin(runtime):
    class Session:
        def __init__(self, value):
            self.value = value
            self.platform_name = value.split(":", 1)[0]
        def __str__(self):
            return self.value
        from_str = staticmethod(lambda value: Session(value))
    method = _load_method(runtime.root, "astrbot/core/star/context.py", "Context", "send_message", {"MessageSesion": Session})
    runtime.context.platform_manager = NS(platform_insts=[runtime.platform])
    with runtime.helper.account_scope(SESSION, "wxid_a", runtime.context), pytest.raises(PermissionError):
        asyncio.run(method(runtime.context, "wechat:GroupMessage:other@chatroom", ["weather"]))
    runtime.bot.send_group_msg.assert_not_called()


def test_shared_aiocqhttp_sender_blocks_plugin_cross_target_and_wrong_account(runtime, monkeypatch):
    monkeypatch.setitem(sys.modules, "aiocqhttp", NS(Event=dict))
    with runtime.helper.account_scope(SESSION, "wxid_a", runtime.context):
        with pytest.raises(PermissionError):
            asyncio.run(runtime.helper.bind_scoped_sender(runtime.bot, None, True, "other@chatroom"))
        bot, event = asyncio.run(runtime.helper.bind_scoped_sender(runtime.bot, None, True, "room@chatroom"))
        assert event["self_id"] == "wxid_a"
        with pytest.raises(PermissionError):
            asyncio.run(bot.send_group_msg(group_id="other@chatroom", message=["weather"]))
        with pytest.raises(PermissionError):
            asyncio.run(bot.send_group_msg(group_id="room@chatroom", self_id="wxid_b", message=["weather"]))
        asyncio.run(bot.send_group_msg(group_id="room@chatroom", message=["weather"]))
    assert runtime.bot.send_group_msg.call_args.kwargs["_wechatrebot_delivery"] == "proactive"
    assert runtime.bot.send_group_msg.await_count == 1


@pytest.mark.parametrize("operation", ["update_job", "delete_job"])
def test_manager_mutations_recheck_live_owner(runtime, operation):
    method = _load_method(runtime.root, "astrbot/core/cron/manager.py", "CronJobManager", operation, {})
    manager = NS(ctx=runtime.context, db=NS(get_cron_job=AsyncMock(return_value=_job()),
                 update_cron_job=AsyncMock(), delete_cron_job=AsyncMock()),
                 _remove_scheduled=Mock(), _basic_handlers={})
    runtime.account[0] = "wxid_b"
    with pytest.raises(PermissionError):
        asyncio.run(method(manager, "job", **({"enabled": False} if operation == "update_job" else {})))
    manager.db.update_cron_job.assert_not_called()
    manager.db.delete_cron_job.assert_not_called()


def test_removed_non_wechat_platform_reminder_can_still_be_deleted(runtime):
    method = _load_method(runtime.root, "astrbot/core/cron/manager.py", "CronJobManager", "delete_job", {})
    job = _job(account=None)
    job.payload["session"] = "removed_discord:GroupMessage:12345"
    runtime.context.get_platform_inst = lambda _id: None
    manager = NS(ctx=runtime.context, db=NS(get_cron_job=AsyncMock(return_value=job),
                 delete_cron_job=AsyncMock()), _remove_scheduled=Mock(), _basic_handlers={})
    asyncio.run(method(manager, job.job_id))
    manager.db.delete_cron_job.assert_awaited_once_with(job.job_id)
    manager._remove_scheduled.assert_called_once_with(job.job_id)
    runtime.bot.get_login_info.assert_not_called()
