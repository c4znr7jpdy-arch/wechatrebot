from __future__ import annotations

import ast
import asyncio
from datetime import datetime
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = Path(r"E:\Project\hook_41827_astrbot_bridge")
PATCH_PATH = ROOT / "patches" / "hook-bridge-outbound-policy.patch"
PACKAGE_NAME = "_patched_hook_bridge_outbound_policy"


@pytest.fixture(scope="module")
def patched_bridge(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not (BRIDGE_SOURCE / "src/hook_bridge/onebot_client.py").is_file():
        pytest.skip("HOOK 4.1.8.27 bridge source checkout is unavailable")

    repository = tmp_path_factory.mktemp("hook-bridge-outbound-policy")
    # The Windows temp directory can itself be below a parent Git worktree. A
    # nested repository makes the patch target and its path stripping explicit.
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    checkout = repository / "hook_41827_astrbot_bridge"
    shutil.copytree(BRIDGE_SOURCE / "src", checkout / "src")

    checked = subprocess.run(
        ["git", "apply", "--check", "--whitespace=error-all", str(PATCH_PATH)],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert checked.returncode == 0, checked.stderr
    applied = subprocess.run(
        ["git", "apply", "--whitespace=error-all", str(PATCH_PATH)],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert applied.returncode == 0, applied.stderr

    for source in (checkout / "src/hook_bridge").glob("*.py"):
        ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    return checkout


@pytest.fixture(scope="module")
def patched_modules(patched_bridge: Path):
    package_root = patched_bridge / "src/hook_bridge"
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)
    try:
        yield SimpleNamespace(
            policy=importlib.import_module(f"{PACKAGE_NAME}.outbound_policy"),
            models=importlib.import_module(f"{PACKAGE_NAME}.models"),
            onebot=importlib.import_module(f"{PACKAGE_NAME}.onebot_client"),
            state=importlib.import_module(f"{PACKAGE_NAME}.state"),
        )
    finally:
        for name in tuple(sys.modules):
            if name == PACKAGE_NAME or name.startswith(f"{PACKAGE_NAME}."):
                sys.modules.pop(name, None)


def _run(awaitable):
    return asyncio.run(awaitable)


def _save_policy(path: Path, group_policy: dict[str, object]) -> None:
    payload = {
        "schema_version": 1,
        "accounts": {
            "wxid_bot": {
                "groups": {
                    "new-room@chatroom": group_policy,
                }
            }
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _configured_policy(
    *,
    reply_mode: str = "mention",
    proactive_enabled: bool = True,
    quiet_enabled: bool = False,
    quiet_start: str = "22:00",
    quiet_end: str = "08:00",
    daily_limit: int = 3,
) -> dict[str, object]:
    return {
        "bot_enabled": True,
        "reply_mode": reply_mode,
        "proactive_enabled": proactive_enabled,
        "quiet_hours": {
            "enabled": quiet_enabled,
            "start": quiet_start,
            "end": quiet_end,
            "timezone": "Asia/Shanghai",
        },
        "daily_limit": daily_limit,
    }


def test_patch_applies_cleanly_to_pinned_bridge_checkout(
    patched_bridge: Path,
) -> None:
    assert (patched_bridge / "src/hook_bridge/outbound_policy.py").is_file()
    onebot_source = (
        patched_bridge / "src/hook_bridge/onebot_client.py"
    ).read_text(encoding="utf-8-sig")
    assert "from .outbound_policy import OutboundGroupPolicy" in onebot_source
    callback_source = (
        patched_bridge / "src/hook_bridge/callback_server.py"
    ).read_text(encoding="utf-8-sig")
    assert 'payload.get("_wechatrebot_delivery")' in callback_source
    assert 'payload.get("_wechatrebot_wake")' in callback_source


def test_new_group_keeps_mention_replies_without_extra_proactive_gate(
    patched_modules,
    tmp_path: Path,
) -> None:
    policy = patched_modules.policy.OutboundGroupPolicy(
        tmp_path / "group-policies.json"
    )

    ordinary_reply = _run(
        policy.authorize(
            "wxid_bot", "new-room@chatroom", delivery="reply", wake=False
        )
    )
    mentioned_reply = _run(
        policy.authorize(
            "wxid_bot", "new-room@chatroom", delivery="reply", wake=True
        )
    )
    proactive = _run(
        policy.authorize(
            "wxid_bot", "new-room@chatroom", delivery="proactive"
        )
    )

    assert ordinary_reply.allowed is False
    assert "明确点名" in ordinary_reply.message
    assert mentioned_reply.allowed is True
    assert proactive.allowed is True


def test_router_hot_reads_saved_group_policy_before_every_send(
    patched_modules,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "group-policies.json"

    class Hook:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def send_text(self, target: str, text: str):
            self.calls.append((target, text))
            return patched_modules.models.SendResult(ok=True)

    hook = Hook()
    state = patched_modules.state.BridgeState(60, 100)
    state.set_identity("wxid_bot", "机器人")
    router = patched_modules.onebot.OneBotActionRouter(
        hook, object(), state, policy_path=policy_path
    )

    def send(text: str):
        return _run(
            router.handle(
                {
                    "action": "send_group_msg",
                    "params": {
                        "group_id": "new-room@chatroom",
                        "message": text,
                        "_wechatrebot_delivery": "reply",
                        "_wechatrebot_wake": True,
                    },
                }
            )
        )

    assert send("缺省策略允许点名回复")["status"] == "ok"

    _save_policy(policy_path, _configured_policy(reply_mode="off"))
    blocked = send("保存后立即禁止回复")
    assert blocked["status"] == "failed"
    assert blocked["retcode"] == 1403

    _save_policy(policy_path, _configured_policy(reply_mode="mention"))
    assert send("再次保存后立即恢复")["status"] == "ok"
    assert hook.calls == [
        ("new-room@chatroom", "缺省策略允许点名回复"),
        ("new-room@chatroom", "再次保存后立即恢复"),
    ]


def test_reactive_raw_music_card_is_not_misclassified_as_proactive(
    patched_modules,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "group-policies.json"

    class Hook:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []

        async def send_app_xml(self, target: str, content: str, app_type: int):
            self.calls.append((target, content, app_type))
            return patched_modules.models.SendResult(ok=True)

    hook = Hook()
    state = patched_modules.state.BridgeState(60, 100)
    state.set_identity("wxid_bot", "机器人")
    router = patched_modules.onebot.OneBotActionRouter(
        hook, object(), state, policy_path=policy_path
    )
    payload = {
        "action": "send_raw_xml",
        "params": {
            "group_id": "new-room@chatroom",
            "content": "<appmsg><type>3</type></appmsg>",
            "_wechatrebot_delivery": "reply",
            "_wechatrebot_wake": True,
        },
    }

    assert _run(router.handle(payload))["status"] == "ok"
    assert hook.calls == [
        ("new-room@chatroom", "<appmsg><type>3</type></appmsg>", 3)
    ]


def test_legacy_proactive_switch_and_scheduled_limits_do_not_block_cards(
    patched_modules,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "group-policies.json"
    usage_path = tmp_path / "group-policy-usage.json"
    current = datetime(2026, 9, 9, 23, 30)
    policy = patched_modules.policy.OutboundGroupPolicy(
        policy_path,
        usage_path=usage_path,
        now=lambda zone: current.replace(tzinfo=zone),
    )
    _save_policy(
        policy_path,
        _configured_policy(
            proactive_enabled=False,
            quiet_enabled=True,
            quiet_start="22:00",
            quiet_end="08:00",
            daily_limit=0,
        ),
    )

    first = _run(
        policy.authorize(
            "wxid_bot", "new-room@chatroom", delivery="proactive"
        )
    )
    decisions = [
        _run(
            policy.authorize(
                "wxid_bot", "new-room@chatroom", delivery="proactive"
            )
        )
        for _ in range(3)
    ]
    assert first.allowed is True
    assert all(decision.allowed for decision in decisions)
    assert not usage_path.exists()


def test_corrupt_policy_fails_closed_for_replies_and_proactive_delivery(
    patched_modules,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "group-policies.json"
    policy_path.write_text('{"schema_version": 1, "accounts": ', encoding="utf-8")
    policy = patched_modules.policy.OutboundGroupPolicy(policy_path)

    for delivery, wake in (("reply", True), ("proactive", False)):
        decision = _run(
            policy.authorize(
                "wxid_bot",
                "new-room@chatroom",
                delivery=delivery,
                wake=wake,
            )
        )
        assert decision.allowed is False
        assert "策略无法确认" in decision.message


def _method_node(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    router = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OneBotActionRouter"
    )
    return next(
        node
        for node in router.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _self_calls(node: ast.AST, method: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and call.func.attr == method
    ]


def test_policy_guard_is_at_onebot_unified_group_send_boundary(
    patched_bridge: Path,
) -> None:
    path = patched_bridge / "src/hook_bridge/onebot_client.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    send_message = _method_node(tree, "_send_message")
    handle = _method_node(tree, "handle")

    authorize_calls = [
        call
        for call in ast.walk(send_message)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "authorize"
    ]
    execute_calls = _self_calls(send_message, "_execute_command")
    assert len(authorize_calls) == 1
    assert len(execute_calls) == 1
    assert authorize_calls[0].lineno < execute_calls[0].lineno
    assert any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_group"
        and authorize_calls[0] in list(ast.walk(node))
        for node in ast.walk(send_message)
    )

    # Normal, forward, voice, and group-file OneBot routes all enter the same
    # guarded method. The raw-XML compatibility route is its only direct group
    # send bypass and therefore must carry an equivalent group authorization.
    assert len(_self_calls(handle, "_send_message")) >= 4
    raw_xml_guard = next(
        node
        for node in ast.walk(handle)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and any(
            isinstance(value, ast.Constant) and value.value == "send_raw_xml"
            for value in ast.walk(node.test)
        )
    )
    assert any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "authorize"
        for call in ast.walk(raw_xml_guard)
    )
