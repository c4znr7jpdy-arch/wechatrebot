"""Self-service client and local API never trust eligibility as a run grant."""

from copy import deepcopy

import pytest

from test_license_api import make_api
from test_license_client import activate, setup
from wechatrebot_manager import app_shell
from wechatrebot_manager.licensing.protocol import LicenseError


@pytest.fixture
def trial_setup(setup):
    client, service, clock, protection = setup
    client.machine_id_source = lambda root: "a" * 64

    def transport(url, request):
        if url.endswith("/trial-claim") and not service.account:
            service.account = request["body"]["account"]
            service.revision += 1
        response = service(url, request)
        if url.endswith("/trial-status") and response["ok"]:
            response["data"] = {"state": "eligible", "eligible": True, "recoverable": False,
                                "account": request["body"]["account"], "valid_until": 0, "remaining_seconds": 0}
        return response

    client.transport = transport
    return client, service, clock, protection


def test_public_self_service_apis_and_unactivated_connection_do_not_enable_automation(trial_setup):
    client, service, _, _ = trial_setup
    assert {"get_trial_status", "activate_free_trial"} <= set(app_shell.PUBLIC_API_METHODS)
    status = client.snapshot()
    assert status["can_connect"] and not status["can_run"] and not status["authorized"]
    api, _, actions = make_api(client)
    assert api.connect_license_wechat()["ok"] and actions == ["connect_wechat"]
    assert not service.calls and not client.directory.exists()


def test_claim_returns_bound_signed_trial_without_a_run_grant(trial_setup):
    client, service, clock, _ = trial_setup
    status = client.claim_trial("wxid_trial")
    assert status["authorized"] and status["account"] == "wxid_trial"
    assert status["plan"] == "trial" and status["valid_until"] == clock.server + 3600
    assert not status["can_run"]
    assert service.calls[-1][1]["body"] == {"account": "wxid_trial", "machine_id": "a" * 64}
    clock.advance(3600)
    status = client.snapshot("wxid_trial")
    assert status["state"] == "expired" and not status["can_run"]
    assert status["message"] == "你的试用已结束，可联系群主购买"


@pytest.mark.parametrize("bad", ["", "x" * 64, None, 123])
def test_bad_physical_identity_fails_before_creating_device_or_request(trial_setup, bad):
    client, service, _, _ = trial_setup
    client.machine_id_source = lambda root: bad
    for method in (client.trial_status, client.claim_trial):
        with pytest.raises(LicenseError, match="设备身份") as caught:
            method("wxid_trial")
        assert caught.value.code == "device_identity_unavailable"
    assert not service.calls and not client.directory.exists()


def test_physical_identity_os_error_is_sanitized(trial_setup):
    client, service, _, _ = trial_setup

    def fail(root):
        raise OSError("private hardware data")

    client.machine_id_source = fail
    with pytest.raises(LicenseError) as caught:
        client.claim_trial("wxid_trial")
    assert "private" not in str(caught.value) and not service.calls


@pytest.mark.parametrize("corruption", [
    {"account": "wxid_other"}, {"eligible": 1}, {"recoverable": True},
    {"remaining_seconds": 1}, {"valid_until": -1}, {"state": "unknown"},
    {"state": "active", "eligible": False, "recoverable": True, "valid_until": 1, "remaining_seconds": 0},
])
def test_untrusted_eligibility_is_strictly_validated(trial_setup, corruption):
    client, _, _, _ = trial_setup
    original = client.transport

    def malformed(url, request):
        response = original(url, request)
        response["data"].update(corruption)
        return response

    client.transport = malformed
    with pytest.raises(LicenseError) as caught:
        client.trial_status("wxid_trial")
    assert caught.value.code == "invalid_response"
    assert not client.snapshot("wxid_trial")["authorized"]


@pytest.mark.parametrize("corruption,error", [
    ({"account": "wxid_other"}, "identity_mismatch"),
    ({"account": ""}, "invalid_lease"),
    ({"plan": "month"}, "invalid_lease"),
    ({"status": "revoked"}, "invalid_lease"),
    ({"valid_until": 1_800_003_601}, "invalid_lease"),
    ({"request_nonce": "b" * 32}, "lease_replayed"),
])
def test_claim_rejects_signed_but_wrong_trial_contract(trial_setup, corruption, error):
    client, service, _, _ = trial_setup
    service.transform = lambda payload: payload.update(corruption)
    with pytest.raises(LicenseError) as caught:
        client.claim_trial("wxid_trial")
    assert caught.value.code == error and not client.snapshot()["authorized"]


def test_valid_paid_run_grant_is_not_replaced_or_paused_by_trial_claim(trial_setup):
    client, service, _, _ = trial_setup
    service.plan = "month"
    activate(client)
    client.bind("wxid_trial")
    original = client.authorize_run("wxid_trial")
    calls = len(service.calls)
    with pytest.raises(LicenseError) as caught:
        client.claim_trial("wxid_trial")
    assert caught.value.code == "active_license_conflict" and len(service.calls) == calls
    status = client.snapshot("wxid_trial")
    assert status["can_run"] and status["run_id"] == original["run_id"]


def test_trial_recovery_cannot_extend_deadline_even_with_a_new_signed_revision(trial_setup):
    client, service, clock, _ = trial_setup
    original = client.claim_trial("wxid_trial")
    clock.advance(60)
    service.valid_until += 60
    service.revision += 1
    with pytest.raises(LicenseError) as caught:
        client.claim_trial("wxid_trial")
    assert caught.value.code == "invalid_lease"
    assert client.snapshot("wxid_trial")["valid_until"] == original["valid_until"]


def test_expired_paid_lease_can_explicitly_accept_first_trial_and_requires_start(trial_setup):
    client, service, clock, _ = trial_setup
    service.plan = "month"
    activate(client)
    client.bind("wxid_trial")
    client.authorize_run("wxid_trial")
    clock.advance(3600)
    service.plan, service.valid_until, service.revision = "trial", clock.server + 3600, 1
    service.transform = lambda payload: payload.update(license_id="new-trial")
    status = client.claim_trial("wxid_trial")
    assert status["license_id"] == "new-trial" and status["authorized"] and not status["can_run"]


@pytest.mark.parametrize("prior", ["expired_trial", "untrusted_clock"])
def test_expired_trial_or_untrusted_old_lease_blocks_claim_before_network(trial_setup, prior):
    client, service, clock, _ = trial_setup
    activate(client)
    client.bind("wxid_trial")
    if prior == "expired_trial":
        clock.advance(3600)
    else:
        clock.wall -= 60
    calls = len(service.calls)
    with pytest.raises(LicenseError) as caught:
        client.claim_trial("wxid_trial")
    assert caught.value.code == ("trial_used" if prior == "expired_trial" else "clock_error")
    assert len(service.calls) == calls


@pytest.mark.parametrize("expected", ["wxid_other", "", None, 123])
def test_api_rechecks_actual_account_instead_of_trusting_ui(trial_setup, expected):
    client, service, _, _ = trial_setup
    api, _, actions = make_api(client, account="wxid_trial")
    result = api.activate_free_trial(expected)
    assert result["error"]["code"] == "LIC-account_changed"
    assert not service.calls and not actions


def test_api_claim_and_status_require_live_identity_and_never_start_robot(trial_setup):
    client, service, _, _ = trial_setup
    api, runtime, actions = make_api(client, account="wxid_trial")
    runtime.account_status_available = False
    assert api.get_trial_status()["error"]["code"] == "LIC-account_required"
    assert api.activate_free_trial("wxid_trial")["error"]["code"] == "LIC-account_required"
    assert not service.calls
    runtime.account_status_available = True
    result = api.get_trial_status()
    assert result["data"]["free_trial"]["eligible"] and not result["data"]["can_run"]
    result = api.activate_free_trial("wxid_trial")
    assert result["data"]["free_trial"]["state"] == "active"
    assert result["data"]["current_account"] == "wxid_trial" and not result["data"]["can_run"]
    assert not actions
    calls = len(service.calls)
    assert "free_trial" not in api.get_license_status()["data"]
    assert len(service.calls) == calls


@pytest.mark.parametrize("method", ["get_trial_status", "activate_free_trial"])
def test_account_switch_during_remote_request_cannot_report_success(trial_setup, method):
    client, _, _, _ = trial_setup
    api, runtime, actions = make_api(client, account="wxid_trial")
    original = client.transport

    def switched(url, request):
        response = original(url, request)
        runtime.wechat_identity = "wxid_other"
        return deepcopy(response)

    client.transport = switched
    args = ("wxid_trial",) if method == "activate_free_trial" else ()
    result = getattr(api, method)(*args)
    assert result["error"]["code"] == "LIC-account_changed" and not actions
    assert not client.snapshot("wxid_other")["can_run"]


@pytest.mark.parametrize("plan", ["trial", "month"])
def test_code_preview_hardware_failure_blocks_only_new_trials(trial_setup, plan):
    client, service, _, _ = trial_setup
    client.machine_id_source = lambda root: ""
    service.plan = plan
    if plan == "trial":
        with pytest.raises(LicenseError) as caught:
            client.preview("WRB-TEST-ACTIVATION")
        assert caught.value.code == "device_identity_unavailable"
        assert not client.snapshot()["authorized"]
    else:
        assert activate(client)["authorized"]
    assert all("machine_id" not in request["body"] for _, request in service.calls)


def test_code_redemption_rereads_hardware_and_cannot_consume_new_trial_if_it_fails(trial_setup):
    client, service, _, _ = trial_setup
    original = client.transport

    def new_code(url, request):
        response = original(url, request)
        if url.endswith("/preview"):
            response["data"]["license_id"] = ""
        return response

    client.transport = new_code
    preview = client.preview("WRB-TEST-ACTIVATION")
    assert service.calls[-1][1]["body"]["machine_id"] == "a" * 64
    calls = len(service.calls)
    client.machine_id_source = lambda root: ""
    with pytest.raises(LicenseError) as caught:
        client.redeem("WRB-TEST-ACTIVATION", preview["preview_id"])
    assert caught.value.code == "device_identity_unavailable" and len(service.calls) == calls
