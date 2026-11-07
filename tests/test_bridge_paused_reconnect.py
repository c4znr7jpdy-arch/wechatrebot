"""Expected refused connections stay quiet only during a verified manual pause."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from test_license_runtime_guard import runtime  # noqa: F401
from test_license_runtime_patches import bridge, patched  # noqa: F401


def refused(windows=False):
    # Windows ERROR_CONNECTION_REFUSED is not a ConnectionRefusedError subclass.
    return OSError(0, "synthetic refused", None, 1225) if windows else ConnectionRefusedError("synthetic refused")


def observe_failures(bridge, monkeypatch, error, *, attempts=1, before_attempt=None,
                     established=False, identity="wxid_actual"):
    async def run():
        ready = asyncio.Event()
        count = 0

        class Connect:
            async def __aenter__(self):
                nonlocal count
                count += 1
                if before_attempt is not None:
                    before_attempt(count)
                if count >= attempts:
                    ready.set()
                if not established:
                    raise error
                return SimpleNamespace()

            async def __aexit__(self, *_):
                pass

        monkeypatch.setattr(bridge.onebot.websockets, "connect", lambda *_, **__: Connect())
        client = bridge.onebot.OneBotClient(
            "ws://127.0.0.1:1", SimpleNamespace(), reconnect_min=.001, reconnect_max=.001,
        )
        client._self_id = identity

        async def disconnect(_):
            raise error

        client._run_connection = disconnect
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(ready.wait(), 1)
        finally:
            await client.close()
            # Both normal close and caller cancellation are covered separately.
            await asyncio.gather(task, return_exceptions=True)
        assert count == attempts

    asyncio.run(run())


def failure_levels(caplog):
    return [record.levelno for record in caplog.records
            if "AstrBot WebSocket disconnected:" in record.getMessage()]


@pytest.mark.parametrize("windows", [False, True], ids=["portable-refused", "windows-1225"])
def test_verified_pause_retries_without_warning(bridge, runtime, monkeypatch, caplog, windows):
    runtime.client.state.update(authorized=True, can_run=False, state="paused")
    caplog.set_level(logging.DEBUG)
    observe_failures(bridge, monkeypatch, refused(windows), attempts=3)
    assert failure_levels(caplog) == [logging.DEBUG] * 3
    assert runtime.client.accounts == ["wxid_actual"] * 3


def test_fresh_resume_restores_warning_on_next_attempt(bridge, runtime, monkeypatch, caplog):
    runtime.client.state.update(authorized=True, can_run=False, state="paused")
    caplog.set_level(logging.DEBUG)

    def resume(count):
        if count == 2:
            runtime.client.state.update(can_run=True, state="running")

    observe_failures(bridge, monkeypatch, refused(), attempts=2, before_attempt=resume)
    assert failure_levels(caplog) == [logging.DEBUG, logging.WARNING]


@pytest.mark.parametrize("case", [
    "running", "ready", "expired", "revoked", "unauthorized", "identity-mismatch",
    "unavailable-identity", "missing-identity", "legacy", "status-failure",
    "timeout", "network-unreachable", "established-connection",
])
def test_real_or_uncertain_failure_keeps_warning(bridge, runtime, monkeypatch, caplog, case):
    runtime.client.state.update(authorized=True, can_run=False, state="paused")
    identity = "wxid_actual"
    error = refused()
    if case in {"running", "ready", "expired", "revoked"}:
        runtime.client.state.update(state=case, can_run=case == "running")
    elif case == "unauthorized":
        runtime.client.state["authorized"] = False
    elif case == "identity-mismatch":
        runtime.identity["account"] = "wxid_other"
    elif case == "unavailable-identity":
        runtime.identity["account"] = ""
    elif case == "missing-identity":
        identity = ""
    elif case == "legacy":
        monkeypatch.setattr(runtime.guard, "status", lambda _: {
            "enabled": False, "authorized": True, "can_run": False,
            "state": "paused", "account": "wxid_actual",
        })
    elif case == "status-failure":
        def unavailable(_):
            raise RuntimeError("synthetic local failure")
        monkeypatch.setattr(runtime.guard, "status", unavailable)
    elif case == "timeout":
        error = TimeoutError("synthetic timeout")
    elif case == "network-unreachable":
        error = OSError("synthetic network unreachable")
    caplog.set_level(logging.DEBUG)
    observe_failures(bridge, monkeypatch, error, identity=identity,
                     established=case == "established-connection")
    assert failure_levels(caplog) == [logging.WARNING]
