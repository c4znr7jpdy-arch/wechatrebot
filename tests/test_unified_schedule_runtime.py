import ast
import asyncio
from datetime import datetime, timezone
import importlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).parents[1]
PROJECT = Path(__file__).parents[1] / "vendor"


def _copy_runtime_package(tmp_path: Path):
    package = tmp_path / "jiang_schedule_runtime"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("content.py", "unified_runtime.py"):
        shutil.copy2(ROOT / "runtime/jiang_schedule" / name, package / name)
    for name in ("task_store.py", "group_analysis_schedule.py", "group_feature_runtime.py"):
        shutil.copy2(ROOT / "src/wechatrebot_manager" / name, package / name)
    sys.path.insert(0, str(tmp_path))
    try:
        return importlib.import_module("jiang_schedule_runtime.unified_runtime")
    finally:
        sys.path.remove(str(tmp_path))


class FakeContent:
    def __init__(self):
        self.generations = 0

    def readiness(self, _action):
        return {"ready": True, "message": ""}

    def require_ready(self, _action):
        return None

    async def generate(self, _task):
        self.generations += 1

        class Result:
            def segments(self):
                return [{"type": "text", "data": {"text": "提醒"}}]

        return Result()


class FakeClient:
    def __init__(self, *, group_exists=True):
        self.group_exists = group_exists

    async def get_group_info(self, group_id, self_id):
        return {"group_id": group_id} if self.group_exists and self_id == "wxid_robot" else {}

    async def get_user_info(self, user_id, self_id):
        return {"user_id": user_id} if self_id == "wxid_robot" else {}


def _task():
    return {
        "id": "runtime-task", "account_id": "wxid_robot", "name": "提醒",
        "action": "text", "arguments": {"text": "提醒"},
        "targets": [{"type": "group", "id": "123456@chatroom"}],
        "schedule": {"kind": "daily", "time": "08:30", "timezone": "Asia/Shanghai"},
        "enabled": True, "draft": False,
    }


def _analysis_runtime(tmp_path: Path):
    module = _copy_runtime_package(tmp_path)
    client = FakeClient()
    account = {"id": "wxid_robot"}
    calls, sent = [], []

    class Analysis:
        def unified_report_ready(self):
            return {"ready": True, "message": ""}

        async def build_unified_report(self, account_id, group_id, days):
            calls.append((account_id, group_id, days))
            return {"type": "text", "text": "仅属于 " + group_id}

    plugin = Analysis()
    star = SimpleNamespace(root_dir_name="astrbot_plugin_qq_group_daily_analysis", name="analysis",
                           activated=True, star_cls=plugin)
    context = SimpleNamespace(get_all_stars=lambda: [star])
    configuration = tmp_path / "app/astrbot/data/cmd_config.json"
    configuration.parent.mkdir(parents=True)
    configuration.write_text('{"plugin_set":["*"]}', encoding="utf-8")

    async def connection():
        return account["id"], client

    async def sender(_client, run, target, content):
        sent.append((target["id"], content.segments()))
        return {"message_id": target["id"]}

    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    runtime = module.UnifiedRuntime(context, install_root=tmp_path, connection=connection,
                                    sender=sender, clock=lambda: now)
    runtime._active = True
    task = {**_task(), "action": "group_analysis", "arguments": {"days": 2}, "targets": [
        {"type": "group", "id": "123456@chatroom"}, {"type": "group", "id": "987654@chatroom"},
    ]}
    return runtime, task, plugin, calls, sent, account, client


def _run_analysis_due(runtime, task):
    saved = asyncio.run(runtime.save(task))
    runtime.clock = lambda: datetime.fromisoformat(saved["next_run_time"])
    asyncio.run(runtime.run_once())
    return runtime.store.runs(task["account_id"], task["id"])[0]


def test_group_reports_are_generated_independently_for_each_recipient(tmp_path):
    runtime, task, _plugin, calls, sent, _account, _client = _analysis_runtime(tmp_path)

    run = _run_analysis_due(runtime, task)

    assert calls == [("wxid_robot", "123456@chatroom", 2), ("wxid_robot", "987654@chatroom", 2)]
    assert sent == [(group, [{"type": "text", "data": {"text": "仅属于 " + group}}])
                    for group in ("123456@chatroom", "987654@chatroom")]
    assert run["status"] == "submitted"
    assert runtime.store.action_owner("group_analysis")["phase"] == "complete"


def test_a_failed_group_does_not_send_its_content_to_another_group(tmp_path):
    runtime, task, plugin, _calls, sent, _account, _client = _analysis_runtime(tmp_path)
    original = plugin.build_unified_report

    async def generate(account, group, days):
        if group == "123456@chatroom":
            raise ValueError("sensitive upstream details")
        return await original(account, group, days)

    plugin.build_unified_report = generate
    run = _run_analysis_due(runtime, task)
    assert [target["status"] for target in run["deliveries"]] == ["failed", "submitted"]
    assert "sensitive" not in run["deliveries"][0]["message"]
    assert [target for target, _content in sent] == ["987654@chatroom"]


@pytest.mark.parametrize("change", ["pause", "delete", "account", "recipient"])
def test_analysis_rechecks_mutable_authorization_after_generation(tmp_path, change):
    runtime, task, plugin, _calls, sent, account, client = _analysis_runtime(tmp_path)
    original = plugin.build_unified_report

    async def generate(*args):
        result = await original(*args)
        current = runtime.store.get(task["account_id"], task["id"])
        if change == "pause":
            runtime.store.set_enabled(task["account_id"], task["id"], False, current["revision"])
        elif change == "delete":
            runtime.store.delete(task["account_id"], task["id"], current["revision"])
        elif change == "account":
            account["id"] = "wxid_other"
        else:
            client.group_exists = False
        return result

    plugin.build_unified_report = generate
    saved = asyncio.run(runtime.save(task))
    runtime.clock = lambda: datetime.fromisoformat(saved["next_run_time"])
    asyncio.run(runtime.run_once())
    assert sent == []
    assert runtime.store.action_owner("group_analysis")["phase"] == "complete"


def test_group_preview_requires_one_group_and_never_sends_or_claims_a_schedule(tmp_path):
    runtime, task, _plugin, calls, sent, _account, _client = _analysis_runtime(tmp_path)
    with pytest.raises(ValueError, match="请选择一个"):
        asyncio.run(runtime.preview(task))
    result = asyncio.run(runtime.preview({**task, "preview_target": task["targets"][1]}))
    assert result["kind"] == "text"
    assert result["text"] == "仅属于 987654@chatroom"
    assert calls == [("wxid_robot", "987654@chatroom", 2)]
    assert sent == []
    assert runtime.store.list("wxid_robot") == []
    assert runtime.store.action_owner("group_analysis") is None


def test_group_analysis_respects_group_feature_permission_before_generating(tmp_path):
    runtime, task, _plugin, calls, sent, _account, _client = _analysis_runtime(tmp_path)
    path = tmp_path / "data/manager/group-policies.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "accounts": {"wxid_robot": {"groups": {
        "123456@chatroom": {"allowed_features": []},
    }}}}))

    with pytest.raises(ValueError, match="尚未允许"):
        asyncio.run(runtime.preview({**task, "preview_target": task["targets"][0]}))
    assert calls == sent == []


@pytest.mark.parametrize("change", ["group_policy", "recipient", "plugin"])
def test_group_preview_rechecks_permission_and_membership_after_generation(tmp_path, change):
    runtime, task, plugin, calls, sent, _account, client = _analysis_runtime(tmp_path)
    original = plugin.build_unified_report

    async def generate(*args):
        result = await original(*args)
        if change == "group_policy":
            path = tmp_path / "data/manager/group-policies.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"schema_version": 1, "accounts": {"wxid_robot": {"groups": {
                "123456@chatroom": {"allowed_features": []},
            }}}}))
        elif change == "recipient":
            client.group_exists = False
        else:
            (tmp_path / "app/astrbot/data/cmd_config.json").write_text('{"plugin_set":[]}')
        return result

    plugin.build_unified_report = generate
    with pytest.raises(ValueError):
        asyncio.run(runtime.preview({**task, "preview_target": task["targets"][0]}))
    assert len(calls) == 1 and sent == []
    assert runtime.store.list("wxid_robot") == []


def test_group_analysis_without_messages_is_recorded_as_skipped(tmp_path):
    runtime, task, plugin, _calls, sent, _account, _client = _analysis_runtime(tmp_path)

    async def no_messages(*_args):
        return {"type": "skipped", "message": "没有符合条件的消息"}

    plugin.build_unified_report = no_messages
    run = _run_analysis_due(runtime, task)
    assert [item["status"] for item in run["deliveries"]] == ["skipped", "skipped"]
    assert sent == []


def test_group_analysis_test_send_uses_only_the_explicit_group(tmp_path):
    runtime, task, _plugin, calls, sent, _account, _client = _analysis_runtime(tmp_path)
    result = asyncio.run(runtime.test({**task, "test_target": task["targets"][1]}))
    assert result["status"] == "submitted"
    assert [group for group, _ in sent] == ["987654@chatroom"]
    assert calls == [("wxid_robot", "987654@chatroom", 2)]
    assert runtime.store.list("wxid_robot") == []


def test_legacy_analysis_schedule_must_be_migrated_before_a_new_task_is_enabled(tmp_path):
    runtime, task, _plugin, _calls, sent, _account, _client = _analysis_runtime(tmp_path)
    config = tmp_path / "app/astrbot/data/config/astrbot_plugin_qq_group_daily_analysis_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"auto_analysis": {"scheduled_group_list": ["123456@chatroom"]}}))

    with pytest.raises(ValueError, match="先预览并确认迁移"):
        asyncio.run(runtime.save(task))
    assert runtime.store.list("wxid_robot") == []
    assert runtime.store.action_owner("group_analysis") is None
    assert sent == []


def test_slow_group_report_does_not_block_a_later_normal_task(tmp_path):
    runtime, task, plugin, calls, sent, _account, _client = _analysis_runtime(tmp_path)
    original = plugin.build_unified_report

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_report(*args):
            started.set()
            await release.wait()
            return await original(*args)

        plugin.build_unified_report = slow_report
        first = await runtime.save({**task, "id": "slow-analysis-a", "targets": task["targets"][:1]})
        await runtime.save({**task, "id": "slow-analysis-b", "targets": task["targets"][:1]})
        normal = await runtime.save({**_task(), "id": "ordinary-later", "schedule": {
            "kind": "daily", "time": "08:31", "timezone": "Asia/Shanghai",
        }})
        runtime.clock = lambda: datetime.fromisoformat(first["next_run_time"])
        reports = await runtime._dispatch_due()
        assert len(reports) == 2
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            runtime.clock = lambda: datetime.fromisoformat(normal["next_run_time"])
            ordinary = await runtime._dispatch_due()
            await asyncio.wait_for(asyncio.gather(*ordinary.values()), timeout=2)
            assert calls == []
            assert sent == [("123456@chatroom", [{"type": "text", "data": {"text": "提醒"}}])]
            assert runtime.store.runs("wxid_robot", normal["id"])[0]["status"] == "submitted"
            assert runtime.store.runs("wxid_robot", first["id"])[0]["status"] == "running"
            release.set()
            await asyncio.wait_for(asyncio.gather(*reports.values()), timeout=2)
            assert len(calls) == 2 and len(sent) == 3
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_stop_cancels_running_and_queued_reports_before_releasing_the_runtime_lease(tmp_path):
    runtime, task, plugin, _calls, sent, _account, _client = _analysis_runtime(tmp_path)
    cancelled = []

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_report(*_args):
            started.set()
            try:
                await release.wait()
            finally:
                cancelled.append(True)

        plugin.build_unified_report = slow_report
        first = await runtime.save({**task, "id": "slow-analysis-a", "targets": task["targets"][:1]})
        await runtime.save({**task, "id": "slow-analysis-b", "targets": task["targets"][:1]})
        runtime.clock = lambda: datetime.fromisoformat(first["next_run_time"])
        runtime.start()
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert len(runtime._workers) == 2
            assert runtime._lease.handle is not None
        finally:
            await runtime.stop()
        assert runtime._workers == {} and runtime._lease.handle is None
        assert runtime._poller is None and not runtime._active
        assert cancelled == [True]
        for identity in ("slow-analysis-a", "slow-analysis-b"):
            run = runtime.store.runs("wxid_robot", identity)[0]
            assert run["status"] == "skipped"
            assert all(item["status"] == "skipped" for item in run["deliveries"])

    asyncio.run(scenario())
    assert sent == []


def test_stop_finishes_claimed_runs_even_if_workers_have_not_started(tmp_path):
    runtime, task, _plugin, calls, sent, _account, _client = _analysis_runtime(tmp_path)

    async def scenario():
        saved = await runtime.save(task)
        runtime.clock = lambda: datetime.fromisoformat(saved["next_run_time"])
        assert await runtime._dispatch_due()
        await runtime.stop()
        assert runtime._workers == {}
        assert runtime.store.runs("wxid_robot", saved["id"])[0]["status"] == "skipped"

    asyncio.run(scenario())
    assert calls == sent == []


def test_runtime_health_exposes_command_scheduling_contract(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    client = FakeClient()

    async def connection():
        return "wxid_robot", client

    runtime = module.UnifiedRuntime(
        object(), install_root=tmp_path, content=FakeContent(),
        connection=connection,
    )
    runtime._active = True

    health = asyncio.run(runtime.health())

    assert health["runtime_ready"] is True
    assert health["command_definition_version"] == 1
    catalog = health["command_catalog"]
    assert any(item["id"] == "weather" and item["status"] == "supported" for item in catalog)
    assert not any(item["id"] == "checkin" for item in catalog)


def test_runtime_generates_once_and_submits_only_live_recipient(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    client = FakeClient()
    content = FakeContent()
    sent = []

    async def connection():
        return "wxid_robot", client

    async def sender(_client, run, target, generated):
        sent.append((run["id"], target["id"], generated.segments()))
        return {"message_id": "wechat-1"}

    runtime = module.UnifiedRuntime(object(), install_root=tmp_path, content=content,
                                    connection=connection, sender=sender, clock=lambda: now)
    saved = runtime.store.save(_task(), now=now)
    runtime.clock = lambda: datetime.fromisoformat(saved["next_run_time"])

    asyncio.run(runtime.run_once())

    assert content.generations == 1
    assert len(sent) == 1
    runs = runtime.store.runs("wxid_robot", "runtime-task")
    assert runs[0]["status"] == "submitted"
    assert runs[0]["deliveries"][0]["status"] == "submitted"


def test_runtime_skips_recipient_removed_from_live_directory(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    client = FakeClient(group_exists=False)
    sent = []

    async def connection():
        return "wxid_robot", client

    async def sender(*args):
        sent.append(args)
        return {"message_id": "unexpected"}

    runtime = module.UnifiedRuntime(object(), install_root=tmp_path, content=FakeContent(),
                                    connection=connection, sender=sender, clock=lambda: now)
    saved = runtime.store.save(_task(), now=now)
    runtime.clock = lambda: datetime.fromisoformat(saved["next_run_time"])

    asyncio.run(runtime.run_once())

    assert sent == []
    delivery = runtime.store.runs("wxid_robot", "runtime-task")[0]["deliveries"][0]
    assert delivery["status"] == "skipped"
    assert "当前账号目录" in delivery["message"]


def test_explicit_test_send_uses_temporary_authorized_run_and_leaves_no_task(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    client = FakeClient()
    sent = []

    async def connection():
        return "wxid_robot", client

    async def sender(_client, run, target, _content):
        assert run["task_id"].startswith("test_")
        sent.append(target["id"])
        return {"message_id": "test-1"}

    runtime = module.UnifiedRuntime(object(), install_root=tmp_path, content=FakeContent(),
                                    connection=connection, sender=sender, clock=lambda: now)
    runtime._active = True
    request = {**_task(), "test_target": {"type": "group", "id": "123456@chatroom"}}

    result = asyncio.run(runtime.test(request))

    assert result["status"] == "submitted"
    assert sent == ["123456@chatroom"]
    assert runtime.store.list("wxid_robot") == []


def test_short_drama_adapter_returns_validated_clickable_card(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)

    class Drama:
        async def build_unified_recommendation(self, media_type, limit):
            assert (media_type, limit) == ("短剧", 12)
            return {
                "type": "link_card",
                "title": "最新短剧推荐｜12部",
                "description": "定时推荐 · 点击查看封面并选择播放",
                "url": "https://player.example/short-drama/player?s=abcdefghijklmnop",
                "image_url": "https://player.example/cover.jpg",
                "fallback_text": "短剧甲\n短剧乙",
            }

    (tmp_path / "app/astrbot/data/config").mkdir(parents=True)
    (tmp_path / "app/astrbot/data/cmd_config.json").write_text(
        '{"plugin_set":["jiang_short_drama"]}', encoding="utf-8"
    )
    (tmp_path / "app/astrbot/data/config/jiang_short_drama_config.json").write_text(
        '{"stateless_player_url":"https://player.example/short-drama/player"}',
        encoding="utf-8",
    )
    context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
        root_dir_name="jiang_short_drama", activated=True, star_cls=Drama(),
    )])
    service = module.ContentService(context, tmp_path)

    content = asyncio.run(service.generate({
        "action": "short_drama",
        "arguments": {"media_type": "短剧", "limit": 12},
    }))

    assert content.preview()["kind"] == "link_card"
    assert content.segments() == [{"type": "wechat_link_card", "data": {
        "title": "最新短剧推荐｜12部",
        "desc": "定时推荐 · 点击查看封面并选择播放",
        "url": "https://player.example/short-drama/player?s=abcdefghijklmnop",
        "image_url": "https://player.example/cover.jpg",
    }}]


def test_short_drama_adapter_downgrades_untrusted_player_url_to_text(
    tmp_path: Path,
) -> None:
    module = _copy_runtime_package(tmp_path)

    class Drama:
        async def build_unified_recommendation(self, _media_type, _limit):
            return {
                "type": "link_card",
                "title": "最新短剧推荐｜2部",
                "description": "点击播放",
                "url": "https://untrusted.example/watch?id=1",
                "image_url": "https://images.example/cover.jpg",
                "fallback_text": "短剧甲\n短剧乙",
            }

    (tmp_path / "app/astrbot/data/config").mkdir(parents=True)
    (tmp_path / "app/astrbot/data/cmd_config.json").write_text(
        '{"plugin_set":["jiang_short_drama"]}', encoding="utf-8"
    )
    (tmp_path / "app/astrbot/data/config/jiang_short_drama_config.json").write_text(
        '{"stateless_player_url":"https://player.example/short-drama/player"}',
        encoding="utf-8",
    )
    context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
        root_dir_name="jiang_short_drama", activated=True, star_cls=Drama(),
    )])

    content = asyncio.run(module.ContentService(context, tmp_path).generate({
        "action": "short_drama",
        "arguments": {"media_type": "短剧", "limit": 2},
    }))

    assert content.preview() == {"kind": "text", "type": "text", "text": "短剧甲\n短剧乙"}


def test_hosted_player_url_validation_is_exact_and_public(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    content_module = sys.modules[module.ContentService.__module__]
    validate = content_module._hosted_player_url
    base = "https://player.example/short-drama/player"

    assert validate(f"{base}?s=abcdefghijklmnop", base)
    assert validate(f"{base}#r=abc_DEF-123", base)
    assert validate(f"{base}#p=abc_DEF-123&ep=1&force=0", base)

    for candidate in (
        "http://player.example/short-drama/player?s=abcdefghijklmnop",
        "https://untrusted.example/short-drama/player?s=abcdefghijklmnop",
        "https://user@player.example/short-drama/player?s=abcdefghijklmnop",
        "https://player.example:444/short-drama/player?s=abcdefghijklmnop",
        "https://player.example/other?s=abcdefghijklmnop",
        base,
        f"{base}?url=https%3A%2F%2Fmedia.example%2Fvideo.m3u8",
        f"{base}?s=abcdefghijklmnop#r=abc",
        f"{base}#p=abc&force=0&ep=1",
        f"{base}#p=abc&ep=0&force=0",
        f"{base}#p=abc&ep=1&force=2",
        f"{base}#r={'a' * 1801}",
        f"{base}#p={'a' * 1801}&ep=1&force=0",
        f"{base}#r=abc\nxyz",
    ):
        assert not validate(candidate, base), candidate

    for private_base in (
        "https://localhost/short-drama/player",
        "https://player.local/short-drama/player",
        "https://127.0.0.1/short-drama/player",
        "https://192.168.1.8/short-drama/player",
        "https://[::1]/short-drama/player",
    ):
        assert not validate(f"{private_base}?s=abcdefghijklmnop", private_base)


def test_short_drama_does_not_depend_on_commands_configuration(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)

    class Drama:
        async def build_unified_recommendation(self, _media_type, _limit):
            return {"type": "text", "text": "短剧甲"}

    (tmp_path / "app/astrbot/data/config").mkdir(parents=True)
    (tmp_path / "app/astrbot/data/cmd_config.json").write_text(
        '{"plugin_set":["jiang_short_drama"]}', encoding="utf-8"
    )
    (tmp_path / "app/astrbot/data/config/jiang_short_drama_config.json").write_text(
        '{"stateless_player_url":"https://player.example/short-drama/player"}',
        encoding="utf-8",
    )
    (tmp_path / "app/astrbot/data/config/jiang_commands_config.json").write_text(
        "not-json", encoding="utf-8"
    )
    context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
        root_dir_name="jiang_short_drama", activated=True, star_cls=Drama(),
    )])

    content = asyncio.run(module.ContentService(context, tmp_path).generate({
        "action": "short_drama",
        "arguments": {"media_type": "短剧", "limit": 1},
    }))

    assert content.preview()["text"] == "短剧甲"


def test_runtime_plugin_patches_apply_to_upstream_copy(tmp_path: Path) -> None:
    assert (PROJECT / "data/plugins/jiang_schedule/main.py").is_file()
    checkout = tmp_path / "schedule-plugin"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    shutil.copy2(PROJECT / "data/plugins/jiang_schedule/main.py", checkout / "main.py")
    for patch in (
        ROOT / "patches/astrbot-plugin-jiang_schedule.patch",
        ROOT / "patches/astrbot-plugin-jiang_schedule--permissions.patch",
        ROOT / "patches/astrbot-plugin-jiang_schedule--unified.patch",
    ):
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    for name in ("content.py", "unified_runtime.py"):
        shutil.copy2(ROOT / "runtime/jiang_schedule" / name, checkout / name)
    shutil.copy2(ROOT / "src/wechatrebot_manager/task_store.py", checkout / "task_store.py")
    source = (checkout / "main.py").read_text(encoding="utf-8")
    compile(source, str(checkout / "main.py"), "exec")
    assert "UnifiedRuntime" in source


def test_manual_weather_patch_reuses_the_scheduled_card_renderer(tmp_path: Path) -> None:
    source = PROJECT / "data/plugins/jiang_commands/main.py"
    assert source.is_file(), source
    checkout = tmp_path / "commands-plugin"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    shutil.copy2(source, checkout / "main.py")
    patch = ROOT / "patches/astrbot-plugin-jiang_commands--scheduled-weather.patch"
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    patched = (checkout / "main.py").read_text(encoding="utf-8")
    compile(patched, str(checkout / "main.py"), "exec")
    assert "generate_weather" in patched
    assert "render_weather_target_image = getattr(" in patched
    assert '"render_weather_target_image", render_weather_image' in patched


def test_short_drama_patch_exposes_unified_content_adapter(tmp_path: Path) -> None:
    source = PROJECT / "data/plugins/jiang_short_drama/main.py"
    assert source.is_file(), source
    checkout = tmp_path / "short-drama-plugin"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    shutil.copy2(source, checkout / "main.py")
    for patch in (
        ROOT / "patches/astrbot-plugin-jiang_short_drama.patch",
        ROOT / "patches/astrbot-plugin-jiang_short_drama--unified.patch",
        ROOT / "patches/astrbot-plugin-jiang_short_drama--zz-hosted-player.patch",
        ROOT / "patches/astrbot-plugin-jiang_short_drama--zzz-all-card-links.patch",
    ):
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    patched = (checkout / "main.py").read_text(encoding="utf-8")
    compile(patched, str(checkout / "main.py"), "exec")
    assert "build_unified_recommendation" in patched
    assert "await self.player_server.start()" not in patched
    assert "build_watch_url(" not in patched
    assert "self.player_server.create_" not in patched
    assert "candidate.hostname == base.hostname" in patched

    tree = ast.parse(patched)
    main_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Main"
    )
    hosted_method = next(
        node for node in main_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_hosted_player_url"
    )
    method_source = ast.get_source_segment(patched, hosted_method)
    assert method_source is not None
    namespace = {"ipaddress": ipaddress, "re": re, "urlsplit": urlsplit}
    exec(
        "class Harness:\n" + textwrap.indent(textwrap.dedent(method_source), "    "),
        namespace,
    )
    harness = namespace["Harness"]()
    harness.config = {
        "stateless_player_url": "https://player.example/short-drama/player"
    }
    assert harness._hosted_player_url(
        "https://player.example/short-drama/player?s=abcdefghijklmnop"
    )
    assert harness._hosted_player_url(
        "https://player.example/short-drama/player#p=abc&ep=1&force=0"
    )
    assert harness._hosted_player_url(
        "https://player.example/short-drama/player?url=https://evil.example/x.m3u8"
    ) is None
    assert harness._hosted_player_url(
        "https://untrusted.example/short-drama/player?s=abcdefghijklmnop"
    ) is None
    assert harness._hosted_player_url(
        "https://player.example/short-drama/player#p="
        + ("a" * 1801)
        + "&ep=1&force=0"
    ) is None
    harness.config["stateless_player_url"] = "https://127.0.0.1/player"
    assert harness._hosted_player_url(
        "https://127.0.0.1/player?s=abcdefghijklmnop"
    ) is None

    cover_function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_safe_card_image_url"
    )
    cover_source = ast.get_source_segment(patched, cover_function)
    assert cover_source is not None
    cover_namespace = {
        "_DEFAULT_COVER": "https://player.example/default.ico",
        "ipaddress": ipaddress,
        "re": re,
        "urlsplit": urlsplit,
    }
    exec(textwrap.dedent(cover_source), cover_namespace)
    safe_cover = cover_namespace["_safe_card_image_url"]
    assert safe_cover("https://images.example/cover.jpg") == (
        "https://images.example/cover.jpg"
    )
    assert safe_cover("http://images.example/cover.jpg") == cover_namespace["_DEFAULT_COVER"]
    assert safe_cover("https://127.0.0.1/cover.jpg") == cover_namespace["_DEFAULT_COVER"]
