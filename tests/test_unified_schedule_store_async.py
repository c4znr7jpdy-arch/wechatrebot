"""Store contention and cancellation must not block or outlive the scheduler."""
import asyncio
from datetime import datetime, timezone
import sqlite3
import threading

import pytest

from test_unified_schedule_runtime import (
    FakeClient, FakeContent, _command_event, _copy_runtime_package, _task,
)


def _runtime(tmp_path):
    module = _copy_runtime_package(tmp_path)
    account = ["wxid_robot"]
    sent = []

    async def connection():
        return account[0], FakeClient()

    async def sender(_client, run, target, _content):
        sent.append((run["id"], target["id"]))
        return {"message_id": "test-receipt"}

    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    runtime = module.UnifiedRuntime(
        object(), install_root=tmp_path, content=FakeContent(),
        connection=connection, sender=sender, clock=lambda: now,
    )
    runtime._active = True
    saved = runtime.store.save(_task(), now=now)
    runtime.clock = lambda: datetime.fromisoformat(saved["next_run_time"])
    return module, runtime, account, sent


class StoreGate:
    """Pause one real store call after its transaction commits."""
    def __init__(self, loop, function, *, predicate=None):
        self.loop, self.function = loop, function
        self.predicate = predicate or (lambda *_args, **_kwargs: True)
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.result = None
        self.thread_id = None

    def __call__(self, *args, **kwargs):
        result = self.function(*args, **kwargs)
        if self.predicate(*args, **kwargs):
            self.result = result
            self.thread_id = threading.get_ident()
            self.loop.call_soon_threadsafe(self.started.set)
            if not self.release.wait(5):
                raise AssertionError("test did not release store operation")
        return result


def test_sqlite_write_lock_does_not_block_the_event_loop(tmp_path, monkeypatch):
    _module, runtime, _account, sent = _runtime(tmp_path)

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        original = runtime.store.claim_due

        def claim(*args):
            loop.call_soon_threadsafe(entered.set)
            return original(*args)

        monkeypatch.setattr(runtime.store, "claim_due", claim)
        lock = sqlite3.connect(runtime.store.path)
        lock.execute("BEGIN IMMEDIATE")
        poll = asyncio.create_task(runtime.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            # A real SQLite writer is still holding the lock, but this heartbeat
            # and the assertion must run before it is released by this task.
            await asyncio.sleep(0)
            assert not poll.done()
        finally:
            lock.rollback()
            lock.close()
        await asyncio.wait_for(poll, 2)

    asyncio.run(scenario())
    assert len(sent) == 1


def test_cancelled_claim_waits_for_commit_and_finishes_runs_before_lease_release(tmp_path, monkeypatch):
    module, runtime, _account, sent = _runtime(tmp_path)

    async def scenario():
        gate = StoreGate(asyncio.get_running_loop(), runtime.store.claim_due)
        monkeypatch.setattr(runtime.store, "claim_due", gate)
        runtime.start()
        await asyncio.wait_for(gate.started.wait(), 2)
        stopping = asyncio.create_task(runtime.stop())
        contender = module.RuntimeLease(runtime._lease.path)
        try:
            await asyncio.sleep(0)
            stopping.cancel()
            await asyncio.sleep(0)
            stopping.cancel()
            await asyncio.sleep(0)
            assert not stopping.done()
            assert not contender.acquire()
            assert gate.thread_id != threading.get_ident()
        finally:
            gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, 2)
        assert runtime._lease.handle is None and runtime._workers == {}
        assert contender.acquire()
        contender.release()
        runs = runtime.store.runs("wxid_robot", "runtime-task")
        assert len(runs) == 1 and runs[0]["status"] == "skipped"
        assert all(item["status"] == "skipped" for item in runs[0]["deliveries"])
        assert not runtime._requests

    asyncio.run(scenario())
    assert sent == []


@pytest.mark.parametrize("operation", ["save", "create_run_now"])
def test_cancelled_test_creation_removes_task_and_finishes_claimed_run(tmp_path, monkeypatch, operation):
    _module, runtime, _account, sent = _runtime(tmp_path)

    async def scenario():
        gate = StoreGate(asyncio.get_running_loop(), getattr(runtime.store, operation))
        monkeypatch.setattr(runtime.store, operation, gate)
        request = asyncio.create_task(runtime.test({**_task(), "test_target": _task()["targets"][0]}))
        try:
            await asyncio.wait_for(gate.started.wait(), 2)
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            await asyncio.sleep(0)
            assert not request.done()
        finally:
            gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 2)
        with runtime.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM scheduled_tasks WHERE id LIKE 'test_%' AND deleted=0").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM scheduled_task_runs WHERE task_id LIKE 'test_%' AND status='running'").fetchone()[0] == 0
        assert not runtime._requests

    asyncio.run(scenario())
    assert sent == []


def test_stop_waits_for_immediate_test_receipt_and_does_not_resend(tmp_path, monkeypatch):
    module, runtime, _account, sent = _runtime(tmp_path)

    async def scenario():
        # Leave the regular task in the future; this scenario executes only the
        # explicit test request, which is not part of the scheduled worker map.
        runtime.clock = lambda: datetime(2026, 9, 14, tzinfo=timezone.utc)
        gate = StoreGate(asyncio.get_running_loop(), runtime.store.set_delivery,
                         predicate=lambda _run, _kind, _target, status, *_rest: status == "submitted")
        monkeypatch.setattr(runtime.store, "set_delivery", gate)
        runtime.start()
        request = asyncio.create_task(runtime.test({**_task(), "test_target": _task()["targets"][0]}))
        try:
            await asyncio.wait_for(gate.started.wait(), 2)
            stopping = asyncio.create_task(runtime.stop())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not stopping.done()
            contender = module.RuntimeLease(runtime._lease.path)
            assert not contender.acquire()
        finally:
            gate.release.set()
        await asyncio.wait_for(stopping, 2)
        with pytest.raises(asyncio.CancelledError):
            await request
        with runtime.store._connect() as connection:
            run = connection.execute("SELECT * FROM scheduled_task_runs WHERE task_id LIKE 'test_%'").fetchone()
            assert run["status"] == "submitted"
            assert connection.execute("SELECT deleted FROM scheduled_tasks WHERE id=?", (run["task_id"],)).fetchone()[0] == 1
        assert runtime._lease.handle is None and not runtime._requests

    asyncio.run(scenario())
    assert len(sent) == 1


def test_store_work_is_serial_and_cancelled_waiters_leave_no_background_work(tmp_path):
    _module, runtime, _account, _sent = _runtime(tmp_path)

    async def scenario():
        gate = StoreGate(asyncio.get_running_loop(), lambda: "first")
        completed = []
        first = asyncio.create_task(runtime._store_call(gate))
        second = None
        try:
            await asyncio.wait_for(gate.started.wait(), 2)
            second = asyncio.create_task(runtime._store_call(lambda: completed.append("second")))
            await asyncio.sleep(0)
            second.cancel()
            await asyncio.sleep(0)
            assert completed == [] and second.cancelled()
        finally:
            gate.release.set()
        assert await asyncio.wait_for(first, 2) == "first"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second, 2)
        assert completed == [] and not runtime._store_lock.locked()

    asyncio.run(scenario())


def test_finalization_cannot_be_cancelled_while_waiting_for_store_slot(tmp_path):
    _module, runtime, _account, _sent = _runtime(tmp_path)
    run = runtime.store.claim_due(runtime.clock(), "wxid_robot")[0]

    async def scenario():
        gate = StoreGate(asyncio.get_running_loop(), lambda: None)
        occupied = asyncio.create_task(runtime._store_call(gate))
        cleanup = None
        try:
            await asyncio.wait_for(gate.started.wait(), 2)
            cleanup = asyncio.create_task(runtime._store_cleanup(runtime.store.finish_run, run["id"]))
            await asyncio.sleep(0)
            cleanup.cancel()
            await asyncio.sleep(0)
            cleanup.cancel()
            await asyncio.sleep(0)
            assert not cleanup.done()
        finally:
            gate.release.set()
        await asyncio.wait_for(occupied, 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cleanup, 2)
        assert runtime.store.runs("wxid_robot", "runtime-task")[0]["status"] == "skipped"

    asyncio.run(scenario())


def test_stop_cleans_workers_even_when_poller_never_started(tmp_path):
    _module, runtime, _account, sent = _runtime(tmp_path)

    async def scenario():
        await runtime._dispatch_due()
        runtime.start()
        runtime._poller.cancel()
        await asyncio.wait_for(runtime.stop(), 2)
        assert not runtime._workers and runtime._poller is None
        assert runtime.store.runs("wxid_robot", "runtime-task")[0]["status"] == "skipped"

    asyncio.run(scenario())
    assert sent == []


def test_account_switch_while_reading_tasks_rejects_old_account_results(tmp_path, monkeypatch):
    _module, runtime, account, _sent = _runtime(tmp_path)

    async def scenario():
        gate = StoreGate(asyncio.get_running_loop(), runtime.store.list)
        monkeypatch.setattr(runtime.store, "list", gate)
        request = asyncio.create_task(runtime.command_tasks(_command_event()))
        try:
            await asyncio.wait_for(gate.started.wait(), 2)
            account[0] = "wxid_other"
        finally:
            gate.release.set()
        with pytest.raises(ValueError, match="账号未连接或已经切换"):
            await asyncio.wait_for(request, 2)

    asyncio.run(scenario())


def test_cancelled_database_failure_does_not_restart_poller_or_leak_lease(tmp_path, monkeypatch, caplog):
    _module, runtime, _account, sent = _runtime(tmp_path)

    async def scenario():
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def fail(*_args):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise AssertionError("test did not release database failure")
            raise sqlite3.OperationalError("private database details")

        monkeypatch.setattr(runtime.store, "claim_due", fail)
        runtime.start()
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            stopping = asyncio.create_task(runtime.stop())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not stopping.done()
        finally:
            release.set()
        await asyncio.wait_for(stopping, 2)
        assert runtime._poller is None and runtime._lease.handle is None
        assert not runtime._active and not runtime._requests

    asyncio.run(scenario())
    assert "OperationalError" in caplog.text
    assert "private database details" not in caplog.text
    assert sent == []


def test_account_switch_during_delivery_authorization_prevents_send(tmp_path, monkeypatch):
    _module, runtime, account, sent = _runtime(tmp_path)

    async def scenario():
        gate = StoreGate(asyncio.get_running_loop(), runtime.store.authorize_delivery)
        monkeypatch.setattr(runtime.store, "authorize_delivery", gate)
        request = asyncio.create_task(runtime.run_once())
        try:
            await asyncio.wait_for(gate.started.wait(), 2)
            account[0] = "wxid_other"
        finally:
            gate.release.set()
        await asyncio.wait_for(request, 2)
        run = runtime.store.runs("wxid_robot", "runtime-task")[0]
        assert run["status"] == "skipped"
        assert "账号或功能发生变化" in run["deliveries"][0]["message"]

    asyncio.run(scenario())
    assert sent == []
