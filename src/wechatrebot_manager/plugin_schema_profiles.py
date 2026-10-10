"""Curated customer forms and compatibility policies for delivered plugins."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .group_analysis_settings import REPORT_STYLE_LABELS, group_analysis_ui_schema as _group_analysis_ui_schema
from .plugin_guides import common_schema, get_plugin_guide


@dataclass(frozen=True, slots=True)
class CustomerSchemaProfile:
    sections: tuple[
        tuple[str, str, tuple[tuple[str, dict[str, Any]], ...]], ...
    ]
    note: str
    setup_steps: tuple[str, ...] = ()
    root_fields: tuple[tuple[str, dict[str, Any]], ...] = ()


_GROUP_ANALYSIS_MODULE = "astrbot_plugin_qq_group_daily_analysis"


_SETU_FEATURE_KEY = "jiang_commands.setu"


_SETU_BACKING_MODULE = "jiang_commands"


_GROUP_ANALYSIS_PROFILE = CustomerSchemaProfile(
    sections=(
        (
            "basic",
            "基础设置",
            (
                ("admin_ids", {}),
                ("group_list_mode", {}),
                ("group_list", {}),
                ("bot_self_ids", {}),
                (
                    "report_template",
                    {
                        "description": "报告样式",
                        "hint": "下方为示例数据生成的样式预览，点击可放大。实际内容以群聊分析结果为准。",
                        "_option_labels": REPORT_STYLE_LABELS,
                    },
                ),
                (
                    "analysis_days",
                    {
                        "description": "统计最近几天",
                        "hint": "一般保持 1 天；需要周报时可改为 7 天。",
                        "min": 1,
                        "max": 30,
                    },
                ),
                (
                    "min_messages_threshold",
                    {
                        "description": "最少消息数",
                        "hint": "低于这个数量时跳过报告，建议保持 200。",
                        "min": 1,
                        "max": 10000,
                    },
                ),
            ),
        ),
        (
            "analysis_features",
            "报告内容",
            (
                (
                    "topic_analysis_enabled",
                    {
                        "description": "热门话题",
                        "hint": "整理当天讨论最多的话题。",
                    },
                ),
                (
                    "user_title_analysis_enabled",
                    {
                        "description": "群友画像",
                        "hint": "生成群友称号和简短画像。",
                    },
                ),
                (
                    "golden_quote_analysis_enabled",
                    {
                        "description": "群聊金句",
                        "hint": "挑选有代表性的精彩发言。",
                    },
                ),
                (
                    "chat_quality_analysis_enabled",
                    {
                        "description": "聊天质量点评",
                        "hint": "从多个角度总结当天群聊质量。",
                    },
                ),
            ),
        ),
        (
            "llm",
            "分析模型",
            (
                (
                    "llm_provider_id",
                    {
                        "description": "用于分析的模型",
                        "hint": "与全局模型服务共用模型，无需重复配置。默认跟随聊天模型；群聊有独立模型时优先使用。选择具体模型后，仅本报告固定使用它。",
                        "_blank_label": "跟随默认聊天模型（推荐）",
                        "_required": False,
                    },
                ),
            ),
        ),
    ),
    note=(
        "这里只展示日常会用到的设置。渲染、重试、并发和提示词等专业参数"
        "继续使用推荐值，保存时不会被重置。"
    ),
    setup_steps=(
        "在全局模型服务配置聊天模型，报告默认跟随使用",
        "到定时任务中的“群聊分析自动日报”选择群聊和生成时间",
        "先积累足够消息，再从测试群验证一次完整报告",
    ),
)


def _with_setu_schema(schema: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(schema)
    result.setdefault(
        "setu_recall_enabled",
        {
            "description": "发送后自动撤回",
            "type": "bool",
            "default": True,
            "hint": "开启后，图片会在设定秒数后自动撤回；关闭后保留图片。",
            "_ui_group": "撤回设置",
            "_ui_full": True,
        },
    )
    result.setdefault(
        "setu_recall_seconds",
        {
            "description": "撤回等待时间（秒）",
            "type": "int",
            "default": 20,
            "min": 1,
            "max": 120,
            "hint": "建议 20 秒，可填写 1–120 秒。",
            "_ui_group": "撤回设置",
            "_ui_depends_on": "setu_recall_enabled",
        },
    )
    return result


def _setu_schema(schema: dict[str, Any]) -> dict[str, Any]:
    complete = _with_setu_schema(schema)
    return {
        key: copy.deepcopy(complete[key])
        for key in ("setu_recall_enabled", "setu_recall_seconds")
    }


def _group_analysis_schedule_schema(schema: dict[str, Any]) -> dict[str, Any]:
    section = schema.get("auto_analysis")
    if not isinstance(section, dict) or not isinstance(section.get("items"), dict):
        return {}
    items = section["items"]
    selected: dict[str, Any] = {}
    for key, overrides in (
        (
            "scheduled_group_list",
            {
                "description": "自动日报群",
                "hint": "点击“从群聊列表添加”选择群；多个群一行一个。清空后停止自动生成。",
                "_ui_control": "line-list",
                "_group_selector": True,
                "placeholder": "例如：123456@chatroom",
            },
        ),
        (
            "auto_analysis_time",
            {
                "description": "每天生成时间",
                "hint": "使用 24 小时制；需要多个时间时一行一个。",
                "_ui_control": "time-list",
                "placeholder": "23:00",
            },
        ),
    ):
        field = items.get(key)
        if isinstance(field, dict):
            selected[key] = {**copy.deepcopy(field), **overrides}
    if not selected:
        return {}
    if isinstance(items.get("scheduled_group_list_mode"), dict):
        selected["scheduled_group_list_mode"] = copy.deepcopy(items["scheduled_group_list_mode"])
    result = {
        "auto_analysis": {
            **copy.deepcopy(section),
            "description": "自动日报",
            "items": selected,
        }
    }
    days = schema.get("basic", {}).get("items", {}).get("analysis_days")
    if isinstance(days, dict):
        basic_items = schema["basic"]["items"]
        result["basic"] = {"type": "object", "items": {
            key: copy.deepcopy(basic_items[key])
            for key in ("analysis_days", "group_list_mode", "group_list")
            if key in basic_items
        }}
    return result


def _without_group_analysis_schedule(schema: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(schema)
    result.pop("auto_analysis", None)
    return result


_ANGEL_HEART_MODULE = "astrbot_plugin_angel_heart"


_ANGEL_HEART_PROFILE = CustomerSchemaProfile(
    root_fields=(
        (
            "analyzer_model",
            {
                "description": "群聊分析模型",
                "hint": (
                    "用于判断是否接话和整理群聊上下文。建议选择与日常回复相同的模型；"
                    "不选择时只能处理明确点名，无法主动判断其他群聊消息。"
                ),
                "_blank_label": "请选择模型（完整使用所必需）",
            },
        ),
        (
            "image_caption_provider_id",
            {
                "description": "图片理解模型（可选）",
                "hint": "仅在需要理解历史图片时使用；请选择支持图片输入的模型。",
                "_blank_label": "不启用图片理解",
            },
        ),
    ),
    sections=(
        (
            "personality",
            "身份与接话原则",
            (
                (
                    "ai_self_identity",
                    {
                        "description": "群聊身份",
                        "hint": "说明机器人是谁、擅长什么，供群聊接话判断使用。",
                        "default": "你是小妹，是群聊里的熟人型机器人。说话自然、直接、简短，不使用机械的客服措辞。",
                    },
                ),
                (
                    "reply_strategy_guide",
                    {
                        "description": "接话原则",
                        "hint": "说明哪些话题适合回应，以及什么情况下不要打扰群聊。",
                    },
                ),
            ),
        ),
        (
            "wake_interaction",
            "点名与唤醒",
            (
                (
                    "alias",
                    {
                        "description": "可识别的昵称",
                        "hint": "机器人名称会自动放在最前；其他昵称用 | 分隔，例如：小妹|妹宝。",
                        "default": "小妹",
                    },
                ),
                (
                    "enter_on_mention_only",
                    {
                        "description": "仅在被点名后参与",
                        "hint": "建议开启，避免机器人在尚未被邀请时加入聊天。",
                    },
                ),
                (
                    "direct_wake_fast_path",
                    {
                        "description": "点名时快速回应",
                        "hint": "被点名或引用时跳过额外判断，直接生成回复。",
                    },
                ),
            ),
        ),
        (
            "timing",
            "回复节奏",
            (
                (
                    "assistant_debounce_time",
                    {
                        "description": "点名后等待补充（秒）",
                        "hint": "短暂等待同一位群友补充完整问题，建议保持 1 秒。",
                    },
                ),
                (
                    "waiting_time",
                    {
                        "description": "回复后休息（秒）",
                        "hint": "一次回复后暂停普通巡检，避免连续插话。",
                    },
                ),
                (
                    "observation_timeout",
                    {
                        "description": "持续参与时长（秒）",
                        "hint": "超过该时间后恢复为仅在被点名时参与。",
                    },
                ),
            ),
        ),
        (
            "reply_length",
            "回复长度",
            (
                (
                    "normal_reply_max_chars",
                    {
                        "description": "普通回复参考字数",
                        "hint": "用于提醒模型保持简短，不会强行截断回复。",
                    },
                ),
                (
                    "focus_reply_max_chars",
                    {
                        "description": "分析类回复参考字数",
                        "hint": "分析、总结等问题可以使用更长回复，不能小于普通回复字数。",
                    },
                ),
            ),
        ),
        (
            "access_control",
            "使用范围",
            (
                (
                    "group_chat_enhancement",
                    {
                        "description": "启用群聊上下文协调",
                        "hint": "开启后才会整理多人上下文并协调接话。",
                    },
                ),
            ),
        ),
    ),
    note=(
        "请先在“模型绑定”中选择群聊分析模型，再调整下方常用设置。"
        "机器人在线时，保存后会立即应用。"
        "按群设置、能量、离场应答和上下文压缩等细致配置可在完整配置页中调整。"
    ),
    setup_steps=(
        "先选择群聊分析模型",
        "确认群聊身份、点名方式和回复节奏",
        "保存并查看生效确认；需要按群设置时再打开完整配置页",
    ),
)


_GROUP_NOTICE_MODULE = "jiang_group_notice"


_GROUP_NOTICE_PROFILE = CustomerSchemaProfile(
    root_fields=(
        ("notify_join", {"_ui_group": "要提示的成员事件"}),
        ("notify_leave", {"_ui_group": "要提示的成员事件"}),
        ("notify_profile_change", {"_ui_group": "要提示的成员事件"}),
        ("welcome_title", {"_ui_group": "成员加入"}),
        ("welcome_tail", {"_ui_group": "成员加入"}),
        ("leave_title", {"_ui_group": "成员退出"}),
        ("leave_body", {"_ui_group": "成员退出"}),
        ("profile_change_title", {"_ui_group": "群昵称变化"}),
        ("profile_change_body", {"_ui_group": "群昵称变化"}),
    ),
    sections=(),
    note="设置按成员加入、退出和群昵称变化分区展示；未修改的文案会继续使用推荐值。",
    setup_steps=(
        "先选择需要提示的成员事件",
        "分别检查加入、退出和群昵称变化文案",
        "保存后在测试群触发一次对应事件",
    ),
)


_CUSTOMER_SCHEMA_PROFILES = {
    _GROUP_ANALYSIS_MODULE: _GROUP_ANALYSIS_PROFILE,
    _ANGEL_HEART_MODULE: _ANGEL_HEART_PROFILE,
    _GROUP_NOTICE_MODULE: _GROUP_NOTICE_PROFILE,
}


# Older full installations declare AngelHeart as page-only.  Manager delta
# updates cannot replace legal/feature-set.json, so keep this narrow UI policy
# override until those installations have received a newer full payload.
_MANAGER_SCHEMA_POLICY_OVERRIDES = frozenset({_ANGEL_HEART_MODULE})


# Product-managed pages stay out of the feature center even when an older full
# installation still declares them in legal/feature-set.json.
_MANAGER_HIDDEN_PAGE_MODULES = frozenset({"jiang_short_drama", "jiang_image", "jiang_schedule", _GROUP_ANALYSIS_MODULE})


_MANAGER_NO_CONFIGURATION_MODULES = frozenset({"jiang_short_drama"})


# Page-only features must stay page-only on older installations whose signed
# feature manifest still declares the legacy schema-and-page mode.
_MANAGER_PAGE_ONLY_MODULES = frozenset(
    {"astrbot_plugin_mimo_tts_clone", "astrbot_plugin_twitter"}
)


def _hide_customer_internal_schema(
    module_name: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Remove runtime-only fields from every customer-facing schema view."""
    if module_name != "meme_manager":
        return schema
    visible_schema = copy.deepcopy(schema)
    generation = visible_schema.get("generation")
    generation_items = generation.get("items") if isinstance(generation, dict) else None
    # Emotion tag instructions are an internal runtime contract. Users
    # describe the persona normally; the plugin injects this block only
    # for chats whose policy permits meme_manager.
    if isinstance(generation_items, dict):
        generation_items.pop("prompt", None)
    return visible_schema


def _project_customer_schema(
    module_name: str,
    schema: dict[str, Any],
) -> tuple[dict[str, Any], CustomerSchemaProfile | None]:
    """Project complex engine schemas into a small customer-facing form."""
    schema = _hide_customer_internal_schema(module_name, schema)
    if module_name == _GROUP_ANALYSIS_MODULE:
        schema = _group_analysis_ui_schema(schema)
    profile = _CUSTOMER_SCHEMA_PROFILES.get(module_name)
    if profile is None:
        projected = common_schema(module_name, schema)
        guide = get_plugin_guide(module_name)
        if guide and projected != schema:
            return projected, CustomerSchemaProfile((), "先改常用项即可；专业设置保留原值，可按需展开。", guide["setup_steps"])
        return schema, None

    result: dict[str, Any] = {}
    for field_key, overrides in profile.root_fields:
        raw_field = schema.get(field_key)
        if not isinstance(raw_field, dict) or raw_field.get("invisible") is True:
            continue
        result[field_key] = {
            **copy.deepcopy(raw_field),
            **copy.deepcopy(overrides),
        }
    for section_key, section_title, selected_fields in profile.sections:
        raw_section = schema.get(section_key)
        if not isinstance(raw_section, dict):
            continue
        raw_items = raw_section.get("items")
        if raw_section.get("type") != "object" or not isinstance(raw_items, dict):
            continue
        projected_items: dict[str, Any] = {}
        for field_key, overrides in selected_fields:
            raw_field = raw_items.get(field_key)
            if not isinstance(raw_field, dict) or raw_field.get("invisible") is True:
                continue
            field_overrides = copy.deepcopy(overrides)
            option_labels = field_overrides.pop("_option_labels", None)
            projected_field = {
                **copy.deepcopy(raw_field),
                **field_overrides,
            }
            if isinstance(option_labels, dict) and isinstance(
                projected_field.get("options"), list
            ):
                projected_field["labels"] = [
                    option_labels.get(str(option), str(option))
                    for option in projected_field["options"]
                ]
            projected_items[field_key] = projected_field
        if projected_items:
            result[section_key] = {
                **copy.deepcopy(raw_section),
                "description": section_title,
                "items": projected_items,
            }
    return result, profile
