"""Discover capabilities from live registrations; never import plugin source to scan it."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .search import CatalogQuery, search_text

logger = logging.getLogger(__name__)


def identity(star) -> set[str]:
    return {str(value) for value in (star.name, star.root_dir_name) if value}


async def available_plugins(context, event) -> list:
    from astrbot.core.platform.wechat_group_policy import event_plugins

    configured = context.get_config(event.unified_msg_origin).get("plugin_set", ["*"])
    stars = list(context.get_all_stars())
    event_names = await event_plugins(
        event, configured, stars, command_handler="capability_invoke",
    ) or []
    return [star for star in stars if star.activated and not star.reserved
            and identity(star).intersection(event_names)]


def natural_owner(capability) -> str:
    return "jiang_raid_signup" if capability.delivery == "plugin_bridge" else "jiang_commands"


def natural_available(capability, stars, commands=None, is_admin=False) -> bool:
    owner = natural_owner(capability)
    # Existing natural wrappers must also respect disabled/admin-only native commands.
    method = getattr(getattr(capability, "handler", None), "__name__", "").removeprefix("_run_")
    registered = [item for item in flatten_commands(commands or [])
                  if item.get("plugin") == owner and item.get("handler_name") == method]
    if registered and not command_entries(registered, stars, is_admin):
        return False
    for star in stars:
        if owner not in identity(star):
            continue
        return capability.delivery != "plugin_bridge" or callable(
            getattr(star.star_cls, "invoke_natural_capability", None)
        )
    return False


def flatten_commands(commands):
    for command in commands:
        if not isinstance(command, dict):
            continue
        yield command
        children = command.get("sub_commands")
        if isinstance(children, list):
            yield from flatten_commands(children)


def command_entries(commands, stars, is_admin: bool) -> list[dict]:
    names = set().union(*(identity(star) for star in stars)) if stars else set()
    return [
        dict(command, sub_commands=[])
        for command in flatten_commands(commands)
        if command.get("plugin") in names
        and command.get("enabled", True)
        and not command.get("reserved", False)
        and not command.get("has_conflict", False)
        and not command.get("is_group", False)
        and command.get("effective_command")
        and (command.get("permission") != "admin" or is_admin)
    ]


def load_declarations(star) -> list[dict]:
    """Read optional metadata beside the already loaded plugin, never arbitrary paths."""
    module_file = getattr(star.module, "__file__", None)
    if not module_file:
        return []
    path = Path(module_file).parent / "capabilities.json"
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > 65536:
            raise ValueError("capabilities.json exceeds 64 KiB")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("schema_version") != 1 or not isinstance(data.get("capabilities"), list):
            raise ValueError("unsupported capability declaration")
        result = []
        seen = set()
        for item in data["capabilities"]:
            if not isinstance(item, dict):
                raise ValueError("capability must be an object")
            key = item.get("id")
            if not isinstance(key, str) or not key.isidentifier() or key in seen:
                raise ValueError("capability id must be unique and an identifier")
            seen.add(key)
            description = item.get("description")
            examples = item.get("examples", [])
            if not isinstance(description, str) or not description.strip():
                raise ValueError("description is required")
            if not isinstance(examples, list) or any(not isinstance(x, str) for x in examples):
                raise ValueError("examples must be strings")
            handler = item.get("handler", "")
            if not isinstance(handler, str) or (handler and not handler.isidentifier()):
                raise ValueError("handler must be a registered method name")
            pattern = item.get("message_pattern", "")
            if not isinstance(pattern, str) or len(pattern) > 1000:
                raise ValueError("invalid message pattern")
            if handler and not pattern:
                raise ValueError("executable declarations require a message pattern")
            if pattern:
                re.compile(pattern)
            # Declarations can only opt into read operations explicitly; unknown is write.
            effect = item.get("effect", "write")
            if effect not in {"read", "write"}:
                raise ValueError("invalid effect")
            result.append({"id": f"decl:{star.name}:{key}", "effect": effect,
                           "description": description, "examples": examples,
                           "handler": handler, "message_pattern": pattern})
        return result
    except (OSError, ValueError, TypeError, AttributeError, re.error):
        logger.exception("Invalid capability declaration for %s", star.name)
        return []


async def discover(context, event, commands) -> dict:
    from astrbot.core.star.star_handler import star_handlers_registry

    stars = await available_plugins(context, event)
    entries = command_entries(commands, stars, event.is_admin())
    for entry in entries:
        handler = star_handlers_registry.get_handler_by_full_name(entry["handler_full_name"])
        entry["parameters"] = ""
        if handler:
            entry["parameters"] = "; ".join(
                f.print_types() for f in handler.event_filters if hasattr(f, "print_types")
            )
    plugins = []
    declarations = []
    for star in stars:
        plugins.append({"name": star.name, "description": star.desc or "",
                        "display_name": star.display_name or star.name})
        for item in load_declarations(star):
            if item.get("handler"):
                handlers = star_handlers_registry.get_handlers_by_module_name(star.module_path)
                matching = [h for h in handlers if h.handler_name == item["handler"] and h.enabled]
                if len(matching) != 1:
                    continue
                # If this declaration targets a standard command, respect its runtime
                # disable/permission/conflict flags in the catalog as well as at execution.
                registered = [c for c in flatten_commands(commands)
                              if c.get("handler_full_name") == matching[0].handler_full_name]
                if registered and not command_entries(registered, stars, event.is_admin()):
                    continue
            declarations.append(dict(item, plugin=star.name))
    return {"stars": stars, "plugins": plugins, "commands": entries, "registered_commands": commands,
            "declarations": declarations}


def render_catalog(snapshot: dict, natural: str, query: str, tools=()) -> str:
    """Keep all plugin descriptions visible, including event-only and background plugins."""
    search = CatalogQuery(query)
    output = [natural] if natural else []
    plugin_names = {item["name"] for item in snapshot["plugins"]}
    owners = {item["name"]: search_text(item) for item in snapshot["plugins"]}
    owner_text = lambda item: owners.get(item.get("plugin"), "")
    plugins = search.rank(snapshot["plugins"], search_text)
    selected_commands = search.rank(snapshot["commands"], search_text, owner_text)
    declarations = search.rank(snapshot["declarations"], search_text, owner_text)
    native = search.rank([x for x in tools if not x.get("plugin") or x["plugin"] in plugin_names],
                         search_text, owner_text)
    matched_owners = {item.get("plugin") for item in selected_commands + declarations + native}
    listed_owners = {item["name"] for item in plugins}
    plugins.extend(item for item in snapshot["plugins"]
                   if item["name"] in matched_owners - listed_owners)
    if search.text and snapshot["plugins"] and not (
        natural or plugins or selected_commands or declarations or native
    ):
        output.append("本次关键词没有匹配结果，不代表机器人没有此功能。请按下方已授权插件简介，"
                      "改用较短的功能关键词或空 query 再查；仍无执行入口时如实说明。")
        plugins = snapshot["plugins"]
    if not (natural or plugins or selected_commands or declarations or native):
        output.append("当前会话没有已启用且获授权的插件能力；请检查插件启用和会话授权。")
    output.append("已启用插件（简介不代表已开放 AI 执行；后台功能按配置自动运行）：\n" +
                  json.dumps(plugins, ensure_ascii=False))
    commands = [{"id": "command:" + item["handler_full_name"],
                 "command": item["effective_command"],
                 "description": item.get("description", ""),
                 "parameters": item.get("parameters", ""),
                 "permission": item.get("permission", "everyone")}
                for item in selected_commands]
    output.append("标准命令：可用 capability_execute；未声明只读的操作需用户发送“执行 完整命令”。\n" +
                  json.dumps(commands, ensure_ascii=False))
    output.append("插件声明的用法（handler 为空仅供说明；read 可自然调用，write 需明确执行完整命令）：\n" +
                  json.dumps(declarations, ensure_ascii=False))
    output.append("本轮模型已获授权的专用工具（优先直接调用）：\n" +
                  json.dumps(native, ensure_ascii=False))
    output.append("未列出或未启用的能力不得声称可用。只凭简介不能推断参数或执行成功。")
    return "\n\n".join(output)
