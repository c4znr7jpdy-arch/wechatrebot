"""Exercise delivered meme commands with the pinned core's real session waiter."""
from __future__ import annotations

import ast
import asyncio
import builtins
from pathlib import Path
import shutil
import subprocess
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Event:
    def __init__(self, message="", *, origin="group:a", sender="admin"):
        self.message_str = message
        self.unified_msg_origin = origin
        self.sender = sender
        self.sent = []

    def get_sender_id(self):
        return self.sender

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.sent.append(result)


@pytest.fixture(scope="module")
def delivered_commands(tmp_path_factory):
    target = tmp_path_factory.mktemp("meme-command-runtime")
    command_file = target / "mixins/commands.py"
    command_file.parent.mkdir()
    shutil.copy2(ROOT / "runtime/meme_manager/mixins/commands.py", command_file)
    subprocess.run(["git", "init", "--quiet"], cwd=target, check=True)
    patches = ROOT / "patches"
    for patch in [patches / "astrbot-plugin-meme_manager.patch",
                  *sorted(patches.glob("astrbot-plugin-meme_manager--*.patch"))]:
        subprocess.run(
            ["git", "apply", "--whitespace=error-all", "--include=mixins/commands.py", str(patch)],
            cwd=target, check=True, capture_output=True,
        )
    return command_file.read_text(encoding="utf-8")


def _command_group(_name):
    def decorate(function):
        function.command = lambda _command: lambda method: method
        return function
    return decorate


@pytest.fixture
def runtime(delivered_commands):
    waiter = ModuleType("pinned_session_waiter")
    waiter.AstrMessageEvent = Event
    waiter.Comp = SimpleNamespace(BaseMessageComponent=object)
    tree = ast.parse((ROOT / "vendor/engine/astrbot/core/utils/session_waiter.py").read_text("utf-8"))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and (node.module or "").startswith("astrbot.")
        or isinstance(node, ast.Import) and any(alias.name.startswith("astrbot.") for alias in node.names)
    )]
    exec(compile(tree, "pinned-session-waiter", "exec"), waiter.__dict__)
    image_store = SimpleNamespace(
        clear_all_emojis=Mock(return_value={"deleted_by_category": {"开心": 1}}),
        clear_category_emojis=Mock(return_value={"deleted_files": ["one.png"]}),
        get_emoji_by_category=Mock(return_value=["one.png"]),
    )
    imports = {
        "astrbot.api": SimpleNamespace(logger=Mock()),
        "astrbot.api.all": SimpleNamespace(__all__=[]),
        "astrbot.api.event": SimpleNamespace(AstrMessageEvent=Event, filter=SimpleNamespace(
            command_group=_command_group,
            permission_type=lambda _permission: lambda method: method,
            PermissionType=SimpleNamespace(ADMIN="admin"),
        )),
        "astrbot.api.util": SimpleNamespace(session_waiter=waiter.session_waiter),
        "astrbot.core.utils.session_waiter": waiter,
        "backend.packs.images": image_store,
        "backend.packs.storage": SimpleNamespace(install_first_official_pack_from_index=Mock()),
        "backend.semantic.storage": SimpleNamespace(invalidate_semantic_metadata=Mock()),
        "config": SimpleNamespace(COMMUNITY_INDEX_URL="unused-test-index"),
    }

    def isolated_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in imports:
            return imports[name]
        return builtins.__import__(name, globals, locals, fromlist, level)

    scope = {"__builtins__": {**vars(builtins), "__import__": isolated_import},
             "__name__": "isolated_meme_commands"}
    exec(compile(delivered_commands, "delivered-meme-commands", "exec"), scope)
    owner = scope["CommandMixin"]()
    owner._get_manageable_categories = lambda: ["开心"]
    owner.category_manager = SimpleNamespace(
        create_category=Mock(return_value=True), delete_category=Mock(return_value=True),
    )

    async def run_mutation(_label, action):
        return action()

    owner._run_default_pack_mutation = AsyncMock(side_effect=run_mutation)
    owner._reload_personas = Mock()
    owner._invalidate_default_pack_semantics = Mock()
    return SimpleNamespace(owner=owner, waiter=waiter, scope=scope, images=image_store)


async def _registered(runtime, count=1):
    for _ in range(100):
        if len(runtime.waiter.USER_SESSIONS) == count:
            return
        await asyncio.sleep(0)
    pytest.fail("Production command did not register its session waiter")


async def _reply(runtime, event):
    for session_filter in list(runtime.waiter.FILTERS):
        await runtime.waiter.SessionWaiter.trigger(session_filter.filter(event), event)


async def _collect(generator):
    return [result async for result in generator]


def _assert_clean(runtime):
    assert not runtime.waiter.USER_SESSIONS
    assert not runtime.waiter.FILTERS


def test_add_category_accepts_description_after_empty_reply(runtime):
    async def scenario():
        command = runtime.owner.add_category_command(Event("表情管理 添加分类 新类别"), "新类别")
        assert "请发送" in await anext(command)
        task = asyncio.create_task(_collect(command))
        await _registered(runtime)
        empty = Event("  ")
        await _reply(runtime, empty)
        assert "描述" in empty.sent[0]
        assert not task.done()
        await _reply(runtime, Event("用于开心时发送"))
        assert "已创建" in (await task)[0]
        runtime.owner.category_manager.create_category.assert_called_once_with("新类别", "用于开心时发送")
        _assert_clean(runtime)
    asyncio.run(scenario())


def test_add_category_with_inline_description_does_not_wait(runtime):
    results = asyncio.run(_collect(runtime.owner.add_category_command(
        Event("表情管理 添加分类 新类别 已有描述"), "新类别",
    )))
    assert "已创建" in results[0]
    runtime.owner.category_manager.create_category.assert_called_once_with("新类别", "已有描述")
    _assert_clean(runtime)


@pytest.mark.parametrize("outcome", ["cancel", "timeout"])
def test_add_category_cancellation_never_creates_category(runtime, outcome):
    async def scenario():
        event = Event("表情管理 添加分类 新类别")
        command = runtime.owner.add_category_command(event, "新类别")
        await anext(command)
        if outcome == "timeout":
            original = runtime.owner._wait_for_category_description
            runtime.owner._wait_for_category_description = lambda event, category: original(event, category, timeout=0.001)
        task = asyncio.create_task(_collect(command))
        await _registered(runtime)
        if outcome == "cancel":
            reply = Event("返回")
            await _reply(runtime, reply)
            assert "已取消" in reply.sent[0]
        results = await task
        if outcome == "cancel":
            assert results == []
        else:
            assert "超时" in results[0]
        runtime.owner._run_default_pack_mutation.assert_not_awaited()
        runtime.owner.category_manager.create_category.assert_not_called()
        _assert_clean(runtime)
    asyncio.run(scenario())


@pytest.mark.parametrize("confirmation", ["确认", "确定"])
def test_confirmation_accepts_only_valid_reply_after_reprompt(runtime, confirmation):
    async def scenario():
        task = asyncio.create_task(runtime.owner._wait_for_command_confirmation(Event()))
        await _registered(runtime)
        invalid = Event("随便")
        await _reply(runtime, invalid)
        assert "请回复" in invalid.sent[0]
        assert not task.done()
        await _reply(runtime, Event(confirmation))
        assert await task is True
        _assert_clean(runtime)
    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["clear_category_command", "clear_all_emojis_command", "delete_category_command"])
@pytest.mark.parametrize("outcome", ["confirm", "cancel", "timeout"])
def test_destructive_commands_require_confirmation(runtime, method, outcome):
    async def scenario():
        event = Event()
        arguments = () if method == "clear_all_emojis_command" else ("开心",)
        command = getattr(runtime.owner, method)(event, *arguments)
        assert "请在 30 秒内回复" in await anext(command)
        if outcome == "timeout":
            original = runtime.owner._wait_for_command_confirmation
            runtime.owner._wait_for_command_confirmation = lambda event: original(event, timeout=0.001)
        task = asyncio.create_task(_collect(command))
        await _registered(runtime)
        if outcome != "timeout":
            await _reply(runtime, Event("确认" if outcome == "confirm" else "取消"))
        results = await task
        if outcome == "confirm":
            assert "✅" in results[0]
            runtime.owner._run_default_pack_mutation.assert_awaited_once()
        else:
            assert results == []
            runtime.owner._run_default_pack_mutation.assert_not_awaited()
            runtime.images.clear_all_emojis.assert_not_called()
            runtime.images.clear_category_emojis.assert_not_called()
            runtime.owner.category_manager.delete_category.assert_not_called()
            if outcome == "timeout":
                assert "超时" in event.sent[0]
        _assert_clean(runtime)
    asyncio.run(scenario())


def test_confirmation_waiters_are_isolated_by_sender_and_session(runtime):
    async def scenario():
        first = asyncio.create_task(runtime.owner._wait_for_command_confirmation(Event(sender="first")))
        second = asyncio.create_task(runtime.owner._wait_for_command_confirmation(Event(sender="second")))
        await _registered(runtime, 2)
        await _reply(runtime, Event("确认", sender="stranger"))
        await _reply(runtime, Event("确认", origin="group:b", sender="first"))
        assert not first.done() and not second.done()
        await _reply(runtime, Event("确认", sender="first"))
        assert await first is True
        assert not second.done()
        await _reply(runtime, Event("退出", sender="second"))
        assert await second is False
        _assert_clean(runtime)
    asyncio.run(scenario())


def test_session_filter_handles_delimiters_and_numeric_sender_ids(runtime):
    session_filter = runtime.scope["SenderScopedSessionFilter"]()
    assert session_filter.filter(Event(origin="a|b", sender="c")) != session_filter.filter(Event(origin="a", sender="b|c"))
    assert session_filter.filter(Event(sender=123)) == session_filter.filter(Event(sender="123"))
