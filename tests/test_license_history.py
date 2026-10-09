"""Expired subscriptions retain local data access without forging a WeChat login."""
import sys
from types import SimpleNamespace

import pytest

from wechatrebot_manager import app_shell, launcher, runtime_status, uninstaller
from wechatrebot_manager.runtime_status import RuntimeSnapshot
from test_app_shell import _api
from test_license_client import running, setup


@pytest.fixture(autouse=True)
def stopped_installation(monkeypatch):
    monkeypatch.setattr(app_shell, "installation_services_stopped", lambda _root: True)


def offline_api(setup):
    client, service, clock, _ = setup
    running(client)
    clock.advance(3601)
    api = _api(client.root)
    runtime = RuntimeSnapshot(False, False, "", False, False, False, False)
    view = SimpleNamespace(runtime=runtime, can_stop=False)
    view.to_dict = lambda: {"state": "LICENSE_PAUSED", "runtime": {
        key: getattr(view.runtime, key) for key in (
            "wechat_identity", "account_status_available", "connection_ready", "engine_ready")}}
    api._lifecycle.licenses = client
    api._lifecycle.refresh = lambda: view
    queries = []
    def conversations(**filters):
        queries.append(filters)
        return {"items": [{"conversation_id": "old@chatroom", "type": "group", "message_count": 1}], "summary": {}}
    def messages(conversation, **filters):
        queries.append(filters)
        return [{"role": "user", "sender_id": "member", "content": "local history"}]
    api._history = SimpleNamespace(conversations=conversations, messages=messages)
    # The offline path must not touch live group/contact services.
    api._groups = SimpleNamespace(list=lambda *_: pytest.fail("offline live directory lookup"))
    return api, view, queries


def test_expired_offline_history_reads_signed_account_without_enabling_runtime(setup):
    api, _, queries = offline_api(setup)
    client, service, _, _ = setup
    before = len(service.calls)
    result = api.get_conversations({"kind": "all", "account_id": "wxid_customer"})
    assert result["ok"] and result["data"]["offline"] is True
    assert result["data"]["items"][0]["conversation_id"] == "old@chatroom"
    messages = api.get_messages("old@chatroom", {"paged": True, "account_id": "wxid_customer"})
    assert messages["data"]["items"][0]["content"] == "local history"
    assert all(query["account_id"] == "wxid_customer" for query in queries)
    status = api.get_status()["data"]
    assert status["history_offline"] and status["history_account"] == "wxid_customer"
    assert status["runtime"]["wechat_identity"] == ""
    assert api.get_license_status()["data"]["current_account"] == ""
    assert not api.start_robot()["ok"]
    assert not api.bind_license_account("wxid_customer")["ok"]
    assert not client.snapshot("wxid_customer")["can_run"]
    assert len(service.calls) == before


def test_offline_history_cannot_select_another_account_or_delete(setup):
    api, _, queries = offline_api(setup)
    result = api.get_conversations({"account_id": "wxid_other"})
    assert result["data"]["account_changed"]
    result = api.get_messages("chat", {"paged": True, "account_id": "wxid_other"})
    assert result["data"]["account_changed"]
    assert not api.delete_conversation("old@chatroom", "wxid_customer")["ok"]
    assert queries == []


def test_expired_offline_export_rechecks_signed_data_account(setup, monkeypatch, tmp_path):
    api, _, queries = offline_api(setup)
    api._window = SimpleNamespace(create_file_dialog=lambda *a, **kw: [str(tmp_path / "history.csv")])
    api._history.export = lambda selected, **kw: (queries.append(kw), selected)[1]
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(SAVE_DIALOG=1))
    result = api.export_history("csv", "wxid_customer")
    assert result["ok"] and result["data"]["path"].endswith("history.csv")
    assert queries == [{"format": "csv", "account_id": "wxid_customer"}]


def test_offline_export_cancels_if_real_account_changes_during_dialog(setup, monkeypatch, tmp_path):
    api, view, queries = offline_api(setup)
    def choose_file(*_args, **_kwargs):
        view.runtime = RuntimeSnapshot(True, True, "wxid_other", True, True, True, True)
        return [str(tmp_path / "history.csv")]
    api._window = SimpleNamespace(create_file_dialog=choose_file)
    api._history.export = lambda *_args, **_kwargs: pytest.fail("export crossed account boundary")
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(SAVE_DIALOG=1))
    result = api.export_history("csv", "wxid_customer")
    assert result["ok"] and result["data"] == {"cancelled": True, "account_changed": True}
    assert queries == []


@pytest.mark.parametrize("port_error", [False, True])
def test_unowned_bridge_or_unknown_port_state_cannot_enable_offline_history(setup, monkeypatch, port_error):
    api, view, queries = offline_api(setup)
    # The health endpoint is unavailable while the bridge still listens.
    monkeypatch.setattr(runtime_status, "port_accepting", lambda _host, port: port == 5000)
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda **_kw: runtime_status.BridgeHealth())
    view.runtime = runtime_status.runtime_snapshot()
    assert not view.runtime.connection_ready and not view.can_stop
    monkeypatch.setattr(app_shell, "installation_services_stopped", launcher.installation_services_stopped)
    monkeypatch.setattr(launcher, "build_service_specs", lambda _root: (
        SimpleNamespace(argv=("python.exe",), ready_host="127.0.0.1", ready_port=5000),))
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **_kwargs: iter(()))
    def probe_port(_host, _port, *, strict):
        assert strict
        if port_error:
            raise OSError("port state unavailable")
        return True
    monkeypatch.setattr(launcher, "port_accepting", probe_port)
    assert not api.get_conversations({})["ok"]
    assert "history_account" not in api.get_status()["data"]
    assert queries == []


def test_invalid_cache_or_unverified_live_bridge_never_uses_offline_identity(setup):
    api, view, queries = offline_api(setup)
    view.runtime = RuntimeSnapshot(False, True, "", True, False, False, False)
    assert not api.get_conversations({})["ok"]
    view.runtime = RuntimeSnapshot(False, False, "", False, False, False, False)
    setup[0].cache.path.write_text('{"protected":"tampered"}')
    assert not api.get_conversations({})["ok"]
    assert "history_account" not in api.get_status()["data"]
    assert queries == []
