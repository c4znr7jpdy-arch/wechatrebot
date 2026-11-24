"""Reject malformed credit previews and signed responses that alter subscription time."""
import pytest

from wechatrebot_manager.licensing.protocol import LicenseError
from test_license_client import setup, activate


def credit_transport(client, service, changes=None):
    def transport(url, request):
        response = service(url, request)
        if url.endswith("/preview"):
            response["data"].update(mode="rebind_credit", rebind_credits=5, rebind_before=1, rebind_after=6)
            response["data"].update(changes or {})
        return response
    client.transport = transport


@pytest.mark.parametrize("changes", [
    {"rebind_credits": 0}, {"rebind_credits": 1001}, {"rebind_credits": True},
    {"rebind_before": -1}, {"rebind_after": 7}, {"rebind_after": "6"},
    {"license_id": ""}, {"plan": "trial"}, {"replaces_license_id": "another-license"},
    {"already_redeemed": True}, {"previous_valid_until": "invalid"}, {"account": []},
])
def test_invalid_credit_preview_cannot_be_confirmed(setup, changes):
    client, service, _, _ = setup
    service.plan = "month"
    activate(client)
    credit_transport(client, service, changes)
    with pytest.raises(LicenseError) as error:
        client.preview("WRB-TEST-CREDITS")
    assert error.value.code == "invalid_response"
    before = len(service.calls)
    with pytest.raises(LicenseError):
        client.redeem("WRB-TEST-CREDITS", "preview-1")
    assert len(service.calls) == before


@pytest.mark.parametrize("change", ["plan", "valid_until"])
def test_credit_response_must_preserve_the_approved_subscription(setup, change):
    client, service, _, _ = setup
    service.plan = "month"
    original = activate(client)
    credit_transport(client, service)
    prepared = client.preview("WRB-TEST-CREDITS")
    service.revision += 1
    if change == "plan":
        service.plan = "year"
    else:
        service.valid_until += 3600
    with pytest.raises(LicenseError) as error:
        client.redeem("WRB-TEST-CREDITS", prepared["preview_id"])
    assert error.value.code == "credit_mismatch"
    assert client.snapshot()["valid_until"] == original["valid_until"]
