"""Rebinding reads server counters and accepts only the confirmed signed account."""
import asyncio
import threading

import pytest
from aiohttp.test_utils import TestServer

from services.activation_service.app import create_app
from services.activation_service.store import LicenseStore
from test_license_api import make_api
from test_license_client import activate, setup
from test_license_client_integration import exchange
from wechatrebot_manager import app_shell
from wechatrebot_manager.licensing.client import LicenseClient, http_transport
from wechatrebot_manager.licensing.protocol import LicenseError


@pytest.fixture
def rebound_setup(setup):
    client, service, clock, protection = setup
    service.plan = "month"
    activate(client)
    client.bind("wxid_old")
    counters = {"used": 0}

    def transport(url, request):
        if url.endswith("/rebind") and not service.error and service.account != request["body"]["account"]:
            service.account = request["body"]["account"]
            service.revision += 1
            counters["used"] += 1
        response = service(url, request)
        if url.endswith("/binding-status") and response["ok"]:
            response["data"] = {"license_id": "license-1", "account": service.account,
                "plan": service.plan, "revision": service.revision, "rebind_limit": 1,
                "rebind_used": counters["used"], "rebind_remaining": 1 - counters["used"],
                "can_rebind": counters["used"] == 0}
        return response

    client.transport = transport
    return client, service, clock, counters


def test_binding_counter_query_does_not_need_wechat_or_change_a_run_grant(rebound_setup):
    client, service, _, _ = rebound_setup
    grant = client.authorize_run("wxid_old")
    api, _, actions = make_api(client)
    api._lifecycle.fresh_runtime_snapshot = lambda: pytest.fail("must not probe a login before consent")
    result = api.get_license_binding_status()
    assert result["ok"] and result["data"]["rebind_remaining"] == 1
    assert service.calls[-1][1]["body"] == {"license_id": "license-1"}
    assert not actions and client.snapshot("wxid_old")["run_id"] == grant["run_id"]
    assert {"get_license_binding_status", "rebind_license_account"} <= set(app_shell.PUBLIC_API_METHODS)


@pytest.mark.parametrize("corruption", [
    {"license_id": "someone-else"}, {"plan": "unknown"}, {"revision": 0},
    {"account": 1}, {"account": "bad account"}, {"can_rebind": 1},
    {"rebind_used": True}, {"rebind_limit": -1}, {"rebind_remaining": 9},
    {"account": "", "can_rebind": True}, {"rebind_used": 1, "rebind_remaining": 0, "can_rebind": True},
])
def test_malformed_counter_response_cannot_be_displayed_as_real_quota(rebound_setup, corruption):
    client, _, _, _ = rebound_setup
    original = client.transport

    def malformed(url, request):
        result = original(url, request)
        if url.endswith("/binding-status"):
            result["data"].update(corruption)
        return result

    client.transport = malformed
    with pytest.raises(LicenseError) as error:
        client.binding_status()
    assert error.value.code == "invalid_response"
    assert not client.snapshot("wxid_old")["can_run"]


def test_rebinding_preserves_entitlement_and_clears_old_automation(rebound_setup):
    client, service, _, counters = rebound_setup
    before = client.authorize_run("wxid_old")
    quota = client.binding_status()
    after = client.rebind("wxid_new", expected_account=quota["account"], expected_revision=quota["revision"])
    assert after["account"] == "wxid_new" and not after["can_run"]
    assert after["plan"] == before["plan"] and after["valid_until"] == before["valid_until"]
    assert not client.snapshot("wxid_old")["can_run"]
    assert service.calls[-1][1]["body"] == {"license_id": "license-1", "account": "wxid_new",
        "expected_account": "wxid_old", "expected_revision": quota["revision"]}
    client.rebind("wxid_new", expected_account=quota["account"], expected_revision=quota["revision"])
    assert counters["used"] == 1 and client.binding_status()["rebind_remaining"] == 0


@pytest.mark.parametrize("corruption", [
    {"account": "wxid_wrong"}, {"valid_until": 1_800_003_601},
    {"plan": "year"}, {"status": "revoked"}, {"request_nonce": "b" * 32},
])
def test_wrong_signed_rebinding_result_never_restores_the_old_run(rebound_setup, corruption):
    client, service, _, _ = rebound_setup
    client.authorize_run("wxid_old")
    quota = client.binding_status()
    service.transform = lambda payload: payload.update(corruption)
    with pytest.raises(LicenseError):
        client.rebind("wxid_new", expected_account=quota["account"], expected_revision=quota["revision"])
    assert not client.snapshot("wxid_old")["can_run"]


@pytest.mark.parametrize("code", ["rebind_unavailable", "rebind_exhausted", "binding_changed"])
def test_rebinding_errors_keep_their_actionable_code(rebound_setup, code):
    client, service, _, _ = rebound_setup
    service.error = code
    with pytest.raises(LicenseError) as error:
        client.rebind("wxid_new", expected_account="wxid_old", expected_revision=2)
    assert error.value.code == code and "private" not in str(error.value)


@pytest.mark.parametrize("code", ["license_unavailable", "license_revoked", "subscription_expired"])
def test_rebinding_denial_cannot_be_bypassed_by_starting_the_cached_old_account(rebound_setup, code):
    client, service, _, _ = rebound_setup
    client.authorize_run("wxid_old")
    service.error = code
    with pytest.raises(LicenseError):
        client.rebind("wxid_new", expected_account="wxid_old", expected_revision=2)
    assert not client.snapshot("wxid_old")["authorized"]
    with pytest.raises(LicenseError):
        client.authorize_run("wxid_old")


@pytest.mark.parametrize("blocked", ["wrong_account", "unknown_account", "core_running", "busy", "installing"])
def test_rebinding_api_rejects_unsafe_state_before_any_server_write(rebound_setup, blocked):
    client, service, _, _ = rebound_setup
    api, runtime, actions = make_api(client, account="wxid_new")
    api._action_lock = threading.Lock()
    if blocked == "wrong_account":
        runtime.wechat_identity = "wxid_other"
    elif blocked == "unknown_account":
        runtime.account_status_available = False
    elif blocked == "core_running":
        runtime.engine_ready = True
    elif blocked == "busy":
        api._action_lock.acquire()
    elif blocked == "installing":
        api._update_installing = True
    calls = len(service.calls)
    result = api.rebind_license_account("wxid_new", "wxid_old", 2)
    assert not result["ok"] and len(service.calls) == calls and not actions
    assert client.snapshot()["account"] == "wxid_old"
    assert api._action_lock.locked() is (blocked == "busy")


def test_rebinding_api_uses_fresh_actual_account_without_starting(rebound_setup):
    client, _, _, _ = rebound_setup
    api, _, actions = make_api(client, account="wxid_new")
    api._action_lock = threading.Lock()
    result = api.rebind_license_account("wxid_new", "wxid_old", 2)
    assert result["ok"] and result["data"]["account"] == "wxid_new"
    assert not result["data"]["can_run"] and not actions and not api._action_lock.locked()


def test_account_change_after_commit_reports_committed_binding_without_start(rebound_setup, monkeypatch):
    client, _, _, _ = rebound_setup
    api, _, actions = make_api(client, account="wxid_new")
    api._action_lock = threading.Lock()
    accounts = iter(("wxid_new", "wxid_other"))
    monkeypatch.setattr(api, "_license_account", lambda **_: next(accounts))
    result = api.rebind_license_account("wxid_new", "wxid_old", 2)
    assert not result["ok"] and result["error"]["code"] == "LIC-account_changed"
    assert client.snapshot()["account"] == "wxid_new" and not actions and not api._action_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "response_loss", "cache_write"])
async def test_real_http_rebinding_recovers_committed_change_without_extra_quota(setup, tmp_path, monkeypatch, failure):
    client, service, clock, protection = setup
    store = LicenseStore(tmp_path / "server/activation.db")
    server = TestServer(create_app(store, service.key, "test", clock=lambda: clock.server))
    await server.start_server()
    failed = False

    def transport(url, request):
        nonlocal failed
        action = url.rsplit("/", 1)[-1]
        result = http_transport(str(server.make_url("/v1/" + action)), request)
        if action == "rebind" and failure == "response_loss" and not failed:
            failed = True
            raise OSError("response lost after commit")
        return result

    try:
        client.transport = transport
        code = store.issue_code(owner_ref="rebind-customer", reference="rebind-month", now=clock.server, plan="month")
        _, original = await exchange(client, code["code"])
        await asyncio.to_thread(client.bind, "wxid_old")
        client.authorize_run("wxid_old")
        quota = await asyncio.to_thread(client.binding_status)
        assert quota["rebind_remaining"] == 1 and quota["rebind_used"] == 0
        original_write = client.cache.write

        def write(state):
            nonlocal failed
            if failure == "cache_write" and not failed and state["lease"]["payload"]["account"] == "wxid_new":
                failed = True
                raise LicenseError("storage_error", "simulated cache failure")
            original_write(state)

        monkeypatch.setattr(client.cache, "write", write)
        kwargs = {"expected_account": quota["account"], "expected_revision": quota["revision"]}
        if failure != "none":
            with pytest.raises(LicenseError):
                await asyncio.to_thread(client.rebind, "wxid_new", **kwargs)
            assert not client.snapshot("wxid_old")["can_run"]
            assert store.binding_status(original["device_id"], {"license_id": original["license_id"]}, clock.server)["rebind_used"] == 1
            client = LicenseClient(client.root, transport=transport, clock=clock, protection=protection)
        result = await asyncio.to_thread(client.rebind, "wxid_new", **kwargs)
        assert result["account"] == "wxid_new" and result["valid_until"] == original["valid_until"]
        assert not result["can_run"]
        latest = await asyncio.to_thread(client.binding_status)
        assert latest["rebind_used"] == 1 and latest["rebind_remaining"] == 0
        with pytest.raises(LicenseError) as error:
            await asyncio.to_thread(client.rebind, "wxid_third", expected_account="wxid_new", expected_revision=latest["revision"])
        assert error.value.code == "rebind_exhausted"
        assert client.snapshot()["account"] == "wxid_new"
    finally:
        await server.close()
