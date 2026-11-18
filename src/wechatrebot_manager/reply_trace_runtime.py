"""Privacy-safe reply events; copied verbatim into the pinned engine patch.

Only random correlation IDs, fixed stage names and measured times leave the
event. The normal engine logger owns storage and rotation. Never pass request,
response, exception text, provider metadata or chat identity to this boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import logging
import time
from uuid import UUID, uuid4


_EXTRA = "_wechatrebot_reply_trace"
_CURRENT = ContextVar("wechatrebot_reply_trace_event", default=None)
_LOGGER = logging.getLogger("wechatrebot.reply_trace")
_STAGES = frozenset({"received", "decision", "model", "send", "pipeline"})
_STATUSES = frozenset({"pending", "success", "failed", "cancelled", "skipped"})
_MAX_EVENTS = 512


def _state(event):
    if event is None:
        event = _CURRENT.get()
    platform = getattr(event, "get_platform_id", None)
    if not callable(platform) or platform() != "aiocqhttp_hook41827":
        return None
    state = event.get_extra(_EXTRA)
    if state is None:
        # The engine already gives each event a random UUID. Reuse only that
        # UUID, never TraceSpan's identity/body fields or protocol message IDs.
        try:
            identifier = UUID(str(getattr(getattr(event, "trace", None), "span_id", "")))
            if identifier.version != 4:
                raise ValueError("not a random event UUID")
        except (ValueError, AttributeError):
            identifier = uuid4()
        state = {"trace_id": identifier.hex, "start": time.monotonic(), "sequence": 0,
                 "kind": "group" if event.get_group_id() else "private"}
        event.set_extra(_EXTRA, state)
    return state


def _emit(state, stage, phase, status, *, operation=0, started=None, duration=None):
    if state is None or state["sequence"] >= _MAX_EVENTS:
        return
    import json

    now = time.monotonic()
    state["sequence"] += 1
    payload = {
        "schema": 1, "trace_id": state["trace_id"], "sequence": state["sequence"],
        "operation": operation, "kind": state["kind"], "stage": stage,
        "phase": phase, "status": status,
        "elapsed_ms": round(max(0.0, now - state["start"]) * 1000, 3),
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    if duration is not None or started is not None:
        payload["duration_ms"] = round(max(0.0, duration if duration is not None else now - started) * 1000, 3)
    try:
        _LOGGER.info("WRB_REPLY_TRACE %s", json.dumps(payload, separators=(",", ":")))
    except (OSError, RuntimeError):
        # A broken log sink must not turn a successfully submitted reply into a
        # retry. The missing record remains visibly incomplete in diagnostics.
        return


def trace_received(event):
    state = _state(event)
    if state is not None and state["sequence"] == 0:
        _emit(state, "received", "point", "success")


async def trace_pipeline(scheduler, event):
    """Observe the existing scheduler without changing permission or cleanup."""
    with trace_stage(event, "pipeline") as outcome:
        await scheduler.execute(event)
        if event.is_stopped():
            outcome["status"] = "skipped"


async def trace_model_responses(event, provider, payload, *, streaming):
    """Time provider awaits, excluding a streaming consumer's send/tool work."""
    import asyncio

    state = _state(event)
    operation = state["sequence"] + 1 if state is not None else 0
    _emit(state, "model", "begin", "pending", operation=operation)
    duration, status = 0.0, "success"
    try:
        if not streaming:
            started = time.monotonic()
            try:
                response = await provider.text_chat(**payload)
            finally:
                duration += time.monotonic() - started
            if getattr(response, "role", None) == "err":
                status = "failed"
        else:
            responses = provider.text_chat_stream(**payload).__aiter__()
            while True:
                started = time.monotonic()
                try:
                    response = await anext(responses)
                except StopAsyncIteration:
                    break
                finally:
                    duration += time.monotonic() - started
                if getattr(response, "role", None) == "err":
                    status = "failed"
                yield response
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "failed"
        raise
    finally:
        _emit(state, "model", "end", status, operation=operation, duration=duration)
    if not streaming:
        yield response


@contextmanager
def trace_context(event):
    """Bind only an actual event.send call, not unrelated background tasks."""
    token = _CURRENT.set(event)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def trace_stage(event, stage):
    """Measure one real operation, including failures and task cancellation."""
    if stage not in _STAGES:
        raise ValueError("unsupported reply trace stage")
    state = _state(event)
    started = time.monotonic()
    operation = state["sequence"] + 1 if state is not None else 0
    outcome = {"status": "success"}
    _emit(state, stage, "begin", "pending", operation=operation)
    try:
        yield outcome
    except BaseException as exc:
        import asyncio

        outcome["status"] = "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "failed"
        raise
    finally:
        status = outcome.get("status", "success")
        if status not in _STATUSES - {"pending"}:
            status = "failed"
        _emit(state, stage, "end", status, operation=operation, started=started)
