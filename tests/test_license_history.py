"""Message history requires a verified login and a matching valid subscription."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wechatrebot_manager import app_shell, runtime_config
from wechatrebot_manager.runtime_status import RuntimeSnapshot
from test_app_shell import _api
from test_license_client import activate, running, setup


def history_api(root, client=None):
    api = _api(root)
    view = SimpleNamespace(runtime=RuntimeSnapshot(True, True, "wxid_customer"))
    view.to_dict = lambda: {"state": "LICENSE_READY", "runtime": {
        key: getattr(view.runtime, key) for key in (
            "wechat_identity", "account_status_available", "connection_ready", "engine_ready")}}
    if client is not None:
        api._lifecycle.licenses = client
    api._lifecycle.refresh = lambda: view
    api._lifecycle.fresh_runtime_snapshot = lambda: view.runtime
    api._history = SimpleNamespace(**{
        name: Mock(side_effect=AssertionError("history must remain inaccessible"))
        for name in ("conversations", "messages", "image_source_id", "export", "delete_conversation", "delete_before")
    })
    api._groups = SimpleNamespace(
        list=Mock(side_effect=AssertionError("unverified live directory lookup")),
        message_image=Mock(side_effect=AssertionError("unverified image lookup")),
    )
    api._window = SimpleNamespace(
        create_file_dialog=Mock(side_effect=AssertionError("unverified export dialog")))
    return api, view


@pytest.mark.parametrize("condition", [
    "unactivated", "unbound", "expired", "revoked", "wrong_account",
    "logged_out", "stopped", "unknown_account", "invalid_cache",
])
def test_history_rejects_access_without_matching_authorization_and_login(setup, condition):
    client, service, clock, _ = setup
    if condition == "unbound":
        activate(client)
    elif condition != "unactivated":
        running(client)
    api, view = history_api(client.root, client)
    if condition == "expired":
        clock.advance(3601)
    elif condition == "revoked":
        service.status = "revoked"
        service.revision += 1
        client.refresh()
    elif condition == "wrong_account":
        view.runtime = RuntimeSnapshot(True, True, "wxid_other")
        # This flag alone does not prove the actual account is licensed.
        assert client.snapshot("wxid_other")["authorized"]
    elif condition == "logged_out":
        view.runtime = RuntimeSnapshot(True, True, "")
    elif condition == "stopped":
        view.runtime = RuntimeSnapshot(False, False, "", False, False, False, False)
    elif condition == "unknown_account":
        view.runtime = RuntimeSnapshot(True, True, "wxid_customer", account_status_available=False)
    elif condition == "invalid_cache":
        client.cache.path.write_text('{"protected":"tampered"}')
    before = len(service.calls)

    results = [
        api.get_conversations({"account_id": "wxid_customer"}),
        api.get_messages("old@chatroom", {"paged": True, "account_id": "wxid_customer"}),
        api.get_messages("old@chatroom"),
        api.get_message_image("old@chatroom", 1, 0, "wxid_customer"),
        api.delete_conversation("old@chatroom", "wxid_customer"),
        api.delete_history_before("2026-09-01", "wxid_customer"),
        api.export_history("csv", "wxid_customer"),
    ]
    for result in results:
        if result["ok"]:
            data = result["data"]
            assert data == [] or data.get("requires_login") is True
    status = api.get_status()["data"]
    assert status["history_account"] == ""
    assert "history_offline" not in status
    for method in vars(api._history).values():
        method.assert_not_called()
    api._groups.list.assert_not_called()
    api._groups.message_image.assert_not_called()
    api._window.create_file_dialog.assert_not_called()
    assert len(service.calls) == before
    if condition in {"unactivated", "expired", "wrong_account", "unbound"}:
        # History authorization must not hide the login needed for trial/binding.
        assert api._history_account_id() == view.runtime.wechat_identity
        assert api.get_license_status()["data"]["current_account"] == view.runtime.wechat_identity


def allow_private_history(api):
    api._history.conversations.side_effect = None
    api._history.conversations.return_value = {
        "items": [{"conversation_id": "friend", "type": "private", "message_count": 1}],
        "summary": {"messages": 1},
    }
    api._history.messages.side_effect = None
    api._history.messages.return_value = [
        {"role": "user", "sender_id": "friend", "content": "local history"}]


def test_valid_paused_subscription_can_read_and_recovers_after_reauthorization(setup):
    client, service, clock, _ = setup
    running(client)
    client.pause()
    assert client.snapshot("wxid_customer")["authorized"]
    assert not client.snapshot("wxid_customer")["can_run"]
    api, _ = history_api(client.root, client)
    allow_private_history(api)

    assert api.get_status()["data"]["history_account"] == "wxid_customer"
    result = api.get_conversations({"kind": "private", "account_id": "wxid_customer"})
    assert result["data"]["items"][0]["conversation_id"] == "friend"
    assert "offline" not in result["data"]
    messages = api.get_messages("friend", {"paged": True, "account_id": "wxid_customer"})
    assert messages["data"]["items"][0]["content"] == "local history"

    clock.advance(3601)
    assert api.get_status()["data"]["history_account"] == ""
    api._history.conversations.reset_mock()
    assert api.get_conversations({"kind": "private"})["data"]["requires_login"]
    api._history.conversations.assert_not_called()
    service.renew()
    client.refresh()
    assert api.get_status()["data"]["history_account"] == "wxid_customer"
    assert api.get_conversations({"kind": "private"})["data"]["items"]
    assert not client.snapshot("wxid_customer")["can_run"]


def test_legacy_history_still_requires_login_but_not_a_subscription(tmp_path):
    api, view = history_api(tmp_path)
    allow_private_history(api)
    assert api._license_client().snapshot()["enabled"] is False
    assert api.get_status()["data"]["history_account"] == "wxid_customer"
    assert api.get_conversations({"kind": "private"})["data"]["items"]
    assert api.get_messages("friend", {"paged": True})["data"]["items"]
    view.runtime = RuntimeSnapshot(False, False, "")
    assert api.get_status()["data"]["history_account"] == ""
    assert api.get_conversations({})["data"]["requires_login"]


@pytest.mark.parametrize("action", ["check_environment", "select_wechat"])
@pytest.mark.parametrize("authorized", [False, True])
def test_environment_responses_include_authoritative_history_access(setup, monkeypatch, action, authorized):
    client, _, _, _ = setup
    if authorized:
        running(client)
    api, view = history_api(client.root, client)
    api._lifecycle.check_environment = lambda: view
    api._lifecycle.refresh = lambda: pytest.fail("reuse the environment check's runtime snapshot")
    monkeypatch.setattr(app_shell, "validate_wechat_executable", lambda _path: SimpleNamespace(
        supported=True, path=client.root / "Weixin.exe", version="4.1.8.27"))
    monkeypatch.setattr(runtime_config, "write_runtime_configs", lambda *_args, **_kwargs: None)
    result = api.check_environment() if action == "check_environment" else api.select_wechat("Weixin.exe")
    assert result["ok"]
    assert result["data"]["history_account"] == ("wxid_customer" if authorized else "")


@pytest.mark.parametrize("operation", ["conversations", "messages", "unpaged_messages", "image"])
@pytest.mark.parametrize("change", ["expired", "logged_out", "wrong_account"])
def test_history_discards_data_when_access_changes_during_read(setup, operation, change):
    client, _, clock, _ = setup
    running(client)
    api, view = history_api(client.root, client)
    allow_private_history(api)
    if operation == "image":
        api._history.image_source_id.side_effect = None
        api._history.image_source_id.return_value = 1
        read = api._groups.message_image
        read.return_value = {"image_data_url": "data:image/png;base64,cHJpdmF0ZQ==", "quality": "original"}
    else:
        read = api._history.conversations if operation == "conversations" else api._history.messages

    def read_and_change_access(*_args, **_kwargs):
        if change == "expired":
            clock.advance(3601)
        elif change == "logged_out":
            view.runtime = RuntimeSnapshot(True, True, "")
        else:
            view.runtime = RuntimeSnapshot(True, True, "wxid_other")
        return read.return_value

    read.side_effect = read_and_change_access
    if operation == "conversations":
        result = api.get_conversations({"kind": "private", "account_id": "wxid_customer"})
    elif operation == "image":
        result = api.get_message_image("friend", 1, 0, "wxid_customer")
    else:
        result = api.get_messages("friend", {"paged": operation == "messages", "account_id": "wxid_customer"})

    read.assert_called_once()
    assert result["ok"] is False
    assert "data" not in result
    assert "local history" not in str(result)
    assert "data:image/png" not in str(result)


@pytest.mark.parametrize("change", ["none", "expired", "logged_out", "wrong_account"])
def test_export_rechecks_authorization_and_login_after_dialog(setup, monkeypatch, tmp_path, change):
    client, _, clock, _ = setup
    running(client)
    api, view = history_api(client.root, client)
    target = tmp_path / "history.csv"

    def choose_file(*_args, **_kwargs):
        if change == "expired":
            clock.advance(3601)
        elif change == "logged_out":
            view.runtime = RuntimeSnapshot(True, True, "")
        elif change == "wrong_account":
            view.runtime = RuntimeSnapshot(True, True, "wxid_other")
        return [str(target)]

    api._window.create_file_dialog.side_effect = choose_file
    api._history.export.side_effect = None
    api._history.export.return_value = target
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(SAVE_DIALOG=1))
    result = api.export_history("csv", "wxid_customer")
    assert result["ok"]
    if change == "none":
        assert result["data"] == {"path": str(target)}
        api._history.export.assert_called_once_with(str(target), format="csv", account_id="wxid_customer")
    else:
        assert result["data"] == {"cancelled": True, "account_changed": True}
        api._history.export.assert_not_called()
