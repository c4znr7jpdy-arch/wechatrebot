import ast
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
PROJECT = Path(__file__).parents[1] / "vendor"


def _patched_main(tmp_path: Path) -> str:
    source = PROJECT / "data/plugins/jiang_short_drama"
    if not (source / "main.py").is_file():
        pytest.fail("short-drama source is unavailable")
    checkout = tmp_path / "jiang_short_drama"
    shutil.copytree(source, checkout)

    def patch_order(path: Path) -> tuple[int, str]:
        raw = path.stem.removeprefix("astrbot-plugin-jiang_short_drama")
        return (1 if raw.startswith("--") else 0, raw.removeprefix("--").casefold())

    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    for patch in sorted(
        ROOT.glob("patches/astrbot-plugin-jiang_short_drama*.patch"),
        key=patch_order,
    ):
        subprocess.run(
            ["git", "apply", "--check", "--ignore-space-change", str(patch)],
            cwd=checkout,
            check=True,
        )
        subprocess.run(
            ["git", "apply", "--ignore-space-change", str(patch)],
            cwd=checkout,
            check=True,
        )
    source_text = (checkout / "main.py").read_text(encoding="utf-8")
    compile(source_text, str(checkout / "main.py"), "exec")
    return source_text


def _routing_helpers(source: str):
    tree = ast.parse(source)
    names = {
        "_is_wechat_group",
        "_interactive_card_payload",
        "_mark_interactive_command",
    }
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    assert {node.name for node in nodes} == names
    namespace = {"AstrMessageEvent": object}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<routing>", "exec"), namespace)
    return namespace


class _Event:
    def __init__(self, platform: str, group_id: str):
        self.platform = platform
        self.group_id = group_id
        self.is_at_or_wake_command = False

    def get_platform_name(self):
        return self.platform

    def get_group_id(self):
        return self.group_id


def test_user_command_cards_are_group_replies(tmp_path: Path) -> None:
    source = _patched_main(tmp_path)
    helpers = _routing_helpers(source)
    event = _Event("aiocqhttp", "51632940287@chatroom")
    message = [{"type": "wechat_link_card", "data": {"title": "推荐"}}]

    payload = helpers["_interactive_card_payload"](event, message)
    helpers["_mark_interactive_command"](event)

    assert payload == {
        "message": message,
        "_wechatrebot_delivery": "reply",
        "_wechatrebot_wake": True,
    }
    assert event.is_at_or_wake_command is True
    assert source.count("payload = _interactive_card_payload(event, message)") == 7


def test_private_and_non_wechat_cards_keep_native_routing(tmp_path: Path) -> None:
    source = _patched_main(tmp_path)
    helpers = _routing_helpers(source)
    message = [{"type": "wechat_link_card", "data": {"title": "推荐"}}]
    for event in (_Event("aiocqhttp", ""), _Event("other", "123@chatroom")):
        assert helpers["_interactive_card_payload"](event, message) == {
            "message": message
        }
        helpers["_mark_interactive_command"](event)
        assert event.is_at_or_wake_command is False
