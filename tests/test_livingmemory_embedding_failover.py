"""Exercise provider/index switching without a robot or external model requests."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_livingmemory_patch import patched_plugin  # noqa: F401
from test_hot_config_runtime import _runtime_function


@pytest.fixture
def failover_module(patched_plugin, monkeypatch):
    package = ModuleType("embedding_test")
    package.__path__ = [str(patched_plugin / "core")]
    monkeypatch.setitem(sys.modules, "embedding_test", package)
    persist = ModuleType("embedding_test.faiss_async_persist")
    persist.get_async_persister = lambda storage: None
    monkeypatch.setitem(sys.modules, persist.__name__, persist)
    name = "embedding_test.embedding_failover"
    spec = importlib.util.spec_from_file_location(name, patched_plugin / "core/embedding_failover.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class Provider:
    def __init__(self, name, dimension=2, *, failed=False):
        self.provider_config = {"id": name, "embedding_dimensions": dimension,
                                "embedding_model": name, "embedding_api_base": "https://model.example/v1"}
        self.model = name
        self.failed = failed
        self.calls = []

    def get_dim(self):
        return self.provider_config["embedding_dimensions"]

    async def get_embedding(self, text):
        self.calls.append(text)
        if self.failed:
            raise RuntimeError("synthetic provider failure")
        return [self.model] * (self.get_dim() or 3)


def database(provider, label):
    db = NS(embedding_provider=provider, embedding_storage=NS(index=label, dimension=provider.get_dim(), path=label),
            doc_store_path=label + ".sqlite", index_store_path=label)

    async def retrieve(query):
        vector = await db.embedding_provider.get_embedding(query)
        return db.embedding_storage.index, vector

    db.retrieve = retrieve
    return db


def coordinator(module, tmp_path, providers):
    databases = [database(providers[0], "document"), database(providers[0], "graph")]
    for db in databases:
        db.doc_store_path = str(tmp_path / db.doc_store_path)
    instance = module.EmbeddingFailover(providers[0], lambda: providers, databases,
                                      [tmp_path / "document.index", tmp_path / "graph.index"])

    async def prepare(candidate):
        await candidate.get_embedding("probe")
        return [NS(index=f"{candidate.model}/{kind}", dimension=candidate.get_dim(), path=str(tmp_path / kind))
                for kind in ("document", "graph")]

    instance._prepare = AsyncMock(side_effect=prepare)
    return instance, databases


def test_same_dimension_models_have_distinct_indexes(failover_module, tmp_path):
    a, b = Provider("first"), Provider("second")
    path = tmp_path / "memory.index"
    assert failover_module.embedding_index_path(path, a) != failover_module.embedding_index_path(path, b)
    original = failover_module.embedding_index_path(path, a)
    a.provider_config["embedding_api_key"] = "different-secret"
    assert failover_module.embedding_index_path(path, a) == original
    a.provider_config["embedding_api_base"] = "https://other.example/v1"
    assert failover_module.embedding_index_path(path, a) != original


@pytest.mark.parametrize("dimension", [2, 5])
def test_query_failure_rebuilds_both_indexes_before_switch(failover_module, tmp_path, dimension):
    async def run():
        first, backup = Provider("first", failed=True), Provider("backup", dimension)
        instance, databases = coordinator(failover_module, tmp_path, [first, backup])
        with pytest.raises(failover_module.EmbeddingUnavailable):
            await databases[0].retrieve("find memory")
        await instance._recovery_task
        assert instance.provider is backup
        assert [db.embedding_storage.index for db in databases] == ["backup/document", "backup/graph"]
        assert all(db.embedding_storage.dimension == dimension for db in databases)
        assert await databases[0].retrieve("again") == ("backup/document", ["backup"] * dimension)
        await instance.close()

    asyncio.run(run())


def test_failed_candidate_keeps_old_indexes_and_tries_next(failover_module, tmp_path):
    async def run():
        first, bad, good = Provider("first", failed=True), Provider("bad"), Provider("good")
        instance, databases = coordinator(failover_module, tmp_path, [first, bad, good])
        prepare = instance._prepare

        async def fail_graph(candidate):
            assert [db.embedding_storage.index for db in databases] == ["document", "graph"]
            if candidate is bad:
                raise RuntimeError("graph rebuild failed after document rebuild")
            return await prepare(candidate)

        instance._prepare = AsyncMock(side_effect=fail_graph)
        result = await instance.write(lambda: databases[0].embedding_provider.get_embedding("new memory"))
        assert result == ["good", "good"]
        assert instance.provider is good
        assert [call.args[0] for call in instance._prepare.await_args_list] == [bad, good]
        await instance.close()

    asyncio.run(run())


def test_writes_wait_for_rebuild_and_queries_can_use_keyword_fallback(failover_module, tmp_path):
    async def run():
        first, backup = Provider("first", failed=True), Provider("backup")
        instance, databases = coordinator(failover_module, tmp_path, [first, backup])
        started, finish = asyncio.Event(), asyncio.Event()
        prepare = instance._prepare

        async def delayed(candidate):
            started.set()
            await finish.wait()
            return await prepare(candidate)

        instance._prepare = delayed
        with pytest.raises(failover_module.EmbeddingUnavailable):
            await databases[0].retrieve("query")
        await started.wait()
        writes = []

        async def add():
            vector = await databases[0].embedding_provider.get_embedding("new memory")
            writes.append((databases[0].embedding_storage.index, vector))

        write = asyncio.create_task(instance.write(add))
        await asyncio.sleep(0)
        assert not write.done() and writes == []
        with pytest.raises(failover_module.EmbeddingUnavailable):
            await databases[1].retrieve("graph query")
        finish.set()
        await write
        assert writes == [("backup/document", ["backup", "backup"])]
        await instance.close()

    asyncio.run(run())


def test_all_failed_candidates_preserve_indexes(failover_module, tmp_path):
    async def run():
        first, backup = Provider("first", failed=True), Provider("backup", failed=True)
        instance, databases = coordinator(failover_module, tmp_path, [first, backup])
        with pytest.raises(failover_module.EmbeddingUnavailable):
            await instance.write(lambda: databases[0].embedding_provider.get_embedding("new memory"))
        assert instance.provider is first
        assert [db.embedding_storage.index for db in databases] == ["document", "graph"]
        assert not instance.switching and not instance._lock.locked()
        await instance.close()

    asyncio.run(run())


def test_cancelled_rebuild_keeps_old_indexes_and_unlocks(failover_module, tmp_path):
    async def run():
        instance, databases = coordinator(failover_module, tmp_path, [Provider("first", failed=True), Provider("backup")])
        started = asyncio.Event()

        async def prepare(_candidate):
            started.set()
            await asyncio.Future()

        instance._prepare = prepare
        with pytest.raises(failover_module.EmbeddingUnavailable):
            await databases[0].retrieve("query")
        await started.wait()
        await instance.close()
        assert [db.embedding_storage.index for db in databases] == ["document", "graph"]
        assert not instance.switching and not instance._lock.locked()

    asyncio.run(run())


def test_shielded_replace_and_cancellation_cleanup_keep_lock_ownership(failover_module, tmp_path, patched_plugin):
    from types import MethodType

    async def run():
        instance, _ = coordinator(failover_module, tmp_path, [Provider("first")])
        stored = {1: {"text": "old", "metadata": {}}}
        source = (patched_plugin / "core/managers/memory_engine_crud.py").read_text("utf-8")
        namespace = {"asyncio": asyncio, "time": __import__("time"), "logger": Mock(),
                     "clamp_float": lambda value, default=0: float(value)}
        engine = NS(atom_enabled=False, _invalidate_search_cache=Mock(),
                    get_memory_source=AsyncMock(return_value=[]), _safe_json_dict=lambda value: value,
                    _create_tracked_task=asyncio.create_task)
        engine.get_memory = AsyncMock(side_effect=lambda identifier: stored.get(identifier))

        async def add(**kwargs):
            stored[2] = {"text": kwargs["content"], "metadata": {}}
            return 2

        async def delete(identifier):
            stored.pop(identifier, None)
            return True

        engine.add_memory = add
        engine.delete_memory = delete
        engine.replace_memory = MethodType(_runtime_function(source, "replace_memory", namespace), engine)
        validator = NS(rebuild_indexes=AsyncMock(), record_provider_fingerprint=AsyncMock())
        instance.attach(engine, validator)
        result = await asyncio.wait_for(engine.replace_memory(1, content="new", metadata={}, importance=1), 1)
        assert result == 2 and list(stored) == [2]
        # A cancellation handler's shielded delete must finish under the same scope.
        async def cancelling_write():
            try:
                raise asyncio.CancelledError()
            except asyncio.CancelledError:
                await asyncio.shield(engine.delete_memory(2))
                raise
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(instance.write(cancelling_write), 1)
        assert not stored and not instance._lock.locked()
        await instance.close()

    asyncio.run(run())


def test_normal_shutdown_cancels_maintenance_before_closing_failover(failover_module, patched_plugin, tmp_path):
    from types import MethodType

    source = (patched_plugin / "core/plugin_initializer.py").read_text("utf-8")
    async def run():
        failover, _ = coordinator(failover_module, tmp_path, [Provider("first")])
        started = asyncio.Event()
        async def maintenance():
            async with failover.exclusive():
                started.set()
                await asyncio.Future()
        task = asyncio.create_task(maintenance())
        await started.wait()
        instance = NS(embedding_failover=failover, _index_maintenance_task=task, _retry_task=None)
        method = MethodType(_runtime_function(source, "stop_background_tasks", {"asyncio": asyncio}), instance)
        await asyncio.wait_for(method(), 1)
        assert task.cancelled() and failover._closed
        operation = AsyncMock()
        with pytest.raises(failover_module.EmbeddingUnavailable):
            await failover.write(operation)
        operation.assert_not_awaited()
    asyncio.run(run())


def test_graph_maintenance_failure_waits_before_switching(failover_module, tmp_path):
    async def run():
        primary, backup = Provider("primary", failed=True), Provider("backup", 4)
        instance, databases = coordinator(failover_module, tmp_path, [primary, backup])
        failed, finish = asyncio.Event(), asyncio.Event()
        async def rebuild_graph():
            with pytest.raises(failover_module.EmbeddingUnavailable):
                await databases[1].embedding_provider.get_embedding("rebuild")
            failed.set()
            await finish.wait()
            assert instance.provider is primary
        engine = NS(rebuild_graph_index=rebuild_graph, _invalidate_search_cache=Mock())
        instance.attach(engine, NS(rebuild_indexes=AsyncMock()))
        task = asyncio.create_task(engine.rebuild_graph_index())
        await failed.wait()
        await asyncio.sleep(0)
        instance._prepare.assert_not_awaited()
        finish.set()
        await task
        await instance._recovery_task
        assert instance.provider is backup
        await instance.close()
    asyncio.run(run())


def test_archived_restore_retries_after_rollback_before_provider_switch(failover_module, patched_plugin, tmp_path):
    pytest.importorskip("numpy")
    from contextlib import asynccontextmanager
    import json
    from types import MethodType

    async def run():
        primary, backup = Provider("primary"), Provider("backup")
        primary.get_embedding = AsyncMock(side_effect=[[1.0, 0.0], RuntimeError("graph failed")])
        backup.get_embedding = AsyncMock(return_value=[0.0, 1.0])
        instance, databases = coordinator(failover_module, tmp_path, [primary, backup])
        storage = databases[0].embedding_storage
        storage.index = {}
        memory = {"text": "archived", "metadata": {"status": "archived"}}
        async def insert(vector, identifier):
            storage.index[identifier] = vector.tolist()
        async def delete(identifiers):
            for identifier in identifiers:
                storage.index.pop(identifier, None)
        storage.insert, storage.delete = insert, delete
        async def execute(sql, values):
            if sql.startswith("UPDATE documents"):
                memory["metadata"] = json.loads(values[0])
        async def index_graph(*args):
            await databases[1].embedding_provider.get_embedding("graph")
        engine = NS(
            faiss_db=databases[0], db_connection=NS(execute=execute, commit=AsyncMock()),
            get_memory=AsyncMock(return_value=memory), _safe_json_dict=lambda value: dict(value),
            bm25_retriever=NS(add_document=AsyncMock()), atom_store=None,
            graph_memory_manager=NS(index_memory=index_graph, delete_memory=AsyncMock()),
            _invalidate_search_cache=Mock(),
        )
        @asynccontextmanager
        async def transaction():
            yield engine.db_connection
        engine._write_transaction = transaction
        source = (patched_plugin / "core/managers/memory_engine_batch.py").read_text("utf-8")
        method = _runtime_function(source, "restore_memory", {
            "json": json, "time": __import__("time"), "classify_atoms": lambda **kwargs: [],
            "clamp_float": lambda value, default=0: default if value is None else float(value),
        })
        engine.restore_memory = MethodType(method, engine)
        instance.attach(engine, NS(rebuild_indexes=AsyncMock()))
        async def prepare(candidate):
            assert memory["metadata"]["status"] == "archived"
            assert storage.index == {}  # The first restore has fully rolled back.
            return [NS(index={}, dimension=2, path=db.embedding_storage.path) for db in databases]
        instance._prepare = AsyncMock(side_effect=prepare)
        assert await engine.restore_memory(1) is True
        assert instance.provider is backup
        assert storage.index == {1: [0.0, 1.0]}
        assert memory["metadata"]["status"] == "active"
        engine.graph_memory_manager.delete_memory.assert_awaited_once_with(1)
        await instance.close()
    asyncio.run(run())


@pytest.mark.parametrize("invalid", [None, [float("nan")], ["secret"], []])
def test_initial_unknown_dimension_uses_available_candidate(failover_module, invalid):
    async def run():
        primary, backup = Provider("primary", 0, failed=True), Provider("backup", 0)
        if invalid is not None:
            primary.get_embedding = AsyncMock(return_value=invalid)
        backup.get_embedding = AsyncMock(return_value=[0.0, 0.0, 1.0])
        result = await failover_module.resolve_initial_provider(primary, [primary, backup])
        assert result is backup and result.get_dim() == 3

    asyncio.run(run())


@pytest.mark.parametrize("fail_graph", [False, True])
def test_rebuild_uses_real_sqlite_and_faiss_without_mixing_spaces(failover_module, tmp_path, monkeypatch, fail_graph):
    faiss = pytest.importorskip("faiss")
    np = pytest.importorskip("numpy")
    pytest.importorskip("aiosqlite")
    api = ModuleType("astrbot.api")
    api.logger = Mock()
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("embedding_storage_test", root / "vendor/engine/astrbot/core/db/vec_db/faiss_impl/embedding_storage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class NumericProvider(Provider):
        async def get_embedding(self, text):
            if fail_graph and text == "graph original":
                raise RuntimeError("synthetic graph embedding failure")
            return [0.0, 1.0]

    def assert_vector(index, vector):
        distances, identifiers = index.search(np.array([vector], dtype=np.float32), 1)
        assert identifiers[0, 0] == 1
        assert distances[0, 0] == pytest.approx(0.0)

    async def run():
        primary, backup = Provider("primary"), NumericProvider("backup")
        databases, bases = [], []
        for kind in ("document", "graph"):
            db_path, index_path = tmp_path / f"{kind}.sqlite", tmp_path / f"{kind}.index"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, doc_id TEXT, text TEXT, metadata TEXT)")
                connection.execute("INSERT INTO documents VALUES (1, 'one', ?, '{}')", (f"{kind} original",))
            storage = module.EmbeddingStorage(2, str(index_path))
            await storage.insert(np.array([1.0, 0.0], dtype=np.float32), 1)
            databases.append(NS(doc_store_path=str(db_path), embedding_storage=storage,
                                index_store_path=str(index_path), retrieve=AsyncMock()))
            bases.append(index_path)
        instance = failover_module.EmbeddingFailover(primary, lambda: [primary, backup], databases, bases)
        # Interrupted rebuilds can contain vectors for text that changed while
        # another provider was active. A fresh switch must not resume them.
        import json
        from embedding_test.validators.index_validator import IndexValidator
        for db, base in zip(databases, bases):
            target = failover_module.embedding_index_path(base, backup)
            checkpoint = Path(str(target) + ".rebuild.tmp")
            faiss.write_index(db.embedding_storage.index, str(checkpoint))
            fingerprint = IndexValidator(db.doc_store_path, NS(embedding_provider=backup)).get_provider_fingerprint()
            Path(str(checkpoint) + ".json").write_text(json.dumps({
                "provider_fingerprint": fingerprint, "dimension": 2,
            }))
        if fail_graph:
            with pytest.raises(failover_module.EmbeddingUnavailable):
                async with instance.exclusive():
                    await instance._recover_locked()
            assert instance.provider is primary
            for db in databases:
                assert_vector(db.embedding_storage.index, [1, 0])
        else:
            async with instance.exclusive():
                await instance._recover_locked()
            assert instance.provider is backup
            for db in databases:
                assert db.embedding_storage.index.ntotal == 1
                assert_vector(db.embedding_storage.index, [0, 1])
                assert_vector(faiss.read_index(db.embedding_storage.path), [0, 1])
        # The original provider's files always survive a switch or failed candidate.
        for path in bases:
            assert_vector(faiss.read_index(str(path)), [1, 0])
        await instance.close()
        if not fail_graph:
            # Switching back must reject the old namespace even if IDs match.
            restored = []
            for db, base in zip(databases, bases):
                with sqlite3.connect(db.doc_store_path) as connection:
                    connection.execute("UPDATE documents SET text = 'edited' WHERE id = 1")
                restored.append(NS(doc_store_path=db.doc_store_path,
                                   embedding_storage=module.EmbeddingStorage(2, str(base)),
                                   index_store_path=str(base), retrieve=AsyncMock()))
            restarted = failover_module.EmbeddingFailover(primary, lambda: [primary, backup], restored, bases)
            await restarted.validate_indexes()
            assert restarted.ready is False
            await restarted.close()

    asyncio.run(run())
