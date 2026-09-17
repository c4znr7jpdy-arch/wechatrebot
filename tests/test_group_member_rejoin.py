from __future__ import annotations

import asyncio
import importlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = Path(r"E:\Project\hook_41827_astrbot_bridge")
PATCH = ROOT / "patches" / "hook-bridge-group-member-rejoin.patch"


def test_leave_then_rejoin_is_new_notice_but_callback_poll_duplicates_are_not(tmp_path: Path) -> None:
    package_dir = tmp_path / "hook_41827_astrbot_bridge" / "src" / "hook_bridge"
    shutil.copytree(BRIDGE_SOURCE / "src" / "hook_bridge", package_dir)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    applied = subprocess.run(
        ["git", "apply", "--whitespace=error-all", str(PATCH)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert applied.returncode == 0, applied.stderr

    package_name = "_isolated_group_member_rejoin_bridge"
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    try:
        app_module = importlib.import_module(f"{package_name}.app")
        state_module = importlib.import_module(f"{package_name}.state")
        application = object.__new__(app_module.BridgeApplication)
        application.state = state_module.BridgeState(600, 100)
        application.state.bot_wxid = "wxid_bot"

        class OneBot:
            def __init__(self) -> None:
                self.events: list[dict] = []

            async def send_event(self, event: dict) -> None:
                self.events.append(event)

        application.onebot = OneBot()

        async def forward(kind: str, source: str) -> bool:
            event = SimpleNamespace(
                kind=kind,
                conversation_id="room@chatroom",
                sender_id="wxid_member",
                wechat_name="小妹",
                group_nickname="",
                member_avatar="",
                account_id="wxid_bot",
                operator_id="wxid_owner",
                notice_sub_type="invite" if kind == "group_increase" else "leave",
                create_time=1,
                group_name="测试群",
            )
            return await application._forward_group_member_notice(event, source=source)

        results = asyncio.run(_exercise(forward))
        assert results == [True, False, True, False, True, False]
        assert [event["notice_type"] for event in application.onebot.events] == [
            "group_increase", "group_decrease", "group_increase"
        ]
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                sys.modules.pop(name, None)


async def _exercise(forward):
    return [
        await forward("group_increase", "callback"),
        await forward("group_increase", "member_poll"),
        await forward("group_decrease", "callback"),
        await forward("group_decrease", "member_poll"),
        await forward("group_increase", "callback"),
        await forward("group_increase", "member_poll"),
    ]
