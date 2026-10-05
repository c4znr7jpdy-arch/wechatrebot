"""Customer-facing group report settings; runtime keys remain unchanged."""
from __future__ import annotations

import copy
import json
from typing import Any


REPORT_STYLE_LABELS = {
    "ATRI": "亚托莉", "BlueArchive": "蔚蓝档案", "scrapbook": "手账风",
    "retro_futurism": "复古未来", "HatsuneMiku": "初音未来", "hack": "极客终端",
    "spring_festival": "新春风", "simple": "简洁风", "art_nouveau": "新艺术画报",
    "deepseek_maid": "鲸鱼女仆", "format": "通用排版", "姜小妹": "小妹主题",
}

_GROUP_HINT = "从群聊列表选择，或在“群聊管理”复制群标识粘贴到这里，例如 123456@chatroom；每行一个，不要填写群昵称。"
_GROUP_FIELD = {
    "_ui_control": "line-list", "_group_selector": True, "_ui_tint": True,
    "placeholder": "例如：123456@chatroom\n987654@chatroom", "hint": _GROUP_HINT,
}


def _decorate_lists(schema: dict[str, Any]) -> None:
    for field in schema.values():
        if not isinstance(field, dict):
            continue
        if field.get("type") == "object":
            _decorate_lists(field.get("items", {}))
        if field.get("type") not in {"list", "template_list"}:
            continue
        field["_ui_tint"] = True
        if field.get("type") == "list" and field.get("items", {}).get("type") == "string" and not field.get("options"):
            field.setdefault("_ui_control", "line-list")
            field.setdefault("placeholder", "每行填写一项；不需要时留空")
        elif field.get("type") == "template_list":
            templates = field.get("templates", {})
            key = next(iter(templates), "")
            sample = {"__template_key": key} if key else {}
            items = templates.get(key, {}).get("items", {})
            if "name" in items:
                sample["name"] = "我的方案"
            if "enable" in items:
                sample["enable"] = True
            field["placeholder"] = "可选，留空使用默认设置。格式示例：\n" + json.dumps([sample], ensure_ascii=False, indent=2)


def group_analysis_ui_schema(schema: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(schema)
    _decorate_lists(result)
    overrides = {
        "basic": {
            "admin_ids": {"description": "群聊分析管理员", "placeholder": "例如：wxid_admin", "hint": "每行填写一个微信用户标识，例如 wxid_admin；不要填写群标识或昵称。仅授予群聊分析和设置指令权限。留空时仅机器人管理员可用。"},
            "group_list_mode": {"labels": ["白名单：仅允许列表中的群", "黑名单：屏蔽列表中的群", "不限制群聊"], "hint": "白名单为空时没有群可用；黑名单为空时不屏蔽群；不限制时忽略名单。此项不会授予管理员权限。"},
            "group_list": _GROUP_FIELD,
            "bot_self_ids": {"placeholder": "例如：wxid_robot\nwxid_member", "hint": "每行填写一个不参与统计的微信用户标识。不要填写群标识或昵称；留空表示不额外屏蔽用户。这里不会授予管理员权限。"},
            "report_template": {"description": "报告样式", "hint": "下方为示例数据生成的样式预览，点击可放大。实际内容以群聊分析结果为准。", "_ui_full": True},
        },
        "auto_analysis": {
            "scheduled_group_list_mode": {"labels": ["沿用群聊使用范围", "白名单：仅指定群", "黑名单：排除指定群"], "hint": "目标群还须通过基础使用范围；定时白名单为空时不生成日报。"},
            "scheduled_group_list": _GROUP_FIELD,
        },
        "incremental": {
            "incremental_group_list_mode": {"labels": ["沿用定时群范围", "白名单：仅指定群", "黑名单：排除指定群"], "hint": "目标群还须通过基础使用范围和定时名单；白名单为空时不启用。"},
            "incremental_group_list": _GROUP_FIELD,
        },
        "daily_comic": {
            "comic_group_list_mode": {"labels": ["沿用群聊使用范围", "白名单：仅指定群", "黑名单：排除指定群"], "hint": "沿用时无需重复填写群；黑名单中的群不生成漫画；白名单只对指定群生成，空白名单不生成。"},
            "comic_group_list": _GROUP_FIELD,
            "drawing_backend": {"labels": ["内置绘图", "通用绘图功能", "香蕉绘图功能"], "hint": "默认使用内置绘图。选用其他绘图功能前，需先在功能中心完成对应配置；不可用时可按下方开关回退到内置绘图。"},
            "drawing_output_exception_retry_keywords": {"placeholder": "例如：图片为空\n生成失败"},
        },
        "html": {"html_output_dir": {"hint": "生成的网页报告和分析数据的保存目录；留空自动保存到本机的群聊分析数据目录。"}},
    }
    for section_key, fields in overrides.items():
        items = result.get(section_key, {}).get("items", {})
        for key, changes in fields.items():
            if isinstance(items.get(key), dict):
                items[key].update(copy.deepcopy(changes))
    style = result.get("basic", {}).get("items", {}).get("report_template")
    if isinstance(style, dict):
        options = style.get("options", [])
        style["labels"] = [REPORT_STYLE_LABELS.get(name, name) for name in options]
        style["_report_previews"] = {name: f"/assets/report-preview-{name}.webp" for name in options if name in REPORT_STYLE_LABELS and name != "姜小妹"}
    for key, field in result.get("llm", {}).get("items", {}).items():
        if key.endswith("provider_id"):
            primary = key == "llm_provider_id"
            label = "跟随默认聊天模型（推荐）" if primary else "跟随报告分析模型"
            field.update({"_required": False, "_blank_label": label})
            field["description"] = "报告分析模型" if primary else field.get("description", key).replace("Provider ID", "模型").replace("LLM", "")
            field["hint"] = ("与全局模型服务共用模型，无需重复配置。默认跟随聊天模型；群聊有独立模型时优先使用。选择具体模型后，仅本报告固定使用它。" if primary else "留空使用报告分析模型；仅在需要时为这项分析指定不同模型。")
            if field.get("options", [None])[0] == "" and field.get("labels"):
                field["labels"][0] = label
    return result
