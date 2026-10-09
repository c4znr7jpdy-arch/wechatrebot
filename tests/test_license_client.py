from __future__ import annotations

import base64
from copy import deepcopy
import json
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
import pytest

from wechatrebot_manager.licensing.client import LicenseClient
from wechatrebot_manager.licensing import config as config_module
from wechatrebot_manager.licensing.device import WindowsProtection, boot_session_id, load_device
from wechatrebot_manager.licensing.protocol import (
    LicenseError, issue_lease, public_key, verify_request,
)
from wechatrebot_manager.single_instance import SingleInstanceLock


class Clock:
    def __init__(self):
        self.wall = self.server = 1_800_000_000
        self.mono = 100_000
        self.boot = "a" * 32

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def boot_id(self):
        return self.boot

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds
        self.server += seconds


class TestProtection:
    __test__ = False

    def __init__(self):
        self.cipher = AESGCM(AESGCM.generate_key(bit_length=256))

    def protect(self, value):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, value, None)

    def unprotect(self, value):
        return self.cipher.decrypt(value[:12], value[12:], None)


class Service:
    def __init__(self, clock):
        self.clock = clock
        self.key = Ed25519PrivateKey.generate()
        self.calls = []
        self.account = ""
        self.plan = "trial"
        self.valid_until = clock.server + 3600
        self.revision = 1
        self.status = "active"
        self.offline = False
        self.transform = None
        self.error = None

    def __call__(self, url, request):
        self.calls.append((url, deepcopy(request)))
        if self.offline:
            raise OSError("do not expose C:/private/server-key.pem")
        action = url.rsplit("/", 1)[-1]
        device_id, body = verify_request(request, action, int(self.clock.server))
        if self.error:
            return {"ok": False, "error": {"code": self.error, "message": "private-key-secret"}}
        if action == "preview":
            return {"ok": True, "data": {
                "preview_id": "preview-1", "plan": self.plan, "mode": "new",
                "valid_until": self.valid_until, "license_id": "license-1",
                "already_redeemed": False, "message": "确认兑换",
                "expires_at": int(self.clock.server) + 300, "replaces_license_id": "",
            }}
        if action == "bind" and self.account != body["account"]:
            self.account = body["account"]
            self.revision += 1
        payload = {
            "schema_version": 1, "audience": "wechatrebot", "license_id": "license-1",
            "device": device_id, "account": self.account, "plan": self.plan,
            "status": self.status, "issued_at": int(self.clock.server),
            "lease_until": min(self.valid_until, int(self.clock.server) + 86400),
            "valid_until": self.valid_until, "revision": self.revision,
            "request_nonce": request["nonce"],
        }
        if self.transform:
            self.transform(payload)
        return {"ok": True, "data": {"lease": issue_lease(self.key, "test", payload)}}

    def renew(self, seconds=30 * 86400):
        self.plan = "month"
        self.valid_until = max(self.valid_until, self.clock.server) + seconds
        self.revision += 1


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    service = Service(clock)
    protection = TestProtection()
    config = tmp_path / "config/licensing.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "schema_version": 1, "enabled": True, "service_url": "https://license.example.test",
        "public_keys": {"test": public_key(service.key)},
    }), encoding="utf-8")
    client = LicenseClient(tmp_path, transport=service, clock=clock, protection=protection)
    return client, service, clock, protection


def activate(client):
    preview = client.preview("WRB-TEST-ACTIVATION")
    return client.redeem("WRB-TEST-ACTIVATION", preview["preview_id"])


def running(client):
    activate(client)
    client.bind("wxid_customer")
    return client.authorize_run("wxid_customer")


def renew(client, service):
    service.renew()
    return activate(client)


def offer_migration(client, service, *, corruption=None):
    deadline = int(service.clock.server) + 30 * 86400

    def response_payload(payload):
        payload.update(
            license_id="license-migrated", account="wxid_migrated", plan="month", revision=2,
            valid_until=deadline, lease_until=min(deadline, payload["issued_at"] + 86400),
        )
        if corruption:
            corruption(payload)

    def transport(url, request):
        response = service(url, request)
        if url.endswith("/preview"):
            response["data"].update(mode="migration", license_id="license-migrated",
                                   replaces_license_id="license-1", plan="month", valid_until=deadline)
        return response

    service.transform = response_payload
    client.transport = transport
    return client.preview("WRB-MIGRATION-CODE")


def test_legacy_and_unconfigured_status_do_not_create_device_or_network(tmp_path):
    client = LicenseClient(tmp_path, transport=lambda *_: pytest.fail("unexpected network"))
    result = client.snapshot()
    assert result["state"] == "legacy"
    assert not result["authorized"] and result["can_run"] and result["can_connect"]
    assert not client.directory.exists()
    assert [(p["id"], p["price_fen"], p["seconds"]) for p in result["plans"]] == [
        ("trial", 0, 3600), ("month", 6900, 0), ("year", 69900, 0),
    ]
    config = tmp_path / "config/licensing.json"
    config.parent.mkdir()
    config.write_text('{"schema_version":1,"enabled":true}', encoding="utf-8")
    assert client.snapshot()["state"] == "configuration_pending"
    assert not client.snapshot()["can_run"]
    with pytest.raises(LicenseError, match="尚未配置"):
        client.preview("WRB-TEST-ACTIVATION")
    assert not client.directory.exists()


def test_malformed_channel_fails_closed(tmp_path):
    config = tmp_path / "config/licensing.json"
    config.parent.mkdir()
    config.write_text("{broken", encoding="utf-8")
    result = LicenseClient(tmp_path).snapshot()
    assert result["enabled"] and not result["can_run"]
    assert result["state"] == "configuration_invalid"


@pytest.mark.parametrize("local_override", [
    '{"schema_version":1,"enabled":false}',
    '{"schema_version":1,"enabled":true,"service_url":"https://attacker.example"}',
    "{broken",
])
def test_commercial_package_ignores_local_disable_and_service_override(setup, monkeypatch, local_override):
    client, service, _, _ = setup
    config = client.root / "config/licensing.json"
    packaged = client.root / "packaged-channel.json"
    packaged.write_bytes(config.read_bytes())
    monkeypatch.setattr(config_module, "PACKAGED_CHANNEL", packaged)
    config.write_text(local_override, encoding="utf-8")
    result = client.snapshot()
    assert result["enabled"] and result["configured"] and not result["can_run"]
    assert activate(client)["authorized"]
    assert all(url.startswith("https://license.example.test/v1/") for url, _ in service.calls)


def test_commercial_package_does_not_trust_customer_supplied_signing_key(setup, monkeypatch):
    client, service, _, _ = setup
    config = client.root / "config/licensing.json"
    packaged = client.root / "packaged-channel.json"
    packaged.write_bytes(config.read_bytes())
    monkeypatch.setattr(config_module, "PACKAGED_CHANNEL", packaged)
    value = json.loads(config.read_text(encoding="utf-8"))
    service.key = Ed25519PrivateKey.generate()
    value["public_keys"] = {"test": public_key(service.key)}
    config.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(LicenseError) as error:
        activate(client)
    assert error.value.code == "invalid_lease"
    assert not client.snapshot()["authorized"]


def test_activation_requires_account_binding_and_explicit_run(setup):
    client, service, _, _ = setup
    assert client.snapshot()["state"] == "unactivated"
    assert not client.directory.exists()
    result = activate(client)
    assert result["state"] == "active_unbound" and result["can_connect"]
    assert result["authorized"] and not result["can_run"]
    assert client.bind("wxid_customer")["state"] == "ready"
    assert not client.snapshot("wxid_customer")["can_run"]
    assert client.authorize_run("wxid_customer")["can_run"]
    call_count = len(service.calls)
    assert client.snapshot("wxid_customer")["can_run"]
    assert not client.snapshot()["can_run"]
    assert len(service.calls) == call_count


def test_prepare_device_is_explicit_stable_and_does_not_activate_or_network(setup):
    client, service, _, _ = setup
    assert not client.snapshot()["device_id"] and not client.directory.exists()
    first = client.prepare_device()
    assert first["device_id"] and not first["authorized"] and not first["can_run"]
    assert client.prepare_device()["device_id"] == first["device_id"]
    assert not service.calls


def test_protection_failure_never_writes_a_plaintext_fallback(setup, monkeypatch):
    client, _, _, protection = setup

    def unavailable(_):
        raise OSError("do not expose private-device-key-path")

    monkeypatch.setattr(protection, "protect", unavailable)
    with pytest.raises(LicenseError) as error:
        client.prepare_device()
    assert error.value.code == "storage_error"
    assert "private-device" not in str(error.value)
    assert not (client.directory / "device.json").exists()
    assert not client.snapshot()["authorized"]


def test_account_change_pauses_and_does_not_resume_when_account_returns(setup):
    client, _, _, _ = setup
    running(client)
    result = client.snapshot("wxid_other")
    assert result["state"] == "account_mismatch" and not result["can_run"]
    assert client.snapshot("wxid_customer")["state"] == "paused"
    assert client.authorize_run("wxid_customer")["can_run"]


def test_expiry_at_exact_boundary_preserves_data_and_requires_manual_resume(setup):
    client, service, clock, _ = setup
    running(client)
    customer_data = client.root / "data/customer.json"
    customer_data.write_text("do not delete", encoding="utf-8")
    clock.advance(3599)
    assert client.snapshot("wxid_customer")["can_run"]
    clock.advance(1)
    result = client.snapshot("wxid_customer")
    assert result["state"] == "expired" and not result["can_connect"] and not result["can_run"]
    result = renew(client, service)
    assert result["authorized"] and result["state"] == "paused" and not result["can_run"]
    assert customer_data.read_text(encoding="utf-8") == "do not delete"
    assert client.authorize_run("wxid_customer")["can_run"]


def test_renewal_after_unobserved_expiry_does_not_resurrect_run_grant(setup):
    client, service, clock, _ = setup
    running(client)
    clock.advance(3700)
    assert not renew(client, service)["can_run"]
    assert not client.snapshot("wxid_customer")["can_run"]


def test_early_renewal_preserves_only_an_existing_run_grant(setup):
    client, service, clock, _ = setup
    started = running(client)
    clock.advance(100)
    renewed = renew(client, service)
    assert renewed["can_run"]
    assert renewed["run_id"] == started["run_id"]
    assert renewed["run_granted_at"] == started["run_granted_at"]
    client.pause()
    clock.advance(100)
    result = renew(client, service)
    assert result["state"] == "paused" and not result["can_run"]
    assert service.calls[-2][1]["body"]["license_id"] == "license-1"


def test_migration_switch_is_bound_to_saved_preview_and_uses_target_revision_history(setup):
    client, service, _, _ = setup
    started = running(client)
    service.revision = 20  # The old trial can have a longer administrative history.
    client.refresh()
    preview = offer_migration(client, service)
    result = client.redeem("WRB-MIGRATION-CODE", preview["preview_id"])
    assert result["license_id"] == "license-migrated" and result["plan"] == "month"
    assert result["valid_until"] == preview["valid_until"] and not result["can_run"]
    state = client.cache.read(result["device_id"])
    assert state["revision"] == 2 and state["run_grant"] is None
    assert client.authorize_run("wxid_migrated")["run_id"] != started["run_id"]


@pytest.mark.parametrize("code,preview_id", [
    ("WRB-MIGRATION-DIFFERENT", "preview-1"),
    ("WRB-MIGRATION-CODE", "another-preview"),
])
def test_migration_code_or_preview_substitution_is_blocked_before_remote_consumption(setup, code, preview_id):
    client, service, _, _ = setup
    running(client)
    offer_migration(client, service)
    count = len(service.calls)
    with pytest.raises(LicenseError) as error:
        client.redeem(code, preview_id)
    assert error.value.code == "preview_required" and len(service.calls) == count
    assert client.snapshot("wxid_customer")["license_id"] == "license-1"


def test_saved_migration_preview_does_not_authorize_a_refresh_to_switch_license(setup):
    client, service, _, _ = setup
    running(client)
    offer_migration(client, service)
    with pytest.raises(LicenseError) as error:
        client.refresh()
    assert error.value.code == "identity_mismatch"
    assert client.snapshot("wxid_customer")["license_id"] == "license-1"


def test_latest_preview_replaces_old_code_confirmation_across_restart(setup):
    client, service, _, protection = setup
    running(client)
    preview = offer_migration(client, service)
    client.preview("WRB-ANOTHER-MIGRATION")
    restarted = LicenseClient(client.root, transport=client.transport, clock=client.clock, protection=protection)
    count = len(service.calls)
    with pytest.raises(LicenseError) as error:
        restarted.redeem("WRB-MIGRATION-CODE", preview["preview_id"])
    assert error.value.code == "preview_required" and len(service.calls) == count
    state = restarted.cache.read(restarted.snapshot()["device_id"])
    assert state["pending_preview"]["code_hash"]
    assert "WRB-ANOTHER-MIGRATION" not in json.dumps(state)


@pytest.mark.parametrize("corruption,error_code", [
    (lambda p: p.update(license_id="different-target"), "identity_mismatch"),
    (lambda p: p.update(valid_until=p["valid_until"] + 1), "migration_mismatch"),
    (lambda p: p.update(plan="year"), "migration_mismatch"),
    (lambda p: p.update(request_nonce="0" * 32), "lease_replayed"),
    (lambda p: p.update(issued_at=p["issued_at"] - 1,
                        lease_until=p["lease_until"] - 1), "lease_rollback"),
])
def test_migration_requires_exact_signed_target_term_and_fresh_response(setup, corruption, error_code):
    client, service, _, protection = setup
    running(client)
    preview = offer_migration(client, service, corruption=corruption)
    restarted = LicenseClient(client.root, transport=client.transport, clock=client.clock, protection=protection)
    with pytest.raises(LicenseError) as error:
        restarted.redeem("WRB-MIGRATION-CODE", preview["preview_id"])
    assert error.value.code == error_code
    result = restarted.snapshot("wxid_customer")
    assert result["license_id"] == "license-1"
    assert restarted.cache.read(result["device_id"])["pending_preview"]["preview_id"] == preview["preview_id"]


def test_pause_and_restart_within_same_second_create_distinct_run_sessions(setup):
    client, _, _, _ = setup
    started = running(client)
    assert len(started["run_id"]) == 32
    stopped = client.pause()
    assert stopped["run_id"] == "" and stopped["run_granted_at"] == 0
    resumed = client.authorize_run("wxid_customer")
    assert resumed["run_granted_at"] == started["run_granted_at"]
    assert resumed["run_id"] != started["run_id"]


def test_repeated_start_while_authorized_keeps_existing_in_flight_session(setup):
    client, _, clock, _ = setup
    started = running(client)
    clock.advance(10)
    repeated = client.authorize_run("wxid_customer")
    assert repeated["can_run"] and repeated["run_id"] == started["run_id"]
    assert repeated["run_granted_at"] == started["run_granted_at"]


def test_network_failure_keeps_valid_lease_but_never_extends_offline_window(setup):
    client, service, clock, _ = setup
    service.plan = "month"
    service.valid_until = clock.server + 30 * 86400
    running(client)
    service.offline = True
    clock.advance(1800)
    with pytest.raises(LicenseError) as error:
        client.refresh()
    assert "server-key" not in str(error.value)
    assert client.snapshot("wxid_customer")["can_run"]
    clock.advance(86400 - 1800)
    result = client.snapshot("wxid_customer")
    assert result["state"] == "offline_expired" and not result["can_run"]
    assert result["remaining_seconds"] > 0
    service.offline = False
    assert not client.refresh()["can_run"]
    assert client.authorize_run("wxid_customer")["can_run"]


def test_trial_never_gets_an_extra_offline_day(setup):
    client, service, clock, _ = setup
    running(client)
    clock.advance(3500)
    result = client.refresh()
    assert result["lease_until"] == result["valid_until"] == service.valid_until
    service.offline = True
    clock.advance(100)
    assert client.snapshot("wxid_customer")["state"] == "expired"


def test_network_latency_does_not_extend_signed_expiry(setup):
    client, service, clock, _ = setup

    def delayed_transport(url, request):
        response = service(url, request)
        clock.advance(3)
        return response

    client.transport = delayed_transport
    result = activate(client)
    assert result["remaining_seconds"] <= service.valid_until - clock.server
    clock.advance(service.valid_until - clock.server)
    assert client.snapshot()["state"] == "expired"


@pytest.mark.parametrize("mutation,code", [
    (lambda p: p.update(request_nonce="0" * 32), "lease_replayed"),
    (lambda p: p.update(device="0" * 64), "invalid_lease"),
    (lambda p: p.update(license_id="other-license"), "identity_mismatch"),
    (lambda p: p.update(revision=0), "lease_rollback"),
    (lambda p: p.update(issued_at=p["issued_at"] - 1), "lease_rollback"),
    (lambda p: p.update(valid_until=p["valid_until"] + 1), "lease_rollback"),
    (lambda p: p.update(lease_until=p["valid_until"] + 1), "invalid_lease"),
])
def test_signed_but_replayed_or_wrong_identity_responses_are_rejected(setup, mutation, code):
    client, service, _, _ = setup
    running(client)
    service.transform = mutation
    with pytest.raises(LicenseError) as error:
        client.refresh()
    assert error.value.code == code
    assert client.snapshot("wxid_customer")["can_run"]


def test_forged_signature_is_rejected_without_overwriting_valid_lease(setup):
    client, service, _, _ = setup
    running(client)
    service.key = Ed25519PrivateKey.generate()
    with pytest.raises(LicenseError) as error:
        client.refresh()
    assert error.value.code == "invalid_lease"
    assert client.snapshot("wxid_customer")["can_run"]


def test_longer_than_24_hour_lease_is_rejected_even_with_valid_server_signature(setup):
    client, service, clock, _ = setup
    service.plan = "month"
    service.valid_until = clock.server + 30 * 86400
    service.transform = lambda p: p.update(lease_until=p["issued_at"] + 86401)
    with pytest.raises(LicenseError) as error:
        activate(client)
    assert error.value.code == "invalid_lease"
    assert not client.snapshot()["authorized"]


def test_protected_files_do_not_expose_raw_device_private_key_or_run_permission(setup):
    client, _, _, protection = setup
    running(client)
    identity = load_device(client.directory, protection)
    raw = identity.key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    device_text = (client.directory / "device.json").read_text(encoding="utf-8")
    state_text = (client.directory / "state.json").read_text(encoding="utf-8")
    assert base64.b64encode(raw).decode("ascii") not in device_text
    assert "run_grant" not in state_text and "wxid_customer" not in state_text
    assert "private_key" not in json.dumps(client.snapshot("wxid_customer"))


def test_corrupt_device_is_not_silently_replaced(setup):
    client, _, _, _ = setup
    running(client)
    device_file = client.directory / "device.json"
    device_file.write_text("corrupt", encoding="utf-8")
    assert client.snapshot()["state"] == "device_error"
    with pytest.raises(LicenseError):
        client.preview("WRB-TEST-ACTIVATION")
    assert device_file.read_text(encoding="utf-8") == "corrupt"


def test_missing_device_with_existing_license_requires_migration(setup):
    client, _, _, _ = setup
    running(client)
    device_file = client.directory / "device.json"
    device_file.unlink()
    assert client.snapshot()["state"] == "device_missing"
    with pytest.raises(LicenseError) as error:
        client.preview("WRB-TEST-ACTIVATION")
    assert error.value.code == "device_missing"
    assert not device_file.exists()


def test_device_failure_preserves_channel_status(setup):
    client, _, _, _ = setup
    running(client)
    (client.directory / "device.json").unlink()
    result = client.snapshot()
    assert result["state"] == "device_missing"
    assert result["configured"]


def test_corrupt_cache_cannot_enable_automation_or_reset_identity(setup):
    client, _, _, _ = setup
    running(client)
    cache_file = client.directory / "state.json"
    cache_file.write_text('{"schema_version":1,"protected":"broken"}', encoding="utf-8")
    result = client.snapshot("wxid_customer")
    assert result["state"] == "cache_error" and not result["can_run"]
    with pytest.raises(LicenseError):
        client.preview("WRB-TEST-ACTIVATION")


def test_clock_rollback_latches_pause_until_online_validation(setup):
    client, _, clock, protection = setup
    running(client)
    clock.advance(60)
    client.snapshot("wxid_customer")
    clock.wall -= 3600
    assert client.snapshot("wxid_customer")["state"] == "clock_error"
    clock.wall = clock.server
    # New clients and corrected clocks alone cannot clear a rollback latch.
    other = LicenseClient(client.root, transport=client.transport, clock=clock, protection=protection)
    assert other.snapshot("wxid_customer")["state"] == "clock_error"
    assert not other.refresh()["can_run"]
    assert other.authorize_run("wxid_customer")["can_run"]


def test_monotonic_time_expires_a_lease_when_wall_clock_stops(setup):
    client, _, clock, _ = setup
    running(client)
    clock.mono += 3600
    clock.server += 3600
    assert client.snapshot("wxid_customer")["state"] == "expired"


def test_monotonic_reset_requires_online_even_when_boot_identifier_is_unchanged(setup):
    client, _, clock, protection = setup
    running(client)
    clock.advance(1800)
    client.snapshot("wxid_customer")
    clock.advance(1800)
    clock.mono = 10
    other = LicenseClient(client.root, transport=client.transport, clock=clock, protection=protection)
    assert other.snapshot("wxid_customer")["state"] == "clock_error"
    assert other.refresh()["state"] == "expired"


def test_restored_cache_after_reboot_and_wall_rollback_cannot_revive_expired_trial(setup):
    client, service, clock, protection = setup
    running(client)
    cache_path = client.directory / "state.json"
    backup = cache_path.read_bytes()
    clock.advance(3600)
    assert client.snapshot("wxid_customer")["state"] == "expired"
    cache_path.write_bytes(backup)
    clock.wall -= 3600
    clock.mono = 10
    clock.boot = "b" * 32
    restarted = LicenseClient(client.root, transport=service, clock=clock, protection=protection)
    result = restarted.snapshot("wxid_customer")
    assert result["state"] == "clock_error" and not result["can_run"]
    assert result["run_id"] == ""
    with pytest.raises(LicenseError) as error:
        restarted.refresh()
    assert error.value.code == "invalid_request"
    clock.wall = clock.server
    assert restarted.refresh()["state"] == "expired"


def test_new_boot_is_detected_even_when_its_uptime_exceeds_cached_uptime(setup):
    client, _, clock, _ = setup
    running(client)
    clock.mono += 10
    clock.wall += 10
    clock.boot = "b" * 32
    result = client.snapshot("wxid_customer")
    assert result["state"] == "clock_error" and not result["can_run"]


def test_boot_change_refreshes_immediately_and_requires_explicit_resume(setup):
    client, service, clock, _ = setup
    running(client)
    calls = len(service.calls)
    clock.advance(10)
    clock.boot = "b" * 32
    clock.mono = 10
    result = client.refresh_if_due()
    assert len(service.calls) == calls + 1
    assert result["authorized"] and not result["can_run"]
    assert client.snapshot("wxid_customer")["state"] == "paused"
    assert client.authorize_run("wxid_customer")["can_run"]


def test_unreadable_boot_identity_fails_closed_without_consuming_codes(setup, monkeypatch):
    client, service, _, _ = setup

    def unavailable():
        raise OSError("private boot environment error")

    monkeypatch.setattr(client, "_boot_reader", unavailable)
    with pytest.raises(LicenseError) as error:
        activate(client)
    assert error.value.code == "boot_identity"
    assert "private boot" not in str(error.value)
    assert not service.calls and not client.snapshot()["authorized"]


def test_lost_boot_identity_latches_pause_and_background_refresh_retries_with_backoff(setup, monkeypatch):
    client, service, clock, _ = setup
    running(client)
    calls = len(service.calls)

    def unavailable():
        raise OSError("unavailable")

    monkeypatch.setattr(client, "_boot_reader", unavailable)
    assert client.refresh_if_due()["state"] == "clock_error"
    assert len(service.calls) == calls
    monkeypatch.setattr(client, "_boot_reader", clock.boot_id)
    assert client.snapshot("wxid_customer")["state"] == "clock_error"
    client.refresh_if_due()
    assert len(service.calls) == calls
    clock.advance(30)
    assert client.refresh_if_due()["authorized"]
    assert len(service.calls) == calls + 1
    assert not client.snapshot("wxid_customer")["can_run"]


def test_background_refresh_is_shared_and_retries_with_backoff(setup):
    client, service, clock, protection = setup
    running(client)
    calls = len(service.calls)
    client.refresh_if_due()
    assert len(service.calls) == calls
    service.offline = True
    clock.advance(1800)
    client.refresh_if_due()
    assert len(service.calls) == calls + 1
    other = LicenseClient(client.root, transport=service, clock=clock, protection=protection)
    other.refresh_if_due()
    assert len(service.calls) == calls + 1
    clock.advance(30)
    other.refresh_if_due()
    assert len(service.calls) == calls + 2
    assert other.snapshot("wxid_customer")["can_run"]


def test_operation_lock_serializes_mutations_but_preserves_cached_runtime_checks(setup):
    client, _, _, _ = setup
    running(client)
    lock = SingleInstanceLock(client.lock_key)
    try:
        assert lock.acquire()
        with pytest.raises(LicenseError) as error:
            client.pause()
        assert error.value.code == "busy"
        assert client.snapshot("wxid_customer")["can_run"]
    finally:
        lock.release()


def test_lock_creation_failure_is_a_safe_denial(setup, monkeypatch):
    client, _, _, _ = setup

    def unavailable(_):
        raise OSError("private install path")

    monkeypatch.setattr(SingleInstanceLock, "acquire", unavailable)
    result = client.snapshot()
    assert result["state"] == "storage_error" and not result["can_run"]
    assert "private install" not in result["message"]


def test_signed_revocation_and_online_migration_denial_pause_immediately(setup):
    client, service, _, _ = setup
    running(client)
    service.status = "revoked"
    service.revision += 1
    assert client.refresh()["state"] == "revoked"
    assert not client.snapshot("wxid_customer")["can_run"]
    service.status = "active"
    service.revision += 1
    assert not client.refresh()["can_run"]
    client.authorize_run("wxid_customer")
    service.error = "license_unavailable"
    with pytest.raises(LicenseError) as error:
        client.refresh()
    assert "private-key" not in str(error.value)
    assert client.snapshot("wxid_customer")["state"] == "license_unavailable"
    assert not client.snapshot("wxid_customer")["can_run"]


def test_online_ownership_denial_while_binding_also_clears_run_permission(setup):
    client, service, _, _ = setup
    running(client)
    service.error = "license_unavailable"
    with pytest.raises(LicenseError):
        client.bind("wxid_customer")
    result = client.snapshot("wxid_customer")
    assert result["state"] == "license_unavailable" and not result["can_run"]


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is a Windows integration")
def test_windows_protection_round_trip_never_returns_plaintext():
    protection = WindowsProtection()
    plaintext = b"only-for-dpapi-unit-test"
    ciphertext = protection.protect(plaintext)
    assert ciphertext != plaintext
    assert plaintext not in ciphertext
    assert protection.unprotect(ciphertext) == plaintext


@pytest.mark.skipif(os.name != "nt", reason="Windows boot identity integration")
def test_windows_boot_identity_is_stable_within_the_current_boot():
    identifier = boot_session_id()
    assert len(identifier) == 32 and int(identifier, 16) > 0
    assert boot_session_id() == identifier
