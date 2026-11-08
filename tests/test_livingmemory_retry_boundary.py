"""Keep new history eligible after an older summary exhausts its retry budget."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import MethodType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_hot_config_runtime import _runtime_function
from test_livingmemory_patch import patched_plugin  # noqa: F401
from test_livingmemory_transactions import _Connection
from test_livingmemory_reset_generation import conversation_manager_type


SESSION = "offline:GroupMessage:room"
MISSING = object()


@pytest.fixture
def reflection(patched_plugin: Path, tmp_path):
    connection = _Connection(tmp_path / "history.sqlite")
    connection.raw.executescript("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, metadata TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT);
    """)
    connection.raw.execute("INSERT INTO sessions VALUES (?, '{}')", (SESSION,))
    connection.raw.executemany("INSERT INTO messages VALUES (?, ?)",
                              [(index, f"message {index}") for index in range(100)])
    connection.raw.commit()

    async def get_session(_session):
        row = connection.raw.execute("SELECT metadata FROM sessions WHERE session_id = ?", (SESSION,)).fetchone()
        return NS(metadata=json.loads(row[0]), message_count=100)

    async def get_range(*, session_id, start_index, end_index):
        return connection.raw.execute("SELECT * FROM messages ORDER BY id LIMIT ? OFFSET ?",
                                      (end_index - start_index, start_index)).fetchall()

    manager = conversation_manager_type(patched_plugin)(
        NS(connection=connection, get_session=get_session, get_message_count=AsyncMock(return_value=100))
    )
    manager.add_message_from_event = AsyncMock()
    manager.get_session_info = get_session
    manager.get_messages_range = AsyncMock(side_effect=get_range)
    scope = {"asyncio": asyncio, "json": json, "logger": Mock(),
             "MessageType": NS(GROUP_MESSAGE="group"), "is_event_memory_allowed": lambda *_: True,
             "resolve_memory_scope": lambda *_: SESSION, "get_persona_id": AsyncMock(return_value="persona")}
    manager_source = (patched_plugin / "core/managers/conversation_manager.py").read_text("utf-8")
    for name in ("get_session_metadata", "update_session_metadata"):
        setattr(manager, name, MethodType(_runtime_function(manager_source, name, scope), manager))
    source = (patched_plugin / "core/event_handler_modules/memory_reflection.py").read_text("utf-8")
    handler = NS(context=NS(), config_manager=NS(get=lambda _key, default: default), conversation_manager=manager,
                 message_utils=NS(enforce_message_limit=AsyncMock()), _schedule_consolidation=Mock(),
                 _storage_tasks=set(), _storage_sessions_inflight=set(), _storage_state_lock=asyncio.Lock(),
                 _storage_task=AsyncMock(), _shutting_down=False, _on_storage_task_done=Mock())
    handler.handle_memory_reflection = MethodType(_runtime_function(source, "handle_memory_reflection", scope), handler)
    event = NS(unified_msg_origin=SESSION, get_message_type=lambda: "group")
    response = NS(role="assistant", completion_text="A valid reply", tools_call_name=[], tools_call_extra_content=None)

    def seed(*, end=20, cursor=0, retries=3):
        pending = {"start_index": cursor, "retry_count": retries}
        if end is not MISSING:
            pending["end_index"] = end
        connection.raw.execute("UPDATE sessions SET metadata = ?", (
            json.dumps({"last_summarized_index": cursor, "pending_summary": pending, "other": "kept"}),))
        connection.raw.commit()

    def read():
        return json.loads(connection.raw.execute("SELECT metadata FROM sessions").fetchone()[0])

    try:
        yield handler, event, response, seed, read, connection
    finally:
        connection.raw.close()


def test_retry_exhaustion_only_abandons_failed_range_and_new_history_is_processed_next(reflection):
    handler, event, response, seed, read, connection = reflection
    seed()

    async def run():
        await handler.handle_memory_reflection(event, response)
        assert read() == {"last_summarized_index": 20, "pending_summary": None, "other": "kept"}
        handler._storage_task.assert_not_awaited()
        await handler.handle_memory_reflection(event, response)
        await asyncio.gather(*handler._storage_tasks)

    asyncio.run(run())
    call = handler._storage_task.await_args
    assert len(call.args[1]) == 80
    assert [row["id"] for row in call.args[1]] == list(range(20, 100))
    assert call.args[3:6] == (20, 100, 0)
    assert connection.raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 100
    assert not connection.in_transaction


@pytest.mark.parametrize("end,cursor,expected", [
    (100, 0, 100), (150, 0, 100), (20, 10, 20), (5, 10, 10), (-1, 10, 10),
    (MISSING, 10, 10), (None, 10, 10), ("bad", 10, 10), ("20", 10, 20),
])
def test_retry_boundary_stays_inside_retained_history_without_moving_backwards(reflection, end, cursor, expected):
    handler, event, response, seed, read, _connection = reflection
    seed(end=end, cursor=cursor)
    asyncio.run(handler.handle_memory_reflection(event, response))
    assert read()["last_summarized_index"] == expected
    assert read()["pending_summary"] is None
    assert read()["other"] == "kept"


def test_nonexhausted_failure_still_retries_the_existing_merged_range(reflection):
    handler, event, response, seed, read, _connection = reflection
    seed(retries=2)

    async def run():
        await handler.handle_memory_reflection(event, response)
        await asyncio.gather(*handler._storage_tasks)

    asyncio.run(run())
    assert handler._storage_task.await_args.args[3:6] == (0, 100, 2)
    assert read()["last_summarized_index"] == 0
