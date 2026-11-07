"""Graceful stop cancels the connection owner without waiting for a heartbeat."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401
from test_bridge_task_lifecycle import application


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    module = importlib.import_module("wechatrebot_manager.licensing.runtime_guard")
    monkeypatch.setattr(module, "_guard", SimpleNamespace(
        queue_event=lambda value: dict(value), delivery_event=lambda value: dict(value),
        start_refresh=lambda: None, stop_refresh=lambda: None,
    ))


class Connection:
    def __init__(self, *, action=False, close_error=False):
        self.action = action
        self.close_error = close_error
        self.receiving = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    async def __aiter__(self):
        self.receiving.set()
        if self.action:
            yield json.dumps({"action": "synthetic_long_action", "echo": "synthetic"})
        await self.closed.wait()

    async def send(self, _):
        if self.closed.is_set():
            raise ConnectionError("synthetic closed socket")

    async def close(self, **_):
        self.close_calls += 1
        self.closed.set()
        if self.close_error:
            raise OSError("synthetic close failure")


def client_for(bridge, monkeypatch, connection, handler=None):
    class Connect:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *_):
            connection.closed.set()

    monkeypatch.setattr(bridge.onebot.websockets, "connect", lambda *_, **__: Connect())
    router = SimpleNamespace(handle=handler or AsyncMock(return_value={"status": "ok"}))
    client = bridge.onebot.OneBotClient("ws://127.0.0.1:1", router, heartbeat_interval=3600)
    client._self_id = "wxid_synthetic_bot"
    return client


@pytest.mark.parametrize("action", [False, True], ids=["idle-heartbeat", "inflight-action"])
def test_close_collects_children_promptly_with_live_socket(bridge, monkeypatch, action):
    async def run():
        connection = Connection(action=action)
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        client = client_for(bridge, monkeypatch, connection, handler)
        before = asyncio.all_tasks()
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(entered.wait() if action else connection.receiving.wait(), 1)
            await client.close()
            await asyncio.wait_for(task, 1)
            assert not task.cancelled(), "requested close remains a normal client exit"
            assert client._run_task is None and not client.connected
            assert connection.close_calls == 1
            if action:
                assert cleaned.is_set()
            assert not [item for item in asyncio.all_tasks() - before if not item.done()]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_repeated_close_does_not_interrupt_child_cleanup(bridge, monkeypatch):
    async def run():
        connection = Connection(action=True)
        entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()

        client = client_for(bridge, monkeypatch, connection, handler)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await client.close()
            await asyncio.wait_for(cleaning.wait(), 1)
            await asyncio.gather(client.close(), client.close())
            assert not task.done() and not finished.is_set()
            release.set()
            await asyncio.wait_for(task, 1)
            assert finished.is_set() and connection.close_calls == 1
            await client.close()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("caller", ["receive", "send", "heartbeat"])
def test_connection_child_can_request_close_without_self_wait(bridge, monkeypatch, caller):
    async def run():
        connection = Connection(action=True)
        ready = asyncio.Event()
        stopped = set()
        client = client_for(bridge, monkeypatch, connection)

        async def worker(name):
            try:
                if name == caller:
                    await ready.wait()
                    await client.close()
                else:
                    ready.set()
                    await asyncio.Event().wait()
            finally:
                stopped.add(name)

        client._receive_loop = lambda _: worker("receive")
        client._sender_loop = lambda: worker("send")
        client._heartbeat_loop = lambda: worker("heartbeat")
        before = asyncio.all_tasks()
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(task, 1)
            assert stopped == {"receive", "send", "heartbeat"}
            assert client._run_task is None
            assert not [item for item in asyncio.all_tasks() - before if not item.done()]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_run_owner_can_close_itself_and_finish_normally(bridge, monkeypatch):
    async def run():
        connection = Connection()
        client = client_for(bridge, monkeypatch, connection)

        async def close_inside_owner(_):
            await client.close()
            await asyncio.sleep(0)

        client._run_connection = close_inside_owner
        task = asyncio.create_task(client.run())
        await asyncio.wait_for(task, 1)
        assert not task.cancelled() and client._run_task is None

    asyncio.run(run())


def test_close_failure_still_cancels_owner_and_collects_action(bridge, monkeypatch):
    async def run():
        connection = Connection(action=True, close_error=True)
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        client = client_for(bridge, monkeypatch, connection, handler)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(OSError, match="synthetic close failure"):
                await client.close()
            await asyncio.wait_for(task, 1)
            assert cleaned.is_set() and client._run_task is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_close_before_start_and_after_owner_exit_are_safe(bridge, monkeypatch):
    async def run():
        connection = Connection()
        client = client_for(bridge, monkeypatch, connection)
        await client.close()
        await client.run()
        await client.close()
        assert connection.close_calls == 0 and client._run_task is None

        other = client_for(bridge, monkeypatch, Connection())
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        other._run_task = finished
        await other.close()
        assert not finished.cancelled()

    asyncio.run(run())


def test_external_run_cancellation_is_preserved_and_collects_children(bridge, monkeypatch):
    async def run():
        connection = Connection(action=True)
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        client = client_for(bridge, monkeypatch, connection, handler)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert cleaned.is_set() and task.cancelled()
            assert client._run_task is None
            await client.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_close_during_existing_cancellation_does_not_cancel_cleanup_again(bridge, monkeypatch):
    async def run():
        connection = Connection(action=True)
        entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()

        client = client_for(bridge, monkeypatch, connection, handler)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 1)
            await client.close()
            assert task.cancelling() == 1 and not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert finished.is_set() and client._run_task is None
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_cancelled_socket_close_does_not_leave_connection_workers(bridge, monkeypatch):
    async def run():
        connection = Connection(action=True)
        entered, closing, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def blocking_close(**_):
            closing.set()
            await asyncio.Event().wait()

        connection.close = blocking_close
        client = client_for(bridge, monkeypatch, connection, handler)
        task = asyncio.create_task(client.run())
        close_task = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            close_task = asyncio.create_task(client.close())
            await asyncio.wait_for(closing.wait(), 1)
            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task
            await asyncio.wait_for(task, 1)
            assert cleaned.is_set() and client._run_task is None
        finally:
            task.cancel()
            await asyncio.gather(task, *([close_task] if close_task else []), return_exceptions=True)

    asyncio.run(run())


def test_application_stop_collects_inflight_action_without_heartbeat_wait(bridge, monkeypatch):
    async def run():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

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
        app._consume_events = lambda: asyncio.Event().wait()
        app.router = SimpleNamespace(close=AsyncMock())
        app.onebot = client_for(bridge, monkeypatch, Connection(action=True), handler)
        monkeypatch.setattr(bridge.app, "configure_logging", lambda _: None)
        before = asyncio.all_tasks()
        task = asyncio.create_task(app.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await app.stop()
            assert await asyncio.wait_for(task, 1) == 0
            assert cleaned.is_set() and app.onebot._run_task is None
            assert all(worker.done() for worker in (app._ws_task, app._consumer_task, app._cleanup_task))
            app.router.close.assert_awaited_once()
            app._stop_callback_server.assert_awaited_once()
            assert not [worker for worker in asyncio.all_tasks() - before if not worker.done()]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("external_count", [1, 2, 3], ids=["first-external", "second-external", "coalesced-second"])
def test_close_then_external_cancel_preserves_one_cleanup_and_a_second_force(bridge, monkeypatch, external_count):
    async def run():
        connection = Connection(action=True)
        entered, cleaning, release, finished, interrupted = (asyncio.Event() for _ in range(5))

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
                finished.set()

        client = client_for(bridge, monkeypatch, connection, handler)
        before = asyncio.all_tasks()
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await client.close()
            await asyncio.wait_for(cleaning.wait(), 1)
            task.cancel()
            if external_count == 3:
                task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if external_count == 1:
                assert not task.done() and not interrupted.is_set()
                release.set()
            else:
                if external_count == 2:
                    task.cancel()
                await asyncio.wait_for(interrupted.wait(), 1)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert finished.is_set() == (external_count == 1)
            assert interrupted.is_set() == (external_count != 1)
            assert client._run_task is None and not client.connected
            assert not [item for item in asyncio.all_tasks() - before if not item.done()]
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_external_first_then_second_cancel_keeps_existing_force_semantics(bridge, monkeypatch):
    async def run():
        connection = Connection(action=True)
        entered, cleaning, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise

        client = client_for(bridge, monkeypatch, connection, handler)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 1)
            await client.close()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert interrupted.is_set() and client._run_task is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
