"""Legacy personal reminders must not masquerade as the group's task inventory."""
import ast
import asyncio
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest


ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def future_task_call(tmp_path_factory):
    checkout = tmp_path_factory.mktemp("future-task-scope")
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    relative = "astrbot/core/tools/cron_tools.py"
    target = checkout / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "vendor/engine" / relative, target)
    subprocess.run([
        "git", "apply", "-p2", f"--include={relative}", "--include=astrbot/core/cron/wechat_accounts.py",
        str(ROOT / "patches/astrbot-4.26.6-wechat.patch"),
    ], cwd=checkout, check=True)
    tree = ast.parse(target.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    tool = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FutureTaskTool")
    call = next(node for node in tool.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "call")
    call.returns = None
    for argument in call.args.args:
        argument.annotation = None
    nodes.append(call)
    namespace = {"Any": Any, "datetime": datetime}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(target), "exec"), namespace)
    call = namespace["call"]
    spec = importlib.util.spec_from_file_location("astrbot.core.cron.wechat_accounts",
        checkout / "astrbot/core/cron/wechat_accounts.py")
    call.account_helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(call.account_helper)
    return call


def _list(call, *, group=True, jobs=()):
    event = SimpleNamespace(
        unified_msg_origin="wechat:GroupMessage:123@chatroom" if group else "wechat:FriendMessage:user",
        get_sender_id=lambda: "admin",
        get_self_id=lambda: "wxid_bot",
        get_platform_name=lambda: "aiocqhttp",
        get_group_id=lambda: "123@chatroom" if group else "",
    )

    async def list_jobs():
        return list(jobs)

    async def login(**kwargs):
        return {"user_id": "wxid_bot"}

    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"),
                               bot=SimpleNamespace(get_login_info=login))
    context = SimpleNamespace(context=SimpleNamespace(
        event=event, context=SimpleNamespace(cron_manager=SimpleNamespace(list_jobs=list_jobs),
                                                 get_platform_inst=lambda _id: platform),
    ))
    with patch.dict(sys.modules, {"astrbot.core.cron.wechat_accounts": call.account_helper}):
        return asyncio.run(call(None, context, action="list"))


def test_empty_legacy_group_query_explicitly_excludes_manager_tasks(future_task_call):
    result = _list(future_task_call)
    assert "不包含管理器中的群定时任务" in result
    assert "不能据此判断本群没有定时任务" in result
    assert "current_group_scheduled_tasks" in result


def test_group_query_preserves_personal_reminder_ownership(future_task_call):
    def job(identity, sender, session="wechat:GroupMessage:123@chatroom"):
        return SimpleNamespace(job_id=identity, name=identity, job_type="active_agent",
                               payload={"session": session, "sender_id": sender, "account_id": "wxid_bot"},
                               enabled=True, next_run_time=None)

    result = _list(future_task_call, jobs=[job("own", "admin"), job("other-admin", "admin2"),
                                         job("other-group", "admin", "wechat:GroupMessage:456@chatroom")])
    assert "own | own" in result
    assert "other-admin" not in result and "other-group" not in result
    assert "不包含管理器中的群定时任务" in result


def test_private_legacy_query_keeps_existing_behavior(future_task_call):
    assert _list(future_task_call, group=False) == "No cron jobs found."
