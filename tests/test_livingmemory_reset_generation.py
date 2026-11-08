"""Interleave real history reset and summary commits on real SQLite."""
import asyncio
import ast
from collections import OrderedDict
from contextlib import asynccontextmanager
import json
import sys
import time
from types import MethodType, ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from weakref import WeakValueDictionary

import pytest

from test_hot_config_runtime import _runtime_function
from test_livingmemory_patch import patched_plugin  # noqa: F401
from test_livingmemory_transactions import _Connection, _engine

SESSION = "offline:GroupMessage:room"


def conversation_manager_type(plugin):
    source = (plugin / "core/managers/conversation_manager.py").read_text("utf-8")
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef))
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node,
    ], type_ignores=[])
    scope = dict(asyncio=asyncio, json=json, time=time, OrderedDict=OrderedDict,
                 asynccontextmanager=asynccontextmanager, WeakValueDictionary=WeakValueDictionary, logger=Mock())
    exec(compile(ast.fix_missing_locations(module), "conversation-manager", "exec"), scope)
    return scope[node.name]


@pytest.fixture
def summaries(patched_plugin, tmp_path, monkeypatch):
    db = _Connection(tmp_path / "memory.sqlite")
    db.raw.executescript("""
        CREATE TABLE sessions(session_id TEXT PRIMARY KEY, metadata TEXT, message_count INTEGER);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, content TEXT);
        CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT);
    """)
    db.raw.execute("INSERT INTO sessions VALUES (?, '{}', 20)", (SESSION,))
    db.raw.executemany("INSERT INTO messages VALUES (?, ?, ?)", [(i, SESSION, f"old {i}") for i in range(20)])
    db.raw.commit()

    async def get_session(session):
        row = db.raw.execute("SELECT metadata, message_count FROM sessions WHERE session_id=?", (session,)).fetchone()
        return NS(metadata=json.loads(row[0]), message_count=row[1], created_at=0, last_active_at=0)

    async def get_count(session):
        return db.raw.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session,)).fetchone()[0]

    async def get_range(**kwargs):
        return [NS(content=row[0], group_id="room") for row in db.raw.execute(
            "SELECT content FROM messages WHERE session_id=? ORDER BY id LIMIT ? OFFSET ?",
            (kwargs["session_id"], kwargs["end_index"] - kwargs["start_index"], kwargs["start_index"]))]

    scope = dict(asyncio=asyncio, json=json, time=time, logger=Mock(), _DEFAULT_MEMORY_SCOPE=object(),
                 is_event_memory_allowed=lambda *_: True, resolve_memory_scope=lambda *_: SESSION,
                 t=lambda key, **_kwargs: key, t_list=lambda _: [])
    store = NS(connection=db, _write_lock=asyncio.Lock(), get_session=get_session, get_message_count=get_count)
    delete_source = (patched_plugin / "storage/conversation_store_messages.py").read_text("utf-8")
    store.delete_session_messages = MethodType(_runtime_function(delete_source, "delete_session_messages", scope), store)
    manager = conversation_manager_type(patched_plugin)(store)
    manager.get_messages_range = get_range

    # Exercise the real MemoryEngine entrypoint and write journal. Only vector
    # retrieval's document insert is replaced with its SQLite storage boundary.
    engine = _engine(patched_plugin, db)
    engine.atom_enabled = False
    engine._invalidate_search_cache = Mock()
    write_source = (patched_plugin / "core/managers/memory_engine_write_ops.py").read_text("utf-8")
    engine._create_write_ops_table = MethodType(_runtime_function(write_source, "_create_write_ops_table", scope), engine)
    asyncio.run(engine._create_write_ops_table(db))

    async def insert_document(content, metadata):
        cursor = await db.execute("INSERT INTO documents(text, metadata) VALUES (?, ?)", (content, json.dumps(metadata)))
        await db.commit()
        return cursor.lastrowid

    engine.hybrid_retriever = NS(add_memory=AsyncMock(side_effect=insert_document))
    engine_source = (patched_plugin / "core/managers/memory_engine_crud.py").read_text("utf-8")
    engine.add_memory = MethodType(_runtime_function(engine_source, "add_memory", scope), engine)
    utils = ModuleType("reset_probe.core.utils")
    utils.get_persona_id = AsyncMock(return_value="persona")
    tree = ast.parse((patched_plugin / "core/utils/__init__.py").read_text("utf-8"))
    operation = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OperationContext")
    exec(compile(ast.Module(body=[operation], type_ignores=[]), "operation-context", "exec"), scope)
    utils.OperationContext = scope["OperationContext"]
    monkeypatch.setitem(sys.modules, utils.__name__, utils)
    owner = NS(conversation_manager=manager, memory_engine=engine, context=NS(),
               config_manager=NS(get=lambda _key, default: default),
               message_utils=NS(enforce_message_limit=AsyncMock()), _schedule_consolidation=Mock(),
               _storage_tasks=set(), _storage_sessions_inflight=set(), _storage_state_lock=asyncio.Lock(),
               _shutting_down=False, _on_storage_task_done=Mock())
    auto_scope = dict(scope, __package__="reset_probe.core.event_handler_modules")
    auto_scope.update(MessageType=NS(GROUP_MESSAGE="group"), get_persona_id=utils.get_persona_id)
    auto_source = (patched_plugin / "core/event_handler_modules/memory_reflection.py").read_text("utf-8")
    owner._record_pending_summary = MethodType(_runtime_function(auto_source, "_record_pending_summary", auto_scope), owner)
    automatic = _runtime_function(auto_source, "_storage_task", auto_scope)
    owner._storage_task = MethodType(automatic, owner)
    reflection = _runtime_function(auto_source, "handle_memory_reflection", auto_scope)
    manual = _runtime_function((patched_plugin / "core/command_handler.py").read_text("utf-8"), "handle_summarize",
                               dict(scope, __package__="reset_probe.core"))

    def seed_new():
        db.raw.executemany("INSERT INTO messages VALUES (?, ?, ?)", [(20+i, SESSION, f"new {i}") for i in range(6)])
        db.raw.execute("UPDATE sessions SET message_count=6 WHERE session_id=?", (SESSION,))
        db.raw.commit()

    async def run_summary(mode):
        if mode == "auto":
            messages = await get_range(session_id=SESSION, start_index=0, end_index=await get_count(SESSION))
            await automatic(owner, SESSION, messages, "persona", 0, len(messages), 0)
        else:
            event = NS(unified_msg_origin=SESSION, plain_result=lambda value: value)
            return [item async for item in manual(owner, event)]

    try:
        yield NS(db=db, owner=owner, manager=manager, engine=engine, seed_new=seed_new,
                 run_summary=run_summary, get_session=get_session, automatic=automatic,
                 reflection=reflection, persona=utils.get_persona_id)
    finally:
        db.raw.close()


@pytest.mark.parametrize("mode", ["auto", "manual"])
@pytest.mark.parametrize("outcome", ["success", "provider-error", "cancel"])
def test_reset_discards_inflight_old_summary_and_new_history_still_commits(summaries, mode, outcome):
    case = summaries

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def process(**kwargs):
            started.set()
            await release.wait()
            if outcome == "provider-error":
                raise OSError("synthetic provider failure")
            return "old summary", {}, .5

        processor = NS(process_conversation=process, classify_atoms_from_metadata=lambda **_: [])
        case.owner.memory_processor = case.owner._memory_processor = processor
        task = asyncio.create_task(case.run_summary(mode))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await case.manager.clear_session(SESSION)
            case.seed_new()
            if outcome == "cancel":
                task.cancel()
            release.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            if mode == "manual" and outcome == "success":
                assert result[-1] == "summarize.cancelled_by_reset"
            assert (await case.get_session(SESSION)).metadata == {}
            assert case.db.raw.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
            assert case.db.raw.execute("SELECT COUNT(*) FROM memory_write_ops").fetchone()[0] == 0
            case.engine.hybrid_retriever.add_memory.assert_not_awaited()

            async def new_process(**kwargs):
                assert all(message.content.startswith("new ") for message in kwargs["messages"])
                return "new summary", {}, .5

            processor.process_conversation = new_process
            await case.run_summary(mode)
            assert (await case.get_session(SESSION)).metadata == {"last_summarized_index": 6, "pending_summary": None}
            assert case.db.raw.execute("SELECT text FROM documents").fetchone()[0] == "new summary"
            journal = case.db.raw.execute("SELECT status FROM memory_write_ops").fetchall()
            assert [row[0] for row in journal] == ["completed"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_clear_waits_for_already_started_memory_commit_then_resets_cursor(summaries, mode):
    case = summaries

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        original = case.engine.hybrid_retriever.add_memory.side_effect

        async def pause_commit(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        case.engine.hybrid_retriever.add_memory.side_effect = pause_commit
        processor = NS(process_conversation=AsyncMock(return_value=("committing summary", {}, .5)),
                       classify_atoms_from_metadata=lambda **_: [])
        case.owner.memory_processor = case.owner._memory_processor = processor
        task = asyncio.create_task(case.run_summary(mode))
        clear = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            clear = asyncio.create_task(case.manager.clear_session(SESSION))
            await asyncio.wait({clear}, timeout=.01)
            assert not clear.done()
            release.set()
            await task
            await clear
            assert (await case.get_session(SESSION)).metadata == {}
            assert case.db.raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert case.db.raw.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        finally:
            release.set()
            await asyncio.gather(*[item for item in (task, clear) if item], return_exceptions=True)

    asyncio.run(run())


def test_queued_automatic_summary_retains_revision_captured_with_history(summaries):
    case = summaries

    async def run():
        history = [NS(content=f"old {index}", group_id="room") for index in range(20)]
        revision = case.manager._summary_revision(SESSION)
        processor = NS(process_conversation=AsyncMock(return_value=("old summary", {}, .5)))
        case.owner.memory_processor = processor
        await case.manager.clear_session(SESSION)
        case.seed_new()
        await case.automatic(case.owner, SESSION, history, "persona", 0, 20, 0, session_revision=revision)
        processor.process_conversation.assert_not_awaited()
        assert (await case.get_session(SESSION)).metadata == {}
        assert case.db.raw.execute("SELECT COUNT(*) FROM memory_write_ops").fetchone()[0] == 0

    asyncio.run(run())


def test_late_retry_marker_and_cursor_repair_cannot_cross_reset(summaries):
    case = summaries

    async def run():
        revision = case.manager._summary_revision(SESSION)
        await case.manager.clear_session(SESSION)
        case.seed_new()
        await case.owner._record_pending_summary(SESSION, 0, 20, 0, revision)
        assert not await case.manager._summary_update_metadata(
            SESSION, revision, {"last_summarized_index": 20, "pending_summary": None}
        )
        assert (await case.get_session(SESSION)).metadata == {}
        assert await case.manager._summary_update_metadata(
            SESSION, case.manager._summary_revision(SESSION), {"other": "kept"}
        )
        assert (await case.get_session(SESSION)).metadata == {"other": "kept"}

    asyncio.run(run())


def test_reflection_scheduler_preserves_history_revision_across_reset(summaries):
    case = summaries

    async def run():
        reached_persona, release = asyncio.Event(), asyncio.Event()

        async def pause_persona(*_args):
            reached_persona.set()
            await release.wait()
            return "persona"

        case.persona.side_effect = pause_persona
        case.manager.add_message_from_event = AsyncMock()
        processor = NS(process_conversation=AsyncMock(return_value=("old summary", {}, .5)))
        case.owner.memory_processor = processor
        event = NS(unified_msg_origin=SESSION, get_message_type=lambda: "group")
        response = NS(role="assistant", completion_text="valid reply", tools_call_name=[],
                      tools_call_extra_content=None)
        task = asyncio.create_task(case.reflection(case.owner, event, response))
        try:
            await asyncio.wait_for(reached_persona.wait(), 1)
            await case.manager.clear_session(SESSION)
            case.seed_new()
            release.set()
            await task
            await asyncio.gather(*case.owner._storage_tasks)
            processor.process_conversation.assert_not_awaited()
            assert (await case.get_session(SESSION)).metadata == {}
            assert case.db.raw.execute("SELECT COUNT(*) FROM memory_write_ops").fetchone()[0] == 0
        finally:
            release.set()
            await asyncio.gather(task, *case.owner._storage_tasks, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_summary_started_during_clear_cannot_snapshot_undeleted_history(summaries, mode):
    case = summaries

    async def run():
        entered_delete, release = asyncio.Event(), asyncio.Event()
        original = case.manager.store.delete_session_messages

        async def pause_delete(session):
            entered_delete.set()
            await release.wait()
            return await original(session)

        case.manager.store.delete_session_messages = pause_delete
        case.manager.add_message_from_event = AsyncMock()
        processor = NS(process_conversation=AsyncMock(return_value=("old summary", {}, .5)),
                       classify_atoms_from_metadata=lambda **_: [])
        case.owner.memory_processor = case.owner._memory_processor = processor
        event = NS(unified_msg_origin=SESSION, plain_result=lambda value: value,
                   get_message_type=lambda: "group")
        response = NS(role="assistant", completion_text="valid reply", tools_call_name=[],
                      tools_call_extra_content=None)
        clear = asyncio.create_task(case.manager.clear_session(SESSION))
        summary = None
        try:
            await asyncio.wait_for(entered_delete.wait(), 1)
            summary = asyncio.create_task(
                case.reflection(case.owner, event, response) if mode == "auto" else case.run_summary(mode)
            )
            await asyncio.wait({summary}, timeout=.01)
            assert not summary.done()
            processor.process_conversation.assert_not_awaited()
            release.set()
            await asyncio.wait_for(asyncio.gather(clear, summary), 1)
            await asyncio.gather(*case.owner._storage_tasks)
            processor.process_conversation.assert_not_awaited()
            assert (await case.get_session(SESSION)).metadata == {}
            assert case.db.raw.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        finally:
            release.set()
            await asyncio.gather(*[item for item in (clear, summary) if item],
                                 *case.owner._storage_tasks, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["auto", "manual"])
@pytest.mark.parametrize("outcome", ["error", "cancel"])
def test_commit_failure_releases_session_lock_so_reset_can_complete(summaries, mode, outcome):
    case = summaries

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause_commit(*_args):
            entered.set()
            await release.wait()
            raise OSError("synthetic index failure")

        case.engine.hybrid_retriever.add_memory.side_effect = pause_commit
        processor = NS(process_conversation=AsyncMock(return_value=("old summary", {}, .5)),
                       classify_atoms_from_metadata=lambda **_: [])
        case.owner.memory_processor = case.owner._memory_processor = processor
        case.owner._format_error_message = lambda *_: "summary failed"
        task = asyncio.create_task(case.run_summary(mode))
        clear = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            clear = asyncio.create_task(case.manager.clear_session(SESSION))
            await asyncio.wait({clear}, timeout=.01)
            assert not clear.done()
            if outcome == "cancel":
                task.cancel()
            release.set()
            results = await asyncio.wait_for(asyncio.gather(task, clear, return_exceptions=True), 1)
            if outcome == "cancel":
                assert isinstance(results[0], asyncio.CancelledError)
            else:
                assert not isinstance(results[0], BaseException)
            assert (await case.get_session(SESSION)).metadata == {}
            assert case.db.raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert case.db.raw.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        finally:
            release.set()
            await asyncio.gather(*[item for item in (task, clear) if item], return_exceptions=True)

    asyncio.run(run())


def test_commit_for_one_session_does_not_block_clearing_another(summaries):
    case = summaries

    async def run():
        revision = case.manager._summary_revision(SESSION)
        async with case.manager._summary_commit("different-session", 0) as current:
            assert current
            await asyncio.wait_for(case.manager.clear_session(SESSION), 1)
        assert case.manager._summary_revision(SESSION) == revision + 1
        assert case.manager._summary_revision("different-session") == 0

    asyncio.run(run())
