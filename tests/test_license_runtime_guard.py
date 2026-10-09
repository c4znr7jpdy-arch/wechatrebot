import asyncio
import importlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from wechatrebot_manager.licensing.protocol import LicenseError, public_key
from wechatrebot_manager.licensing.runtime_guard import RuntimeGuard, guard_provider_class, resolve_install_root

module = importlib.import_module("wechatrebot_manager.licensing.runtime_guard")


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
        "public_keys": {"test": public_key(Ed25519PrivateKey.generate())}, "qq_group": "", "qq_url": "",
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
