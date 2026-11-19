"""Only fresh account evidence may invalidate an account-scoped history view."""
from io import BytesIO
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from test_app_shell import _api
from wechatrebot_manager import group_directory


def runtime(account="wxid_current", *, available=True):
    return SimpleNamespace(wechat_identity=account, account_status_available=available)


@pytest.mark.parametrize("directory_account", ["", "wxid_previous"])
@pytest.mark.parametrize("phase", ["same", "unknown", "switched", "logout"])
def test_directory_identity_mismatch_uses_fresh_runtime_evidence(
    tmp_path, directory_account, phase,
):
    api = _api(tmp_path)
    snapshots = iter([runtime(), {
        "same": runtime(), "unknown": runtime("", available=False),
        "switched": runtime("wxid_new"), "logout": runtime(""),
    }[phase]])
    api._lifecycle.fresh_runtime_snapshot = lambda: next(snapshots)
    api._groups = SimpleNamespace(list=lambda _refresh: {
        "account_id": directory_account, "stale": True,
        "items": [{"group_id": "untrusted@chatroom"}],
    })
    api._history = SimpleNamespace(conversations=lambda **_kw: pytest.fail("unverified directory"))

    result = api.get_conversations({"kind": "group", "account_id": "wxid_current"})

    if phase in {"same", "unknown"}:
        assert result["ok"] is False
        assert result["error"]["code"] == "11007"
        assert result["error"]["recorded"] is False
        assert "data" not in result
    else:
        assert result["ok"] is True
        assert result["data"]["items"] == []
        assert result["data"]["account_id"] == ("wxid_new" if phase == "switched" else "")
        assert result["data"]["account_changed" if phase == "switched" else "requires_login"] is True
    assert api._lifecycle.events.items == []


def test_bridge_conflict_does_not_report_same_runtime_account_as_changed(tmp_path, monkeypatch):
    api = _api(tmp_path)
    api._lifecycle.fresh_runtime_snapshot = runtime
    config = tmp_path / "app/bridge/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"callback_port": 5000, "manager_query_token": "a" * 40}), encoding="utf-8")
    api._groups = group_directory.GroupDirectoryService(tmp_path)
    api._groups._account = "wxid_current"
    api._groups._snapshot = {"account_id": "wxid_current", "items": [{"group_id": "cached@chatroom"}]}
    monkeypatch.setattr(group_directory, "probe_bridge_health", lambda **_kw: SimpleNamespace(
        status_available=True, wechat_logged_in=True, wechat_identity="wxid_current",
    ))

    def conflict(request, **_kw):
        raise HTTPError(request.full_url, 409, "account unavailable", {}, BytesIO(b"{}"))

    monkeypatch.setattr(group_directory, "build_opener", lambda *_args: SimpleNamespace(open=conflict))
    api._history = SimpleNamespace(conversations=lambda **_kw: pytest.fail("unverified directory"))

    result = api.get_conversations({"kind": "group", "account_id": "wxid_current", "refresh_groups": True})

    assert result["ok"] is False
    assert result["error"]["code"] == "11007"
    assert "data" not in result
    assert api._lifecycle.events.items == []


@pytest.mark.parametrize("available_after", [True, False])
def test_member_enrichment_failure_does_not_invent_account_change(tmp_path, available_after):
    api = _api(tmp_path)
    snapshots = iter([runtime(), runtime("wxid_current" if available_after else "", available=available_after)])
    api._lifecycle.fresh_runtime_snapshot = lambda: next(snapshots)
    api._history = SimpleNamespace(messages=lambda *_args, **_kw: [
        {"id": 1, "role": "user", "content": "retained message", "sender_id": "wxid_sender"},
    ])

    def unavailable(_conversation):
        raise group_directory.GroupDirectoryUnavailable("temporary probe failure")

    api._groups = SimpleNamespace(members=unavailable)
    result = api.get_messages("test@chatroom", {"paged": True, "account_id": "wxid_current"})

    if available_after:
        assert result["ok"] is True
        assert result["data"]["account_id"] == "wxid_current"
        assert result["data"]["items"][0]["content"] == "retained message"
        assert not result["data"].get("account_changed")
    else:
        assert result["ok"] is False
        assert result["error"]["code"] == "11007"
        assert "data" not in result
