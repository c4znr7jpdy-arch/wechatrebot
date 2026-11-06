"""Bridge workers fail visibly and connection children always unwind together."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401


@pytest.mark.parametrize("bad_frame", [
    '{"action":"get_status","echo":' + "9" * 5000 + "}",
    '{"action":"get_status","echo":' + "[" * 10000 + "0" + "]" * 10000 + "}",
], ids=["integer-limit", "nesting-limit"])
def test_invalid_json_limits_do_not_stop_following_actions(bridge, bad_frame, caplog):
    class Connection:
        async def __aiter__(self):
            yield bad_frame
            yield '{"action":"get_status","echo":"valid"}'

    router = SimpleNamespace(handle=AsyncMock(return_value={"status": "ok"}))
    client = bridge.onebot.OneBotClient("ws://127.0.0.1:6200/ws", router)
    client._send_json = AsyncMock()

    asyncio.run(client._receive_loop(Connection()))

    router.handle.assert_awaited_once_with({"action": "get_status", "echo": "valid"})
    client._send_json.assert_awaited_once_with({"status": "ok"})
    assert "ignored invalid JSON from AstrBot" in caplog.text


@pytest.mark.parametrize("outcome", ["cancel", "normal", "error"])
def test_connection_collects_all_child_tasks(bridge, outcome):
    async def run():
        client = bridge.onebot.OneBotClient("ws://127.0.0.1:6200/ws", SimpleNamespace())
        ready = asyncio.Event()
        release = asyncio.Event()
        entered = set()
        finished = set()

        async def child(name):
            entered.add(name)
            if len(entered) == 3:
                ready.set()
            try:
                if name == "receive" and outcome != "cancel":
                    await release.wait()
                    if outcome == "error":
                        raise RuntimeError("synthetic receive failure")
                    return
                await asyncio.Event().wait()
            finally:
                # Ensure cleanup is awaited, rather than only cancellation sent.
                await asyncio.sleep(0)
                finished.add(name)

        client._receive_loop = lambda connection: child("receive")
        client._sender_loop = lambda: child("sender")
        client._heartbeat_loop = lambda: child("heartbeat")
        task = asyncio.create_task(client._run_connection(SimpleNamespace()))
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
                assert task.cancelled()
            elif outcome == "error":
                release.set()
                with pytest.raises(RuntimeError, match="synthetic receive failure"):
                    await asyncio.wait_for(task, timeout=1)
            else:
                release.set()
                await asyncio.wait_for(task, timeout=1)
            assert finished == {"receive", "sender", "heartbeat"}
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def application(bridge):
    app = object.__new__(bridge.app.BridgeApplication)
    app._stop_event = asyncio.Event()
    app._ws_task = None
    app._consumer_task = None
    app._injector_watch_task = None
    app._hook_watch_task = None
    return app


@pytest.mark.parametrize("worker", ["_ws_task", "_consumer_task"])
@pytest.mark.parametrize("outcome", ["normal", "error", "cancel"])
def test_critical_worker_exit_is_reported_without_hook_watchers(bridge, worker, outcome):
    async def run():
        app = application(bridge)

        async def complete():
            if outcome == "error":
                raise RuntimeError("synthetic worker failure")
            if outcome == "cancel":
                raise asyncio.CancelledError()

        task = asyncio.create_task(complete(), name="synthetic-critical-worker")
        setattr(app, worker, task)
        with pytest.raises(RuntimeError, match={
            "normal": "stopped unexpectedly", "error": "synthetic worker failure",
            "cancel": "unexpectedly cancelled",
        }[outcome]):
            await asyncio.wait_for(app._wait_for_stop_or_runtime_failure(), timeout=1)
        assert task.done()

    asyncio.run(run())


def test_successful_injector_exit_keeps_critical_workers_supervised(bridge):
    async def run():
        app = application(bridge)
        release = asyncio.Event()

        async def fail_later():
            await release.wait()
            raise RuntimeError("late worker failure")

        app._injector_watch_task = asyncio.create_task(asyncio.sleep(0))
        app._ws_task = asyncio.create_task(fail_later())
        waiter = asyncio.create_task(app._wait_for_stop_or_runtime_failure())
        await app._injector_watch_task
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()
        with pytest.raises(RuntimeError, match="late worker failure"):
            await asyncio.wait_for(waiter, timeout=1)

    asyncio.run(run())


def test_requested_stop_wins_over_expected_worker_exit(bridge):
    async def run():
        app = application(bridge)
        app._ws_task = asyncio.create_task(asyncio.sleep(0))
        await app._ws_task
        app._stop_event.set()
        await asyncio.wait_for(app._wait_for_stop_or_runtime_failure(), timeout=1)

    asyncio.run(run())


def test_cancelling_supervisor_preserves_cancellation_and_does_not_stop_workers(bridge):
    async def run():
        app = application(bridge)
        app._ws_task = asyncio.create_task(asyncio.Event().wait())
        before = asyncio.all_tasks()
        waiter = asyncio.create_task(app._wait_for_stop_or_runtime_failure())
        await asyncio.sleep(0)
        waiter.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not app._ws_task.done()  # BridgeApplication._shutdown owns it.
            assert not [task for task in asyncio.all_tasks() - before if not task.done()]
        finally:
            app._ws_task.cancel()
            await asyncio.gather(app._ws_task, return_exceptions=True)

    asyncio.run(run())


def test_worker_failure_returns_failure_and_runs_existing_shutdown(bridge, monkeypatch):
    async def run():
        app = application(bridge)
        app.settings = SimpleNamespace()
        app.launch_injector = False
        app.group_member_poll_interval_seconds = 0
        app._ensure_components = AsyncMock()
        app._start_callback_server = AsyncMock()
        app._periodic_media_cleanup = lambda: asyncio.Event().wait()
        app._consume_events = lambda: asyncio.Event().wait()

        async def fail():
            raise RuntimeError("synthetic runtime failure")

        app.onebot = SimpleNamespace(run=fail)

        async def shutdown():
            for task in (app._ws_task, app._consumer_task, app._cleanup_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(app._ws_task, app._consumer_task, app._cleanup_task,
                                 return_exceptions=True)

        app._shutdown = AsyncMock(side_effect=shutdown)
        monkeypatch.setattr(bridge.app, "configure_logging", lambda settings: None)
        monkeypatch.setattr(bridge.app, "runtime_guard", lambda: SimpleNamespace(start_refresh=lambda: None))

        assert await asyncio.wait_for(app.run(), timeout=1) == 1
        app._shutdown.assert_awaited_once()
        assert app._hook_ready is False

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["stop", "cancel", "worker-error"])
def test_real_shutdown_collects_connection_workers(bridge, monkeypatch, outcome):
    async def run():
        ready = asyncio.Event()
        closed = asyncio.Event()

        class Connection:
            def __aiter__(self):
                return self

            async def __anext__(self):
                ready.set()
                await closed.wait()
                raise StopAsyncIteration

            async def close(self, **kwargs):
                closed.set()

        connection = Connection()

        class Connect:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *args):
                await connection.close()

        app = application(bridge)
        app.settings = SimpleNamespace()
        app.launch_injector = False
        app.group_member_poll_interval_seconds = 0
        app._group_member_watch_task = None
        app._injector = None
        app._owns_session = False
        app._session = None
        app._ensure_components = AsyncMock()
        app._start_callback_server = AsyncMock()
        app._stop_callback_server = AsyncMock()
        app._periodic_media_cleanup = lambda: asyncio.Event().wait()
        app.router = SimpleNamespace(close=AsyncMock())
        app.onebot = bridge.onebot.OneBotClient("ws://127.0.0.1:6200/ws", app.router)

        async def consume():
            await ready.wait()
            if outcome == "worker-error":
                raise RuntimeError("synthetic worker failure")
            await asyncio.Event().wait()

        app._consume_events = consume
        monkeypatch.setattr(bridge.onebot.websockets, "connect", lambda *args, **kwargs: Connect())
        monkeypatch.setattr(bridge.app, "configure_logging", lambda settings: None)
        monkeypatch.setattr(bridge.app, "runtime_guard", lambda: SimpleNamespace(
            start_refresh=lambda: None, stop_refresh=lambda: None))
        before = asyncio.all_tasks()
        task = asyncio.create_task(app.run())
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)
            else:
                if outcome == "stop":
                    await app.stop()
                assert await asyncio.wait_for(task, timeout=1) == (1 if outcome == "worker-error" else 0)
            assert closed.is_set()
            assert app.onebot._run_task is None
            assert all(worker.done() for worker in (
                app._ws_task, app._consumer_task, app._cleanup_task))
            assert not [worker for worker in asyncio.all_tasks() - before if not worker.done()]
            app.router.close.assert_awaited_once()
            app._stop_callback_server.assert_awaited_once()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
