import json
from types import SimpleNamespace

import pytest

from wechatrebot_manager import group_directory as directory
from wechatrebot_manager.runtime_config import write_runtime_configs


def service(tmp_path, monkeypatch):
    config = tmp_path / "app/bridge/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"callback_port": 5000, "manager_query_token": "a" * 40}), encoding="utf-8")
    account = ["test-account"]
    monkeypatch.setattr(directory, "probe_bridge_health", lambda **kw: SimpleNamespace(
        wechat_logged_in=bool(account[0]), wechat_identity=account[0]))
    obj = directory.GroupDirectoryService(tmp_path)
    monkeypatch.setattr(obj._portraits, "enrich", lambda account, item, entity, fetch, **kw: item)
    calls = []
    def request(port, token, identity, path, **_options):
        calls.append(path)
        return {"account_id": identity, "items": [{"group_id": "test@chatroom", "group_name": "测试群"}], "total": 1}
    monkeypatch.setattr(obj, "_request", request)
    return obj, account, calls


def test_directory_manual_refresh_requeries_without_messages(tmp_path, monkeypatch):
    obj, _, calls = service(tmp_path, monkeypatch)
    assert obj.list()["total"] == 1
    obj.list()
    assert len(calls) == 1
    obj.list(True)
    assert calls == ["/api/manager/groups"] * 2


def test_temporary_health_failure_does_not_return_or_destroy_cached_directory(tmp_path, monkeypatch):
    obj, account, _ = service(tmp_path, monkeypatch)
    original = obj.list()
    with monkeypatch.context() as patch:
        patch.setattr(directory, "probe_bridge_health", lambda **kw: SimpleNamespace(
            status_available=False, wechat_logged_in=False, wechat_identity="",
        ))
        with pytest.raises(directory.GroupDirectoryUnavailable):
            obj.list()
        assert obj._snapshot["items"] == original["items"]
    assert obj.list()["items"] == original["items"]
    account[0] = ""
    assert obj.list()["items"] == []


def test_directory_automatically_refreshes_stale_snapshot(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(directory.time, "monotonic", lambda: now[0])
    obj, _, calls = service(tmp_path, monkeypatch)
    groups = [[{"group_id": "old@chatroom", "avatar": "old.jpg"}]]

    def request(_port, _token, identity, path, **_options):
        calls.append(path)
        return {"account_id": identity, "items": groups[0], "total": len(groups[0])}

    monkeypatch.setattr(obj, "_request", request)
    assert [item["group_id"] for item in obj.list()["items"]] == ["old@chatroom"]
    groups[0] = [
        {"group_id": "old@chatroom", "avatar": "old.jpg"},
        {"group_id": "new@chatroom", "avatar": "new.jpg"},
    ]
    now[0] += 14
    assert len(obj.list()["items"]) == 1
    now[0] += 1
    assert obj.list()["items"][1]["avatar"] == "new.jpg"
    assert calls == ["/api/manager/groups"] * 2


def test_failed_automatic_refresh_retries_later_and_manual_refresh_bypasses_delay(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(directory.time, "monotonic", lambda: now[0])
    obj, _, calls = service(tmp_path, monkeypatch)
    assert obj.list()["stale"] is False

    def failure(*_args, **_kwargs):
        calls.append("failed")
        raise directory.GroupDirectoryError("读取失败")

    monkeypatch.setattr(obj, "_request", failure)
    now[0] += 60
    assert obj.list()["stale"] is True
    assert obj.list()["stale"] is True
    assert calls.count("failed") == 1
    assert obj.list(True)["stale"] is True
    assert calls.count("failed") == 2
    now[0] += 15
    assert obj.list()["stale"] is True
    assert calls.count("failed") == 3


def test_failed_refresh_preserves_same_account_snapshot(tmp_path, monkeypatch):
    obj, _, _ = service(tmp_path, monkeypatch)
    original = obj.list()
    def failure(*args, **kwargs):
        raise directory.GroupDirectoryError("读取失败")
    monkeypatch.setattr(obj, "_request", failure)
    result = obj.list(True)
    assert result["stale"] is True
    assert result["items"] == original["items"]
    assert result["updated_at"] == original["updated_at"]


@pytest.mark.parametrize("new_account", ["", "different-account"])
def test_identity_change_never_exposes_previous_groups(tmp_path, monkeypatch, new_account):
    obj, account, _ = service(tmp_path, monkeypatch)
    obj.list()
    account[0] = new_account
    def failure(*args, **kwargs):
        raise directory.GroupDirectoryError("读取失败")
    monkeypatch.setattr(obj, "_request", failure)
    assert obj.list(True)["items"] == []


def test_members_require_valid_current_directory_selection(tmp_path, monkeypatch):
    obj, _, calls = service(tmp_path, monkeypatch)
    with pytest.raises(directory.GroupDirectoryError):
        obj.members("unknown@chatroom")
    with pytest.raises(directory.GroupDirectoryError):
        obj.members("../send_msg")
    assert calls == []
    obj.list()
    obj.members("test@chatroom")
    assert calls[-1] == "/api/manager/groups/test%40chatroom/members"


def test_member_request_releases_directory_lock_before_network_io(tmp_path, monkeypatch):
    obj, _, _ = service(tmp_path, monkeypatch)
    obj.list()

    def request_without_directory_lock(port, token, identity, path):
        acquired = obj._lock.acquire(blocking=False)
        assert acquired is True
        obj._lock.release()
        return {"account_id": identity, "items": [], "total": 0}

    monkeypatch.setattr(obj, "_request", request_without_directory_lock)

    assert obj.members("test@chatroom")["total"] == 0


def test_late_member_request_cannot_clear_a_new_account_snapshot(tmp_path, monkeypatch):
    obj, _, _ = service(tmp_path, monkeypatch)
    obj._account = "new-account"
    obj._snapshot = {"account_id": "new-account", "items": []}

    obj._clear_current_account("old-account", state_locked=False)

    assert obj._account == "new-account"
    assert obj._snapshot == {"account_id": "new-account", "items": []}


def test_query_credential_generated_once_and_preserved(tmp_path):
    write_runtime_configs(tmp_path)
    path = tmp_path / "app/bridge/config.json"
    first = json.loads(path.read_text("utf-8"))["manager_query_token"]
    assert len(first) >= 32
    write_runtime_configs(tmp_path)
    assert json.loads(path.read_text("utf-8"))["manager_query_token"] == first
