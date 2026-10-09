"""Manager API authorization boundaries without launching processes or WeChat."""

import threading
from types import SimpleNamespace

import pytest

from wechatrebot_manager import app_shell
from wechatrebot_manager.licensing import api as licensing_api
from wechatrebot_manager.licensing.contact import create_contact
from wechatrebot_manager.licensing.contact_client import ContactClient
from wechatrebot_manager.licensing.protocol import LicenseError
from wechatrebot_manager.lifecycle import LifecycleController
from wechatrebot_manager.runtime_status import RuntimeSnapshot

from test_license_client import activate, setup
from test_lifecycle import _environment


def make_api(client, *, account=""):
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._install_root = client.root
    api._mandatory_update = False
    runtime = SimpleNamespace(wechat_identity=account, account_status_available=True)
    api._lifecycle = SimpleNamespace(licenses=client, refresh=lambda: SimpleNamespace(runtime=runtime))
    actions = []

    def queue(action, _message, _error_code="19000"):
        actions.append(action)
        return api._ok({"accepted": True})

    api._queue_robot_action = queue
    return api, runtime, actions


def test_public_api_exposes_subscription_actions_without_raw_credential_helpers():
    assert {
        "get_license_status", "prepare_license_device", "preview_activation_code",
        "redeem_activation_code", "refresh_license", "connect_license_wechat",
        "bind_license_account", "get_license_contact",
    } <= set(app_shell.PUBLIC_API_METHODS)
    assert not {"_license_client", "_check_license_start", "authorize_run"} & set(app_shell.PUBLIC_API_METHODS)
    assert "open_license_group" not in app_shell.PUBLIC_API_METHODS


def test_status_reads_actual_account_without_license_network_or_device_creation(setup):
    client, service, _, _ = setup
    api, _, actions = make_api(client, account="wxid_actual")
    result = api.get_license_status()
    assert result["ok"] and result["data"]["current_account"] == "wxid_actual"
    assert result["data"]["state"] == "unactivated"
    assert not service.calls and not actions and not client.directory.exists()


@pytest.mark.parametrize("expected", ["wxid_previous", "", None, 123])
def test_account_changed_since_confirmation_is_rejected_before_remote_binding(setup, expected):
    client, service, _, _ = setup
    activate(client)
    api, _, actions = make_api(client, account="wxid_current")
    calls = len(service.calls)
    result = api.bind_license_account(expected)
    assert not result["ok"] and result["error"]["code"] == "LIC-account_changed"
    assert len(service.calls) == calls and not actions
    assert client.snapshot()["account"] == ""


def test_unavailable_actual_wechat_identity_cannot_be_replaced_with_ui_input(setup):
    client, service, _, _ = setup
    activate(client)
    api, runtime, actions = make_api(client, account="wxid_cached")
    runtime.account_status_available = False
    calls = len(service.calls)
    result = api.bind_license_account("wxid_cached")
    assert result["error"]["code"] == "LIC-account_required"
    assert len(service.calls) == calls and not actions
    assert api.get_license_status()["data"]["current_account"] == ""


def test_binding_uses_confirmed_fresh_account_but_never_starts_automation(setup):
    client, service, _, _ = setup
    activate(client)
    api, _, actions = make_api(client, account="wxid_current")
    result = api.bind_license_account("wxid_current")
    assert result["ok"] and result["data"]["account"] == "wxid_current"
    assert service.calls[-1][1]["body"]["account"] == "wxid_current"
    assert not result["data"]["can_run"] and not actions


def test_redemption_and_renewal_leave_start_as_an_explicit_user_action(setup):
    client, service, clock, _ = setup
    api, runtime, actions = make_api(client)
    preview = api.preview_activation_code("WRB-TEST-ACTIVATION")
    result = api.redeem_activation_code("WRB-TEST-ACTIVATION", preview["data"]["preview_id"])
    assert result["ok"] and result["data"]["state"] == "active_unbound"
    assert not result["data"]["can_run"] and not actions
    assert api.connect_license_wechat()["ok"] and actions == ["connect_wechat"]
    runtime.wechat_identity = "wxid_current"
    assert api.bind_license_account("wxid_current")["ok"]
    client.authorize_run("wxid_current")
    clock.advance(3600)
    assert api.get_license_status()["data"]["state"] == "expired"
    service.renew()
    preview = api.preview_activation_code("WRB-TEST-RENEWAL")
    result = api.redeem_activation_code("WRB-TEST-RENEWAL", preview["data"]["preview_id"])
    assert result["ok"] and not result["data"]["can_run"]
    assert actions == ["connect_wechat"]
    assert api.start_robot()["ok"] and actions == ["connect_wechat", "start"]


def test_missing_service_configuration_cannot_connect_wechat(setup):
    client, service, _, _ = setup
    config = client.root / "config/licensing.json"
    config.write_text('{"schema_version":1,"enabled":true}', encoding="utf-8")
    api, _, actions = make_api(client)
    result = api.connect_license_wechat()
    assert not result["ok"] and result["error"]["code"] == "LIC-configuration_pending"
    assert not service.calls and not actions and not client.directory.exists()


@pytest.mark.parametrize("stage", ["unactivated", "unbound", "other_account", "expired"])
def test_calling_start_api_directly_cannot_bypass_authorization(setup, stage):
    client, _, clock, _ = setup
    if stage != "unactivated":
        activate(client)
    if stage in {"other_account", "expired"}:
        client.bind("wxid_bound")
    if stage == "expired":
        clock.advance(3600)
    api, _, actions = make_api(client, account="wxid_actual" if stage == "other_account" else "wxid_bound")
    result = api.start_robot()
    assert not result["ok"] and result["error"]["code"].startswith("LIC-")
    assert not actions


def test_calling_lifecycle_start_directly_still_requires_signed_authorization(setup):
    client, service, _, _ = setup
    controller = LifecycleController(
        client.root, license_client=client,
        environment_checker=lambda _: _environment(client.root),
        environment_saver=lambda *_: None, preflight=lambda _: (), process_probe=lambda: (),
        snapshotter=lambda: RuntimeSnapshot(False, True, "wxid_current", account_status_available=True),
        supervisor_factory=lambda *_: pytest.fail("must not construct a process supervisor"),
    )
    with pytest.raises(LicenseError):
        controller.start()
    assert not service.calls


def test_contact_api_reads_anonymous_data_without_license_or_runtime_side_effects(setup, monkeypatch):
    client, service, clock, _ = setup
    api, _, actions = make_api(client)
    record = create_contact(b"\x89PNG\r\n\x1a\nqr", "WXRebot", clock.server + 3600)
    calls = []

    def fetch(url):
        calls.append(url)
        return {"ok": True, "data": record, "server_time": clock.server}

    api._license_contacts = ContactClient(client.root, clock=clock, transport=fetch)
    monkeypatch.setattr(api, "_license_client", lambda: pytest.fail("contact must not use licensing identity"))
    monkeypatch.setattr(api._lifecycle, "refresh", lambda: pytest.fail("contact must not probe WeChat"))
    configuration = (client.root / "config/licensing.json").read_bytes()
    result = api.get_license_contact()
    assert result["ok"] and result["data"]["source"] == "remote"
    assert result["data"]["group_name"] == "WXRebot"
    assert api.get_license_contact()["data"] == result["data"] and len(calls) == 1
    assert api.get_license_contact(True)["ok"] and len(calls) == 2
    assert not service.calls and not actions and not client.directory.exists()
    assert (client.root / "config/licensing.json").read_bytes() == configuration


def test_contact_api_lazily_reuses_one_client_and_validates_force(setup, monkeypatch):
    client, _, clock, _ = setup
    api, _, _ = make_api(client)
    constructed = []

    def factory(root):
        constructed.append(root)
        return ContactClient(root, clock=clock, transport=lambda _: {
            "ok": True, "data": None, "server_time": clock.server,
        })

    monkeypatch.setattr(licensing_api, "ContactClient", factory)
    assert api.get_license_contact()["data"]["state"] == "unavailable"
    assert api.get_license_contact(True)["data"]["state"] == "unavailable"
    assert constructed == [client.root]
    assert api.get_license_contact("true")["error"]["code"] == "LIC-invalid_data"
    assert not client.directory.exists()


def test_backend_io_error_does_not_expose_private_data(setup, monkeypatch):
    client, _, _, _ = setup
    api, _, _ = make_api(client)

    def unavailable(_):
        raise OSError("private-client-key-location")

    monkeypatch.setattr(client, "preview", unavailable)
    result = api.preview_activation_code("WRB-TEST-ACTIVATION")
    assert result["error"]["code"] == "LIC-operation_failed"
    assert "private-client" not in result["error"]["message"]


@pytest.mark.parametrize("method", ["start_robot", "connect_license_wechat"])
@pytest.mark.parametrize("unavailable", [False, True])
def test_license_start_and_connect_preserve_pending_task_migration_gate(setup, monkeypatch, method, unavailable):
    client, _, _, _ = setup
    activate(client)
    client.bind("wxid_current")
    api, _, _ = make_api(client, account="wxid_current")
    del api._queue_robot_action
    api._action_lock = threading.Lock()
    def pending(root):
        assert root == client.root and api._action_lock.locked()
        if unavailable:
            raise OSError("private migration path")
        return True
    monkeypatch.setattr(app_shell, "has_pending_migrations", pending)
    monkeypatch.setattr(app_shell.threading, "Thread", lambda **_kwargs: pytest.fail("must not start a service worker"))
    result = getattr(api, method)()
    assert result["ok"] and result["data"]["accepted"] is False
    assert "迁移" in result["message"] and "private" not in result["message"]
    assert not api._action_lock.locked()
    assert not client.snapshot("wxid_current")["can_run"]
