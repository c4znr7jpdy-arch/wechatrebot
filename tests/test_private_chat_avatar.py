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


def test_private_conversation_and_old_messages_use_current_contact_avatar() -> None:
    api = ApplicationApi.__new__(ApplicationApi)
    api._lifecycle = SimpleNamespace()
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
