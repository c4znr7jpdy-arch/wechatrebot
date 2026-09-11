from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
PLUGIN = Path("E:/Project/data/plugins/astrbot_plugin_qq_group_daily_analysis")
BASE_PATCH = ROOT / "patches/astrbot-plugin-astrbot_plugin_qq_group_daily_analysis.patch"
SOURCE_PATCH = (
    ROOT
    / "patches/astrbot-plugin-astrbot_plugin_qq_group_daily_analysis--source.patch"
)


def _patched_plugin(tmp_path: Path) -> Path:
    if not PLUGIN.is_dir():
        pytest.skip("Local group analysis plugin source checkout is unavailable")

    files: set[str] = set()
    for patch in (BASE_PATCH, SOURCE_PATCH):
        files.update(
            re.findall(
                r"^--- a/(.+)$", patch.read_text(encoding="utf-8"), re.MULTILINE
            )
        )
    for relative in files:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PLUGIN / relative, target)

    subprocess.run(
        ["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True
    )
    # The extension patch is intentionally based on the already-commercialized
    # source. This test also locks down the required build order.
    for patch in (BASE_PATCH, SOURCE_PATCH):
        subprocess.run(
            ["git", "apply", "--check", "--whitespace=error-all", str(patch)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "apply", "--whitespace=error-all", str(patch)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
    return tmp_path


def _method(source: str, name: str, namespace: dict[str, Any]):
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    node.decorator_list = []
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "group-analysis", "exec"),
        namespace,
    )
    return namespace[name]


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass

    def debug(self, *_args, **_kwargs) -> None:
        pass


@dataclass(frozen=True)
class _Content:
    type: str
    text: str = ""
    url: str = ""
    at_user_id: str = ""


class _ContentType:
    TEXT = "text"
    IMAGE = "image"
    AT = "at"


@dataclass(frozen=True)
class _Message:
    message_id: str
    sender_id: str
    sender_name: str
    group_id: str
    text_content: str
    contents: tuple[_Content, ...]
    timestamp: int
    platform: str
    reply_to_id: str | None = None
    sender_card: str | None = None


def _adapter_methods(source: str):
    scope = {
        "Any": Any,
        "asyncio": asyncio,
        "datetime": datetime,
        "json": json,
        "timedelta": timedelta,
        "timezone": timezone,
        "Path": Path,
        "replace": replace,
        "sqlite3": sqlite3,
        "logger": _Logger(),
        "MessageContent": _Content,
        "MessageContentType": _ContentType,
        "UnifiedMessage": _Message,
    }
    read = _method(source, "_read_bridge_history", scope)
    fetch = _method(source, "_fetch_from_bridge_history", scope)
    current_account = _method(source, "_current_wechat_account", scope)
    return read, fetch, current_account


def test_group_analysis_patch_uses_durable_bridge_history_without_self_learning(
    tmp_path: Path,
) -> None:
    patched = _patched_plugin(tmp_path)
    adapter = (
        patched / "src/infrastructure/platform/adapters/onebot_adapter.py"
    ).read_text(encoding="utf-8")
    assert "self_learning_data" not in adapter
    assert "E:/Project" not in adapter
    assert "_fetch_from_bridge_history" in adapter
    assert '"message-history.db"' in adapter
    assert "mode=ro" in adapter
    assert "asyncio.to_thread" in adapter


def test_bridge_history_is_group_scoped_filtered_ordered_and_read_only(
    tmp_path: Path,
) -> None:
    patched = _patched_plugin(tmp_path)
    source = (
        patched / "src/infrastructure/platform/adapters/onebot_adapter.py"
    ).read_text(encoding="utf-8")
    read, fetch, current_account = _adapter_methods(source)
    database = tmp_path / "message-history.db"
    now = int(datetime.now(timezone.utc).timestamp())
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                group_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1, "wxid_bot", "user", "过期", "wxid_old", "旧成员", "123@chatroom", now - 172800, "{}"),
                (2, "wxid_bot", "user", "第一条", "wxid_a", "wxid_a", "123@chatroom", now - 120, '{"avatar":"https://wx.qlogo.cn/a.jpg"}'),
                (3, "wxid_bot", "assistant", "机器人回复", "wxid_bot", "机器人", "123@chatroom", now - 90, "{}"),
                (4, "wxid_bot", "user", "自己发的", "wxid_bot", "机器人", "123@chatroom", now - 80, "{}"),
                (5, "wxid_bot", "user", "其他群", "wxid_x", "其他人", "999@chatroom", now - 70, "{}"),
                (6, "wxid_bot", "user", "   ", "wxid_blank", "空消息", "123@chatroom", now - 60, "{}"),
                (7, "wxid_bot", "user", "第二条", "wxid_b", "小白", "123@chatroom", now - 30, "{}"),
                (8, "wxid_other_bot", "user", "其他账号的同群", "wxid_c", "其他人", "123@chatroom", now - 20, "{}"),
                (9, "", "user", "旧库未归属", "wxid_legacy", "旧成员", "123@chatroom", now - 10, "{}"),
            ),
        )

    class Adapter:
        bot = SimpleNamespace(self_id="wxid_bot")
        bot_self_ids = ["wxid_stale_bot"]
        _bridge_history_path = database
        _read_bridge_history = read
        _current_wechat_account = current_account

        def __init__(self) -> None:
            self.avatars: dict[str, str] = {}

        def _cache_member_avatar(self, user_id: str, avatar: str | None) -> None:
            if avatar:
                self.avatars[user_id] = avatar

        @staticmethod
        def _is_placeholder_name(name: str, user_id: str) -> bool:
            return not name or name == user_id or name.startswith("wxid_")

        async def _build_nickname_mapping(self, _group_id: str) -> dict[str, str]:
            return {"wxid_a": "群主群昵称", "wxid_b": "小白群昵称"}

    adapter = Adapter()
    messages = asyncio.run(
        fetch(
            adapter,
            "123@chatroom",
            days=1,
            max_count=2,
            before_id=None,
            since_ts=None,
        )
    )

    assert [message.text_content for message in messages] == ["第一条", "第二条"]
    assert [message.sender_name for message in messages] == ["群主群昵称", "小白"]
    assert [message.sender_card for message in messages] == [
        "群主群昵称",
        "小白群昵称",
    ]
    assert adapter.avatars == {"wxid_a": "https://wx.qlogo.cn/a.jpg"}
    assert all(message.platform == "wechat" for message in messages)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 9

    older = asyncio.run(
        fetch(
            adapter,
            "123@chatroom",
            days=1,
            max_count=10,
            before_id="7",
            since_ts=None,
        )
    )
    assert [message.text_content for message in older] == ["第一条"]


def test_history_query_fails_closed_when_current_account_is_ambiguous(
    tmp_path: Path,
) -> None:
    patched = _patched_plugin(tmp_path)
    source = (
        patched / "src/infrastructure/platform/adapters/onebot_adapter.py"
    ).read_text(encoding="utf-8")
    read, fetch, current_account = _adapter_methods(source)

    class Adapter:
        bot = SimpleNamespace()
        bot_self_ids = ["wxid_old", "wxid_new"]
        _bridge_history_path = tmp_path / "must-not-be-read.db"
        _read_bridge_history = read
        _current_wechat_account = current_account

    assert (
        asyncio.run(
            fetch(
                Adapter(),
                "123@chatroom",
                days=1,
                max_count=20,
                before_id=None,
                since_ts=None,
            )
        )
        == []
    )


def test_bridge_history_without_avatar_metadata_remains_readable(
    tmp_path: Path,
) -> None:
    patched = _patched_plugin(tmp_path)
    source = (
        patched / "src/infrastructure/platform/adapters/onebot_adapter.py"
    ).read_text(encoding="utf-8")
    read, _, _ = _adapter_methods(source)
    database = tmp_path / "legacy-message-history.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                group_id TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "wxid_bot",
                "user",
                "旧记录",
                "wxid_member",
                "旧成员",
                "123@chatroom",
                100.0,
            ),
        )

    class Adapter:
        bot_self_ids: list[str] = []

        @staticmethod
        def _cache_member_avatar(_user_id: str, _avatar: str | None) -> None:
            raise AssertionError("legacy rows have no avatar metadata")

    messages = read(
        Adapter(),
        database,
        "wxid_bot",
        "123@chatroom",
        0,
        10,
        None,
    )

    assert [message.sender_name for message in messages] == ["旧成员"]


def test_bridge_history_path_is_derived_from_commercial_layout(
    tmp_path: Path,
) -> None:
    patched = _patched_plugin(tmp_path)
    source = (
        patched / "src/infrastructure/platform/adapters/onebot_adapter.py"
    ).read_text(encoding="utf-8")
    fake_module = (
        tmp_path
        / "install/app/astrbot/data/plugins/group/src/infrastructure/platform/adapters/onebot_adapter.py"
    )
    locate = _method(
        source,
        "_locate_bridge_history",
        {"Path": Path, "__file__": str(fake_module)},
    )

    assert locate() == tmp_path / "install/app/bridge/var/message-history.db"
