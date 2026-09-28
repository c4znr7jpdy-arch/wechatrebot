"""Build help from active handlers; never import or execute other plugins."""
import json
from pathlib import Path


EXTRA_COMMANDS = {
    "jiang_commands": [("城市天气", "例：/北京天气"), ("省份油价", "例：/山东油价 或 /油价山东")],
    "astrbot_plugin_music": [("点歌 歌名", "搜索并播放歌曲")],
    "jiang_mystool": [("米游社帮助", "完整用法"), ("登录", "米游社扫码登录"), ("登录状态", "查询登录进度"), ("签到", "游戏签到"), ("任务", "米游社任务"), ("地址", "收货地址"), ("商品", "兑换商品"), ("兑换", "商品兑换"), ("原神便签", "原神实时便签"), ("星穹铁道便签", "星穹铁道实时便签"), ("设置", "米游社设置"), ("账号设置", "账号设置"), ("通知设置", "通知设置"), ("导出Cookies", "导出自己的登录凭据，请仅在私聊操作")],
    "jiang_schedule": [("定时新闻 8点", "每天发送抖音热榜（管理员）"), ("定时天气 北京 8点", "定时天气（管理员）"), ("定时油价 四川 8点", "定时油价（管理员）"), ("定时epic 8点", "免费游戏推送（管理员）"), ("定时kfc 周四 12点", "定时文案（管理员）"), ("定时签到 8点", "定时签到（管理员）"), ("定时列表", "查看任务（管理员）"), ("定时删除新闻", "删除新闻任务（管理员）"), ("订阅动态 UID", "订阅 B站动态（管理员）"), ("取消订阅动态 UID", "取消订阅（管理员）"), ("订阅列表", "查看订阅（管理员）")],
    "jiang_short_drama": [("短剧 剧名", "搜索短剧；也支持电视剧、电影、动漫、综艺、体育和“剧 名称”"), ("短剧推荐", "最新作品推荐")],
    "jiang_image": [("@机器人 帮我画一只猫", "自然语言生图；引用图片后说明修改要求可改图")],
    "jiang_video_understanding": [("发送视频并 @机器人 提问", "视频理解")],
    "astrbot_plugin_parser": [("发送视频或图文链接", "自动解析已支持的网站")],
}


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
    for handler in handlers:
        owner = star_map.get(handler.handler_module_path)
        module = getattr(owner, "root_dir_name", "")
        if module not in sections:
            continue
        if any(isinstance(f, PermissionTypeFilter) and not f.filter(event, cfg)
               for f in handler.event_filters):
            continue
        for f in handler.event_filters:
            if not isinstance(f, (CommandFilter, CommandGroupFilter)):
                continue
            if hasattr(f, "custom_filter_ok") and not f.custom_filter_ok(event, cfg):
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
    for module, commands in EXTRA_COMMANDS.items():
        if module not in sections or (module == "jiang_schedule" and not event.is_admin()):
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
