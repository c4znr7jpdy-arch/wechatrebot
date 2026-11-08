"""Run delivered memory methods against real SQLite, without loading AstrBot."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
import json
from pathlib import Path
import sqlite3
import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from test_hot_config_runtime import _runtime_function
from test_livingmemory_patch import patched_plugin


class _Connection:
    """Async boundary over SQLite; only fault injection replaces commit delivery."""

    def __init__(self, path: Path):
        self.raw = sqlite3.connect(path, timeout=0.05)
        self.raw.row_factory = sqlite3.Row
        self.commit_failure: BaseException | None = None

    @property
    def in_transaction(self):
        return self.raw.in_transaction

    async def execute(self, sql, parameters=()):
        await asyncio.sleep(0)
        return _Cursor(self.raw.execute(sql, parameters))

    async def executemany(self, sql, parameters):
        await asyncio.sleep(0)
        return _Cursor(self.raw.executemany(sql, parameters))

    async def commit(self):
        await asyncio.sleep(0)
        if self.commit_failure is not None:
            error, self.commit_failure = self.commit_failure, None
            raise error
        self.raw.commit()

    async def rollback(self):
        await asyncio.sleep(0)
        self.raw.rollback()

    async def close(self):
        self.raw.close()


class _Cursor:
    def __init__(self, raw):
        self.raw = raw
        self.rowcount = raw.rowcount
        self.lastrowid = raw.lastrowid

    async def fetchone(self):
        return self.raw.fetchone()

    async def fetchall(self):
        return self.raw.fetchall()


@pytest.fixture
def memory_database(tmp_path):
    path = tmp_path / "memory.sqlite"
    connection = _Connection(path)
    connection.raw.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_id TEXT, metadata TEXT);
        INSERT INTO documents VALUES (1, 'one', '{"access_count":0}');
        CREATE TABLE memory_sources (
            memory_id INTEGER PRIMARY KEY, source_json TEXT,
            created_at REAL, updated_at REAL
        );
        CREATE VIRTUAL TABLE livingmemory_memories_fts USING fts5(doc_id UNINDEXED, text);
        INSERT INTO livingmemory_memories_fts VALUES (1, 'memory');
        CREATE TABLE probe (value TEXT);
    """)
    try:
        yield connection, path
    finally:
        connection.raw.close()


def _engine(plugin: Path, connection: _Connection):
    engine = SimpleNamespace(
        db_connection=connection, _db_write_lock=asyncio.Lock(),
        _pending_tasks=set(), atom_lifecycle_manager=None,
        graph_vector_db=None, graph_memory_manager=None, atom_store=None,
        _write_op_repair_enabled=True, _write_op_max_retries=3,
    )
    scope = {
        "asyncio": asyncio, "json": json, "time": time,
        "logger": Mock(), "Any": object,
    }
    methods = {
        "memory_engine.py": ("_write_transaction", "close"),
        "memory_engine_crud.py": ("_update_access_times_internal", "save_memory_source"),
        "memory_engine_write_ops.py": (
            "_create_write_ops_table", "_start_write_op", "_advance_write_op",
            "_delete_document_indexes_for_batch",
        ),
    }
    for filename, names in methods.items():
        source = (plugin / "core/managers" / filename).read_text("utf-8")
        for name in names:
            # The pre-fix baseline has no helper; keep its failure reproducible.
            if f"def {name}(" not in source:
                continue
            function = _runtime_function(source, name, scope)
            if name == "_write_transaction":
                function = asynccontextmanager(function)
            setattr(engine, name, MethodType(function, engine))
    return engine


def _other_connection_can_write(path):
    # Keep the memory engine's connection open, as it is for the robot lifetime.
    with closing(sqlite3.connect(path, timeout=0.05)) as second:
        with second:
            second.execute("INSERT INTO probe VALUES ('other connection')")


def test_swallowed_access_update_sql_failure_releases_lock(patched_plugin, memory_database):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)
    connection.raw.executescript("""
        CREATE TRIGGER fail_access AFTER UPDATE ON documents
        BEGIN SELECT RAISE(FAIL, 'injected access update failure'); END;
    """)

    assert asyncio.run(engine._update_access_times_internal([1])) is False
    _other_connection_can_write(path)
    assert not connection.in_transaction
    assert json.loads(connection.raw.execute("SELECT metadata FROM documents").fetchone()[0]) == {"access_count": 0}
    connection.raw.execute("DROP TRIGGER fail_access")
    assert asyncio.run(engine._update_access_times_internal([1])) is True


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, asyncio.CancelledError])
def test_failed_commit_or_cancellation_rolls_back_and_allows_retry(patched_plugin, memory_database, failure):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)
    connection.commit_failure = failure("injected commit failure")

    with pytest.raises(failure):
        asyncio.run(engine.save_memory_source(1, [{"role": "user", "content": "first"}]))
    assert not connection.in_transaction
    _other_connection_can_write(path)
    assert connection.raw.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 0
    asyncio.run(engine.save_memory_source(1, [{"role": "user", "content": "retry"}]))
    assert connection.raw.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 1


def test_stale_transaction_is_rolled_back_before_next_write(patched_plugin, memory_database):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)
    connection.raw.execute("INSERT INTO probe VALUES ('uncommitted')")

    asyncio.run(engine.save_memory_source(1, []))
    assert not connection.in_transaction
    assert connection.raw.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    _other_connection_can_write(path)


def test_cancelling_begin_clears_already_queued_transaction(patched_plugin, memory_database):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)
    execute = connection.execute

    async def interrupted_begin(sql, parameters=()):
        result = await execute(sql, parameters)
        if sql == "BEGIN IMMEDIATE":
            raise asyncio.CancelledError()
        return result

    connection.execute = interrupted_begin
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine.save_memory_source(1, []))
    assert not connection.in_transaction
    _other_connection_can_write(path)


def test_close_rolls_back_stale_transaction_and_drops_handle(patched_plugin, memory_database):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)
    connection.raw.execute("INSERT INTO probe VALUES ('uncommitted')")

    asyncio.run(engine.close())
    assert engine.db_connection is None
    with closing(sqlite3.connect(path, timeout=0.05)) as second:
        assert second.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    _other_connection_can_write(path)


def test_write_operation_progress_is_committed_and_releases_lock(patched_plugin, memory_database):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)
    connection.raw.execute("""
        CREATE TABLE memory_write_ops (
            id INTEGER PRIMARY KEY, step TEXT, status TEXT, payload TEXT,
            updated_at REAL, retry_count INTEGER DEFAULT 0, error TEXT
        )
    """)
    connection.raw.execute(
        "INSERT INTO memory_write_ops VALUES (1, 'started', 'pending', '{}', 0, 0, NULL)"
    )
    connection.raw.commit()

    asyncio.run(
        engine._advance_write_op(
            1,
            "indexed",
            status="completed",
            payload_patch={"memory_id": 1},
        )
    )
    row = connection.raw.execute(
        "SELECT step, status, payload FROM memory_write_ops WHERE id = 1"
    ).fetchone()
    assert (row[0], row[1], json.loads(row[2])) == (
        "indexed",
        "completed",
        {"memory_id": 1},
    )
    assert not connection.in_transaction
    _other_connection_can_write(path)
    asyncio.run(engine._advance_write_op(1, "finalized", status="completed"))
    assert connection.raw.execute(
        "SELECT step FROM memory_write_ops WHERE id = 1"
    ).fetchone()[0] == "finalized"
    assert not connection.in_transaction


def test_writes_on_shared_connection_are_serialized(patched_plugin, memory_database):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)

    async def run():
        entered, release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def first():
            async with engine._write_transaction() as db:
                await db.execute("INSERT INTO probe VALUES ('first')")
                entered.set()
                await release.wait()
                raise ValueError("injected write failure")

        async def second():
            async with engine._write_transaction() as db:
                second_entered.set()
                await db.execute("INSERT INTO probe VALUES ('second')")

        one = asyncio.create_task(first())
        await entered.wait()
        two = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release.set()
        with pytest.raises(ValueError):
            await one
        await asyncio.wait_for(two, 1)

    asyncio.run(run())
    assert [row[0] for row in connection.raw.execute("SELECT value FROM probe")] == ["second"]
    _other_connection_can_write(path)


def test_cancelling_an_inflight_write_rolls_back_before_unlock(patched_plugin, memory_database):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)

    async def run():
        entered = asyncio.Event()

        async def write():
            async with engine._write_transaction() as db:
                await db.execute("INSERT INTO probe VALUES ('cancelled')")
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(write())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not connection.in_transaction
    assert connection.raw.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    _other_connection_can_write(path)


def test_delete_repair_releases_main_write_before_vector_connection(patched_plugin, memory_database):
    connection, path = memory_database
    engine = _engine(patched_plugin, connection)

    async def vector_delete(_):
        assert not connection.in_transaction
        _other_connection_can_write(path)

    engine.faiss_db = SimpleNamespace(delete=vector_delete)
    assert asyncio.run(engine._delete_document_indexes_for_batch([1])) == 1
    assert not connection.in_transaction
    _other_connection_can_write(path)
