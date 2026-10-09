import ast
import asyncio
import copy
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
PLUGIN_SOURCE = ROOT / "vendor/data/plugins/jiang_schedule/main.py"
BASE_PATCH = ROOT / "patches" / "astrbot-plugin-jiang_schedule.patch"
PERMISSIONS_PATCH = (
    ROOT / "patches" / "astrbot-plugin-jiang_schedule--permissions.patch"
)


@pytest.fixture(scope="module")
def patched_source(tmp_path_factory: pytest.TempPathFactory) -> str:
    if not PLUGIN_SOURCE.is_file():
        pytest.fail("commercial AstrBot plugin source is unavailable")
    root = tmp_path_factory.mktemp("schedule-permissions")
    # The host user profile is itself a Git worktree. Create a nested repository so
    # git apply cannot resolve patch paths against that unrelated parent checkout.
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.copy2(PLUGIN_SOURCE, root / "main.py")
    for patch in (BASE_PATCH, PERMISSIONS_PATCH):
        subprocess.run(
            ["git", "apply", "--check", str(patch)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "apply", str(patch)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    source = (root / "main.py").read_text(encoding="utf-8")
    ast.parse(source)
    assert 'raise RuntimeError("定时任务未能加载' in source
    return source


def _permission_namespace(source: str) -> dict[str, object]:
    tree = ast.parse(source)
    wanted_assignments = {
        "_COMMAND_PREFIX",
        "_RE_ADD",
        "_RE_DEL",
        "_RE_LIST",
        "_RE_BILI_SUB",
        "_RE_BILI_UNSUB",
        "_RE_BILI_LIST",
    }
    wanted_functions = {
        "_can_manage_current_chat",
        "_is_mutation_command",
        "_permission_denied_message",
        "_shared_checkin_denied_message",
    }
    nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & wanted_assignments:
                nodes.append(copy.deepcopy(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in wanted_functions:
                nodes.append(copy.deepcopy(node))
    namespace: dict[str, object] = {
        "re": re,
        "AstrMessageEvent": object,
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), "schedule-permissions", "exec"), namespace)
    return namespace


def _handler_class(source: str, namespace: dict[str, object]):
    tree = ast.parse(source)
    main_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Main"
    )
    schedule = copy.deepcopy(
        next(
            node
            for node in main_class.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "schedule"
        )
    )
    schedule.decorator_list = []
    harness = ast.ClassDef(
        name="ScheduleHarness",
        bases=[],
        keywords=[],
        body=[schedule],
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module([harness], type_ignores=[]))
    exec(compile(module, "schedule-handler", "exec"), namespace)
    return namespace["ScheduleHarness"]


class FakeEvent:
    def __init__(
        self,
        text: str,
        *,
        sender_id: str,
        session_id: str,
        origin: str,
        private: bool,
        admin: bool = False,
        group_id: str = "",
        group=None,
    ) -> None:
        self._text = text
        self._sender_id = sender_id
        self._session_id = session_id
        self.unified_msg_origin = origin
        self._private = private
        self._admin = admin
        self._group_id = group_id
        self.message_obj = SimpleNamespace(group=group)

    def get_message_str(self) -> str:
        return self._text

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_session_id(self) -> str:
        return self._session_id

    def get_group_id(self) -> str:
        return self._group_id

    def is_private_chat(self) -> bool:
        return self._private

    def is_admin(self) -> bool:
        return self._admin

    @staticmethod
    def plain_result(message: str) -> str:
        return message


def _collect(handler) -> list[str]:
    async def run() -> list[str]:
        return [item async for item in handler]

    return asyncio.run(run())


def test_group_mutations_require_reliable_admin_identity(patched_source: str) -> None:
    namespace = _permission_namespace(patched_source)
    allowed = namespace["_can_manage_current_chat"]
    normal = FakeEvent(
        "/定时新闻 8点",
        sender_id="wxid_member",
        session_id="room@chatroom",
        origin="adapter:GroupMessage:room@chatroom",
        private=False,
        group_id="room@chatroom",
    )
    configured_admin = copy.copy(normal)
    configured_admin._admin = True

    assert allowed(normal) is False
    assert allowed(configured_admin) is True


def test_explicit_framework_group_roles_are_scoped_to_same_group(
    patched_source: str,
) -> None:
    allowed = _permission_namespace(patched_source)["_can_manage_current_chat"]
    group = SimpleNamespace(
        group_id="room@chatroom",
        group_owner="wxid_owner",
        group_admins=["wxid_moderator"],
    )
    moderator = FakeEvent(
        "/定时新闻 8点",
        sender_id="wxid_moderator",
        session_id="room@chatroom",
        origin="adapter:GroupMessage:room@chatroom",
        private=False,
        group_id="room@chatroom",
        group=group,
    )
    assert allowed(moderator) is True

    moderator._group_id = "other@chatroom"
    assert allowed(moderator) is False


def test_private_mutations_are_limited_to_sender_session(patched_source: str) -> None:
    namespace = _permission_namespace(patched_source)
    allowed = namespace["_can_manage_current_chat"]
    own_chat = FakeEvent(
        "/定时新闻 8点",
        sender_id="wxid_alice",
        session_id="wxid_alice",
        origin="adapter:FriendMessage:wxid_alice",
        private=True,
    )
    mismatched = copy.copy(own_chat)
    mismatched._session_id = "wxid_bob"

    assert allowed(own_chat) is True
    assert allowed(mismatched) is False
    assert "保护其他聊天" in namespace["_permission_denied_message"](mismatched)


@pytest.mark.parametrize(
    "command",
    [
        "/定时新闻 8点",
        "/定时删除新闻",
        "/订阅动态 12345",
        "/取消订阅动态 12345",
    ],
)
def test_every_write_command_is_classified_as_mutation(
    patched_source: str, command: str
) -> None:
    classify = _permission_namespace(patched_source)["_is_mutation_command"]
    assert classify(command) is True
    assert classify("/定时列表") is False
    assert classify("/订阅列表") is False


def test_denied_group_write_stops_before_storage(patched_source: str) -> None:
    namespace = _permission_namespace(patched_source)
    namespace["_load_tasks"] = lambda: pytest.fail("storage must not be read")
    harness = _handler_class(patched_source, namespace)()
    event = FakeEvent(
        "/定时删除新闻",
        sender_id="wxid_member",
        session_id="room@chatroom",
        origin="adapter:GroupMessage:room@chatroom",
        private=False,
        group_id="room@chatroom",
    )

    result = _collect(harness.schedule(event))

    assert len(result) == 1
    assert "只有机器人管理员" in result[0]
    assert "本次没有修改" not in result[0] or "管理员" in result[0]


def test_private_write_targets_only_its_own_unified_session(
    patched_source: str,
) -> None:
    namespace = _permission_namespace(patched_source)
    existing = [
        {
            "type": "weather",
            "session": "adapter:FriendMessage:wxid_bob",
            "hour": 9,
            "minute": 0,
            "extra": {"city": "成都"},
        }
    ]
    saved: list[list[dict]] = []
    registered: list[dict] = []

    async def register(_context, task):
        registered.append(copy.deepcopy(task))

    namespace.update(
        {
            "_load_tasks": lambda: copy.deepcopy(existing),
            "_save_tasks": lambda tasks: saved.append(copy.deepcopy(tasks)),
            "_register_task": register,
            "_normalize_type": lambda _value: "news",
            "_parse_time": lambda _hour, _minute: (8, 0),
            "_type_label": lambda _value: "热点新闻",
            "_DOW_MAP": {},
            "_DOW_LABEL": {},
        }
    )
    harness_type = _handler_class(patched_source, namespace)
    harness = harness_type()
    harness.context = object()
    event = FakeEvent(
        "/定时新闻 8点",
        sender_id="wxid_alice",
        session_id="wxid_alice",
        origin="adapter:FriendMessage:wxid_alice",
        private=True,
    )

    result = _collect(harness.schedule(event))

    assert "已添加" in result[0]
    assert saved[0][0] == existing[0]
    assert saved[0][1]["session"] == "adapter:FriendMessage:wxid_alice"
    assert registered == [saved[0][1]]


def test_group_admin_delete_preserves_same_type_task_in_other_group(
    patched_source: str,
) -> None:
    namespace = _permission_namespace(patched_source)
    saved: list[list[dict]] = []
    sync_calls: list[object] = []

    async def sync(context):
        sync_calls.append(context)

    namespace.update(
        {
            "_load_tasks": lambda: [
                {
                    "type": "news",
                    "session": "adapter:GroupMessage:room@chatroom",
                    "hour": 8,
                    "minute": 0,
                    "extra": {},
                },
                {
                    "type": "news",
                    "session": "adapter:GroupMessage:other@chatroom",
                    "hour": 9,
                    "minute": 0,
                    "extra": {},
                },
            ],
            "_save_tasks": lambda tasks: saved.append(copy.deepcopy(tasks)),
            "_sync_jobs": sync,
            "_normalize_type": lambda _value: "news",
            "_type_label": lambda _value: "热点新闻",
        }
    )
    harness_type = _handler_class(patched_source, namespace)
    harness = harness_type()
    harness.context = object()
    event = FakeEvent(
        "/定时删除新闻",
        sender_id="wxid_admin",
        session_id="room@chatroom",
        origin="adapter:GroupMessage:room@chatroom",
        private=False,
        admin=True,
        group_id="room@chatroom",
    )

    result = _collect(harness.schedule(event))

    assert "已删除" in result[0]
    assert [task["session"] for task in saved[0]] == [
        "adapter:GroupMessage:other@chatroom"
    ]
    assert sync_calls == [harness.context]


def test_read_only_list_never_discloses_another_chat(patched_source: str) -> None:
    namespace = _permission_namespace(patched_source)
    namespace.update(
        {
            "_load_tasks": lambda: [
                {
                    "type": "news",
                    "session": "adapter:GroupMessage:room@chatroom",
                    "hour": 8,
                    "minute": 0,
                    "extra": {},
                },
                {
                    "type": "weather",
                    "session": "adapter:GroupMessage:other@chatroom",
                    "hour": 9,
                    "minute": 0,
                    "extra": {"city": "不应泄露的城市"},
                },
            ],
            "_type_label": lambda value: {"news": "热点新闻", "weather": "天气预报"}[value],
            "_DOW_LABEL": {},
        }
    )
    harness = _handler_class(patched_source, namespace)()
    event = FakeEvent(
        "/定时列表",
        sender_id="wxid_member",
        session_id="room@chatroom",
        origin="adapter:GroupMessage:room@chatroom",
        private=False,
        group_id="room@chatroom",
    )

    result = _collect(harness.schedule(event))

    assert "热点新闻" in result[0]
    assert "不应泄露的城市" not in result[0]
    assert "联系机器人管理员" in result[0]


def test_global_checkin_slot_cannot_be_overwritten_from_another_chat(
    patched_source: str,
) -> None:
    namespace = _permission_namespace(patched_source)

    class CheckinManager:
        @staticmethod
        def public_config():
            return {
                "schedule": {
                    "enabled": True,
                    "session": "adapter:GroupMessage:other@chatroom",
                }
            }

        @staticmethod
        def update_schedule(_payload):
            pytest.fail("another chat's check-in task must not be overwritten")

    namespace.update(
        {
            "_normalize_type": lambda _value: "checkin",
            "_parse_time": lambda _hour, _minute: (8, 0),
            "_CHECKIN_MANAGER": CheckinManager(),
        }
    )
    harness_type = _handler_class(patched_source, namespace)
    harness = harness_type()
    harness.context = object()
    event = FakeEvent(
        "/定时签到 8点",
        sender_id="wxid_admin",
        session_id="room@chatroom",
        origin="adapter:GroupMessage:room@chatroom",
        private=False,
        admin=True,
        group_id="room@chatroom",
    )

    result = _collect(harness.schedule(event))

    assert "另一个聊天" in result[0]
    assert "本次没有修改" in result[0]


def test_private_user_cannot_use_shared_checkin_accounts(patched_source: str) -> None:
    namespace = _permission_namespace(patched_source)

    class CheckinManager:
        @staticmethod
        def public_config():
            pytest.fail("shared check-in configuration must stay unread")

    namespace.update(
        {
            "_normalize_type": lambda _value: "checkin",
            "_parse_time": lambda _hour, _minute: (8, 0),
            "_CHECKIN_MANAGER": CheckinManager(),
        }
    )
    harness_type = _handler_class(patched_source, namespace)
    harness = harness_type()
    harness.context = object()
    event = FakeEvent(
        "/定时签到 8点",
        sender_id="wxid_alice",
        session_id="wxid_alice",
        origin="adapter:FriendMessage:wxid_alice",
        private=True,
    )

    result = _collect(harness.schedule(event))

    assert "只有机器人管理员" in result[0]
    assert "本次没有修改" in result[0]
