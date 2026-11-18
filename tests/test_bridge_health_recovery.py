"""Fresh HOOK health probes recover from transport noise without caching access."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_group_self_join_notice import BOT, bridge  # noqa: F401


def setup_probe(app, monkeypatch, connect):
    app.settings = SimpleNamespace(hook_api_host="127.0.0.1", hook_api_port=19088)
    app._hook_ready = True
    app.onebot.connected = app.onebot.heartbeat_fresh = True
    module = sys.modules[app.__module__]
    monkeypatch.setattr(module.asyncio, "open_connection", connect)
    return module


def stream_writer(**kwargs):
    return SimpleNamespace(
        close=Mock(), wait_closed=AsyncMock(**kwargs),
        transport=SimpleNamespace(abort=Mock()),
    )


def test_connection_refusal_retries_before_denying_message_processing(bridge, monkeypatch):
    app, _ = bridge
    writer = stream_writer()
    connect = AsyncMock(side_effect=[ConnectionRefusedError(10061, "refused"), (None, writer)])
    setup_probe(app, monkeypatch, connect)
    snapshot = asyncio.run(app._health_snapshot())
    assert snapshot["hook_ready"] and snapshot["wechat_identity"] == BOT
    assert connect.await_count == 2
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


def test_failed_snapshot_does_not_latch_offline_or_reuse_old_success(bridge, monkeypatch):
    app, _ = bridge
    connect = AsyncMock(side_effect=ConnectionRefusedError(10061, "refused"))
    setup_probe(app, monkeypatch, connect)

    async def run():
        failed = await app._health_snapshot()
        assert not failed["hook_ready"] and not failed["wechat_logged_in"]
        assert not failed["heartbeat_fresh"] and failed["wechat_identity"] == ""
        assert app._hook_ready  # Only the consecutive-failure watcher owns this state.
        connect.side_effect = None
        connect.return_value = (None, stream_writer())
        recovered = await app._health_snapshot()
        assert recovered["hook_ready"] and recovered["wechat_identity"] == BOT
        connect.side_effect = ConnectionRefusedError(10061, "refused")
        assert not (await app._health_snapshot())["hook_ready"]

    asyncio.run(run())
    assert connect.await_count == 5


def test_parallel_queries_share_only_the_running_probe(bridge, monkeypatch):
    app, _ = bridge
    started, release = asyncio.Event(), asyncio.Event()

    async def connect(*_args):
        started.set()
        await release.wait()
        return None, stream_writer()

    connector = AsyncMock(side_effect=connect)
    setup_probe(app, monkeypatch, connector)

    async def run():
        first = asyncio.create_task(app._health_snapshot())
        await started.wait()
        second = asyncio.create_task(app._health_snapshot())
        await asyncio.sleep(0)
        release.set()
        snapshots = await asyncio.gather(first, second)
        assert all(s["hook_ready"] for s in snapshots)
        assert connector.await_count == 1
        app.state.bot_wxid = "wxid_new_account"
        assert (await app._health_snapshot())["wechat_identity"] == "wxid_new_account"
        assert connector.await_count == 2

    asyncio.run(asyncio.wait_for(run(), 1))


def test_cancelled_request_does_not_cancel_another_requests_probe(bridge, monkeypatch):
    app, _ = bridge
    started, release = asyncio.Event(), asyncio.Event()

    async def connect(*_args):
        started.set()
        await release.wait()
        return None, stream_writer()

    connector = AsyncMock(side_effect=connect)
    setup_probe(app, monkeypatch, connector)

    async def run():
        first = asyncio.create_task(app._health_snapshot())
        await started.wait()
        second = asyncio.create_task(app._health_snapshot())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second)["hook_ready"]
        assert connector.await_count == 1

    asyncio.run(asyncio.wait_for(run(), 1))


def test_connect_timeout_is_bounded_and_still_denies_access(bridge, monkeypatch):
    app, _ = bridge

    async def blocked(*_args):
        await asyncio.Event().wait()

    connector = AsyncMock(side_effect=blocked)
    module = setup_probe(app, monkeypatch, connector)
    monkeypatch.setattr(module, "_HOOK_PROBE_CONNECT_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(module, "_HOOK_PROBE_RETRY_DELAY_SECONDS", .001)

    async def run():
        snapshot = await app._health_snapshot()
        assert not snapshot["hook_ready"] and snapshot["wechat_identity"] == ""
        assert connector.await_count == 2 and app._hook_ready

    asyncio.run(asyncio.wait_for(run(), .5))


@pytest.mark.parametrize("reset", [False, True])
def test_cleanup_failure_does_not_invalidate_a_successful_connection(bridge, monkeypatch, reset):
    app, _ = bridge

    async def blocked_close():
        await asyncio.Event().wait()

    writer = stream_writer(side_effect=ConnectionResetError() if reset else blocked_close)
    connector = AsyncMock(return_value=(None, writer))
    module = setup_probe(app, monkeypatch, connector)
    monkeypatch.setattr(module, "_HOOK_PROBE_CLOSE_TIMEOUT_SECONDS", .01)

    async def run():
        assert (await app._health_snapshot())["hook_ready"]
        assert connector.await_count == 1
        writer.transport.abort.assert_called_once()

    asyncio.run(asyncio.wait_for(run(), .5))


def test_slow_failed_query_cannot_overwrite_newer_lifecycle_state(bridge):
    app, _ = bridge
    app._hook_ready = True
    app.onebot.connected = app.onebot.heartbeat_fresh = True
    started, release = asyncio.Event(), asyncio.Event()

    async def failed_probe():
        started.set()
        await release.wait()
        return False

    app._hook_api_accepting_connections = failed_probe

    async def run():
        query = asyncio.create_task(app._health_snapshot())
        await started.wait()
        app._hook_ready = True  # A newer background check succeeded.
        release.set()
        assert not (await query)["hook_ready"]
        assert app._hook_ready

    asyncio.run(asyncio.wait_for(run(), .5))


def test_old_success_cannot_revive_a_stopped_lifecycle(bridge):
    app, _ = bridge
    app._hook_ready = True
    app.onebot.connected = app.onebot.heartbeat_fresh = True

    async def stopped_during_probe():
        app._hook_ready = False
        return True

    app._hook_api_accepting_connections = stopped_during_probe
    snapshot = asyncio.run(app._health_snapshot())
    assert not snapshot["hook_ready"] and not snapshot["wechat_logged_in"]
    assert snapshot["wechat_identity"] == "" and not snapshot["heartbeat_fresh"]


@pytest.mark.parametrize("phase", ["connect", "close"])
def test_shutdown_cancels_and_collects_shared_probe(bridge, monkeypatch, phase):
    app, _ = bridge
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    writer = stream_writer(side_effect=blocked)

    async def connect(*_args):
        if phase == "connect":
            await blocked()
        return None, writer

    module = setup_probe(app, monkeypatch, AsyncMock(side_effect=connect))
    monkeypatch.setattr(module, "runtime_guard", lambda: SimpleNamespace(stop_refresh=Mock()))
    for attr in ("_consumer_task", "_cleanup_task", "_injector_watch_task", "_hook_watch_task",
                 "_group_member_watch_task", "_ws_task", "_injector", "_injector_output"):
        setattr(app, attr, None)
    app.onebot = app.router = None
    app._owns_session = False
    app._stop_callback_server = AsyncMock()

    async def run():
        request = asyncio.create_task(app._hook_api_accepting_connections())
        await started.wait()
        probe = app._hook_probe_task
        await app._shutdown()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert probe.done() and probe.cancelled()
        if phase == "close":
            writer.transport.abort.assert_called_once()

    asyncio.run(asyncio.wait_for(run(), 1))
