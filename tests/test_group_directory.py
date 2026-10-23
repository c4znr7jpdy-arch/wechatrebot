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


def test_directory_without_portraits_keeps_names_and_default_portrait_loading(tmp_path, monkeypatch):
    obj, _, calls = service(tmp_path, monkeypatch)
    enriched = []

    def enrich(account, item, entity, fetch, **_options):
        enriched.append(entity)
        return {**item, "avatar": "cached-avatar"}

    monkeypatch.setattr(obj._portraits, "enrich", enrich)

    result = obj.list(include_portraits=False)

    assert result["items"] == [{"group_id": "test@chatroom", "group_name": "测试群"}]
    assert not result["stale"]
    assert enriched == []
    assert calls == ["/api/manager/groups"]
    assert obj.list()["items"][0]["avatar"] == "cached-avatar"
    assert enriched == ["test@chatroom"]


def test_directory_without_portraits_preserves_fallback_and_account_checks(tmp_path, monkeypatch):
    obj, account, _ = service(tmp_path, monkeypatch)
    original = obj.list(include_portraits=False)
    monkeypatch.setattr(obj._portraits, "enrich", lambda *_args, **_kw: pytest.fail("unexpected portrait load"))

    def failure(*_args, **_kwargs):
        raise directory.GroupDirectoryError("读取失败")

    monkeypatch.setattr(obj, "_request", failure)
    result = obj.list(True, include_portraits=False)
    assert result["stale"]
    assert result["items"] == original["items"]
    with monkeypatch.context() as patch:
        patch.setattr(directory, "probe_bridge_health", lambda **_kw: SimpleNamespace(
            status_available=False, wechat_logged_in=False, wechat_identity="",
        ))
        with pytest.raises(directory.GroupDirectoryUnavailable):
            obj.list(include_portraits=False)
    account[0] = "different-account"
    assert obj.list(include_portraits=False)["items"] == []


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


def test_qrcode_cache_expiry_and_account_isolation(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(directory.time, "monotonic", lambda: now[0])
    obj, account, calls = service(tmp_path, monkeypatch)
    obj.list()

    def request(port, token, identity, path):
        assert obj._lock.acquire(blocking=False)
        obj._lock.release()
        calls.append(path)
        return {"account_id": identity, "items": [{
            "group_id": "test@chatroom", "image_data_url": "data:image/png;base64,aQ==",
        }]}

    monkeypatch.setattr(obj, "_request", request)
    expected = obj.qrcode(account[0], "test@chatroom")
    assert expected["account_id"] == "test-account"
    obj.qrcode(account[0], "test@chatroom")["image_data_url"] = "changed"
    assert obj.qrcode(account[0], "test@chatroom") == expected
    assert len(calls) == 2
    now[0] += 300
    assert obj.qrcode(account[0], "test@chatroom") == expected
    assert len(calls) == 3
    account[0] = "another-account"
    with pytest.raises(directory.GroupDirectoryError, match="账号已变化"):
        obj.qrcode("test-account", "test@chatroom")
    assert not obj._qrcodes
    assert len(calls) == 3


def test_qrcode_requires_verified_account_and_current_group(tmp_path, monkeypatch):
    obj, account, calls = service(tmp_path, monkeypatch)
    for account_id, group_id in [(account[0], "../private"), ("", "test@chatroom"),
                                  (account[0], "unknown@chatroom")]:
        with pytest.raises(directory.GroupDirectoryError):
            obj.qrcode(account_id, group_id)
    assert calls == []
    obj.list()
    obj._qrcodes["test@chatroom"] = (float("inf"), {"image_data_url": "cached"})
    monkeypatch.setattr(directory, "probe_bridge_health", lambda **kw: SimpleNamespace(
        status_available=False, wechat_logged_in=False, wechat_identity="",
    ))
    with pytest.raises(directory.GroupDirectoryUnavailable):
        obj.qrcode(account[0], "test@chatroom")
    assert len(calls) == 1


def test_qrcode_failures_never_cache_or_return_stale_data(tmp_path, monkeypatch):
    obj, account, _ = service(tmp_path, monkeypatch)
    obj.list()
    calls = []

    def request(*args):
        calls.append(args)
        if len(calls) == 1:
            raise directory.GroupDirectoryError("读取失败")
        return {"items": [{"group_id": "another@chatroom", "image_data_url": "data:image/png;base64,aQ=="}]}

    monkeypatch.setattr(obj, "_request", request)
    for _ in range(2):
        with pytest.raises(directory.GroupDirectoryError):
            obj.qrcode(account[0], "test@chatroom")
        assert not obj._qrcodes
    assert len(calls) == 2


def test_late_qrcode_response_cannot_populate_a_new_account_cache(tmp_path, monkeypatch):
    obj, account, _ = service(tmp_path, monkeypatch)
    obj.list()

    def request(*args):
        obj._account = "new-account"
        return {"items": [{"group_id": "test@chatroom", "image_data_url": "data:image/png;base64,aQ=="}]}

    monkeypatch.setattr(obj, "_request", request)
    with pytest.raises(directory.GroupDirectoryError, match="账号已变化"):
        obj.qrcode(account[0], "test@chatroom")
    assert not obj._qrcodes
