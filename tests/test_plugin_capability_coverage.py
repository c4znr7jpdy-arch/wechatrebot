"""Verify AI usage metadata against the actual delivered, patched plugin sources."""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from types import SimpleNamespace as NS

import pytest

from build_legal_inventory import _simple_yaml
from capability_catalog_fixture import source_catalog
from test_capability_discovery import ROOT, Event, context, discovery, engine, execution, plugin


@pytest.fixture(scope="module")
def shipped(patched_plugins):
    return source_catalog(patched_plugins, ROOT / "runtime/jiang_capability_router/capabilities.py")


def metadata_owner(folder):
    path = folder / "metadata.yaml"
    raw = path.read_text("utf-8-sig")
    metadata = json.loads(raw) if raw.lstrip().startswith("{") else _simple_yaml(path)
    return metadata["name"].split(" #", 1)[0].strip()


def test_every_delivered_plugin_has_commands_tools_or_explained_behavior(shipped, patched_plugins):
    features = json.loads((ROOT / "release/feature-set.json").read_text("utf-8"))["features"]
    covered = {row["plugin"] for kind in ("commands", "tools", "declarations") for row in shipped[kind]}
    missing = [row["module"] for row in features
               if metadata_owner(patched_plugins / row["module"]) not in covered]
    assert missing == [], f"Plugins lack AI commands, tools or usage declarations: {missing}"


def test_packaged_declarations_use_real_registered_handlers_and_valid_examples(patched_plugins):
    registered_filters = {"command", "command_group", "regex", "event_message_type"}
    checked = 0
    for folder in patched_plugins.iterdir():
        declaration_path = folder / "capabilities.json"
        if not declaration_path.is_file():
            continue
        owner = plugin(metadata_owner(folder), module=NS(__file__=str(folder / "main.py")))
        declarations = discovery.load_declarations(owner)
        raw = json.loads(declaration_path.read_text("utf-8"))["capabilities"]
        assert len(declarations) == len(raw) > 0, folder.name
        registered = set()
        for source in folder.rglob("*.py"):
            if any(part in {"tests", "node_modules", "__pycache__"} for part in source.parts):
                continue
            tree = ast.parse(source.read_text("utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(isinstance(decorator, ast.Call)
                       and getattr(decorator.func, "attr", "") in registered_filters
                       for decorator in node.decorator_list):
                    registered.add(node.name)
        for item in declarations:
            if not item["handler"]:
                continue
            assert item["handler"] in registered, (folder.name, item["id"], item["handler"])
            assert item["examples"], (folder.name, item["id"])
            for example in item["examples"]:
                assert re.fullmatch(item["message_pattern"], example), (folder.name, item["id"], example)
            checked += 1
    assert checked >= 20


@pytest.fixture(scope="module")
def mystool_declarations(patched_plugins):
    folder = patched_plugins / "jiang_mystool"
    declarations = discovery.load_declarations(plugin("jiang_mystool", module=NS(__file__=str(folder / "main.py"))))
    return [dict(item, plugin="jiang_mystool") for item in declarations]


def test_every_mystool_router_alias_has_documented_usage(patched_plugins, mystool_declarations):
    tree = ast.parse((patched_plugins / "jiang_mystool/main.py").read_text("utf-8-sig"))
    router = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "command_router")
    routes = next(node.value for node in ast.walk(router)
                  if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                                                        and target.id == "handlers" for target in node.targets))
    aliases = {ast.literal_eval(key) for key in routes.keys}
    documented = {example.removeprefix("/").split()[0]
                  for item in mystool_declarations for example in item["examples"]}
    assert aliases <= documented, aliases - documented
    assert all(not item["handler"] or item["handler"] == "command_router" for item in mystool_declarations)


@pytest.mark.parametrize("message,effect", [
    ("/米游社帮助", "read"), ("/米游社", "read"), ("/登录状态", "read"),
    ("/商品", "read"), ("/商品 原神", "read"), ("/兑换", "read"),
    ("/原神便签", "read"), ("/星穹铁道便签", "read"), ("/设置", "read"),
    ("/账号设置", "read"), ("/账号设置 123456", "read"), ("/通知设置", "read"),
    ("/登录", "write"), ("/签到", "write"), ("/任务", "write"),
    ("/兑换 + 12345", "write"), ("/兑换 - 12345", "write"),
    ("/账号设置 平台 ios", "write"), ("/账号设置 123456 签到 开启", "write"),
    ("/通知设置 关闭", "write"),
])
def test_mystool_effects_and_confirmation_match_real_operations(mystool_declarations, message, effect):
    matches = [item for item in mystool_declarations
               if item["handler"] and re.fullmatch(item["message_pattern"], message)]
    assert len(matches) == 1, (message, [item["id"] for item in matches])
    item = matches[0]
    assert item["effect"] == effect
    snapshot = {"commands": [], "declarations": mystool_declarations}
    assert execution.select_entry(snapshot, item["id"], message) == ("jiang_mystool", "command_router", effect)
    assert execution.authorized_message("这个功能怎么用", message, effect) == (effect == "read")
    assert execution.authorized_message("执行 " + message, message, effect)


@pytest.mark.parametrize("message", ["/地址", "/地址 123456", "/导出Cookie", "/导出Cookies"])
def test_private_account_details_are_explained_without_ai_execution(mystool_declarations, message):
    assert any(example.split()[0] == message.split()[0]
               for item in mystool_declarations if not item["handler"] for example in item["examples"])
    assert not any(item["handler"] and re.fullmatch(item["message_pattern"], message)
                   for item in mystool_declarations)
    for item in mystool_declarations:
        if not item["handler"]:
            with pytest.raises(ValueError, match="没有执行入口"):
                execution.select_entry({"commands": [], "declarations": mystool_declarations}, item["id"], message)


def test_asking_about_mystool_returns_actual_usage(shipped):
    text = discovery.render_catalog(shipped, "", "米游社插件怎么用", shipped["tools"])
    for command in ("/米游社帮助", "/登录", "/签到", "/任务", "/原神便签", "/星穹铁道便签"):
        assert command in text
    assert "decl:jiang_mystool:" in text


def test_music_usage_respects_qishui_source_input_limit(shipped):
    entry = next(item for item in shipped["declarations"]
                 if item["plugin"] == "astrbot_plugin_music" and item["id"].endswith(":search"))
    assert not re.fullmatch(entry["message_pattern"], "汽水点歌 稻香")
    assert re.fullmatch(entry["message_pattern"], "汽水点歌 track_id=1234567890123456789")
    assert re.fullmatch(entry["message_pattern"], "网易点歌 稻香")


def test_declaration_admin_session_and_handler_permissions_are_live(engine, tmp_path):
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"schema_version": 1, "capabilities": [
        {"id": "manage", "description": "管理员管理定时任务", "handler": "schedule",
         "message_pattern": "/定时新闻 .+", "examples": ["/定时新闻 8点"],
         "effect": "write", "permission": "admin"},
        {"id": "usage", "description": "配置用途说明", "effect": "read"},
    ]}), encoding="utf-8")
    owner = plugin(module=NS(__file__=str(tmp_path / "main.py")))
    handler = NS(handler_name="schedule", handler_full_name="sample.main.schedule", enabled=True)
    registry = sys.modules["astrbot.core.star.star_handler"].star_handlers_registry
    registry.get_handlers_by_module_name = lambda name: [handler]
    event = Event()
    read = lambda: asyncio.run(discovery.discover(context([owner]), event, []))["declarations"]
    assert [item["id"] for item in read()] == ["decl:sample:usage"]
    event.role = "admin"
    assert len(read()) == 2
    handler.enabled = False
    assert [item["id"] for item in read()] == ["decl:sample:usage"]
    handler.enabled = True
    engine.add("sample")
    assert read() == []
    engine.clear()
    event.plugins_name = []
    assert read() == []


def test_schedule_management_is_personal_in_private_and_admin_only_in_groups(engine, patched_plugins):
    folder = patched_plugins / "jiang_schedule"
    owner = plugin("jiang_schedule", module=NS(__file__=str(folder / "main.py")))
    handler = NS(handler_name="schedule", handler_full_name="jiang_schedule.main.schedule", enabled=True)
    registry = sys.modules["astrbot.core.star.star_handler"].star_handlers_registry
    registry.get_handlers_by_module_name = lambda name: [handler]
    event = Event()
    read = lambda: asyncio.run(discovery.discover(context([owner]), event, []))["declarations"]
    private_writes = [item for item in read() if item["handler"] and item["effect"] == "write"]
    assert private_writes
    assert all(item["permission"] == "group_admin" for item in private_writes)
    event.get_group_id = lambda: "group@chatroom"
    assert not [item for item in read() if item["handler"] and item["effect"] == "write"]
    event.role = "admin"
    assert [item["id"] for item in read() if item["handler"] and item["effect"] == "write"] == [
        item["id"] for item in private_writes]


@pytest.mark.parametrize("permission", ["owner", "", None, 123])
def test_invalid_declaration_permissions_fail_closed(tmp_path, permission):
    (tmp_path / "capabilities.json").write_text(json.dumps({"schema_version": 1, "capabilities": [
        {"id": "private", "description": "配置修改", "permission": permission}
    ]}), encoding="utf-8")
    owner = plugin(module=NS(__file__=str(tmp_path / "main.py")))
    assert discovery.load_declarations(owner) == []
