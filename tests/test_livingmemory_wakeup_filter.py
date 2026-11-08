"""Exercise delivered synthetic-event filtering without starting AstrBot."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_hot_config_runtime import _runtime_function
from test_livingmemory_patch import patched_plugin


MESSAGE_TYPE = SimpleNamespace(GROUP_MESSAGE="GroupMessage")
SESSION_ID = "wechat-account:FriendMessage:example-user"


def _event(*, platform="aiocqhttp", event_kind=None, group=False):
    event = SimpleNamespace(
        unified_msg_origin=SESSION_ID,
        get_platform_name=lambda: platform,
        get_sender_id=lambda: "example-user",
        get_self_id=lambda: "bot-account",
        get_message_type=lambda: "GroupMessage" if group else "FriendMessage",
    )
    if event_kind is not None:
        event.event_kind = event_kind
    return event


def _manager(plugin: Path):
    manager = SimpleNamespace(
        identity_aliases="",
        _resolve_sender_name=Mock(return_value="用户昵称"),
        _resolve_bot_identity=Mock(return_value=("bot-account", "机器人昵称")),
        add_message=AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    method = _runtime_function(
        (plugin / "core/managers/conversation_manager.py").read_text("utf-8"),
        "add_message_from_event",
        {
            "logger": Mock(), "MessageType": MESSAGE_TYPE,
            "resolve_sender_alias": lambda aliases, platform, sender_id, name: name,
        },
    )
    manager.add_message_from_event = MethodType(method, manager)
    return manager


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize(
    "platform,event_kind", [("cron", None), ("aiocqhttp", "agent_wakeup")]
)
def test_synthetic_turn_never_reaches_history_write(
    patched_plugin: Path, role: str, platform: str, event_kind: str | None
):
    manager = _manager(patched_plugin)
    result = asyncio.run(manager.add_message_from_event(
        _event(platform=platform, event_kind=event_kind), role, "合成任务内容"
    ))
    assert result is None
    manager.add_message.assert_not_awaited()
    manager._resolve_sender_name.assert_not_called()
    manager._resolve_bot_identity.assert_not_called()


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("group", [False, True])
def test_real_dialogue_preserves_session_and_sender_identity(
    patched_plugin: Path, role: str, group: bool
):
    manager = _manager(patched_plugin)
    result = asyncio.run(manager.add_message_from_event(
        _event(group=group), role, "真实对话内容"
    ))
    assert result is manager.add_message.return_value
    manager.add_message.assert_awaited_once_with(
        session_id=SESSION_ID,
        role=role,
        content="真实对话内容",
        sender_id="bot-account" if role == "assistant" else "example-user",
        sender_name="机器人昵称" if role == "assistant" else "用户昵称",
        group_id=SESSION_ID if group else None,
        platform="aiocqhttp",
        is_bot_message=role == "assistant",
    )


def test_legacy_normal_event_without_platform_still_writes(patched_plugin: Path):
    manager = _manager(patched_plugin)
    event = _event()
    del event.get_platform_name
    asyncio.run(manager.add_message_from_event(event, "user", "旧事件内容"))
    assert manager.add_message.await_args.kwargs["platform"] == "unknown"


@pytest.mark.parametrize(
    "platform,event_kind", [("cron", None), ("aiocqhttp", "agent_wakeup")]
)
def test_synthetic_assistant_does_not_advance_reflection_threshold(
    patched_plugin: Path, platform: str, event_kind: str | None
):
    manager = _manager(patched_plugin)
    existing_rows = [SimpleNamespace(role="user")] * 19
    saved_cursor = 0

    async def persist(**kwargs):
        existing_rows.append(SimpleNamespace(**kwargs))
        return existing_rows[-1]

    manager.add_message.side_effect = persist
    manager.get_session_info = AsyncMock(return_value=SimpleNamespace(message_count=19))
    manager.store = SimpleNamespace(
        get_message_count=AsyncMock(side_effect=lambda _: len(existing_rows))
    )
    manager.get_session_metadata = AsyncMock(
        side_effect=lambda session, key, default: saved_cursor
        if key == "last_summarized_index" else default
    )
    manager.update_session_metadata = AsyncMock()
    manager.get_messages_range = AsyncMock(return_value=[])
    handler = SimpleNamespace(
        config_manager=SimpleNamespace(get=lambda key, default: default),
        conversation_manager=manager,
        message_utils=SimpleNamespace(enforce_message_limit=AsyncMock()),
        _schedule_consolidation=Mock(),
    )
    logger = Mock()
    method = _runtime_function(
        (patched_plugin / "core/event_handler_modules/memory_reflection.py").read_text("utf-8"),
        "handle_memory_reflection",
        {
            "asyncio": asyncio, "logger": logger, "MessageType": MESSAGE_TYPE,
            "is_event_memory_allowed": lambda config, event: True,
        },
    )
    response = SimpleNamespace(
        role="assistant", completion_text="定时任务最终结果",
        tools_call_name=[], tools_call_extra_content=None,
    )
    asyncio.run(method(handler, _event(platform=platform, event_kind=event_kind), response))

    assert len(existing_rows) == 19
    manager.add_message.assert_not_awaited()
    manager.store.get_message_count.assert_awaited_once_with(SESSION_ID)
    manager.update_session_metadata.assert_not_awaited()
    manager.get_messages_range.assert_not_awaited()
    handler._schedule_consolidation.assert_not_called()
    logger.error.assert_not_called()
