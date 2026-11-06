"""Nonblocking, shared snapshots of read-only release information."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import logging
import time
from typing import Literal

from .github import GitHubError

_LOG = logging.getLogger(__name__)
ReadKind = Literal["stable", "listing"]


@dataclass
class _Read:
    value: dict | list | None = None
    succeeded_at: float | None = None
    refresh_after: float = float("-inf")
    error: str = ""
    generation: int = 0
    task: asyncio.Task | None = None


class ReleaseReadCache:
    """Keep network waits outside navigation; never cache a publish decision."""

    def __init__(self, releases, *, clock=time.monotonic, ttl=60, retry_seconds=15, timeout=60):
        if ttl < 0 or retry_seconds < 0 or timeout <= 0:
            raise ValueError("Invalid release cache timing")
        self.releases = releases
        self.clock, self.ttl, self.retry_seconds, self.timeout = clock, ttl, retry_seconds, timeout
        self._reads = {kind: _Read() for kind in ("stable", "listing")}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def snapshot(self, kind: ReadKind) -> dict:
        """Called on the app event loop; schedule one refresh and return immediately."""
        state = self._reads[kind]
        now = self.clock()
        if not self._closed and state.task is None and now >= state.refresh_after:
            state.task = asyncio.create_task(self._refresh(kind, state, state.generation))
            self._tasks.add(state.task)
            state.task.add_done_callback(self._tasks.discard)
        return {"value": deepcopy(state.value), "loading": state.task is not None,
                "error": state.error,
                "age_seconds": None if state.succeeded_at is None else max(0, int(now - state.succeeded_at))}

    async def _refresh(self, kind: ReadKind, state: _Read, generation: int) -> None:
        error, value = "", None
        try:
            try:
                async with asyncio.timeout(self.timeout):
                    value = await getattr(self.releases, kind)()
                if not isinstance(value, dict if kind == "stable" else list):
                    raise TypeError("Unexpected release read result")
            except GitHubError as exc:
                error = str(exc) or "GitHub 暂不可用，请稍后重试。"
            except TimeoutError:
                _LOG.warning("Release cache refresh timed out (TimeoutError)")
                error = "GitHub 查询超时，请稍后重试。"
            except Exception as exc:
                _LOG.error("Release cache refresh failed (%s)", type(exc).__name__)
                error = "GitHub 暂不可用，请稍后重试。"
            if not self._closed and state.generation == generation:
                now = self.clock()
                state.error = error
                state.refresh_after = now + (self.retry_seconds if error else self.ttl)
                if not error:
                    state.value, state.succeeded_at = deepcopy(value), now
        finally:
            if state.task is asyncio.current_task():
                state.task = None

    def invalidate(self, *kinds: ReadKind) -> None:
        """Discard display data after a write; late reads cannot repopulate it."""
        for kind in kinds or self._reads:
            state = self._reads[kind]
            state.generation += 1
            if state.task is not None:
                state.task.cancel()
            state.value, state.succeeded_at, state.task = None, None, None
            state.refresh_after, state.error = float("-inf"), ""

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for state in self._reads.values():
            state.task = None
