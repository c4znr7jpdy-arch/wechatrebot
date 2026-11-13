import asyncio
import importlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.request import Request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from wechatrebot_manager.licensing.protocol import LicenseError, public_key
from wechatrebot_manager.licensing.runtime_guard import RuntimeGuard, guard_provider_class, resolve_install_root

from test_license_client import activate, setup as license_setup

module = importlib.import_module("wechatrebot_manager.licensing.runtime_guard")


def test_delayed_bridge_health_reads_current_account_without_caching(monkeypatch):
    payload = {
        "service_ready": True, "status_available": True, "hook_ready": True,
        "wechat_logged_in": True, "wechat_identity": "wxid_current",
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            # A healthy bridge can reply outside the old 0.5s deadline.
            time.sleep(.65)
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(module, "Request", lambda _url, **kwargs: Request(
        f"http://127.0.0.1:{server.server_port}/api/health", **kwargs,
    ))
    try:
        assert module.probe_account() == "wxid_current"
        # A cached identity must never turn an authoritative logout into access.
        payload.update(wechat_logged_in=False, wechat_identity="")
        assert module.probe_account() == ""
        assert requests == ["/api/health", "/api/health"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("payload", [
    {"service_ready": True, "status_available": False},
    {"service_ready": True, "status_available": True, "hook_ready": False},
    {"service_ready": True, "status_available": True, "hook_ready": True,
     "wechat_logged_in": True, "wechat_identity": ""},
])
def test_inconclusive_bridge_health_is_distinct_from_logout(monkeypatch, payload):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _size):
            return json.dumps(payload).encode()

    monkeypatch.setattr(module, "build_opener", lambda *_args: SimpleNamespace(
        open=lambda *_args, **_kwargs: Response(),
    ))
    with pytest.raises(LicenseError) as error:
        module.probe_account()
    assert error.value.code == "account_status_unavailable"


def test_bridge_health_transport_failure_is_inconclusive(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr(module, "build_opener", lambda *_args: SimpleNamespace(open=unavailable))
    with pytest.raises(LicenseError) as error:
        module.probe_account()
    assert error.value.code == "account_status_unavailable"


class FakeClient:
    def __init__(self):
        self.state = {"enabled": True, "can_run": True, "state": "running", "message": "authorized",
                      "run_id": "a" * 32, "run_granted_at": 100, "account": "wxid_actual"}
        self.accounts = []
        self.on_refresh = lambda: None

    def snapshot(self, account=""):
        self.accounts.append(account)
        return {**self.state, "can_run": self.state["can_run"] and account == self.state["account"]}

    def refresh_if_due(self):
        self.on_refresh()
        return {**self.state, "can_run": True}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = tmp_path / "config/licensing.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "schema_version": 1, "enabled": True, "service_url": "https://license.example.invalid",
        "public_keys": {"test": public_key(Ed25519PrivateKey.generate())},
    }), encoding="utf-8")
    client = FakeClient()
    identity = {"account": "wxid_actual"}
    guard = RuntimeGuard(tmp_path, client=client, account_source=lambda: identity["account"])
    monkeypatch.setattr(module, "_guard", guard)
    try:
        yield SimpleNamespace(guard=guard, client=client, identity=identity, root=tmp_path)
    finally:
        guard.stop_refresh()


def test_installed_module_path_takes_priority_over_environment(tmp_path):
    module_file = tmp_path / "installed/runtime/python/Lib/site-packages/wechatrebot_manager/licensing/runtime_guard.py"
    assert resolve_install_root(module_file, {"WECHATREBOT_INSTALL_ROOT": str(tmp_path / "wrong")}) == tmp_path / "installed"
    source_file = tmp_path / "source/src/wechatrebot_manager/licensing/runtime_guard.py"
    assert resolve_install_root(source_file, {"WECHATREBOT_INSTALL_ROOT": str(tmp_path / "dev")}) == tmp_path / "dev"
    assert resolve_install_root(source_file, {}) is None


def test_unconfigured_legacy_mode_never_queries_wechat_or_starts_refresh(tmp_path):
    def unexpected():
        pytest.fail("Legacy mode must not request runtime identity")
    guard = RuntimeGuard(tmp_path, account_source=unexpected)
    assert not guard.require()["enabled"]
    guard.start_refresh()
    assert guard._refresh_thread is None


def test_runtime_requires_explicit_run_permission_and_actual_account(runtime):
    assert runtime.guard.require()["can_run"]
    runtime.client.state["can_run"] = False
    with pytest.raises(LicenseError):
        runtime.guard.require()
    runtime.client.state["can_run"] = True
    runtime.identity["account"] = "wxid_other"
    with pytest.raises(LicenseError):
        runtime.guard.require()
    runtime.identity["account"] = ""
    with pytest.raises(LicenseError):
        runtime.guard.require()


def test_refresh_rechecks_actual_identity_instead_of_trusting_response(runtime):
    runtime.client.on_refresh = lambda: runtime.identity.update(account="wxid_switched")
    assert not runtime.guard.refresh_once()["can_run"]
    assert runtime.client.accounts[-1] == "wxid_switched"


def test_old_core_stays_blocked_after_subscription_restored(runtime, monkeypatch):
    monkeypatch.setattr(runtime.guard, "start_refresh", lambda: None)
    runtime.guard.claim_core_session()
    runtime.client.state["can_run"] = False
    with pytest.raises(LicenseError):
        runtime.guard.require_core()
    runtime.client.state.update(can_run=True, run_id="b" * 32)
    assert runtime.guard.require()["can_run"]
    with pytest.raises(LicenseError):
        runtime.guard.require_core()


def test_unconfirmed_account_blocks_work_and_recovers_same_core_session(runtime, monkeypatch):
    monkeypatch.setattr(runtime.guard, "start_refresh", lambda: None)
    runtime.guard.claim_core_session()
    source = runtime.guard.account_source

    def unavailable():
        raise LicenseError("account_status_unavailable", "重新确认连接")

    runtime.guard.account_source = unavailable
    writes = []

    class Provider:
        async def text_chat(self):
            writes.append("model_call")
            return "answer"

    guard_provider_class(Provider)
    for action in (runtime.guard.require_core,
                   lambda: runtime.guard.require_hook("/api/send_text_msg"),
                   lambda: asyncio.run(Provider().text_chat())):
        with pytest.raises(LicenseError) as error:
            action()
        assert error.value.code == "account_status_unavailable"
    assert not writes and not runtime.guard._core_blocked
    assert not runtime.guard.allow_event({"post_type": "message", "self_id": "wxid_actual"})

    runtime.guard.account_source = source
    assert runtime.guard.require_core()["run_id"] == "a" * 32
    assert asyncio.run(Provider().text_chat()) == "answer"
    assert writes == ["model_call"]


@pytest.mark.parametrize("changes", [
    {"can_run": False, "state": "paused"},
    {"can_run": False, "state": "expired"},
    {"can_run": False, "state": "revoked"},
    {"run_id": "b" * 32},
])
def test_unconfirmed_account_cannot_hide_invalid_or_replaced_run(runtime, monkeypatch, changes):
    monkeypatch.setattr(runtime.guard, "start_refresh", lambda: None)
    runtime.guard.claim_core_session()
    source = runtime.guard.account_source

    def unavailable():
        raise LicenseError("account_status_unavailable", "重新确认连接")

    runtime.guard.account_source = unavailable
    runtime.client.state.update(changes)
    with pytest.raises(LicenseError) as error:
        runtime.guard.require_core()
    assert error.value.code == "automation_paused" and runtime.guard._core_blocked
    runtime.guard.account_source = source
    runtime.client.state.update(can_run=True, state="running", run_id="a" * 32)
    with pytest.raises(LicenseError):
        runtime.guard.require_core()


def test_signed_run_grant_survives_unconfirmed_account_without_allowing_sends(license_setup, monkeypatch):
    client, _, _, _ = license_setup
    activate(client)
    client.bind("wxid_actual")
    grant = client.authorize_run("wxid_actual")
    guard = RuntimeGuard(client.root, client=client, account_source=lambda: "wxid_actual")
    monkeypatch.setattr(guard, "start_refresh", lambda: None)
    guard.claim_core_session()

    def unavailable():
        raise LicenseError("account_status_unavailable", "重新确认连接")

    guard.account_source = unavailable
    for action in (guard.require_core, lambda: guard.require_hook("/api/send_text_msg")):
        with pytest.raises(LicenseError) as error:
            action()
        assert error.value.code == "account_status_unavailable"
    assert not guard._core_blocked
    assert client.snapshot("wxid_actual")["run_id"] == grant["run_id"]
    guard.account_source = lambda: "wxid_actual"
    assert guard.require_core()["run_id"] == grant["run_id"]


def test_signed_grant_expiry_stops_core_even_while_account_remains_unconfirmed(license_setup, monkeypatch):
    client, _, clock, _ = license_setup
    activate(client)
    client.bind("wxid_actual")
    client.authorize_run("wxid_actual")
    guard = RuntimeGuard(client.root, client=client, account_source=lambda: "wxid_actual")
    monkeypatch.setattr(guard, "start_refresh", lambda: None)
    monkeypatch.setattr(module, "_guard", guard)
    guard.claim_core_session()
    probes, calls = [], []

    def unavailable():
        probes.append(True)
        if len(probes) == 2:
            clock.advance(3600)
        raise LicenseError("account_status_unavailable", "重新确认连接")

    guard.account_source = unavailable

    class Lifecycle:
        async def stop(self):
            calls.append("graceful_stop")

    async def check():
        async with asyncio.timeout(2):
            await module.watch_core(Lifecycle(), interval=0.001)

    asyncio.run(check())
    assert len(probes) == 2 and calls == ["graceful_stop"]
    assert guard._core_blocked and client.snapshot("wxid_actual")["state"] == "expired"


def test_queued_events_are_bound_to_run_session_and_pre_resume_events_are_dropped(runtime):
    event = {"post_type": "message", "self_id": "wxid_actual", "time": 101, "message": "hello"}
    queued = runtime.guard.queue_event(event)
    assert queued["_wechatrebot_license_run_id"] == "a" * 32
    assert runtime.guard.allow_event(queued, queued=True)
    runtime.client.state.update(run_id="b" * 32, run_granted_at=102)
    assert not runtime.guard.allow_event(queued, queued=True)
    assert runtime.guard.queue_event(event) is None
    runtime.client.state["can_run"] = False
    assert runtime.guard.queue_event({**event, "time": 103}) is None
    assert runtime.guard.allow_event({"post_type": "meta_event"}, queued=True)


@pytest.mark.parametrize("path", ["/api/send_text_msg", "/api/send_app_msg", "/api/send_xml", "/api/revoke_any", "/api/cdn_c2c_video_upload", "/api/unknown_future_write"])
def test_final_hook_requests_block_all_unapproved_writes(runtime, path):
    runtime.client.state["can_run"] = False
    with pytest.raises(LicenseError):
        runtime.guard.require_hook(path)


@pytest.mark.parametrize("path", ["/api/get_profile_cache", "/api/get_contact", "/api/get_room_members", "/api/download_file", "/api/get_cgi_offset"])
def test_data_and_identity_reads_remain_available_after_expiry(runtime, path):
    runtime.client.state["can_run"] = False
    runtime.guard.require_hook(path)


def test_unactivated_identity_connection_allows_login_reads_but_blocks_business(runtime):
    runtime.client.state.update(can_run=False, state="unactivated", run_id="", account="")
    runtime.identity["account"] = ""
    calls = []

    class Actions:
        @module.guard_bridge_action
        async def handle(self, request):
            calls.append(request["action"])
            runtime.guard.require_hook("/api/get_profile_cache")
            return {"account": runtime.identity["account"]}

    async def run():
        runtime.guard.require_hook("/api/health")
        assert await Actions().handle({"action": "get_login_info"}) == {"account": ""}
        runtime.identity["account"] = "wxid_new_trial"
        assert await Actions().handle({"action": "get_login_info"}) == {"account": "wxid_new_trial"}
        with pytest.raises(LicenseError):
            await Actions().handle({"action": "send_group_msg"})
        with pytest.raises(LicenseError):
            runtime.guard.require_hook("/api/send_text_msg")

    asyncio.run(run())
    assert calls == ["get_login_info", "get_login_info"]
    assert runtime.guard.queue_event({"post_type": "message", "self_id": "wxid_new_trial"}) is None
    assert not runtime.guard.status()["can_run"]


def test_provider_wrapper_checks_before_call_and_after_result(runtime):
    calls = []

    class Provider:
        async def text_chat(self):
            calls.append("request")
            runtime.client.state["can_run"] = False
            return "late result"

    guard_provider_class(Provider)
    with pytest.raises(LicenseError):
        asyncio.run(Provider().text_chat())
    assert calls == ["request"]
    with pytest.raises(LicenseError):
        asyncio.run(Provider().text_chat())
    assert calls == ["request"]


def test_stream_wrapper_stops_before_reading_next_chunk_and_closes_generator(runtime):
    calls = []

    class Provider:
        async def text_chat_stream(self):
            try:
                calls.append("first")
                yield "first"
                calls.append("second")
                yield "second"
            finally:
                calls.append("closed")

    guard_provider_class(Provider)

    async def consume():
        stream = Provider().text_chat_stream()
        assert await anext(stream) == "first"
        runtime.client.state["can_run"] = False
        with pytest.raises(LicenseError):
            await anext(stream)

    asyncio.run(consume())
    assert calls == ["first", "closed"]


def test_core_expiry_does_not_wait_for_blocked_network_refresh(runtime):
    started, release = threading.Event(), threading.Event()

    def slow_refresh():
        started.set()
        release.wait(3)

    runtime.client.on_refresh = slow_refresh
    runtime.guard.start_refresh()
    assert started.wait(1)
    runtime.client.state["can_run"] = False
    calls = []

    class Lifecycle:
        async def stop(self):
            calls.append("graceful_stop")

    try:
        asyncio.run(module.watch_core(Lifecycle(), interval=0.001))
        assert calls == ["graceful_stop"] and not release.is_set()
    finally:
        release.set()


def test_core_watch_waits_for_unconfirmed_account_then_recovers(runtime, monkeypatch):
    monkeypatch.setattr(runtime.guard, "start_refresh", lambda: None)
    runtime.guard.claim_core_session()
    calls = []
    probes = []

    def account():
        probes.append(True)
        if len(probes) == 1:
            raise LicenseError("account_status_unavailable", "重新确认连接")
        return "wxid_actual"

    runtime.guard.account_source = account

    class Lifecycle:
        async def stop(self):
            calls.append("stop")

    async def check():
        task = asyncio.create_task(module.watch_core(Lifecycle(), interval=0.001))
        try:
            async with asyncio.timeout(2):
                while len(probes) < 3:
                    await asyncio.sleep(0.001)
            assert not calls and not runtime.guard._core_blocked
            assert runtime.guard.require_core()["can_run"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


def test_missing_or_invalid_runtime_configuration_fails_closed(runtime):
    (runtime.root / "config/licensing.json").write_text("invalid JSON", encoding="utf-8")
    assert not runtime.guard.status()["can_run"]


def test_action_context_reaches_delayed_child_tasks(runtime):
    results = []

    class Actions:
        @module.guard_bridge_action
        async def handle(self, request):
            async def delayed():
                await asyncio.sleep(0)
                runtime.guard.require_hook("/api/revoke_any")
                results.append("write")
            return asyncio.create_task(delayed())

    async def run():
        task = await Actions().handle({"action": "auto_recall_last_msg"})
        runtime.client.state.update(run_id="b" * 32)
        with pytest.raises(LicenseError):
            await task
        # A separate new action gets the new session; no leaked ContextVar.
        task = await Actions().handle({"action": "auto_recall_last_msg"})
        await task

    asyncio.run(run())
    assert results == ["write"]


def test_bridge_action_uses_requested_account_before_business_write(runtime):
    calls = []

    class Actions:
        @module.guard_bridge_action
        async def handle(self, request):
            calls.append("write")

    with pytest.raises(LicenseError):
        asyncio.run(Actions().handle({"action": "send_group_msg", "params": {"self_id": "wxid_old"}}))
    assert calls == []


def test_action_account_is_rechecked_at_hook_after_licensed_account_switch(runtime):
    calls = []

    class Actions:
        @module.guard_bridge_action
        async def handle(self, request):
            runtime.identity["account"] = "wxid_other"
            runtime.client.state["account"] = "wxid_other"
            runtime.guard.require_hook("/api/send_text_msg")
            calls.append("write")

    with pytest.raises(LicenseError):
        asyncio.run(Actions().handle({"action": "send_group_msg", "params": {"self_id": "wxid_actual"}}))
    assert calls == []


def test_legacy_action_account_survives_downloads_and_child_tasks(tmp_path, monkeypatch):
    identity = ["wxid_actual"]
    guard = RuntimeGuard(tmp_path, account_source=lambda: identity[0])
    monkeypatch.setattr(module, "_guard", guard)
    calls = []

    class Actions:
        @module.guard_bridge_action
        async def handle(self, request):
            async def delayed():
                await asyncio.sleep(0)
                guard.require_hook("/api/send_text_msg")
                calls.append("write")
            return asyncio.create_task(delayed())

    async def run():
        task = await Actions().handle({"action": "send_group_msg", "params": {"self_id": "wxid_actual"}})
        identity[0] = "wxid_other"
        with pytest.raises(LicenseError):
            await task
        with pytest.raises(LicenseError):
            await Actions().handle({"action": "send_group_msg", "params": {"self_id": "wxid_actual"}})
        # A new request uses its own account after the previous scope is reset.
        task = await Actions().handle({"action": "send_group_msg", "params": {"self_id": "wxid_other"}})
        await task

    asyncio.run(run())
    assert calls == ["write"]
