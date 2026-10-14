from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROOM = "test@chatroom"
BOT = "wxid_bot"


@pytest.fixture
def bridge(tmp_path):
    package_dir = tmp_path / "app/bridge/src/hook_bridge"
    shutil.copytree(ROOT / "vendor/hook_41827_astrbot_bridge/src/hook_bridge", package_dir)
    manifest = json.loads((ROOT / "release/source-manifest.json").read_text("utf-8"))
    for entry in manifest["entries"]:
        if entry["origin"] == "commercial" and entry["target"].startswith("app/bridge/src/hook_bridge/"):
            destination = tmp_path / entry["target"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / entry["source"], destination)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    # Use the complete delivery patch order, including dedup and prelogin guards.
    for patch in sorted((ROOT / "patches").glob("hook-bridge-*.patch")):
        applied = subprocess.run(
            ["git", "apply", "--ignore-space-change", "-p2", "--directory=app/bridge", str(patch)],
            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
        )
        assert applied.returncode == 0, applied.stderr
    name = "_self_join_test_bridge"
    spec = importlib.util.spec_from_file_location(name, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    try:
        app_module = importlib.import_module(f"{name}.app")
        models = importlib.import_module(f"{name}.models")
        state_module = importlib.import_module(f"{name}.state")
        app = object.__new__(app_module.BridgeApplication)
        app._active_video_downloads = 0
        app._video_download_generation = 0
        app.state = state_module.BridgeState(600, 100)
        app.state.bot_wxid = BOT
        app._polled_group_members = {}
        app._polled_group_names = {}
        app.onebot = SimpleNamespace(send_event=AsyncMock())
        app.hook = SimpleNamespace(
            get_groups=AsyncMock(return_value=[{"wxid": ROOM, "nickname": "Test"}]),
            get_group_members=AsyncMock(return_value=[]),
        )
        yield app, models
    finally:
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def notice(models, member, kind="group_increase"):
    return models.NormalizedEvent(
        kind=kind, account_id=BOT, conversation_id=ROOM, sender_id=member,
        wechat_name=member, group_name="Test", notice_sub_type="invite",
    )


def members(*ids):
    return [{"userName": id, "nickName": id} for id in ids]


def test_initial_room_callback_does_not_welcome_existing_member(bridge):
    app, models = bridge
    event = notice(models, "wxid_existing")
    assert not app._callback_matches_polled_member_transition(event)
    app.hook.get_group_members.return_value = members(BOT, "wxid_existing")
    assert asyncio.run(app._poll_group_member_changes_once()) == 0
    app.onebot.send_event.assert_not_called()
    assert set(app._polled_group_members[ROOM]) == {BOT, "wxid_existing"}
    assert not app._callback_matches_polled_member_transition(event)

    # A genuine new arrival after initialization is still announced once.
    newcomer = notice(models, "wxid_new")
    assert app._callback_matches_polled_member_transition(newcomer)
    assert asyncio.run(app._forward_group_member_notice(newcomer, source="callback"))
    app.hook.get_group_members.return_value = members(BOT, "wxid_existing", "wxid_new")
    assert asyncio.run(app._poll_group_member_changes_once()) == 0
    assert app.onebot.send_event.call_count == 1
    assert app.onebot.send_event.call_args.args[0]["user_id"] == "wxid_new"


@pytest.mark.parametrize("source", ["callback", "member_poll"])
@pytest.mark.parametrize("use_account_fallback", [False, True])
def test_self_join_never_reaches_notification_plugins(bridge, source, use_account_fallback):
    app, models = bridge
    if use_account_fallback:
        app.state.bot_wxid = ""
    assert not asyncio.run(app._forward_group_member_notice(notice(models, BOT), source=source))
    app.onebot.send_event.assert_not_called()


def test_self_rejoin_callback_discards_old_room_baseline(bridge):
    app, models = bridge
    app._polled_group_members[ROOM] = {BOT: models.GroupMemberIdentity(), "wxid_old": models.GroupMemberIdentity()}
    assert not app._callback_matches_polled_member_transition(notice(models, BOT))
    assert ROOM not in app._polled_group_members
    assert not app._callback_matches_polled_member_transition(notice(models, "wxid_existing"))
    app.hook.get_group_members.return_value = members(BOT, "wxid_existing")
    assert asyncio.run(app._poll_group_member_changes_once()) == 0
    app.onebot.send_event.assert_not_called()


def test_poll_observing_bot_arrival_initializes_without_notices(bridge):
    app, models = bridge
    app._polled_group_members[ROOM] = {"wxid_existing": models.GroupMemberIdentity()}
    app.hook.get_group_members.return_value = members(BOT, "wxid_existing", "wxid_other")
    assert asyncio.run(app._poll_group_member_changes_once()) == 0
    app.onebot.send_event.assert_not_called()
    app.hook.get_group_members.return_value = members(BOT, "wxid_existing", "wxid_other", "wxid_new")
    assert asyncio.run(app._poll_group_member_changes_once()) == 1
    assert app.onebot.send_event.call_args.args[0]["user_id"] == "wxid_new"


def test_real_member_leave_and_rejoin_still_announce_each_transition(bridge):
    app, models = bridge
    app.hook.get_group_members.return_value = members(BOT, "wxid_existing")
    assert asyncio.run(app._poll_group_member_changes_once()) == 0
    for ids in [(BOT, "wxid_existing", "wxid_new"), (BOT, "wxid_existing"), (BOT, "wxid_existing", "wxid_new")]:
        app.hook.get_group_members.return_value = members(*ids)
        assert asyncio.run(app._poll_group_member_changes_once()) == 1
    events = [call.args[0] for call in app.onebot.send_event.call_args_list]
    assert [event["notice_type"] for event in events] == ["group_increase", "group_decrease", "group_increase"]
    assert all(event["user_id"] == "wxid_new" for event in events)
