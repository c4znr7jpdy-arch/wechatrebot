import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechatrebot_manager import contact_directory, group_directory
from wechatrebot_manager.app_shell import ApplicationApi


def _contact_service(tmp_path: Path, monkeypatch):
    config = tmp_path / "app/bridge/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"callback_port": 5000, "manager_query_token": "a" * 40}),
        encoding="utf-8",
    )
    account = ["test-account"]
    monkeypatch.setattr(
        group_directory,
        "probe_bridge_health",
        lambda **_kwargs: SimpleNamespace(
            wechat_logged_in=bool(account[0]), wechat_identity=account[0]
        ),
    )
    service = contact_directory.ContactDirectoryService(tmp_path)
    calls: list[str] = []
    return service, account, calls


def test_contacts_are_cached_and_cleared_when_account_changes(
    tmp_path: Path, monkeypatch
) -> None:
    service, account, calls = _contact_service(tmp_path, monkeypatch)

    def request(_port, _token, identity, path):
        calls.append(path)
        user_ids = [part.split("=", 1)[1] for part in path.partition("?")[2].split("&")]
        return {
            "account_id": identity,
            "items": [
                {
                    "user_id": user_id,
                    "name": f"联系人-{user_id}",
                    "avatar": f"https://example.test/{user_id}.jpg",
                }
                for user_id in user_ids
            ],
            "total": len(user_ids),
        }

    monkeypatch.setattr(service, "_request", request)

    first = service.contacts(["wxid_a", "wxid_b", "wxid_a", "room@chatroom"])
    second = service.contacts(["wxid_a"])

    assert sorted(first) == ["wxid_a", "wxid_b"]
    assert second["wxid_a"]["avatar"].endswith("wxid_a.jpg")
    assert calls == ["/api/manager/contacts?id=wxid_a&id=wxid_b"]

    account[0] = "different-account"
    service.contacts(["wxid_a"])
    assert calls[-1] == "/api/manager/contacts?id=wxid_a"


def test_missing_contact_uses_cooldown_before_retry(tmp_path: Path, monkeypatch) -> None:
    service, _, calls = _contact_service(tmp_path, monkeypatch)

    def request(_port, _token, identity, path):
        calls.append(path)
        return {"account_id": identity, "items": [], "total": 0}

    monkeypatch.setattr(service, "_request", request)

    assert service.contacts(["wxid_missing"]) == {}
    assert service.contacts(["wxid_missing"]) == {}
    assert calls == ["/api/manager/contacts?id=wxid_missing"]

    service._contact_retry_at["wxid_missing"] = 0
    assert service.contacts(["wxid_missing"]) == {}
    assert calls == [
        "/api/manager/contacts?id=wxid_missing",
        "/api/manager/contacts?id=wxid_missing",
    ]


def test_private_directory_is_account_scoped_refreshable_and_kept_in_memory(
    tmp_path: Path, monkeypatch
) -> None:
    service, account, calls = _contact_service(tmp_path, monkeypatch)

    def request(_port, _token, identity, path, **_options):
        calls.append(path)
        return {
            "account_id": identity,
            "items": [
                {
                    "user_id": "wxid_friend",
                    "name": "好友备注",
                    "nickname": "微信昵称",
                    "remark": "好友备注",
                    "alias": "friend-account",
                    "avatar": "https://example.test/friend.jpg",
                    "gender": "女",
                    "gender_code": 2,
                    "province": "Zhejiang",
                    "city": "Hangzhou",
                    "country": "CN",
                    "contact_type": "微信好友",
                }
            ],
            "total": 1,
            "truncated": False,
        }

    monkeypatch.setattr(service, "_request", request)

    first = service.list()
    second = service.list()
    refreshed = service.list(True)

    assert first["items"][0]["alias"] == "friend-account"
    assert second["items"] == first["items"]
    assert refreshed["stale"] is False
    assert calls == [
        "/api/manager/contacts?directory=1",
        "/api/manager/contacts?directory=1&refresh=1",
    ]

    account[0] = "different-account"
    service.list(True)
    assert service._contact_account == "different-account"


def test_private_profile_requires_current_directory_selection(
    tmp_path: Path, monkeypatch
) -> None:
    service, _, calls = _contact_service(tmp_path, monkeypatch)

    def request(_port, _token, identity, path, **_options):
        calls.append(path)
        item = {
            "user_id": "wxid_friend",
            "name": "好友",
            "nickname": "微信好友",
            "remark": "好友",
            "alias": "friend-account",
            "avatar": "",
            "gender": "未知",
            "contact_type": "微信好友",
        }
        if path.startswith("/api/manager/contacts/"):
            return {"account_id": identity, "items": [item], "item": item, "total": 1}
        return {"account_id": identity, "items": [item], "total": 1}

    monkeypatch.setattr(service, "_request", request)

    with pytest.raises(group_directory.GroupDirectoryError):
        service.profile("wxid_friend")
    service.list()
    assert service.profile("wxid_friend")["item"]["nickname"] == "微信好友"
    with pytest.raises(group_directory.GroupDirectoryError):
        service.profile("../private")


def test_network_profile_queries_each_time_without_loading_directory(tmp_path, monkeypatch):
    service, _, calls = _contact_service(tmp_path, monkeypatch)

    def request(_port, _token, identity, path):
        calls.append(path)
        item = {"user_id": "wxid_friend", "nickname": f"昵称{len(calls)}",
                "alias": "public_alias", "signature": "最新签名"}
        return {"account_id": identity, "item": item, "items": [item], "total": 1}

    monkeypatch.setattr(service, "_request", request)
    first = service.network_profile("test-account", "wxid_friend")
    second = service.network_profile("test-account", "wxid_friend")
    assert first["item"]["nickname"] == "昵称1"
    assert second["item"]["nickname"] == "昵称2"
    assert second["item"]["signature"] == "最新签名"
    assert calls == ["/api/manager/contacts/wxid_friend"] * 2
    assert service._directory_snapshot is None
    assert [path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.json")] == ["app/bridge/config.json"]


@pytest.mark.parametrize("user_id", ["", "../private", "room@chatroom", "a" * 129, None, {}])
def test_network_profile_rejects_invalid_contact_before_request(tmp_path, monkeypatch, user_id):
    service, _, _ = _contact_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_request", lambda *_args: pytest.fail("unexpected request"))
    with pytest.raises(group_directory.GroupDirectoryError, match="标识"):
        service.network_profile("test-account", user_id)


@pytest.mark.parametrize("expected_account", ["", "old-account", None])
def test_network_profile_rejects_stale_account_before_request(tmp_path, monkeypatch, expected_account):
    service, _, _ = _contact_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_request", lambda *_args: pytest.fail("unexpected request"))
    with pytest.raises(group_directory.GroupDirectoryError, match="账号已变化"):
        service.network_profile(expected_account, "wxid_friend")


@pytest.mark.parametrize("item", [None, {}, {"user_id": "wxid_other"}])
def test_network_profile_rejects_missing_or_mismatched_peer(tmp_path, monkeypatch, item):
    service, _, _ = _contact_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_request", lambda *_args: {"item": item})
    with pytest.raises(group_directory.GroupDirectoryError, match="详情格式"):
        service.network_profile("test-account", "wxid_friend")
    assert not service._contacts


def test_network_profile_never_falls_back_to_cached_profile_on_error(tmp_path, monkeypatch):
    service, _, _ = _contact_service(tmp_path, monkeypatch)
    service._contact_account = "test-account"
    service._contacts["wxid_friend"] = {"user_id": "wxid_friend", "nickname": "旧资料"}

    def request(*_args):
        raise group_directory.GroupDirectoryError("读取超时")

    monkeypatch.setattr(service, "_request", request)
    with pytest.raises(group_directory.GroupDirectoryError, match="超时"):
        service.network_profile("test-account", "wxid_friend")


def test_network_profile_discards_result_if_account_changes_during_query(tmp_path, monkeypatch):
    service, account, _ = _contact_service(tmp_path, monkeypatch)

    def request(*_args):
        account[0] = "other-account"
        service._connection()
        return {"item": {"user_id": "wxid_friend", "nickname": "不可泄露"}}

    monkeypatch.setattr(service, "_request", request)
    with pytest.raises(group_directory.GroupDirectoryError, match="账号已变化"):
        service.network_profile("test-account", "wxid_friend")
    assert not service._contacts


def test_network_profile_api_is_exposed_and_returns_actionable_failure():
    from wechatrebot_manager.app_shell import PUBLIC_API_METHODS

    api = ApplicationApi.__new__(ApplicationApi)
    calls = []

    def query(account, contact):
        calls.append((account, contact))
        if contact == "wxid_failed":
            raise group_directory.GroupDirectoryError("联系人资料暂不可用，请稍后重试。")
        return {"account_id": account, "item": {"user_id": contact}}

    api._contacts = SimpleNamespace(network_profile=query)
    assert "get_contact_network_profile" in PUBLIC_API_METHODS
    assert api.get_contact_network_profile("test-account", "wxid_friend")["data"]["item"]["user_id"] == "wxid_friend"
    failure = api.get_contact_network_profile("test-account", "wxid_failed")
    assert failure["ok"] is False
    assert "暂不可用" in failure["error"]["message"]
    assert calls == [("test-account", "wxid_friend"), ("test-account", "wxid_failed")]


def test_private_conversation_and_old_messages_use_current_contact_avatar() -> None:
    api = ApplicationApi.__new__(ApplicationApi)
    api._lifecycle = SimpleNamespace(
        fresh_runtime_snapshot=lambda: SimpleNamespace(wechat_identity="wxid_bot")
    )
    api._history = SimpleNamespace(
        conversations=lambda **_filters: {
            "items": [
                {
                    "conversation_id": "wxid_friend",
                    "type": "private",
                    "display_name": "好友",
                    "message_count": 1,
                    "last_timestamp": 100,
                    "avatar": "",
                }
            ],
            "summary": {"messages": 1},
        },
        messages=lambda *_args, **_kwargs: (
            {
                "id": 1,
                "role": "user",
                "sender_id": "wxid_friend",
                "sender_name": "好友",
                "wechat_name": "",
                "avatar": "",
                "content": "你好",
                "timestamp": 100,
            },
        ),
    )
    api._contacts = SimpleNamespace(
        contacts=lambda user_ids: {
            user_id: {
                "user_id": user_id,
                "name": "微信好友",
                "avatar": "https://example.test/friend.jpg",
            }
            for user_id in user_ids
        }
    )

    conversations = api.get_conversations({"kind": "private"})
    messages = api.get_messages("wxid_friend")

    assert conversations["data"]["items"][0]["avatar"] == (
        "https://example.test/friend.jpg"
    )
    assert messages["data"][0]["avatar"] == "https://example.test/friend.jpg"
    assert messages["data"][0]["wechat_name"] == "微信好友"


def test_group_messages_use_member_wechat_id_and_profile_fallback() -> None:
    api = ApplicationApi.__new__(ApplicationApi)
    api._lifecycle = SimpleNamespace(fresh_runtime_snapshot=lambda: SimpleNamespace(wechat_identity="wxid_bot"))
    received: dict[str, object] = {}

    def messages(_conversation_id, **filters):
        received.update(filters)
        return ({
            "id": 1,
            "role": "user",
            "sender_id": "wxid_member",
            "sender_name": "成员",
            "wechat_name": "",
            "wechat_id": "",
            "group_nickname": "",
            "avatar": "",
            "content": "你好",
            "timestamp": 100,
        },)

    api._history = SimpleNamespace(messages=messages)
    api._groups = SimpleNamespace(
        members=lambda _group_id: {
            "account_id": "wxid_bot",
            "items": [{
                "user_id": "wxid_member",
                "wechat_id": "member_account",
                "name": "微信昵称",
                "group_nickname": "群昵称",
                "avatar": "https://example.test/member.jpg",
            }]
        }
    )

    result = api.get_messages(
        "room@chatroom",
        {"search": "wxid_member", "start": "2026-09-14", "end": "2026-09-15"},
    )

    message = result["data"][0]
    assert message["wechat_id"] == "member_account"
    assert message["group_nickname"] == "群昵称"
    assert message["avatar"] == "https://example.test/member.jpg"
    assert received["search"] == "wxid_member"
    assert float(received["start_time"]) < float(received["end_time"])


def test_group_member_cache_never_crosses_accounts() -> None:
    api = ApplicationApi.__new__(ApplicationApi)
    account = ["wxid_first"]
    api._lifecycle = SimpleNamespace(fresh_runtime_snapshot=lambda: SimpleNamespace(wechat_identity=account[0]))
    api._history = SimpleNamespace(messages=lambda *_args, **_kwargs: ({"role": "user", "sender_id": "wxid_member"},))
    api._groups = SimpleNamespace(members=lambda _group_id: {"account_id": account[0], "items": [{"user_id": "wxid_member", "wechat_id": account[0] + "_alias"}]})
    assert api.get_messages("room@chatroom")["data"][0]["wechat_id"] == "wxid_first_alias"
    account[0] = "wxid_second"
    assert api.get_messages("room@chatroom")["data"][0]["wechat_id"] == "wxid_second_alias"
    assert list(api._history_group_members) == [("wxid_second", "room@chatroom")]


def test_group_member_directory_for_different_account_is_not_used() -> None:
    api = ApplicationApi.__new__(ApplicationApi)
    api._lifecycle = SimpleNamespace(fresh_runtime_snapshot=lambda: SimpleNamespace(wechat_identity="wxid_current"))
    api._history = SimpleNamespace(messages=lambda *_args, **_kwargs: ({"role": "user", "sender_id": "wxid_member"},))
    api._groups = SimpleNamespace(members=lambda _group_id: {"account_id": "wxid_other", "items": [{"user_id": "wxid_member", "wechat_id": "other_alias"}]})
    assert api.get_messages("room@chatroom")["data"][0]["wechat_id"] == "wxid_member"
    assert not api._history_group_members
