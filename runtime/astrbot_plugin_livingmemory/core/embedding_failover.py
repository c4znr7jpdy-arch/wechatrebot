"""Keep document and graph vectors in the same provider space during failover."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import functools
import hashlib
import json
import logging
import math
from pathlib import Path
import time
from types import SimpleNamespace

logger = logging.getLogger(__name__)


class EmbeddingUnavailable(RuntimeError):
    """Vector retrieval is unavailable; keyword retrieval can still proceed."""


def provider_identity(provider) -> str:
    config = getattr(provider, "provider_config", {}) or {}
    return str(config.get("id") or "")


def _embedding_dimension(vector) -> int:
    if not isinstance(vector, list) or not vector or not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in vector
    ):
        raise ValueError("向量模型未返回有效向量")
    return len(vector)


async def resolve_initial_provider(preferred, providers):
    """Resolve unknown dimensions, trying configured alternatives on failure."""
    seen = set()
    for provider in [preferred, *providers]:
        identity = provider_identity(provider)
        if identity in seen:
            continue
        seen.add(identity)
        if provider.get_dim() > 0:
            return provider
        try:
            async with asyncio.timeout(20):
                vector = await provider.get_embedding("向量服务可用性检查")
            provider.provider_config["embedding_dimensions"] = _embedding_dimension(vector)
            if provider.get_dim() > 0:
                return provider
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    raise EmbeddingUnavailable("向量模型暂不可用，未修改已有记忆或索引。")


def embedding_index_path(path: str | Path, provider) -> Path:
    """Never open another model's index, including equal-dimension models."""
    config = getattr(provider, "provider_config", {}) or {}
    identity = {
        "id": provider_identity(provider),
        "type": config.get("type"),
        "model": getattr(provider, "model", None) or config.get("embedding_model"),
        "base": config.get("embedding_api_base") or config.get("api_base"),
        "dimension": provider.get_dim(),
        "dimensions_mode": config.get("embedding_dimensions_mode"),
        "input_type": config.get("input_type"),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    path = Path(path)
    return path.with_name(f"{path.stem}.embedding-{digest}{path.suffix}")


class _EmbeddingProxy:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def __getattr__(self, name):
        return getattr(self.coordinator.provider, name)

    @property
    def fingerprint_provider(self):
        return self.coordinator.provider

    async def get_embedding(self, text):
        return await self.coordinator.embed("get_embedding", text)

    async def get_embedding_with_retry(self, text, *args, **kwargs):
        return await self.coordinator.embed("get_embedding", text)

    async def get_embeddings(self, texts):
        return await self.coordinator.embed("get_embeddings", texts)

    async def get_embeddings_batch(self, texts, *args, **kwargs):
        return await self.coordinator.embed("get_embeddings_batch", texts, *args, **kwargs)


async def _provider_request(provider, method, *args, **kwargs):
    config = getattr(provider, "provider_config", {}) or {}
    try:
        timeout = max(1.0, min(float(config.get("timeout", 20)), 60.0))
    except (ValueError, TypeError):
        timeout = 20.0
    async with asyncio.timeout(timeout):
        return await getattr(provider, method)(*args, **kwargs)


class _RebuildProvider:
    """Bound each rebuild request without imposing a whole-corpus deadline."""

    def __init__(self, provider):
        self.fingerprint_provider = provider

    def __getattr__(self, name):
        return getattr(self.fingerprint_provider, name)

    async def get_embeddings(self, texts):
        provider = self.fingerprint_provider
        if callable(getattr(provider, "get_embeddings", None)):
            return await _provider_request(provider, "get_embeddings", texts)
        return [await _provider_request(provider, "get_embedding", text) for text in texts]


class EmbeddingFailover:
    """Serialize vector mutations and switch only after both indexes are ready.

    A query never waits for a rebuild. A failed query starts one background
    recovery; writes wait for that task, then continue with the rebuilt provider.
    Rebuilds use raw providers and separate files, so a failure cannot alter the
    active indexes. The selected default remains in the plugin configuration.
    """

    _WRITE_METHODS = (
        "add_memory", "update_memory", "replace_memory", "delete_memory",
        "batch_delete_memories", "archive_memories", "restore_memory",
        "rebuild_graph_index", "_repair_incomplete_write_ops",
    )

    def __init__(self, provider, providers, databases, base_paths, *, ready=True):
        self.provider = provider
        self._providers = providers
        self.databases = tuple(db for db in databases if db is not None)
        self.base_paths = tuple(Path(path) for path in base_paths)
        self.ready = ready
        self.switching = False
        self._lock = asyncio.Lock()
        self._queries = 0
        self._queries_done = asyncio.Event()
        self._queries_done.set()
        self._owner = None
        self._scope = ContextVar("livingmemory_embedding_scope", default=None)
        self._mode = ContextVar("livingmemory_embedding_operation", default="query")
        self._recovery_task = None
        self._cooldown = {}
        self._closed = False
        self._engine = None
        self.last_error = ""
        self.proxy = _EmbeddingProxy(self)
        for db in self.databases:
            db.embedding_provider = self.proxy
            original = db.retrieve

            @functools.wraps(original)
            async def retrieve(*args, _original=original, **kwargs):
                return await self.query(lambda: _original(*args, **kwargs))

            db.retrieve = retrieve

    @asynccontextmanager
    async def exclusive(self):
        task = asyncio.current_task()
        scope = self._scope.get()
        if scope and scope.active and scope.task is task:
            yield
            return
        if scope and scope.active:
            # CRUD cancellation/replace paths await shielded child tasks. Carry
            # their ownership across that boundary, serializing sibling tasks.
            async with scope.children:
                if scope.active:
                    child = SimpleNamespace(task=task, active=True, children=asyncio.Lock())
                    token = self._scope.set(child)
                    try:
                        yield
                    finally:
                        child.active = False
                        self._scope.reset(token)
                    return
        async with self._lock:
            await self._queries_done.wait()
            self._owner = task
            scope = SimpleNamespace(task=task, active=True, children=asyncio.Lock())
            token = self._scope.set(scope)
            try:
                yield
            finally:
                scope.active = False
                self._scope.reset(token)
                self._owner = None

    def attach(self, engine, validator):
        self._engine = engine
        for name in self._WRITE_METHODS:
            original = getattr(engine, name, None)
            if not callable(original):
                continue

            @functools.wraps(original)
            async def mutate(*args, _original=original, _name=name, **kwargs):
                operation = lambda: _original(*args, **kwargs)
                if _name == "rebuild_graph_index":
                    return await self.maintenance(operation)
                return await self.write(operation)

            setattr(engine, name, mutate)
        original_rebuild = validator.rebuild_indexes

        @functools.wraps(original_rebuild)
        async def rebuild(*args, **kwargs):
            return await self.maintenance(lambda: original_rebuild(*args, **kwargs))

        validator.rebuild_indexes = rebuild

    async def validate_indexes(self):
        """Cached provider indexes may lag writes made through another provider."""
        from .validators.index_validator import IndexValidator

        for db in self.databases:
            validator = IndexValidator(db.doc_store_path, db)
            if await validator.provider_fingerprint_changed():
                self.ready = False
                return
            expected = await validator._get_document_ids()
            actual = validator._get_ids_from_index(db.embedding_storage.index)
            if actual is None or actual != expected:
                self.ready = False
                return

    async def query(self, operation):
        if self._closed or self.switching or not self.ready or self._lock.locked():
            if not self.ready:
                self.start_recovery()
            raise EmbeddingUnavailable("向量索引准备中，暂用关键词检索。")
        try:
            self._queries += 1
            self._queries_done.clear()
            token = self._mode.set("query")
            try:
                return await operation()
            finally:
                self._mode.reset(token)
                self._queries -= 1
                if not self._queries:
                    self._queries_done.set()
        except EmbeddingUnavailable:
            self.start_recovery()
            raise

    async def write(self, operation):
        if self._closed:
            raise EmbeddingUnavailable("向量服务正在关闭。")
        scope = self._scope.get()
        if not scope or not scope.active:
            pending = self._recovery_task
            if pending and not pending.done():
                await asyncio.shield(pending)
        async with self.exclusive():
            if self._closed:
                raise EmbeddingUnavailable("向量服务正在关闭。")
            token = self._mode.set("write")
            try:
                return await operation()
            finally:
                self._mode.reset(token)

    async def maintenance(self, operation):
        async with self.exclusive():
            if self._closed:
                raise EmbeddingUnavailable("向量服务正在关闭。")
            token = self._mode.set("maintenance")
            try:
                return await operation()
            finally:
                self._mode.reset(token)

    async def embed(self, method, *args, **kwargs):
        if not self.ready and self._mode.get() == "write":
            await self._recover_locked(include_current=True)
        try:
            return await _provider_request(self.provider, method, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._cooldown[provider_identity(self.provider)] = time.monotonic() + 30
            self.last_error = type(error).__name__
            if self._mode.get() != "write":
                self.start_recovery()
                raise EmbeddingUnavailable("向量模型暂不可用，正在准备备用模型。") from error
            await self._recover_locked()
            try:
                return await _provider_request(self.provider, method, *args, **kwargs)
            except Exception as retry_error:
                raise EmbeddingUnavailable("备用向量模型请求失败，请稍后重试。") from retry_error

    def start_recovery(self):
        if self._closed or (self._recovery_task and not self._recovery_task.done()):
            return
        # Recovery must wait for an in-progress write/maintenance operation;
        # it is not one of that operation's shielded compensation children.
        token = self._scope.set(None)
        try:
            self._recovery_task = asyncio.create_task(self._recover())
        finally:
            self._scope.reset(token)

    async def _recover(self):
        try:
            async with self.exclusive():
                await self._recover_locked(include_current=not self.ready)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = type(error).__name__
            logger.warning("向量服务自动恢复未完成：%s", self.last_error)

    async def _recover_locked(self, *, include_current=False):
        if self._closed:
            raise EmbeddingUnavailable("向量服务正在关闭。")
        self.switching = True
        try:
            from .faiss_async_persist import get_async_persister

            # A current-provider rebuild can replace the same on-disk path.
            # Drain old saves before preparing it so they cannot overwrite it.
            for db in self.databases:
                persister = get_async_persister(db.embedding_storage)
                if persister:
                    await persister.flush_now()
            candidates = list(self._providers())
            if include_current:
                candidates = [self.provider, *candidates]
            seen = set() if include_current else {provider_identity(self.provider)}
            for candidate in candidates:
                identity = provider_identity(candidate)
                if identity in seen or self._cooldown.get(identity, 0) > time.monotonic():
                    continue
                seen.add(identity)
                try:
                    prepared = await self._prepare(candidate)
                    await self._activate(candidate, prepared)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.last_error = type(error).__name__
                    logger.warning("备用向量索引准备失败：%s", self.last_error)
                    self._cooldown[identity] = time.monotonic() + 30
            raise EmbeddingUnavailable("向量模型暂时均不可用，已保留现有记忆与索引。")
        finally:
            self.switching = False

    async def _prepare(self, candidate):
        from .validators.index_validator import IndexValidator

        async with asyncio.timeout(30):
            vector = await candidate.get_embedding("向量服务可用性检查")
        dimension = _embedding_dimension(vector)
        if not dimension or (candidate.get_dim() and dimension != candidate.get_dim()):
            raise ValueError("备用向量模型返回了不匹配的维度")
        if not candidate.get_dim():
            candidate.provider_config["embedding_dimensions"] = dimension
        prepared = []
        for db, base_path in zip(self.databases, self.base_paths, strict=True):
            target = embedding_index_path(base_path, candidate)
            storage = SimpleNamespace(dimension=dimension, path=str(target), index=None)
            shadow_db = SimpleNamespace(embedding_provider=_RebuildProvider(candidate), embedding_storage=storage)
            validator = IndexValidator(db.doc_store_path, shadow_db)
            shadow_engine = SimpleNamespace(faiss_db=shadow_db)
            options = validator._get_rebuild_options(self._engine or SimpleNamespace(config={}))
            options.update(max_retries=1, max_failure_ratio=0.0, batch_delay=0.0,
                           request_delay=0.0, resume_checkpoint=False)
            total = await validator._get_document_count()
            result = await validator._rebuild_vector_index_full(shadow_engine, total, options)
            if not result.get("switched") or result.get("errors"):
                raise EmbeddingUnavailable("备用向量索引未完整建立，保留当前索引。")
            prepared.append(storage)
        return prepared

    async def _activate(self, candidate, prepared):
        # No await between replacing document/graph indexes and provider identity.
        for db, target in zip(self.databases, prepared, strict=True):
            storage = db.embedding_storage
            storage.index, storage.dimension, storage.path = target.index, target.dimension, target.path
            db.index_store_path = target.path
        self.provider = candidate
        self.ready = True
        self.last_error = ""
        if self._engine:
            self._engine._invalidate_search_cache()
        for db in self.databases:
            try:
                from .validators.index_validator import IndexValidator

                await IndexValidator(db.doc_store_path, db).record_provider_fingerprint()
            except Exception as error:
                # The switch succeeded; a missing marker only causes a safe
                # consistency check on the next startup, not another failover.
                logger.warning("向量索引已切换，但指纹记录失败：%s", type(error).__name__)

    async def close(self):
        self._closed = True
        task = self._recovery_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.exclusive():
            pass
