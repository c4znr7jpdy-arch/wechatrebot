"""低风险自然语言能力的注册、解析与执行。"""

from __future__ import annotations

import json
import importlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .discovery import flatten_commands
from .search import CatalogQuery, match_score as _catalog_match_score, search_text


_PLUGINS_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

import _nonebot_stubs

_nonebot_stubs.setup()

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_AI_PLUGIN_PARENT = _PROJECT_ROOT / "ai_plugin"
if str(_AI_PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(_AI_PLUGIN_PARENT))

import ai_plugin.bilibili_dynamic as _bilibili_dynamic_module
from ai_plugin.epic import fetch_epic_free, format_epic_free
from ai_plugin.kfc import fetch_kfc_text
from ai_plugin.news import fetch_hot_news, format_news
import ai_plugin.oilprice as _oilprice_module

# ai_plugin 是复用模块库，不属于本插件的模块前缀；AstrBot 热更新插件时不会
# 自动清除它的 import 缓存，因此在网关加载时显式刷新相关共享模块。
_bilibili_dynamic_module = importlib.reload(_bilibili_dynamic_module)
fetch_latest_dynamic_message = (
    _bilibili_dynamic_module.fetch_latest_dynamic_message
)
_oilprice_module = importlib.reload(_oilprice_module)
fetch_oilprice = _oilprice_module.fetch_oilprice
format_oilprice = _oilprice_module.format_oilprice
from ai_plugin.weather import fetch_weather, format_weather


@dataclass(frozen=True)
class CapabilityMediaResult:
    text: str
    image_path: str | None = None


CapabilityHandler = Callable[
    [dict[str, Any]],
    Awaitable[str | CapabilityMediaResult],
]


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    name: str
    description: str
    aliases: tuple[str, ...]
    required_arguments: tuple[str, ...]
    argument_hints: dict[str, str]
    permission: str
    side_effect: str
    delivery: str
    handler: CapabilityHandler | None


@dataclass(frozen=True)
class Resolution:
    capability: CapabilitySpec | None
    reason: str
    clarification: str = ""


async def _run_kfc(_: dict[str, Any]) -> str:
    return await fetch_kfc_text()


async def _run_news(arguments: dict[str, Any]) -> str:
    try:
        count = int(arguments.get("count", 20))
    except (TypeError, ValueError):
        count = 20
    items = await fetch_hot_news(max(5, min(count, 20)))
    return format_news(items)


async def _run_weather(arguments: dict[str, Any]) -> str:
    city = str(arguments.get("city", "")).strip()
    data = await fetch_weather(city)
    return format_weather(data)


async def _run_epic(_: dict[str, Any]) -> str:
    games = await fetch_epic_free()
    return format_epic_free(games)


async def _run_oilprice(arguments: dict[str, Any]) -> str:
    province = str(arguments.get("province", "")).strip()
    data = await fetch_oilprice(province)
    return format_oilprice(data)


async def _run_bili_dynamic(
    arguments: dict[str, Any],
) -> CapabilityMediaResult:
    uid = str(arguments.get("uid", "")).strip()
    text, image_path = await fetch_latest_dynamic_message(
        uid,
        title=f"B站用户 {uid}",
    )
    return CapabilityMediaResult(text=text, image_path=image_path)


CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        capability_id="content.kfc",
        name="疯狂星期四文案",
        description="获取并直接发送一条新的 KFC 疯狂星期四文案",
        aliases=("kfc", "肯德基", "疯狂星期四", "疯四", "v我50", "转我50"),
        required_arguments=(),
        argument_hints={},
        permission="public",
        side_effect="send_content",
        delivery="direct",
        handler=_run_kfc,
    ),
    CapabilitySpec(
        capability_id="info.news",
        name="热点新闻",
        description="查询并直接发送当前热点新闻",
        aliases=("新闻", "热点", "热搜", "今日热点", "热点新闻"),
        required_arguments=(),
        argument_hints={"count": "条数，5 至 20"},
        permission="public",
        side_effect="read",
        delivery="direct",
        handler=_run_news,
    ),
    CapabilitySpec(
        capability_id="info.weather",
        name="天气预报",
        description="查询指定城市的天气预报",
        aliases=("天气", "天气预报", "气温", "下雨", "带伞"),
        required_arguments=("city",),
        argument_hints={"city": "城市名，例如成都、绵阳"},
        permission="public",
        side_effect="read",
        delivery="direct",
        handler=_run_weather,
    ),
    CapabilitySpec(
        capability_id="info.epic",
        name="Epic 喜加一",
        description="查询 Epic 当前可免费领取的游戏",
        aliases=("epic", "喜加一", "免费游戏", "epic免费"),
        required_arguments=(),
        argument_hints={},
        permission="public",
        side_effect="read",
        delivery="direct",
        handler=_run_epic,
    ),
    CapabilitySpec(
        capability_id="info.oilprice",
        name="今日油价",
        description="查询指定省份的今日油价",
        aliases=("油价", "汽油价格", "柴油价格"),
        required_arguments=("province",),
        argument_hints={"province": "省份名，例如四川、北京"},
        permission="public",
        side_effect="read",
        delivery="direct",
        handler=_run_oilprice,
    ),
    CapabilitySpec(
        capability_id="info.bili_dynamic",
        name="B站用户动态",
        description="查询指定 B站 UID 的最新动态",
        aliases=("b站动态", "哔哩哔哩动态", "bilibili动态", "用户动态"),
        required_arguments=("uid",),
        argument_hints={
            "uid": "B站数字 UID",
        },
        permission="public",
        side_effect="read",
        delivery="direct",
        handler=_run_bili_dynamic,
    ),
    CapabilitySpec(
        capability_id="info.raid_activity",
        name="盛世报名活动",
        description="查询当前最近可报名的盛世活动和活动日历",
        aliases=("报名活动", "可报名活动", "活动日历", "最近活动", "有什么活动", "活动列表"),
        required_arguments=(),
        argument_hints={},
        permission="public",
        side_effect="read",
        delivery="plugin_bridge",
        handler=None,
    ),
    CapabilitySpec(
        capability_id="action.raid_signup",
        name="盛世活动报名",
        description="使用当前群备注报名指定的盛世活动",
        aliases=("我要报名", "我想报名", "帮我报名", "给我报名", "报名参加", "报上", "报名"),
        required_arguments=("event_title",),
        argument_hints={"event_title": "活动名称，例如双10"},
        permission="public",
        side_effect="write",
        delivery="plugin_bridge",
        handler=None,
    ),
    CapabilitySpec(
        capability_id="action.raid_cancel_signup",
        name="取消盛世活动报名",
        description="取消当前用户在指定盛世活动中的报名",
        aliases=("取消报名", "撤销报名", "退出活动"),
        required_arguments=("event_title",),
        argument_hints={"event_title": "活动名称，例如双10"},
        permission="public",
        side_effect="write",
        delivery="plugin_bridge",
        handler=None,
    ),
)

CAPABILITY_BY_ID = {item.capability_id: item for item in CAPABILITIES}

_FOLLOWUP_MARKERS = (
    "再来",
    "再整",
    "再发",
    "继续",
    "换一个",
    "换一条",
    "多来",
    "还要",
    "不够",
    "再查",
    "再看",
)


def normalize_intent(text: str) -> str:
    cleaned = str(text or "").strip().lower()
    if cleaned.startswith("[系统身份提示：") and "]\n" in cleaned:
        cleaned = cleaned.split("]\n", 1)[1]
    cleaned = re.sub(r"\[at:[^\]]+\]", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"@[\w\-\u4e00-\u9fff]+", " ", cleaned)
    return re.sub(r"\s+", "", cleaned)


def is_followup_intent(text: str) -> bool:
    normalized = normalize_intent(text)
    return any(marker in normalized for marker in _FOLLOWUP_MARKERS)


def is_explicit_write_intent(capability: CapabilitySpec, intent: str) -> bool:
    """写操作必须由用户当前消息明确授权，不能只靠模型选择能力 ID。"""
    if capability.side_effect != "write":
        return True
    normalized = normalize_intent(intent)
    if any(marker in normalized for marker in ("不要报名", "别报名", "先别", "暂时别")):
        return False
    if any(marker in normalized for marker in ("怎么", "如何", "能不能", "可不可以", "有哪些", "有什么")):
        return False
    if normalized.endswith(("吗", "么", "？", "?")):
        return False
    if capability.capability_id == "action.raid_signup":
        return any(marker in normalized for marker in ("报名", "报上"))
    if capability.capability_id == "action.raid_cancel_signup":
        return any(marker in normalized for marker in ("取消报名", "撤销报名", "退出活动"))
    return False


def _explicit_matches(text: str) -> list[tuple[int, CapabilitySpec]]:
    normalized = normalize_intent(text)
    matches: list[tuple[int, CapabilitySpec]] = []
    for capability in CAPABILITIES:
        matched_aliases = [
            alias for alias in capability.aliases if alias.lower() in normalized
        ]
        if matched_aliases:
            matches.append((max(len(alias) for alias in matched_aliases), capability))
    return sorted(matches, key=lambda item: item[0], reverse=True)


def resolve_capability(
    intent: str,
    requested_id: str = "",
    recent_capability_id: str = "",
) -> Resolution:
    matches = _explicit_matches(intent)
    if matches:
        best_score = matches[0][0]
        best = [capability for score, capability in matches if score == best_score]
        if len(best) == 1:
            return Resolution(best[0], "explicit_alias")
        names = "、".join(item.name for item in best)
        return Resolution(None, "ambiguous", f"你想用的是{names}中的哪一个？")

    if is_followup_intent(intent) and recent_capability_id in CAPABILITY_BY_ID:
        return Resolution(
            CAPABILITY_BY_ID[recent_capability_id],
            "recent_action_followup",
        )

    requested_id = str(requested_id or "").strip()
    if requested_id in CAPABILITY_BY_ID:
        return Resolution(CAPABILITY_BY_ID[requested_id], "requested_id")

    normalized = normalize_intent(intent)
    if "文案" in normalized:
        return Resolution(
            None,
            "ambiguous_copywriting",
            "你要普通文案还是疯狂星期四文案？",
        )
    return Resolution(
        None,
        "not_found",
        "我没匹配到要调用的功能，你说具体点要查啥子嘛",
    )


def parse_arguments(arguments_json: str) -> dict[str, Any]:
    raw = str(arguments_json or "").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("arguments_json 必须是 JSON 对象")
    return data


def _clean_location(value: str) -> str:
    cleaned = normalize_intent(value)
    for prefix in ("帮我查一下", "帮我查", "查一下", "查询", "看看", "看下", "查"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    for suffix in ("今天", "明天", "后天", "未来", "这几天"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned.removesuffix("市").removesuffix("省")


def enrich_arguments(
    capability: CapabilitySpec,
    intent: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(arguments)
    if capability.capability_id == "info.weather" and not enriched.get("city"):
        match = re.search(r"([\u4e00-\u9fff]{2,10})(?:市)?天气", normalize_intent(intent))
        if match:
            enriched["city"] = _clean_location(match.group(1))
    if capability.capability_id == "info.oilprice" and not enriched.get("province"):
        normalized = normalize_intent(intent)
        match = re.search(r"([\u4e00-\u9fff]{1,10})(?:省)?油价", normalized)
        if match:
            enriched["province"] = _clean_location(match.group(1))
        else:
            match = re.search(r"油价([\u4e00-\u9fff]{1,10})", normalized)
            if match:
                enriched["province"] = _clean_location(match.group(1))
    if capability.capability_id == "info.bili_dynamic" and not enriched.get("uid"):
        match = re.search(r"(?:b站|哔哩哔哩|bilibili)?(?:用户)?动态[^0-9]*(\d{5,20})", normalize_intent(intent))
        if match:
            enriched["uid"] = match.group(1)
    if capability.capability_id == "action.raid_signup" and not enriched.get("event_title"):
        normalized = normalize_intent(intent)
        match = re.search(
            r"(?:我要|我想|帮我|给我|替我|麻烦)?(?:报名参加|报名|报上)(?:一下)?(.+)",
            normalized,
        )
        if match:
            enriched["event_title"] = match.group(1).strip("，。！？!? ")
    if (
        capability.capability_id == "action.raid_cancel_signup"
        and not enriched.get("event_title")
    ):
        normalized = normalize_intent(intent)
        match = re.search(r"(?:取消报名|撤销报名|退出活动)(?:一下)?(.+)", normalized)
        if match:
            enriched["event_title"] = match.group(1).strip("，。！？!? ")
    return enriched


def merge_recent_arguments(
    provided: dict[str, Any],
    recent_details: dict[str, Any] | None,
    *,
    use_recent: bool,
) -> dict[str, Any]:
    merged = dict(recent_details or {}) if use_recent else {}
    merged.update(provided)
    return merged


def missing_arguments(
    capability: CapabilitySpec,
    arguments: dict[str, Any],
) -> list[str]:
    return [
        name
        for name in capability.required_arguments
        if not str(arguments.get(name, "")).strip()
    ]


def missing_argument_message(
    capability: CapabilitySpec,
    missing: list[str],
) -> str:
    labels = [capability.argument_hints.get(name, name) for name in missing]
    return f"要用{capability.name}还差参数：{'；'.join(labels)}"


def _command_search_text(
    command: dict[str, Any],
    plugin_description: str = "",
) -> str:
    return " ".join((search_text(command), plugin_description))


def _clean_command_description(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > 180:
        return text[:177] + "..."
    return text


def catalog_text(
    query: str = "",
    commands: list[dict[str, Any]] | None = None,
    plugin_descriptions: dict[str, str] | None = None,
    capabilities=None,
    empty_message: bool = True,
) -> str:
    """Build the model-facing catalog for natural and command-only abilities."""
    plugin_descriptions = plugin_descriptions or {}
    normalized_query = normalize_intent(query)

    selected_capabilities = CatalogQuery(query).rank(
        CAPABILITIES if capabilities is None else capabilities,
        lambda item: " ".join((item.name, *item.aliases)),
        lambda item: item.description,
    )

    command_groups: dict[str, list[dict[str, Any]]] = {}
    for command in flatten_commands(commands or []):
        if not command.get("enabled", True) or command.get("reserved", False):
            continue
        effective_command = str(command.get("effective_command") or "").strip()
        if not effective_command:
            continue
        plugin_name = str(command.get("plugin") or "未知插件")
        command_groups.setdefault(plugin_name, []).append(command)

    selected_command_groups: dict[str, list[dict[str, Any]]] = {}
    for plugin_name, plugin_commands in command_groups.items():
        plugin_description = plugin_descriptions.get(plugin_name, "")
        group_search_text = " ".join(
            _command_search_text(command, plugin_description)
            for command in plugin_commands
        )
        if normalized_query and _catalog_match_score(query, group_search_text) < 2:
            continue

        if normalized_query:
            matched_commands = [
                command
                for command in plugin_commands
                if _catalog_match_score(
                    query,
                    _command_search_text(command, plugin_description),
                )
                >= 2
            ]
            if not matched_commands:
                matched_commands = plugin_commands
        else:
            matched_commands = plugin_commands
        selected_command_groups[plugin_name] = matched_commands

    lines: list[str] = []
    if selected_capabilities:
        lines.append("可由 AI 直接自然调用的能力：")
        for capability in selected_capabilities:
            args = ""
            if capability.required_arguments:
                args = "；需要" + "、".join(
                    capability.argument_hints.get(name, name)
                    for name in capability.required_arguments
                )
            lines.append(
                f"- {capability.name} ({capability.capability_id})："
                f"{capability.description}{args}"
            )

    if selected_command_groups:
        if lines:
            lines.append("")
        lines.append(
            "命令型插件能力（需要用户发送对应 /命令 触发；AI 可以解释用法，"
            "但不要声称已经代替用户执行）："
        )
        for plugin_name, plugin_commands in selected_command_groups.items():
            display_name = next(
                (
                    str(command.get("plugin_display_name") or "").strip()
                    for command in plugin_commands
                    if str(command.get("plugin_display_name") or "").strip()
                ),
                plugin_name,
            )
            plugin_description = _clean_command_description(
                plugin_descriptions.get(plugin_name, "")
            )
            heading = f"- {display_name} [{plugin_name}]"
            if plugin_description:
                heading += f"：{plugin_description}"
            lines.append(heading)
            for command in plugin_commands:
                effective_command = str(command.get("effective_command") or "").strip()
                description = _clean_command_description(command.get("description"))
                permission = str(command.get("permission") or "everyone")
                permission_note = "；仅管理员" if permission == "admin" else ""
                detail = f" — {description}" if description else ""
                lines.append(f"  - /{effective_command}{permission_note}{detail}")

    if not lines:
        if not empty_message:
            return ""
        return (
            f"当前已注册的自然能力和命令型插件中，没有匹配“{query}”的项目。"
            "可以换用插件名、功能名或命令关键词再查。"
        )

    lines.append("")
    lines.append(
        "说明：以上命令目录来自 AstrBot 当前运行时的实际注册结果，会自动反映插件启停、"
        "命令改名和权限；音乐播放、联网搜索及 API 聚合还可能由专用 LLM 工具提供。"
    )
    return "\n".join(lines)
