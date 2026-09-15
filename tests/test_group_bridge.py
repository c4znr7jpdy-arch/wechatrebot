from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

from aiohttp.test_utils import TestClient, TestServer
import pytest


PROJECT_ROOT = Path(r"E:\Project")
BRIDGE_ROOT = PROJECT_ROOT / "hook_41827_astrbot_bridge"
PATCH_PATHS = tuple(
    Path(__file__).resolve().parents[1] / "patches" / name
    for name in (
        "hook-bridge-group-directory.patch",
        "hook-bridge-message-history-identity.patch",
    )
)


@pytest.fixture(scope="module")
def patched_bridge(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("group-bridge-patch")
    copied_bridge = root / "hook_41827_astrbot_bridge"
    package_dir = copied_bridge / "src" / "hook_bridge"
    shutil.copytree(BRIDGE_ROOT / "src" / "hook_bridge", package_dir)

    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    for patch in PATCH_PATHS:
        applied = subprocess.run(
            ["git", "apply", "--whitespace=error-all", str(patch)],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert applied.returncode == 0, applied.stderr

    package_name = "_isolated_group_bridge"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    assert package_spec is not None and package_spec.loader is not None
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)
    callback_server = importlib.import_module(f"{package_name}.callback_server")
    history_store = importlib.import_module(f"{package_name}.history_store")
    hook_client = importlib.import_module(f"{package_name}.hook_client")
    config = importlib.import_module(f"{package_name}.config")
    event_normalizer = importlib.import_module(f"{package_name}.event_normalizer")
    message_codec = importlib.import_module(f"{package_name}.message_codec")
    onebot_client = importlib.import_module(f"{package_name}.onebot_client")
    state = importlib.import_module(f"{package_name}.state")

    yield {
        "root": copied_bridge,
        "callback_server": callback_server,
        "history_store": history_store,
        "hook_client": hook_client,
        "config": config,
        "event_normalizer": event_normalizer,
        "message_codec": message_codec,
        "onebot_client": onebot_client,
        "state": state,
    }

    for module_name in list(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _run(coro):
    return asyncio.run(coro)


def _headers(
    *, token: str = "local-secret", account: str = "wxid_bot"
) -> dict[str, str]:
    return {
        "X-WeChatReBot-Token": token,
        "X-WeChatReBot-Account": account,
    }


async def _with_client(app, exercise) -> None:
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await exercise(client)
    finally:
        await client.close()


def test_patch_adds_default_token_and_passes_current_hook(
    patched_bridge: dict[str, Any], tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    settings = patched_bridge["config"].Settings.load(config_path)
    assert settings.manager_query_token == ""

    app_path = patched_bridge["root"] / "src" / "hook_bridge" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_callback_app"
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords["manager_hook"] == "self.hook"
    assert keywords["manager_query_token"] == "self.settings.manager_query_token"


def test_group_directory_auth_origin_normalization_and_no_store(
    patched_bridge: dict[str, Any]
) -> None:
    callback_server: ModuleType = patched_bridge["callback_server"]

    class Hook:
        async def get_groups_strict(
            self, *, force_refresh: bool = False
        ) -> list[dict[str, Any]]:
            assert force_refresh is True
            return [
                {
                    "contact": {
                        "userName": {"String": "room@chatroom"},
                        "nickName": {"String": "真实群名"},
                    },
                    "memberCount": {"String": "12"},
                },
                {
                    "wxid": {"String": "second@chatroom"},
                    "nickname": {"String": "成员数未知的群"},
                },
                {
                    "wxid": "third@chatroom",
                    "nickname": "另一种成员数字段",
                    "memberNum": {"String": "8"},
                },
                {
                    "username": "snake@chatroom",
                    "nick_name": "真实微信字段群",
                    "small_head_url": "https://example.invalid/group-avatar",
                },
                {"wxid": "wxid_friend", "nickname": "不是群"},
            ]

    async def status() -> dict[str, str]:
        return {"wechat_identity": "wxid_bot"}

    app = callback_server.create_callback_app(
        asyncio.Queue(),
        1024,
        manager_hook=Hook(),
        manager_query_token="local-secret",
        status_provider=status,
    )

    async def exercise(client: TestClient) -> None:
        missing = await client.get("/api/manager/groups")
        assert missing.status == 401
        assert (await missing.json()) == {
            "ok": False,
            "error": "群聊目录访问凭证无效",
        }
        assert missing.headers["Cache-Control"] == "no-store"

        cross_site = await client.get(
            "/api/manager/groups",
            headers={**_headers(), "Origin": "https://example.invalid"},
        )
        assert cross_site.status == 403
        assert "data" not in await cross_site.json()

        response = await client.get("/api/manager/groups", headers=_headers())
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.json() == {
            "ok": True,
            "data": {
                "account_id": "wxid_bot",
                "items": [
                    {
                        "group_id": "room@chatroom",
                        "name": "真实群名",
                        "member_count": 12,
                        "avatar": "",
                    },
                    {
                        "group_id": "second@chatroom",
                        "name": "成员数未知的群",
                        "member_count": None,
                        "avatar": "",
                    },
                    {
                        "group_id": "third@chatroom",
                        "name": "另一种成员数字段",
                        "member_count": 8,
                        "avatar": "",
                    },
                    {
                        "group_id": "snake@chatroom",
                        "name": "真实微信字段群",
                        "member_count": None,
                        "avatar": "https://example.invalid/group-avatar",
                    },
                ],
                "total": 4,
                "truncated": False,
                "source": "wechat_cache",
            },
        }

    _run(_with_client(app, exercise))


def test_nonempty_unrecognized_group_payload_is_not_reported_as_empty_success(
    patched_bridge: dict[str, Any]
) -> None:
    callback_server: ModuleType = patched_bridge["callback_server"]

    class Hook:
        async def get_groups_strict(
            self, *, force_refresh: bool = False
        ) -> list[dict[str, Any]]:
            assert force_refresh is True
            return [{"unknown_id": "room@chatroom", "unknown_name": "未知结构"}]

    async def status() -> dict[str, str]:
        return {"wechat_identity": "wxid_bot"}

    app = callback_server.create_callback_app(
        asyncio.Queue(),
        1024,
        manager_hook=Hook(),
        manager_query_token="local-secret",
        status_provider=status,
    )

    async def exercise(client: TestClient) -> None:
        response = await client.get("/api/manager/groups", headers=_headers())
        body = await response.json()
        assert response.status == 502
        assert body == {"ok": False, "error": "读取群聊目录失败，请稍后重试"}

    _run(_with_client(app, exercise))


def test_contact_directory_returns_requested_private_avatar(
    patched_bridge: dict[str, Any]
) -> None:
    callback_server: ModuleType = patched_bridge["callback_server"]
    refreshes: list[bool] = []

    class Hook:
        async def get_friends(
            self, *, force_refresh: bool = False
        ) -> list[dict[str, Any]]:
            refreshes.append(force_refresh)
            return [
                {
                    "contact": {
                        "username": "wxid_friend",
                        "nick_name": "微信好友",
                        "remark": {"String": "张三"},
                        "alias": "zhangsan_88",
                        "small_head_url": "https://example.invalid/friend-avatar",
                        "sex": 1,
                        "country": "CN",
                        "province": "Zhejiang",
                        "city": "Hangzhou",
                    }
                },
                {
                    "username": "other@chatroom",
                    "nick_name": "群聊不应返回",
                    "small_head_url": "https://example.invalid/group-avatar",
                },
                {
                    "username": "wxid_other",
                    "nick_name": "未请求联系人",
                    "small_head_url": "https://example.invalid/other-avatar",
                },
            ]

        async def get_contact(self, user_id: str) -> dict[str, Any]:
            assert user_id == "wxid_friend"
            return {
                "userName": user_id,
                "nickName": "微信好友",
                "remark": "张三",
                "alias": "zhangsan_88",
                "smallHeadImgUrl": "https://example.invalid/friend-avatar",
                "sex": 1,
                "country": "CN",
                "province": "Zhejiang",
                "city": "Hangzhou",
                "signature": "周末去爬山",
            }

    async def status() -> dict[str, str]:
        return {"wechat_identity": "wxid_bot"}

    app = callback_server.create_callback_app(
        asyncio.Queue(),
        1024,
        manager_hook=Hook(),
        manager_query_token="local-secret",
        status_provider=status,
    )

    async def exercise(client: TestClient) -> None:
        invalid = await client.get(
            "/api/manager/contacts?id=..%2Fprivate",
            headers=_headers(),
        )
        assert invalid.status == 400

        response = await client.get(
            "/api/manager/contacts?id=wxid_friend",
            headers=_headers(),
        )
        assert response.status == 200
        assert await response.json() == {
            "ok": True,
            "data": {
                "account_id": "wxid_bot",
                "items": [
                    {
                        "user_id": "wxid_friend",
                        "name": "张三",
                        "nickname": "微信好友",
                        "remark": "张三",
                        "alias": "zhangsan_88",
                        "avatar": "https://example.invalid/friend-avatar",
                        "gender": "男",
                        "gender_code": 1,
                        "province": "Zhejiang",
                        "city": "Hangzhou",
                        "country": "CN",
                        "signature": "",
                        "contact_type": "微信好友",
                    }
                ],
                "total": 1,
                "truncated": False,
                "source": "wechat_cache",
            },
        }

        directory = await client.get(
            "/api/manager/contacts?directory=1&refresh=1",
            headers=_headers(),
        )
        assert directory.status == 200
        assert directory.headers["Cache-Control"] == "no-store"
        directory_body = await directory.json()
        assert directory_body["data"]["total"] == 2
        assert {item["user_id"] for item in directory_body["data"]["items"]} == {
            "wxid_friend",
            "wxid_other",
        }
        assert refreshes == [False, True]

        detail = await client.get(
            "/api/manager/contacts/wxid_friend",
            headers=_headers(),
        )
        assert detail.status == 200
        assert detail.headers["Cache-Control"] == "no-store"
        detail_body = await detail.json()
        assert detail_body["data"]["item"]["signature"] == "周末去爬山"
        assert detail_body["data"]["item"]["alias"] == "zhangsan_88"

    _run(_with_client(app, exercise))


def test_private_sender_profile_avatar_is_saved_on_normalized_event(
    patched_bridge: dict[str, Any]
) -> None:
    event = patched_bridge["event_normalizer"].normalize_event(
        {
            "messageType": "私聊消息",
            "account_wxid": "wxid_bot",
            "fromUserName": {"String": "wxid_friend"},
            "toUserName": {"String": "wxid_bot"},
            "sender_profile": {
                "nickName": {"String": "微信好友"},
                "small_head_url": "https://example.invalid/friend-avatar",
            },
            "content": {"String": "你好"},
            "msgType": 1,
            "newMsgId": "private-avatar-1",
        }
    )

    assert event.member_avatar == "https://example.invalid/friend-avatar"


def test_group_identity_and_avatar_are_available_to_plugins(
    patched_bridge: dict[str, Any]
) -> None:
    event = patched_bridge["event_normalizer"].normalize_event(
        {
            "event_type": 2000,
            "account_wxid": "wxid_bot",
            "fromUserName": {"String": "room@chatroom"},
            "toUserName": {"String": "wxid_bot"},
            "content": {"String": "wxid_member:\n你好"},
            "real_content": "你好",
            "member_info": {
                "userName": "wxid_member",
                "nickName": "微信名称",
                "displayName": "群昵称",
                "small_head_url": "https://wx.qlogo.cn/member.jpg",
            },
            "msgType": 1,
            "newMsgId": "group-avatar-1",
        }
    )
    encoded = patched_bridge["message_codec"].encode_inbound(
        event, "wxid_bot", 1
    )

    assert encoded["sender"]["nickname"] == "微信名称"
    assert encoded["sender"]["card"] == "群昵称"
    assert encoded["sender"]["avatar"] == "https://wx.qlogo.cn/member.jpg"
    assert encoded["wx_avatar"] == "https://wx.qlogo.cn/member.jpg"


def test_revoke_callback_is_history_only_and_not_forwarded_to_astrbot(
    patched_bridge: dict[str, Any]
) -> None:
    revoke_xml = (
        '<sysmsg type="revokemsg"><revokemsg><msgid>1</msgid>'
        '<replacemsg>群成员撤回了一条消息</replacemsg>'
        '</revokemsg></sysmsg>'
    )
    event = patched_bridge["event_normalizer"].normalize_event(
        {
            "event_type": 2000,
            "account_wxid": "wxid_bot",
            "fromUserName": {"String": "room@chatroom"},
            "toUserName": {"String": "wxid_bot"},
            "content": {"String": f"wxid_member:\n{revoke_xml}"},
            "real_content": revoke_xml,
            "member_info": {
                "userName": "wxid_member",
                "nickName": "群成员",
            },
            "msgType": 10002,
            "newMsgId": "revoke-1",
        }
    )
    app_source = (
        patched_bridge["root"] / "src" / "hook_bridge" / "app.py"
    ).read_text(encoding="utf-8")

    assert event.kind == "history"
    assert event.content == revoke_xml
    assert 'elif event.kind == "history":' in app_source
    assert "await self._record_inbound_history(event)" in app_source


def test_onebot_member_and_contact_queries_include_avatar(
    patched_bridge: dict[str, Any]
) -> None:
    class Hook:
        async def get_group_members(self, _room_id: str) -> list[dict[str, str]]:
            return [
                {
                    "userName": "wxid_member",
                    "nickName": "微信名称",
                    "displayName": "群昵称",
                    "small_head_url": "https://wx.qlogo.cn/member.jpg",
                }
            ]

        async def get_contact(self, user_id: str) -> dict[str, str]:
            return {
                "userName": user_id,
                "nick_name": "联系人",
                "big_head_url": "https://wx.qlogo.cn/contact.jpg",
            }

    router = patched_bridge["onebot_client"].OneBotActionRouter(
        Hook(),
        SimpleNamespace(),
        patched_bridge["state"].BridgeState(60, 100),
    )
    member = _run(
        router.handle(
            {
                "action": "get_group_member_info",
                "params": {
                    "group_id": "room@chatroom",
                    "user_id": "wxid_member",
                },
            }
        )
    )["data"]
    contact = _run(
        router.handle(
            {"action": "get_user_info", "params": {"user_id": "wxid_member"}}
        )
    )["data"]

    assert member["nickname"] == "微信名称"
    assert member["card"] == "群昵称"
    assert member["avatar"] == "https://wx.qlogo.cn/member.jpg"
    assert contact["nickname"] == "联系人"
    assert contact["avatar"] == "https://wx.qlogo.cn/contact.jpg"


def test_group_member_update_notice_preserves_old_and_new_identity(
    patched_bridge: dict[str, Any]
) -> None:
    app_source = (
        patched_bridge["root"] / "src" / "hook_bridge" / "app.py"
    ).read_text(encoding="utf-8")
    assert "encode_group_member_update_notice(" in app_source
    assert "old_wechat_name=previous.wechat_name" in app_source
    assert "old_group_nickname=previous.group_nickname" in app_source

    event = patched_bridge["event_normalizer"].normalize_event(
        {
            "event_type": 1012,
            "account_wxid": "wxid_bot",
            "data": {
                "roomid": "room@chatroom",
                "memberlist": {
                    "userName": "wxid_member",
                    "nickName": "新微信名称",
                    "displayName": "新群昵称",
                    "small_head_url": "https://wx.qlogo.cn/new.jpg",
                },
            },
        }
    )
    notice = patched_bridge["message_codec"].encode_group_member_update_notice(
        event,
        "wxid_bot",
        old_wechat_name="旧微信名称",
        old_group_nickname="旧群昵称",
    )

    assert notice["notice_type"] == "group_member_update"
    assert notice["wx_old_nickname"] == "旧微信名称"
    assert notice["wx_old_display_name"] == "旧群昵称"
    assert notice["wx_nickname"] == "新微信名称"
    assert notice["wx_display_name"] == "新群昵称"
    assert notice["wx_avatar"] == "https://wx.qlogo.cn/new.jpg"


def test_empty_token_and_account_change_never_return_cached_data(
    patched_bridge: dict[str, Any]
) -> None:
    callback_server: ModuleType = patched_bridge["callback_server"]
    identity = {"value": "wxid_bot"}

    async def status() -> dict[str, str]:
        return {"wechat_identity": identity["value"]}

    class Hook:
        async def get_groups_strict(
            self, *, force_refresh: bool = False
        ) -> list[dict[str, Any]]:
            assert force_refresh is True
            identity["value"] = "wxid_other"
            return [{"wxid": "room@chatroom", "nickname": "不可泄露"}]

    disabled_app = callback_server.create_callback_app(
        asyncio.Queue(), 1024, manager_hook=Hook(), status_provider=status
    )

    async def disabled(client: TestClient) -> None:
        response = await client.get("/api/manager/groups", headers=_headers())
        assert response.status == 503
        assert "data" not in await response.json()

    _run(_with_client(disabled_app, disabled))

    identity["value"] = "wxid_bot"
    app = callback_server.create_callback_app(
        asyncio.Queue(),
        1024,
        manager_hook=Hook(),
        manager_query_token="local-secret",
        status_provider=status,
    )

    async def changed(client: TestClient) -> None:
        response = await client.get("/api/manager/groups", headers=_headers())
        body = await response.json()
        assert response.status == 409
        assert body["ok"] is False
        assert "data" not in body
        assert "不可泄露" not in json.dumps(body, ensure_ascii=False)

    _run(_with_client(app, changed))


def test_member_directory_validates_group_and_caps_real_fields_at_500(
    patched_bridge: dict[str, Any]
) -> None:
    callback_server: ModuleType = patched_bridge["callback_server"]

    class Hook:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_group_members_strict(
            self, room_id: str
        ) -> list[dict[str, Any]]:
            self.calls.append(room_id)
            return [
                {
                    "userName": {"String": f"wxid_{index}"},
                    "nickName": {"String": f"成员{index}"},
                        "displayName": {"String": f"群昵称{index}"},
                        "alias": {"String": f"wechat_{index}"},
                    "owner": True,
                    "small_head_url": "https://sensitive.invalid/avatar",
                }
                for index in range(501)
            ]

    hook = Hook()

    async def status() -> dict[str, str]:
        return {"wechat_identity": "wxid_bot"}

    app = callback_server.create_callback_app(
        asyncio.Queue(),
        1024,
        manager_hook=hook,
        manager_query_token="local-secret",
        status_provider=status,
    )

    async def exercise(client: TestClient) -> None:
        invalid = await client.get(
            "/api/manager/groups/not-a-group/members", headers=_headers()
        )
        assert invalid.status == 400
        assert hook.calls == []

        response = await client.get(
            "/api/manager/groups/room@chatroom/members", headers=_headers()
        )
        body = await response.json()
        assert response.status == 200
        assert body["data"]["account_id"] == "wxid_bot"
        assert body["data"]["total"] == 501
        assert body["data"]["truncated"] is True
        assert len(body["data"]["items"]) == 500
        assert body["data"]["items"][0] == {
            "user_id": "wxid_0",
            "name": "成员0",
            "group_nickname": "群昵称0",
            "wechat_id": "wechat_0",
            "avatar": "https://sensitive.invalid/avatar",
        }
        assert set(body["data"]["items"][0]) == {
            "user_id",
            "name",
            "group_nickname",
            "wechat_id",
            "avatar",
        }
        assert body["data"]["source"] == "wechat_cache"

    _run(_with_client(app, exercise))


def test_strict_hook_reads_reject_malformed_shapes_without_changing_legacy_read(
    patched_bridge: dict[str, Any]
) -> None:
    hook_client: ModuleType = patched_bridge["hook_client"]
    client = object.__new__(hook_client.HookClient)

    async def malformed_groups(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert path == "/api/get_chatroom_list"
        assert payload == {}
        return {"unexpected": []}

    client._read_data = malformed_groups
    assert _run(client.get_groups()) == []
    with pytest.raises(hook_client.HookApiError):
        _run(client.get_groups_strict())

    async def malformed_members(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert path == "/api/get_room_members"
        assert payload == {"room_id": "room@chatroom"}
        return {"newChatroomData": {"chatRoomMember": "not-a-list"}}

    client._read_data = malformed_members
    with pytest.raises(hook_client.HookApiError):
        _run(client.get_group_members_strict("room@chatroom"))


def test_legacy_group_read_populates_empty_hook_cache_with_bounded_retry(
    patched_bridge: dict[str, Any]
) -> None:
    hook_client: ModuleType = patched_bridge["hook_client"]
    client = object.__new__(hook_client.HookClient)
    client._group_auto_refresh_failures = 0
    client._group_auto_refresh_next_at = 0.0
    calls: list[str] = []
    group_reads = 0

    async def read(path: str, payload: dict[str, Any]):
        nonlocal group_reads
        assert payload == {}
        calls.append(path)
        if path == "/api/update_all_friend":
            return {}
        assert path == "/api/get_chatroom_list"
        group_reads += 1
        if group_reads == 1:
            return []
        return [{"userName": "family@chatroom", "nickName": "爱人们"}]

    client._read_data = read

    groups = _run(client.get_groups())

    assert groups == [{"userName": "family@chatroom", "nickName": "爱人们"}]
    assert calls == [
        "/api/get_chatroom_list",
        "/api/update_all_friend",
        "/api/get_chatroom_list",
    ]
    assert client._group_auto_refresh_failures == 0


def test_explicit_contact_refresh_returns_the_complete_refresh_payload(
    patched_bridge: dict[str, Any]
) -> None:
    hook_client: ModuleType = patched_bridge["hook_client"]
    client = object.__new__(hook_client.HookClient)
    client._friend_refresh_attempted = False
    calls: list[str] = []

    async def read(path: str, payload: dict[str, Any]):
        assert payload == {}
        calls.append(path)
        if path == "/api/update_all_friend":
            return {
                "contact_list": [
                    {"userName": {"String": "wxid_friend"}, "nickName": "好友"}
                ]
            }
        raise AssertionError(path)

    client._read_data = read

    assert _run(client.get_friends(force_refresh=True)) == [
        {"userName": {"String": "wxid_friend"}, "nickName": "好友"}
    ]
    assert calls == ["/api/update_all_friend"]


@pytest.mark.parametrize("list_key", ["contactList", "contact_list"])
def test_get_contact_unwraps_single_contact_response(
    patched_bridge: dict[str, Any], list_key: str
) -> None:
    hook_client: ModuleType = patched_bridge["hook_client"]
    client = object.__new__(hook_client.HookClient)
    calls: list[tuple[str, dict[str, Any]]] = []
    contact = {
        "userName": {"String": "wxid_friend"},
        "nickName": {"String": "微信好友"},
        "alias": "friend_88",
    }

    async def read(path: str, payload: dict[str, Any]):
        calls.append((path, payload))
        return {
            "baseResponse": {"ret": 0},
            "contactCount": 1,
            list_key: [contact],
        }

    client._read_data = read

    assert _run(client.get_contact("wxid_friend")) == contact
    assert calls == [("/api/get_contact", {"wxid": "wxid_friend"})]


def test_get_contact_keeps_legacy_direct_record_response(
    patched_bridge: dict[str, Any]
) -> None:
    hook_client: ModuleType = patched_bridge["hook_client"]
    client = object.__new__(hook_client.HookClient)
    contact = {"userName": "wxid_friend", "nickName": "微信好友"}

    async def read(_path: str, _payload: dict[str, Any]):
        return contact

    client._read_data = read

    assert _run(client.get_contact("wxid_friend")) == contact


def test_explicit_group_refresh_is_never_permanently_locked_after_empty_result(
    patched_bridge: dict[str, Any]
) -> None:
    hook_client: ModuleType = patched_bridge["hook_client"]
    client = object.__new__(hook_client.HookClient)
    client._group_auto_refresh_failures = 0
    client._group_auto_refresh_next_at = float("inf")
    refresh_count = 0

    async def read(path: str, payload: dict[str, Any]):
        nonlocal refresh_count
        assert payload == {}
        if path == "/api/update_all_friend":
            refresh_count += 1
            return {}
        assert path == "/api/get_chatroom_list"
        if refresh_count < 2:
            return []
        return [{"userName": "family@chatroom", "nickName": "爱人们"}]

    client._read_data = read

    assert _run(client.get_groups_strict(force_refresh=True)) == []
    assert _run(client.get_groups_strict(force_refresh=True)) == [
        {"userName": "family@chatroom", "nickName": "爱人们"}
    ]
    assert refresh_count == 2


def test_group_refresh_uses_chatrooms_returned_directly_by_contact_refresh(
    patched_bridge: dict[str, Any]
) -> None:
    hook_client: ModuleType = patched_bridge["hook_client"]
    client = object.__new__(hook_client.HookClient)
    client._group_auto_refresh_failures = 0
    client._group_auto_refresh_next_at = 0.0

    async def read(path: str, payload: dict[str, Any]):
        assert payload == {}
        if path == "/api/update_all_friend":
            return {
                "contact_list": [
                    {"username": {"String": "family@chatroom"}, "nick_name": "爱人们"},
                    {"userName": {"String": "wxid_friend"}, "nickName": "好友"},
                ]
            }
        assert path == "/api/get_chatroom_list"
        return []

    client._read_data = read

    assert _run(client.get_groups_strict(force_refresh=True)) == [
        {"username": {"String": "family@chatroom"}, "nick_name": "爱人们"}
    ]


def test_history_migration_keeps_legacy_rows_unassigned_and_records_account(
    patched_bridge: dict[str, Any], tmp_path: Path
) -> None:
    history_store: ModuleType = patched_bridge["history_store"]
    database = tmp_path / "legacy-message-history.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                group_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                timestamp REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                dedup_key TEXT NOT NULL UNIQUE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO messages (
                session_id, role, content, sender_id, sender_name,
                group_id, platform, timestamp, metadata, dedup_key
            ) VALUES (?, 'user', ?, ?, ?, ?, 'hook_bridge', ?, '{}', ?)
            """,
            (
                "hook_bridge:GroupMessage:room@chatroom",
                "旧消息",
                "wxid_legacy",
                "旧成员",
                "room@chatroom",
                100.0,
                "in:legacy",
            ),
        )

    store = history_store.MessageHistoryStore(database)
    event = SimpleNamespace(
        account_id="wxid_current_bot",
        conversation_id="room@chatroom",
        message_id="new-message",
        group_name="客户群",
        group_nickname="群昵称",
        wechat_name="微信昵称",
        sender_name="成员",
        sender_id="wxid_member",
        content="新消息",
        create_time=101,
    )
    assert store.record_inbound(event) is True

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT account_id, content FROM messages ORDER BY id"
        ).fetchall()
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(messages)").fetchall()
        }
    assert rows == [("", "旧消息"), ("wxid_current_bot", "新消息")]
    assert "idx_bridge_history_account_group" in indexes


@pytest.mark.parametrize(
    ("case_id", "message_type", "content", "expected"),
    (
        ("emoji", 47, '<msg><emoji md5="abc" /></msg>', "[表情]"),
        ("image", 3, '<msg><img cdnmidimgurl="abc" /></msg>', "[图片]"),
        (
            "file",
            49,
            '<msg><appmsg><title>报价单.pdf</title><type>6</type></appmsg></msg>',
            "[文件] 报价单.pdf",
        ),
        (
            "link",
            49,
            '<msg><appmsg><title>项目说明</title><type>5</type></appmsg></msg>',
            "[链接卡片] 项目说明",
        ),
        (
            "records",
            49,
            '<msg><appmsg><title>聊天记录</title><type>19</type><recorditem /></appmsg></msg>',
            "[聊天记录] 聊天记录",
        ),
        (
            "quote",
            49,
            '<msg><appmsg><title><![CDATA[收到，我来处理]]></title><type>57</type>'
            '<refermsg><type>1</type><displayname>张三</displayname>'
            '<content><![CDATA[周六算你的]]></content></refermsg></appmsg></msg>',
            "收到，我来处理",
        ),
        (
            "unknown-card",
            49,
            '<msg><appmsg><title>活动邀请</title><type>2001</type></appmsg></msg>',
            "[卡片消息] 活动邀请",
        ),
        (
            "revoke",
            10002,
            '<sysmsg type="revokemsg"><revokemsg><msgid>1</msgid>'
            '<replacemsg><![CDATA[“张三”撤回了一条消息]]></replacemsg>'
            '</revokemsg></sysmsg>',
            "“张三”撤回了一条消息",
        ),
        ("unknown-xml", 1, '<msg><unknown value="1" /></msg>', "[特殊消息]"),
    ),
)
def test_inbound_structured_history_stores_a_summary_instead_of_xml(
    patched_bridge: dict[str, Any],
    tmp_path: Path,
    case_id: str,
    message_type: int,
    content: str,
    expected: str,
) -> None:
    history_store: ModuleType = patched_bridge["history_store"]
    database = tmp_path / f"{case_id}-message-history.db"
    store = history_store.MessageHistoryStore(database)
    event = SimpleNamespace(
        account_id="wxid_bot",
        conversation_id="room@chatroom",
        message_id=f"{case_id}-message",
        group_name="结构化消息测试群",
        group_nickname="群成员",
        wechat_name="微信成员",
        member_avatar="",
        sender_name="群成员",
        sender_id="wxid_member",
        message_type=message_type,
        content=content,
        create_time=100,
    )

    assert store.record_inbound(event) is True

    with sqlite3.connect(database) as connection:
        content, metadata_json = connection.execute(
            "SELECT content, metadata FROM messages WHERE dedup_key = ?",
            (f"in:wxid_bot:{case_id}-message",),
        ).fetchone()
    assert content == expected
    assert "<" not in content and ">" not in content
    metadata = json.loads(metadata_json)
    if case_id == "quote":
        assert metadata["message_kind"] == "quote"
        assert metadata["quote_sender"] == "张三"
        assert metadata["quote_content"] == "周六算你的"
    elif case_id == "revoke":
        assert metadata["message_kind"] == "revoke"
