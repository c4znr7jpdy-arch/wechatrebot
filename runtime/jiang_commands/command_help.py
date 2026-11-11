"""Build help from active handlers; never import or execute other plugins."""
import json
from pathlib import Path

from .declarations import declaration_visible, load_declarations


EXTRA_COMMANDS = {
    "jiang_commands": [("城市天气", "例：/北京天气"), ("省份油价", "例：/山东油价 或 /油价山东")],
}


def _handler_visible(handler, event, cfg, permission_filter) -> bool:
    if not getattr(handler, "enabled", True):
        return False
    for item in handler.event_filters:
        if isinstance(item, permission_filter) and not item.filter(event, cfg):
            return False
        if hasattr(item, "custom_filter_ok") and not item.custom_filter_ok(event, cfg):
            return False
    return True


def _declared_help(star, handlers, event, cfg, permission_filter):
    """Use reviewed examples for manual routers and background-only features too."""
    for item in load_declarations(star):
        if not declaration_visible(item, event):
            continue
        if item["handler"]:
            matching = [handler for handler in handlers
                        if handler.handler_module_path == star.module_path
                        and getattr(handler, "handler_name", "") == item["handler"]]
            if len(matching) != 1 or not _handler_visible(matching[0], event, cfg, permission_filter):
                continue
        examples = "；".join(dict.fromkeys(item["examples"]))
        yield (examples + " — " if examples else "") + item["description"]


def build_help(context, event):
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType, star_handlers_registry
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.command_group import CommandGroupFilter
    from astrbot.core.star.filter.permission import PermissionTypeFilter
    from astrbot.core.platform.wechat_group_policy import group_plugins

    cfg = context.get_config(event.unified_msg_origin)
    enabled = getattr(event, "plugins_name", None)
    configured = cfg.get("plugin_set", ["*"])
    stars = [s for s in context.get_all_stars()
             if s.activated and not s.reserved and s.star_cls is not None
             and (configured == ["*"] or s.name in configured or s.root_dir_name in configured)
             and (enabled is None or s.name in enabled or s.root_dir_name in enabled)]
    sections = {s.root_dir_name: [] for s in stars}
    try:
        manifest = json.loads((Path(__file__).resolve().parents[5] / "legal/feature-set.json").read_text("utf-8-sig"))
        labels = {item["module"]: item["display_name"] for item in manifest["features"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        labels = {}
    names = {s.root_dir_name: labels.get(s.root_dir_name) or s.display_name or "扩展功能" for s in stars}
    handlers = star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent, plugins_name=enabled)
    handlers = list(handlers)
    for handler in handlers:
        owner = star_map.get(handler.handler_module_path)
        module = getattr(owner, "root_dir_name", "")
        if module not in sections:
            continue
        if not _handler_visible(handler, event, cfg, PermissionTypeFilter):
            continue
        for f in handler.event_filters:
            if not isinstance(f, (CommandFilter, CommandGroupFilter)):
                continue
            commands = f.get_complete_command_names()
            if not commands:
                continue
            description = (handler.desc or "").strip().splitlines()
            description = description[0] if description else ""
            aliases = sorted(set(commands[1:]))
            line = "/" + commands[0]
            if aliases:
                line += "（别名：" + "、".join(aliases) + "）"
            if description:
                line += " — " + description
            if line not in sections[module]:
                sections[module].append(line)
    for star in stars:
        for line in _declared_help(star, handlers, event, cfg, PermissionTypeFilter):
            if line not in sections[star.root_dir_name]:
                sections[star.root_dir_name].append(line)
    for module, commands in EXTRA_COMMANDS.items():
        if module not in sections:
            continue
        for command, description in commands:
            prefix = "" if command.startswith(("@", "发送")) else "/"
            sections[module].append(prefix + command + " — " + description)
    if "jiang_commands" in sections:
        allowed = True
        if event.get_platform_name() == "aiocqhttp" and str(event.get_group_id()).endswith("@chatroom"):
            allowed = "jiang_commands" in (group_plugins(
                Path.cwd().parent.parent / "data/manager/group-policies.json",
                str(event.get_self_id() or ""), str(event.get_group_id()),
                configured, stars, command_handler="random_image") or [])
        if allowed:
            sections["jiang_commands"].append("图来 — 随机图片（群聊需单独授权）")
    pages = ["可用功能命令\n按当前会话已启用的插件生成；需要的账号、模型或接口请先在管理器配置。\n参数及详细用法可查看各插件帮助；群聊按当前唤醒规则使用。"]
    for module, lines in sections.items():
        if not lines:
            continue
        block = "\n【" + str(names[module]) + "】\n" + "\n".join(lines)
        for line in block.splitlines():
            if len(pages[-1]) + len(line) + 1 > 1800:
                pages.append("功能命令（续）")
            pages[-1] += "\n" + line
    return pages
