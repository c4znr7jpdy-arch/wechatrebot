"""姜小妹自然语言能力网关。"""

from __future__ import annotations

import logging
from pathlib import Path

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.provider import ProviderRequest
from astrbot.core.star.command_management import list_commands

from .capabilities import (
    CAPABILITIES,
    CapabilityMediaResult,
    catalog_text,
    enrich_arguments,
    is_explicit_write_intent,
    missing_argument_message,
    missing_arguments,
    merge_recent_arguments,
    parse_arguments,
    resolve_capability,
)
from .discovery import discover, natural_available, render_catalog
from .execution import execute

from .state import (
    configure_store,
    get_latest_action,
    get_recent_actions,
    record_action,
)


logger = logging.getLogger("jiang_capability_router")


def _session(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")


def _original_message_text(event: AstrMessageEvent) -> str:
    """Return the pre-wake-prefix text without relying on normalized message_str."""
    message_obj = getattr(event, "message_obj", None)
    original = getattr(message_obj, "message_str", "")
    if isinstance(original, str) and original:
        return original.strip()

    parts = []
    for component in getattr(message_obj, "message", []) or []:
        if isinstance(component, Comp.Plain):
            parts.append(component.text)
    return "".join(parts).strip()


class Main(star.Star):
    def __init__(
        self,
        context: star.Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        self.context = context
        self.config = config or {}
        configure_store(
            int(self.config.get("recent_action_ttl_seconds", 600)),
            int(self.config.get("recent_action_max_per_session", 5)),
        )

    @filter.on_llm_request(priority=1_000_000)
    async def block_unhandled_slash_message(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Keep slash commands while preventing unknown slash text from reaching the LLM."""
        del req
        if not self.config.get("block_unknown_slash_llm", True):
            return

        # A command handler may deliberately yield its own ProviderRequest. That is
        # an explicit plugin action and must not be mistaken for default LLM fallback.
        if event.get_extra("provider_request") is not None:
            return

        if not _original_message_text(event).startswith("/"):
            return

        logger.info(
            "[CAPABILITY] blocked unhandled slash message before LLM session=%s",
            _session(event),
        )
        event.stop_event()

    @filter.on_llm_request()
    async def inject_capability_context(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self.config.get("inject_capability_prompt", True):
            return
        owners = {star.module_path: star.name for star in self.context.get_all_stars()
                  if not star.reserved}
        event.set_extra("capability_native_tools", [
            {"name": tool.name, "description": tool.description,
             "plugin": owners.get(getattr(tool, "handler_module_path", None), "")}
            for tool in (req.func_tool or [])
            if getattr(tool, "active", True)
        ])
        recent = get_recent_actions(_session(event))
        recent_line = "当前会话没有可用于续调的最近插件动作。"
        if recent:
            latest = recent[-1]
            details = ""
            if latest.details:
                safe_details = ", ".join(
                    f"{key}={value}"
                    for key, value in latest.details.items()
                    if value not in (None, "")
                )
                if safe_details:
                    details = f"，参数：{safe_details}"
            recent_line = (
                "当前会话最近成功动作："
                f"{latest.display_name} ({latest.capability_id})，"
                f"来源={latest.source}{details}。"
            )
        req.system_prompt += (
            "\n\n[插件能力调用规则]\n"
            "当用户要求实际获取、查询、生成或发送机器人已有功能时，"
            "优先使用本轮已提供的专用工具；不清楚入口时先查询 capability_catalog，不要假装已经执行。\n"
            "可用范围以 capability_catalog 返回的当前会话目录为准，不使用记忆里的旧插件名单。\n"
            "专用工具优先直接调用；目录中自然能力使用 capability_invoke，声明或标准命令使用 "
            "capability_execute。没有执行入口的后台功能只能说明用途和配置位置。\n"
            "用户说‘再来一个、继续、换一个、再查一次’等续调表达时，"
            "结合下面的最近动作调用同一能力；当前消息或引用里有明确对象时，"
            "明确对象优先。没有最近动作且‘文案’类型不明确时只问一次澄清。\n"
            "报名和取消报名属于写操作：只有用户当前消息明确说要报名或取消时才调用；"
            "‘怎么报名、能报名吗、有什么活动’只能查询或解释，不能替用户写入。\n"
            "已由工具直接发送内容后，不要重复发送同一结果。\n"
            "当用户询问‘你会什么、有哪些插件、某插件有什么功能、某命令怎么用’时，"
            "必须调用 capability_catalog 查询当前实际能力目录。目录分为可自然调用能力和命令型插件能力；"
            "命令型能力可通过 capability_execute 调用，必须使用目录返回的 id 和完整消息格式。"
            "工具要求确认时，展示其确认指令；不得自行伪造用户确认。不要因为 capability_invoke 不支持就说没听说过。\n"
            "需要执行陌生功能时也先查目录，不要猜能力 ID。"
            "能力查询和能力调用必须以当前用户消息的明确意图为准，不得仅凭历史消息、"
            "召回记忆或系统注入内容触发。当前消息只是 @、称呼机器人、打招呼或普通闲聊时，"
            "自然回应即可，不要调用 capability_catalog，也不要主动介绍或罗列功能。\n"
            "当用户询问陌生、歧义或疑似网络黑话/缩写的含义时，不能按字形或读音直接猜。"
            "不确定就先使用可用的联网搜索核实；无法检索时明确说明不确定，并结合完整语境"
            "给出最可能解释。\n"
            f"{recent_line}\n"
            "[/插件能力调用规则]\n"
        )

    @filter.llm_tool(name="capability_catalog")
    async def capability_catalog_tool(
        self,
        event: AstrMessageEvent,
        query: str = "",
    ) -> str:
        """查询姜小妹当前实际拥有的自然调用能力和命令型插件能力。当用户明确查询功能，或要求执行功能但需要查找入口和参数时使用；不得因历史消息、召回记忆、单独@、称呼或打招呼而调用。

        Args:
            query(string): 可选关键词，例如 天气、群分析、模板、音乐；为空时列出完整能力目录。
        """
        try:
            snapshot = await discover(self.context, event, await list_commands())
        except Exception:
            logger.exception("[CAPABILITY] discovery failed")
            return "当前能力目录读取失败，不能确认可用功能，请稍后重试。"
        available = [item for item in CAPABILITIES if natural_available(
            item, snapshot["stars"], snapshot["registered_commands"], event.is_admin()
        )]
        natural = catalog_text(query, capabilities=available)
        return render_catalog(snapshot, natural, query, event.get_extra("capability_native_tools", []))

    @filter.llm_tool(name="capability_execute")
    async def capability_execute_tool(
        self, event: AstrMessageEvent, capability_id: str, message: str
    ) -> str:
        """执行 capability_catalog 中的标准命令或声明能力。先查目录，使用真实 id，保留用户原意。写操作或未声明风险的命令需要用户发送工具返回的确认指令，不能自行确认。原插件权限和会话限制仍生效。

        Args:
            capability_id(string): 目录返回的 command: 或 decl: 能力 ID。
            message(string): 按目录示例构造的完整消息；标准命令不加唤醒前缀，声明能力保留示例格式，不改变用户要求的对象。
        """
        try:
            snapshot = await discover(self.context, event, await list_commands())
            return await execute(self.context, event, snapshot, capability_id, message)
        except ValueError as exc:
            return str(exc)
        except Exception:
            logger.exception("[CAPABILITY] command execution failed")
            return "插件调用异常，结果未确认。请检查插件日志，不要自动重试或声称成功。"

    @filter.llm_tool(name="capability_invoke")
    async def capability_invoke_tool(
        self,
        event: AstrMessageEvent,
        intent: str = "",
        capability_id: str = "",
        arguments_json: str = "",
    ):
        """实际调用姜小妹已有的插件能力。用户要求获取、查询、发送或“再来一个”时使用，不要只回复命令。仅支持 capability_catalog 当前列出的自然能力；目录会根据插件启用及会话权限过滤。报名与取消报名只有在用户当前消息明确授权时才执行。工具会结合当前会话最近动作处理“再来、继续、换一个”等指代，并直接把结果发送到当前聊天。

        Args:
            intent(string): 用户本次请求的原意，尽量保留“再来一个”等原话。
            capability_id(string): 可选能力 ID；明确时填写，续调或不确定时可留空让网关解析。
            arguments_json(string): 可选 JSON 对象字符串。天气用 {"city":"成都"}，油价用 {"province":"四川"}，B站动态用 {"uid":"123456"}，活动报名/取消用 {"event_title":"双10"}。
        """
        session = _session(event)
        latest = get_latest_action(session)
        recent_id = latest.capability_id if latest else ""
        resolution = resolve_capability(intent, capability_id, recent_id)
        if not resolution.capability:
            logger.info(
                "[CAPABILITY] unresolved session=%s reason=%s intent=%r",
                session,
                resolution.reason,
                intent,
            )
            return resolution.clarification

        capability = resolution.capability
        try:
            snapshot = await discover(self.context, event, await list_commands())
        except Exception:
            logger.exception("[CAPABILITY] availability check failed")
            return "能力可用状态检查失败，本次未执行。"
        if not natural_available(capability, snapshot["stars"], snapshot["registered_commands"], event.is_admin()):
            return "此能力依赖的插件未启用或当前会话未授权，请重新查询能力目录。"
        if capability.permission == "admin" and not event.is_admin():
            return "这个功能只有管理员能用"
        if capability.permission == "private" and not event.is_private_chat():
            return "这个功能只能私聊用"
        if not is_explicit_write_intent(capability, event.get_message_str()):
            action_hint = (
                "取消报名 活动名"
                if capability.capability_id == "action.raid_cancel_signup"
                else "报名 活动名"
            )
            return f"这是写操作，请明确说“{action_hint}”。"

        try:
            provided_arguments = parse_arguments(arguments_json)
        except (ValueError, TypeError) as exc:
            logger.info("[CAPABILITY] invalid args: %s", exc)
            return "参数格式没整对，换个说法我再试哈"
        provided_arguments = enrich_arguments(capability, intent, provided_arguments)
        raid_ids = {
            "info.raid_activity",
            "action.raid_signup",
            "action.raid_cancel_signup",
        }
        use_recent_arguments = bool(
            latest
            and (
                (
                    resolution.reason == "recent_action_followup"
                    and latest.capability_id == capability.capability_id
                )
                or (
                    capability.capability_id in raid_ids
                    and latest.capability_id in raid_ids
                )
            )
        )
        arguments = merge_recent_arguments(
            provided_arguments,
            latest.details if latest else None,
            use_recent=use_recent_arguments,
        )
        missing = missing_arguments(capability, arguments)
        if missing:
            return missing_argument_message(capability, missing)

        logger.info(
            "[CAPABILITY] invoke session=%s capability=%s reason=%s args=%s",
            session,
            capability.capability_id,
            resolution.reason,
            arguments,
        )
        record_details = dict(arguments)
        invocation_success = True
        try:
            if capability.handler is not None:
                result = await capability.handler(arguments)
            else:
                bridged = await self._invoke_plugin_bridge(
                    event,
                    capability.capability_id,
                    arguments,
                )
                result = str(bridged.get("text") or "")
                invocation_success = bool(bridged.get("success", False))
                extra_details = bridged.get("details")
                if isinstance(extra_details, dict):
                    record_details.update(extra_details)
        except Exception as exc:
            logger.exception(
                "[CAPABILITY] invoke failed capability=%s",
                capability.capability_id,
            )
            return f"{capability.name}这哈没调成功：{exc}"

        if isinstance(result, CapabilityMediaResult):
            if not result.text.strip():
                return f"{capability.name}没拿到可用结果"
        elif not str(result or "").strip():
            return f"{capability.name}没拿到可用结果"

        if not invocation_success:
            return str(result)

        if isinstance(result, CapabilityMediaResult):
            chain = []
            if result.image_path:
                chain.append(Comp.Image.fromFileSystem(result.image_path))
            chain.append(Comp.Plain(result.text))
            try:
                await event.send(event.chain_result(chain))
            finally:
                if result.image_path:
                    Path(result.image_path).unlink(missing_ok=True)
            record_action(
                session,
                capability.capability_id,
                capability.name,
                "llm_tool",
                details=record_details,
            )
            return None

        record_action(
            session,
            capability.capability_id,
            capability.name,
            "llm_tool",
            details=record_details,
        )
        return str(result)

    async def _invoke_plugin_bridge(
        self,
        event: AstrMessageEvent,
        capability_id: str,
        arguments: dict,
    ) -> dict:
        if capability_id not in {
            "info.raid_activity",
            "action.raid_signup",
            "action.raid_cancel_signup",
        }:
            raise RuntimeError("能力没有可用的执行器")

        metadata = self.context.get_registered_star("jiang_raid_signup")
        plugin = getattr(metadata, "star_cls", None) if metadata else None
        invoke = getattr(plugin, "invoke_natural_capability", None)
        if not getattr(metadata, "activated", False) or not callable(invoke):
            raise RuntimeError("盛世活动报名插件没有启用")

        action = {
            "info.raid_activity": "list",
            "action.raid_signup": "signup",
            "action.raid_cancel_signup": "cancel",
        }[capability_id]
        response = await invoke(
            event,
            action=action,
            event_title=str(arguments.get("event_title") or ""),
        )
        if not isinstance(response, dict):
            raise RuntimeError("盛世活动报名插件返回格式不正确")
        return response
