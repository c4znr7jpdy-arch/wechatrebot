from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from .customer_config import PluginEntry, load_plugins, load_robot_settings, set_plugin_enabled
from .feature_policy import DeliveryFeature, load_delivery_features
from .json_io import write_json_atomic
from .plugin_guides import advanced_schema, common_schema, get_plugin_guide
from .provider import (
    OpenAIProviderProfile,
    PROVIDER_REQUEST_MAX_ATTEMPTS,
    provider_display_name,
)
from .single_instance import SingleInstanceLock


_PUBLIC_PLUGIN_ID = re.compile(r"^feature-[a-f0-9]{16}$")
_PAGE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_PLUGIN_EXTENSION_SEGMENT = re.compile(r"^[A-Za-z0-9_.~-]+$")
_PROXY_PREFIXES = (
    "/api/plugin/page/",
    "/api/plug/",
)
_MAX_BODY_BYTES = 4 * 1024 * 1024
_CONFIG_WRITE_LOCK = threading.RLock()
_SECRET_FIELD = re.compile(
    r"(?:api.?key|access.?key|authorization|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)


class PluginCenterError(RuntimeError):
    status_code = HTTPStatus.BAD_REQUEST


class EngineUnavailableError(PluginCenterError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE


class ConfigConflictError(PluginCenterError):
    status_code = HTTPStatus.CONFLICT


@dataclass(frozen=True, slots=True)
class PluginCatalogItem:
    module_name: str
    display_name: str
    enabled: bool
    configurable: bool
    field_count: int
    model_field_count: int
    pages: tuple[str, ...]
    configuration: str = "schema-and-page"
    description: str = ""
    simplified: bool = False


@dataclass(frozen=True, slots=True)
class CustomerSchemaProfile:
    sections: tuple[
        tuple[str, str, tuple[tuple[str, dict[str, Any]], ...]], ...
    ]
    note: str
    setup_steps: tuple[str, ...] = ()


_GROUP_ANALYSIS_MODULE = "astrbot_plugin_qq_group_daily_analysis"
_GROUP_ANALYSIS_PROFILE = CustomerSchemaProfile(
    sections=(
        (
            "basic",
            "报告设置",
            (
                (
                    "report_template",
                    {
                        "description": "报告样式",
                        "hint": "“手账风”就是示例截图中的长图样式。",
                        "_option_labels": {
                            "scrapbook": "手账风（示例样式）",
                            "姜小妹": "姜小妹主题",
                            "simple": "简洁风",
                            "spring_festival": "新春风",
                            "BlueArchive": "蔚蓝档案",
                            "HatsuneMiku": "初音未来",
                            "retro_futurism": "复古未来",
                            "hack": "极客终端",
                            "format": "通用排版",
                        },
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
            "auto_analysis",
            "自动日报",
            (
                (
                    "scheduled_group_list",
                    {
                        "description": "自动日报群",
                        "hint": "点击“从群聊列表添加”选择群；多个群一行一个。留空时不自动发送。",
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
                        "hint": "通常选择与智能回复相同的模型；不选择时使用当前群聊的默认模型。",
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
        "在“自动日报群”旁点击“从群聊列表添加”，无需在群里发送命令",
        "选择需要生成日报的群；多个群可以继续添加",
        "确认生成时间和分析模型后保存",
    ),
)
_CUSTOMER_SCHEMA_PROFILES = {
    _GROUP_ANALYSIS_MODULE: _GROUP_ANALYSIS_PROFILE,
}


def _project_customer_schema(
    module_name: str,
    schema: dict[str, Any],
) -> tuple[dict[str, Any], CustomerSchemaProfile | None]:
    """Project complex engine schemas into a small customer-facing form."""
    profile = _CUSTOMER_SCHEMA_PROFILES.get(module_name)
    if profile is None:
        projected = common_schema(module_name, schema)
        guide = get_plugin_guide(module_name)
        if guide and projected != schema:
            return projected, CustomerSchemaProfile((), "先改常用项即可；专业设置保留原值，可按需展开。", guide["setup_steps"])
        return schema, None

    result: dict[str, Any] = {}
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


def _public_plugin_id(module_name: str) -> str:
    digest = hashlib.sha256(module_name.encode("utf-8")).hexdigest()[:16]
    return f"feature-{digest}"


def _normalize_plugin_extension_endpoint(endpoint: str) -> str:
    value = str(endpoint or "").strip().strip("/")
    parts = value.split("/") if value else []
    if not parts or any(
        part in {"", ".", ".."} or not _PLUGIN_EXTENSION_SEGMENT.fullmatch(part)
        for part in parts
    ):
        raise PluginCenterError("高级配置页请求无效。")
    return "/".join(parts)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginCenterError("配置文件无法读取，请检查文件是否完整。") from exc
    if not isinstance(value, dict):
        raise PluginCenterError("配置文件格式不正确，应为 JSON 对象。")
    return value


def _json_revision(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _field_count(schema: dict[str, Any]) -> int:
    count = 0
    for item in schema.values():
        if not isinstance(item, dict) or item.get("invisible") is True:
            continue
        if item.get("type") == "object" and isinstance(item.get("items"), dict):
            count += _field_count(item["items"])
        else:
            count += 1
    return count


def _model_field_kind(key: str, field: dict[str, Any]) -> str | None:
    special = str(field.get("_special") or "").casefold()
    provider_type = str(field.get("_provider_type") or "").casefold()
    normalized = key.casefold()
    if special == "select_persona":
        return "persona"
    if (
        special == "select_embedding_provider"
        or provider_type == "embedding"
        or "embedding" in normalized
    ):
        return "embedding"
    if provider_type == "rerank" or "rerank" in normalized:
        return "rerank"
    if special == "select_provider" or normalized.endswith(
        ("provider_id", "provider_ids", "_provider", "_providers")
    ):
        return "chat"
    if "persona" in normalized and normalized.endswith(("_id", "_name")):
        return "persona"
    return None


def _split_schema(
    schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    basic: dict[str, Any] = {}
    models: dict[str, Any] = {}
    for key, field in schema.items():
        if not isinstance(field, dict) or field.get("invisible") is True:
            continue
        if field.get("type") == "object" and isinstance(field.get("items"), dict):
            basic_items, model_items = _split_schema(field["items"])
            if basic_items:
                basic[key] = {**field, "items": basic_items}
            if model_items:
                models[key] = {**field, "items": model_items}
        elif _model_field_kind(str(key), field):
            models[key] = field
        else:
            basic[key] = field
    return basic, models


def _merge_defaults(schema: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    for key, field in schema.items():
        if not isinstance(field, dict):
            continue
        field_type = field.get("type")
        if field_type == "object" and isinstance(field.get("items"), dict):
            current = result.get(key)
            result[key] = _merge_defaults(
                field["items"], current if isinstance(current, dict) else {}
            )
        elif key not in result and "default" in field:
            result[key] = field["default"]
    return result


def _normalize_value(field: dict[str, Any], value: Any) -> Any:
    if value is None:
        return None
    field_type = str(field.get("type") or "string")
    if field_type in {"string", "text"}:
        if isinstance(value, (str, int, float, bool)):
            return str(value).lower() if isinstance(value, bool) else str(value)
        return value
    if field_type == "int" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    if field_type == "float" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if field_type == "bool" and isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        return value
    if field_type == "object" and isinstance(value, dict):
        items = field.get("items")
        return _normalize_config(items, value) if isinstance(items, dict) else dict(value)
    if field_type == "list" and isinstance(value, list):
        item_schema = field.get("items")
        if isinstance(item_schema, dict) and item_schema.get("type"):
            return [_normalize_value(item_schema, item) for item in value]
        return list(value)
    if field_type == "template_list" and isinstance(value, list):
        templates = field.get("templates")
        result = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(templates, dict):
                result.append(item)
                continue
            template = templates.get(item.get("__template_key"))
            template_items = template.get("items") if isinstance(template, dict) else None
            result.append(
                _normalize_config(template_items, item)
                if isinstance(template_items, dict)
                else dict(item)
            )
        return result
    return value


def _normalize_config(
    schema: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    result = dict(config)
    for key, field in schema.items():
        if isinstance(field, dict) and key in result:
            result[key] = _normalize_value(field, result[key])
    return result


def _white_label_schema(value: Any) -> Any:
    """Clean customer-facing schema copy without changing functional field keys."""
    if isinstance(value, dict):
        return {str(key): _white_label_schema(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_white_label_schema(item) for item in value]
    if isinstance(value, str):
        result = re.sub(r"(?i)astrbot", "回复引擎", value)
        result = re.sub(r"(?i)new[-_ ]?hook", "微信连接", result)
        result = re.sub(r"(?i)hook", "微信连接", result)
        result = re.sub(r"(?i)napcat", "微信连接", result)
        result = re.sub(r"(?i)webui\s*dashboard", "高级管理页", result)
        result = re.sub(r"(?i)webui|dashboard", "管理页", result)
        result = result.replace("QQ群", "微信群").replace("QQ号", "微信账号")
        result = re.sub(r"(?i)qq", "微信", result)
        return re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", result).strip()
    return value


def _is_secret_field(key: str, field: dict[str, Any]) -> bool:
    return bool(
        field.get("secret")
        or field.get("_secret")
        or _SECRET_FIELD.search(key)
    )


def _customer_schema(
    schema: dict[str, Any],
    stored: dict[str, Any],
) -> dict[str, Any]:
    """Return only fields customers may edit, with secrets represented safely."""
    result: dict[str, Any] = {}
    for key, raw_field in schema.items():
        if not isinstance(raw_field, dict) or raw_field.get("invisible") is True:
            continue
        field = copy.deepcopy(raw_field)
        current = stored.get(key)
        if field.get("type") == "list" and not field.get("options") and key in {
            "whitelist", "blacklist", "session_list", "scheduled_push_groups",
            "allowed_group_ids", "alarm_target_sessions", "whitelist_users",
            "auto_tts_group_whitelist",
        }:
            field["_ui_control"] = "line-list"
            field["hint"] = "每行填写一个标识；群聊可在“群聊管理”复制群标识，私人会话请按本功能说明填写。留空的作用请按本功能说明确认。"
        if field.get("type") == "object" and isinstance(field.get("items"), dict):
            nested_stored = current if isinstance(current, dict) else {}
            field["items"] = _customer_schema(field["items"], nested_stored)
            if not field["items"]:
                continue
        elif field.get("type") == "template_list" and isinstance(
            field.get("templates"), dict
        ):
            values = current if isinstance(current, list) else []
            for template_key, template in field["templates"].items():
                if not isinstance(template, dict) or not isinstance(
                    template.get("items"), dict
                ):
                    continue
                samples = [
                    item
                    for item in values
                    if isinstance(item, dict)
                    and item.get("__template_key") == template_key
                ]
                combined: dict[str, Any] = {}
                for sample in samples:
                    for sample_key, sample_value in sample.items():
                        if sample_key not in combined or (
                            not combined[sample_key] and sample_value
                        ):
                            combined[sample_key] = sample_value
                template["items"] = _customer_schema(
                    template["items"], combined
                )
        if _is_secret_field(str(key), field):
            field["secret"] = True
            field["_secret_configured"] = bool(current)
            field["default"] = ""
        result[str(key)] = field
    return _white_label_schema(result)


def _customer_config(
    schema: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Return editable values only and never send stored credentials to the UI."""
    result: dict[str, Any] = {}
    for key, field in schema.items():
        if not isinstance(field, dict) or field.get("invisible") is True:
            continue
        value = config.get(key)
        if field.get("type") == "object" and isinstance(field.get("items"), dict):
            nested = _customer_config(
                field["items"], value if isinstance(value, dict) else {}
            )
            if nested:
                result[key] = nested
        elif field.get("type") == "template_list" and isinstance(
            field.get("templates"), dict
        ):
            rows: list[dict[str, Any]] = []
            for index, item in enumerate(value if isinstance(value, list) else []):
                if not isinstance(item, dict):
                    continue
                template_key = str(item.get("__template_key") or "")
                template = field["templates"].get(template_key)
                clean = (
                    _customer_config(template["items"], item)
                    if isinstance(template, dict)
                    and isinstance(template.get("items"), dict)
                    else {}
                )
                rows.append(
                    {
                        "__template_key": template_key,
                        "__manager_index": index,
                        **clean,
                    }
                )
            result[key] = rows
        elif _is_secret_field(str(key), field):
            result[key] = ""
        elif key in config:
            result[key] = value
    return result


def _merge_customer_config(
    schema: dict[str, Any],
    submitted: dict[str, Any],
    stored: dict[str, Any],
) -> dict[str, Any]:
    """Merge a redacted customer form without losing hidden or unknown settings."""
    result = copy.deepcopy(stored)
    for key, field in schema.items():
        if not isinstance(field, dict):
            continue
        if field.get("invisible") is True:
            if key not in result and "default" in field:
                result[key] = copy.deepcopy(field["default"])
            continue
        if field.get("type") == "object" and isinstance(field.get("items"), dict):
            result[key] = _merge_customer_config(
                field["items"],
                submitted.get(key) if isinstance(submitted.get(key), dict) else {},
                stored.get(key) if isinstance(stored.get(key), dict) else {},
            )
            continue
        if field.get("type") == "template_list" and isinstance(
            field.get("templates"), dict
        ):
            if key not in submitted:
                continue
            submitted_rows = submitted.get(key)
            stored_rows = stored.get(key)
            if not isinstance(submitted_rows, list):
                result[key] = submitted_rows
                continue
            existing = stored_rows if isinstance(stored_rows, list) else []
            merged_rows: list[Any] = []
            for row in submitted_rows:
                if not isinstance(row, dict):
                    merged_rows.append(row)
                    continue
                template_key = str(row.get("__template_key") or "")
                template = field["templates"].get(template_key)
                try:
                    manager_index = int(row.get("__manager_index", -1))
                except (TypeError, ValueError):
                    manager_index = -1
                previous = (
                    existing[manager_index]
                    if 0 <= manager_index < len(existing)
                    and isinstance(existing[manager_index], dict)
                    and existing[manager_index].get("__template_key") == template_key
                    else {}
                )
                if not isinstance(template, dict) or not isinstance(
                    template.get("items"), dict
                ):
                    if previous:
                        merged_rows.append(copy.deepcopy(previous))
                    continue
                clean_row = {
                    child_key: child_value
                    for child_key, child_value in row.items()
                    if child_key not in {"__template_key", "__manager_index"}
                }
                merged_row = _merge_customer_config(
                    template["items"], clean_row, previous
                )
                merged_rows.append(
                    {"__template_key": template_key, **merged_row}
                )
            result[key] = merged_rows
            continue
        if _is_secret_field(str(key), field):
            if key in submitted and submitted[key] is None:
                result[key] = ""
            elif key in submitted and str(submitted[key]).strip():
                result[key] = submitted[key]
            elif key not in result and "default" in field:
                result[key] = copy.deepcopy(field["default"])
            continue
        if key in submitted:
            result[key] = submitted[key]
        elif key not in result and "default" in field:
            result[key] = copy.deepcopy(field["default"])
    return result


def _with_runtime_choices(
    schema: dict[str, Any],
    *,
    providers: list[dict[str, str]],
    embedding_providers: list[dict[str, str]],
    rerank_providers: list[dict[str, str]],
    personas: list[str],
) -> dict[str, Any]:
    result = copy.deepcopy(schema)
    for key, field in result.items():
        if not isinstance(field, dict):
            continue
        if field.get("type") == "object" and isinstance(field.get("items"), dict):
            field["items"] = _with_runtime_choices(
                field["items"],
                providers=providers,
                embedding_providers=embedding_providers,
                rerank_providers=rerank_providers,
                personas=personas,
            )
            continue
        kind = _model_field_kind(str(key), field)
        choices: list[dict[str, str]] = []
        if kind == "chat":
            choices = providers
        elif kind == "embedding":
            choices = embedding_providers
        elif kind == "rerank":
            choices = rerank_providers
        elif kind == "persona":
            choices = [{"value": value, "label": value} for value in personas]
        if kind:
            field["options"] = [""] + [item["value"] for item in choices]
            field["labels"] = ["使用系统默认"] + [item["label"] for item in choices]
    return result


def _validate_config(schema: dict[str, Any], config: dict[str, Any], path: str = "") -> None:
    expected_types: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "text": (str,),
        "int": (int,),
        "float": (int, float),
        "bool": (bool,),
        "list": (list,),
        "template_list": (list,),
        "object": (dict,),
    }
    for key, field in schema.items():
        if not isinstance(field, dict) or key not in config:
            continue
        field_path = f"{path}.{key}" if path else key
        field_type = str(field.get("type") or "string")
        value = config[key]
        if value is None:
            continue
        accepted = expected_types.get(field_type)
        if accepted and (
            not isinstance(value, accepted)
            or (field_type in {"int", "float"} and isinstance(value, bool))
        ):
            raise PluginCenterError(f"“{field.get('description') or field_path}”的值类型不正确。")
        options = field.get("options")
        if isinstance(options, list):
            values = value if field_type == "list" and isinstance(value, list) else [value]
            if any(item not in options for item in values):
                raise PluginCenterError(
                    f"“{field.get('description') or field_path}”包含不支持的选项。"
                )
        slider = field.get("slider")
        if isinstance(slider, dict) and isinstance(value, (int, float)):
            minimum = slider.get("min")
            maximum = slider.get("max")
            if isinstance(minimum, (int, float)) and value < minimum:
                raise PluginCenterError(
                    f"“{field.get('description') or field_path}”不能小于 {minimum}。"
                )
            if isinstance(maximum, (int, float)) and value > maximum:
                raise PluginCenterError(
                    f"“{field.get('description') or field_path}”不能大于 {maximum}。"
                )
        if field_type == "object" and isinstance(value, dict) and isinstance(field.get("items"), dict):
            _validate_config(field["items"], value, field_path)
        elif field_type == "list" and isinstance(value, list):
            item_schema = field.get("items")
            if isinstance(item_schema, dict) and item_schema.get("type"):
                for index, item in enumerate(value):
                    _validate_config(
                        {"item": item_schema},
                        {"item": item},
                        f"{field_path}[{index}]",
                    )
        elif field_type == "template_list" and isinstance(value, list):
            templates = field.get("templates")
            if not isinstance(templates, dict):
                continue
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    raise PluginCenterError(
                        f"“{field.get('description') or field_path}”的第 {index + 1} 项格式不正确。"
                    )
                template_key = item.get("__template_key")
                template = templates.get(template_key)
                if isinstance(template, dict) and isinstance(template.get("items"), dict):
                    _validate_config(
                        template["items"],
                        item,
                        f"{field_path}[{index}]",
                    )


def _dashboard_path(install_root: Path) -> Path:
    return install_root / "app" / "astrbot" / "data" / "cmd_config.json"


def _plugin_root(install_root: Path) -> Path:
    return install_root / "app" / "astrbot" / "data" / "plugins"


def _config_path(install_root: Path, module_name: str) -> Path:
    return install_root / "app" / "astrbot" / "data" / "config" / f"{module_name}_config.json"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class EngineApiClient:
    def __init__(self, install_root: str | Path, *, timeout: float = 5.0) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.timeout = timeout

    def _settings(self) -> tuple[str, str, int]:
        payload = _read_object(_dashboard_path(self.install_root))
        dashboard = payload.get("dashboard")
        if not isinstance(dashboard, dict):
            raise EngineUnavailableError("回复引擎的本地控制接口尚未配置。")
        secret = str(dashboard.get("jwt_secret") or "").strip()
        username = str(dashboard.get("username") or "local-console").strip()
        try:
            port = int(dashboard.get("port") or 6185)
        except (TypeError, ValueError) as exc:
            raise EngineUnavailableError("回复引擎的本地控制接口尚未就绪。") from exc
        if not secret or not 1 <= port <= 65535:
            raise EngineUnavailableError("回复引擎的本地控制接口尚未就绪。")
        return secret, username, port

    def _token(self) -> str:
        secret, username, _ = self._settings()
        header = _base64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        body = _base64url(
            json.dumps(
                {"username": username, "iat": int(time.time()), "exp": int(time.time()) + 300},
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"{header}.{body}".encode("ascii")
        signature = _base64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
        return f"{header}.{body}.{signature}"

    def _url(self, path: str) -> str:
        _, _, port = self._settings()
        if not path.startswith("/"):
            raise ValueError("engine API path must be absolute")
        return f"http://127.0.0.1:{port}{path}"

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        accept_language: str = "zh-CN",
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept-Language": accept_language,
        }
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=max(self.timeout, 60) if method in {"PUT", "PATCH", "POST"} else self.timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise EngineUnavailableError("回复引擎尚未在线，请先在运行中心启动机器人。") from exc

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, raw = self.raw_request(method, path, body=body)
        try:
            response = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginCenterError("回复引擎返回了无法识别的数据。") from exc
        if status >= 400 or not isinstance(response, dict) or response.get("status") == "error":
            message = ""
            if isinstance(response, dict):
                message = str(_white_label_schema(response.get("message") or "")).strip()
            raise PluginCenterError(
                message or "回复引擎未接受本次操作，请检查配置后重试。"
            )
        return response.get("data", response)

    def is_available(self) -> bool:
        try:
            self.plugin_states()
            return True
        except EngineUnavailableError:
            return False

    def plugin_states(self) -> dict[str, bool]:
        plugins = self.request("GET", "/api/v1/plugins")
        if not isinstance(plugins, list):
            raise PluginCenterError("无法确认功能运行状态，请刷新后重试。")
        system = self.request("GET", "/api/v1/system-config")
        config = system.get("config") if isinstance(system, dict) else None
        if not isinstance(config, dict):
            raise PluginCenterError("无法确认功能启用范围，请刷新后重试。")
        configured = config.get("plugin_set", ["*"])
        enabled_names = (
            {str(item) for item in configured}
            if isinstance(configured, list)
            else {"*"}
        )
        allow_all = "*" in enabled_names
        states: dict[str, bool] = {}
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            identifiers = tuple(
                str(identifier or "").strip()
                for identifier in (plugin.get("root_dir_name"), plugin.get("name"))
                if str(identifier or "").strip()
            )
            activated = bool(plugin.get("activated")) and (
                allow_all or any(identifier in enabled_names for identifier in identifiers)
            )
            # AstrBot uses the directory name for delivery/catalog matching and
            # the registered name for API routes.  They are usually equal, but
            # third-party plugins may expose different values.  Index both so a
            # live state is never mistaken for a locally disabled plugin.
            for module_name in identifiers:
                states[module_name] = activated
        return states

    def set_plugin_filter(self, plugin_names: list[str]) -> None:
        desired = sorted({str(name).strip() for name in plugin_names if str(name).strip()})
        system = self.request("GET", "/api/v1/system-config")
        config = system.get("config") if isinstance(system, dict) else None
        if not isinstance(config, dict):
            raise PluginCenterError("无法读取当前功能启用范围，请刷新后重试。")
        config["plugin_set"] = desired
        self.request("PUT", "/api/v1/system-config", config)
        verified = self.request("GET", "/api/v1/system-config")
        verified_config = verified.get("config") if isinstance(verified, dict) else None
        actual = verified_config.get("plugin_set") if isinstance(verified_config, dict) else None
        if not isinstance(actual, list) or sorted({str(name) for name in actual}) != desired:
            raise PluginCenterError("功能启用范围未能保存，请刷新后重试。")

    def _provider_records(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        source_data = self.request("GET", "/api/v1/provider-sources")
        provider_data = self.request("GET", "/api/v1/providers")
        sources = (
            source_data.get("provider_sources", [])
            if isinstance(source_data, dict)
            else []
        )
        providers = (
            provider_data.get("providers", [])
            if isinstance(provider_data, dict)
            else []
        )
        return (
            [item for item in sources if isinstance(item, dict)],
            [item for item in providers if isinstance(item, dict)],
        )

    def set_default_provider(self, provider_id: str) -> None:
        system = self.request("GET", "/api/v1/system-config")
        config = system.get("config") if isinstance(system, dict) else None
        if not isinstance(config, dict):
            raise PluginCenterError("无法读取当前模型设置，请刷新后重试。")
        settings = config.get("provider_settings")
        if not isinstance(settings, dict):
            settings = {}
            config["provider_settings"] = settings
        settings["request_max_retries"] = PROVIDER_REQUEST_MAX_ATTEMPTS
        settings["fallback_chat_models"] = []
        settings["default_provider_id"] = provider_id
        settings["context_limit_reached_strategy"] = "llm_compress"
        settings["llm_compress_provider_id"] = provider_id
        self.request("PUT", "/api/v1/system-config", config)
        verified = self.request("GET", "/api/v1/system-config")
        verified_config = verified.get("config") if isinstance(verified, dict) else None
        verified_settings = (
            verified_config.get("provider_settings")
            if isinstance(verified_config, dict)
            else None
        )
        if not isinstance(verified_settings, dict) or str(
            verified_settings.get("default_provider_id") or ""
        ) != provider_id:
            raise PluginCenterError("模型设置已提交，但没有收到生效确认，请刷新后重试。")

    def save_provider(
        self,
        profile: OpenAIProviderProfile,
        *,
        original_provider_id: str | None = None,
        make_default: bool = True,
    ) -> None:
        normalized = profile.normalized()
        errors = normalized.validate()
        if errors:
            raise ValueError("；".join(errors))
        sources, providers = self._provider_records()
        source_existing = next(
            (item for item in sources if item.get("id") == normalized.source_id),
            None,
        )
        source_payload = normalized.astrbot_source()
        if source_existing is not None:
            source_payload = {**source_payload, **source_existing}
            source_payload.update(
                {
                    "id": normalized.source_id,
                    "key": [normalized.api_key],
                    "api_base": normalized.api_base,
                    "timeout": normalized.astrbot_source()["timeout"],
                    "enable": True,
                }
            )
            self.request(
                "PUT",
                "/api/v1/provider-sources/by-id",
                {"source_id": normalized.source_id, "config": source_payload},
            )
        else:
            self.request("POST", "/api/v1/provider-sources", {"config": source_payload})

        known_ids = {str(item.get("id") or "") for item in providers}
        target = str(original_provider_id or "").strip()
        if target not in known_ids:
            target = normalized.model_id if normalized.model_id in known_ids else ""
        model_payload = normalized.astrbot_model()
        if target:
            current_model = dict(
                next(item for item in providers if item.get("id") == target)
            )
            for inherited_field in (
                "key",
                "api_base",
                "timeout",
                "proxy",
                "custom_headers",
                "provider",
                "type",
                "provider_type",
            ):
                current_model.pop(inherited_field, None)
            model_payload = {**model_payload, **current_model}
            model_payload.update(
                {
                    "id": normalized.model_id,
                    "provider_source_id": normalized.source_id,
                    "model": normalized.model,
                    "enable": True,
                }
            )
            self.request(
                "PUT",
                "/api/v1/providers/by-id",
                {"provider_id": target, "config": model_payload},
            )
        else:
            self.request("POST", "/api/v1/providers", {"config": model_payload})
        if make_default:
            self.set_default_provider(normalized.model_id)

        verified_sources, verified_providers = self._provider_records()
        source = next(
            (item for item in verified_sources if item.get("id") == normalized.source_id),
            None,
        )
        provider = next(
            (item for item in verified_providers if item.get("id") == normalized.model_id),
            None,
        )
        if (
            not isinstance(source, dict)
            or str(source.get("api_base") or "").rstrip("/") != normalized.api_base
            or not isinstance(provider, dict)
            or str(provider.get("model") or "") != normalized.model
        ):
            raise PluginCenterError("模型服务已提交，但没有收到生效确认，请刷新后重试。")

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        result = self.request(
            "POST",
            "/api/v1/providers/test",
            {"provider_id": provider_id},
        )
        return result if isinstance(result, dict) else {"result": result}

    def delete_provider(self, provider_id: str, source_id: str) -> None:
        _sources, providers = self._provider_records()
        if provider_id not in {str(item.get("id") or "") for item in providers}:
            raise PluginCenterError("所选模型服务不存在，请刷新后重试。")
        self.request(
            "DELETE",
            f"/api/v1/providers/by-id?provider_id={quote(provider_id, safe='')}",
        )
        remaining_sources, remaining_providers = self._provider_records()
        if any(item.get("id") == provider_id for item in remaining_providers):
            raise PluginCenterError(
                "模型服务删除后仍然存在，没有收到生效确认，请刷新后重试。"
            )
        remaining = [
            item
            for item in remaining_providers
            if item.get("provider_source_id") == source_id
        ]
        if not remaining and any(
            item.get("id") == source_id for item in remaining_sources
        ):
            self.request(
                "DELETE",
                f"/api/v1/provider-sources/by-id?source_id={quote(source_id, safe='')}",
            )
            verified_sources, _ = self._provider_records()
            if any(item.get("id") == source_id for item in verified_sources):
                raise PluginCenterError(
                    "模型服务已删除，但服务连接清理未确认，请刷新后重试。"
                )

    def _registered_plugin(self, module_name: str) -> dict[str, Any]:
        plugins = self.request("GET", "/api/v1/plugins")
        if not isinstance(plugins, list):
            raise PluginCenterError("无法确认功能运行状态，请刷新后重试。")
        matches = [p for p in plugins if isinstance(p, dict) and
                   (p.get("root_dir_name") == module_name or p.get("name") == module_name)]
        if len(matches) != 1 or not matches[0].get("name"):
            raise PluginCenterError("这项功能尚未正确加载，请检查运行记录；没有修改其他功能。")
        return matches[0]

    def get_config(self, module_name: str) -> dict[str, Any]:
        """Return a plugin configuration snapshot with its revision.

        Product-level settings pages use this to merge a small set of friendly
        controls into the full plugin configuration without discarding advanced
        fields owned by the plugin.
        """
        registered = self._registered_plugin(module_name)
        encoded = quote(str(registered["name"]), safe="")
        result = self.request("GET", f"/api/v1/plugins/{encoded}/config")
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
            raise PluginCenterError("无法读取当前功能设置，请刷新后重试。")
        return {
            "config": result["config"],
            "revision": str(result.get("revision") or _json_revision(result["config"])),
            "hot_apply_supported": result.get("hot_apply_supported") is True,
            "activated": bool(registered.get("activated")),
        }

    def save_config(self, module_name: str, config: dict[str, Any], base_revision: str | None = None) -> dict[str, Any]:
        registered = self._registered_plugin(module_name)
        encoded = quote(str(registered["name"]), safe="")
        endpoint = f"/api/v1/plugins/{encoded}/config"
        before = self.request("GET", endpoint)
        if not isinstance(before, dict) or before.get("hot_apply_supported") is not True:
            raise PluginCenterError("当前运行版本不支持可靠热更新，请先安装完整软件更新后再保存。")
        try:
            result = self.request("PUT", endpoint, {"config": config, "base_revision": base_revision or _json_revision(before.get("config") or {})})
        except EngineUnavailableError:
            raise PluginCenterError("保存确认超时，生效状态尚未确认。请稍后重新读取设置，不要连续重复提交。") from None
        receipt = result.get("hot_apply", {}) if isinstance(result, dict) else {}
        expected_state = "applied" if registered.get("activated") else "pending_enable"
        allowed_states = {"applied", "pending_setup"} if expected_state == "applied" else {"pending_enable"}
        if receipt.get("verified") is not True or receipt.get("state") not in allowed_states:
            raise PluginCenterError("设置已提交，但未收到生效确认，请重新读取设置并检查运行记录。")
        after = self.request("GET", endpoint)
        active = self._registered_plugin(module_name)
        if (not isinstance(after, dict) or not isinstance(after.get("config"), dict)
                or _json_revision(after["config"]) != receipt.get("revision")
                or bool(active.get("activated")) != bool(registered.get("activated"))):
            raise ConfigConflictError("保存后检测到配置或功能状态变化，请重新读取设置确认。")
        return receipt

    def set_enabled(self, module_name: str, enabled: bool) -> None:
        encoded = quote(str(self._registered_plugin(module_name)["name"]), safe="")
        self.request("PATCH", f"/api/v1/plugins/{encoded}/enabled", {"enabled": enabled})
        if bool(self._registered_plugin(module_name).get("activated")) != enabled:
            raise PluginCenterError("功能开关未能生效，请检查运行记录后重试。")

    def reload(self, module_name: str) -> None:
        encoded = quote(str(self._registered_plugin(module_name)["name"]), safe="")
        self.request("POST", f"/api/v1/plugins/{encoded}/reload")
        if not self._registered_plugin(module_name).get("activated"):
            raise PluginCenterError("功能尚未恢复运行，请检查设置和运行记录。")

    def page_entry(self, module_name: str, page_name: str) -> str:
        encoded_plugin = quote(module_name, safe="")
        encoded_page = quote(page_name, safe="")
        data = self.request("GET", f"/api/v1/plugins/{encoded_plugin}/pages/{encoded_page}")
        if not isinstance(data, dict) or not isinstance(data.get("content_path"), str):
            raise PluginCenterError("高级配置页暂时无法打开。")
        return data["content_path"]


class PluginCenterService:
    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.engine = EngineApiClient(self.install_root)

    def _runtime_plugin_states(self) -> tuple[bool, dict[str, bool]]:
        if not self.engine.is_available():
            return False, {}
        try:
            return True, self.engine.plugin_states()
        except EngineUnavailableError:
            return False, {}

    def _catalog_items(
        self, enabled_overrides: dict[str, bool] | None = None
    ) -> tuple[PluginCatalogItem, ...]:
        root = _plugin_root(self.install_root)
        installed = {item.module_name: item for item in load_plugins(self.install_root)}
        policies = load_delivery_features(self.install_root)
        ordered: list[tuple[PluginEntry, DeliveryFeature | None]]
        if policies is None:
            ordered = [(item, None) for item in installed.values()]
        else:
            ordered = [
                (installed[policy.module], policy)
                for policy in policies
                if policy.module in installed and policy.visible
            ]

        result: list[PluginCatalogItem] = []
        for raw_plugin, policy in ordered:
            module_name = str(raw_plugin.module_name)
            plugin_dir = root / module_name
            raw_schema = _read_object(plugin_dir / "_conf_schema.json")
            exposes_schema = policy is None or policy.exposes_schema
            exposes_pages = policy is None or policy.exposes_pages
            schema, profile = _project_customer_schema(
                module_name, raw_schema if exposes_schema else {}
            )
            _, model_schema = _split_schema(schema)
            pages_root = plugin_dir / "pages"
            pages = (
                tuple(
                    path.name
                    for path in sorted(
                        pages_root.iterdir(), key=lambda item: item.name.casefold()
                    )
                    if path.is_dir()
                    and _PAGE_ID.fullmatch(path.name)
                    and (path / "index.html").is_file()
                )
                if exposes_pages and pages_root.is_dir()
                else ()
            )
            result.append(
                PluginCatalogItem(
                    module_name=module_name,
                    display_name=(policy.display_name if policy else raw_plugin.display_name),
                    enabled=(
                        enabled_overrides.get(module_name, False)
                        if enabled_overrides is not None
                        else bool(raw_plugin.enabled)
                    ),
                    configurable=bool(schema),
                    field_count=_field_count(schema),
                    model_field_count=_field_count(model_schema),
                    pages=pages,
                    configuration=(policy.configuration if policy else "schema-and-page"),
                    description=(policy.description if policy else ""),
                    simplified=profile is not None,
                )
            )
        return tuple(result)

    def catalog(self) -> dict[str, Any]:
        engine_online, live_states = self._runtime_plugin_states()
        entries = []
        for plugin in self._catalog_items(live_states if engine_online else None):
            guide = get_plugin_guide(plugin.module_name)
            entries.append(
                {
                    "id": _public_plugin_id(plugin.module_name),
                    "display_name": plugin.display_name,
                    "enabled": plugin.enabled,
                    "configurable": plugin.configurable,
                    "field_count": plugin.field_count,
                    "model_field_count": plugin.model_field_count,
                    "pages": plugin.pages,
                    "configuration": plugin.configuration,
                    "description": plugin.description,
                    "simplified": plugin.simplified,
                    "audience": guide.get("audience", "按需使用"),
                    "readiness": guide.get("readiness", "请按说明准备"),
                }
            )
        return {
            "plugins": entries,
            "engine_online": engine_online,
            "product_name": load_robot_settings(self.install_root).profile.display_name,
        }

    def _plugin(
        self,
        public_id: str,
        enabled_overrides: dict[str, bool] | None = None,
    ) -> PluginCatalogItem:
        if not _PUBLIC_PLUGIN_ID.fullmatch(public_id):
            raise PluginCenterError("功能标识无效。")
        for plugin in self._catalog_items(enabled_overrides):
            if _public_plugin_id(plugin.module_name) == public_id:
                return plugin
        raise PluginCenterError("所选功能不存在或尚未交付。")

    def detail(self, public_id: str) -> dict[str, Any]:
        engine_online, live_states = self._runtime_plugin_states()
        item = self._plugin(public_id, live_states if engine_online else None)
        module_name = item.module_name
        raw_schema = _read_object(
            _plugin_root(self.install_root) / module_name / "_conf_schema.json"
        )
        schema = raw_schema if item.configurable else {}
        stored = _read_object(_config_path(self.install_root, module_name))
        config = _normalize_config(schema, _merge_defaults(schema, stored))
        engine_config = _read_object(_dashboard_path(self.install_root))
        providers: list[dict[str, str]] = []
        embedding_providers: list[dict[str, str]] = []
        rerank_providers: list[dict[str, str]] = []
        configured_providers = engine_config.get("provider", [])
        configured_sources = engine_config.get("provider_sources", [])
        if not isinstance(configured_providers, list):
            configured_providers = []
        source_ids = {
            str(source.get("id") or "")
            for source in (
                configured_sources if isinstance(configured_sources, list) else []
            )
            if isinstance(source, dict)
        }
        for provider in configured_providers:
            if not isinstance(provider, dict) or provider.get("enable") is False:
                continue
            provider_id = str(provider.get("id") or "").strip()
            if not provider_id:
                continue
            model_name = str(provider.get("model") or "").strip()
            source_id = str(provider.get("provider_source_id") or "").strip()
            label = provider_display_name(
                source_id if source_id in source_ids else "",
                model_name or provider_id,
            )
            choice = {"value": provider_id, "label": str(_white_label_schema(label))}
            provider_kind = " ".join(
                (
                    str(provider.get("type") or ""),
                    provider_id,
                    model_name,
                )
            ).casefold()
            if "embedding" in provider_kind:
                embedding_providers.append(choice)
            elif "rerank" in provider_kind:
                rerank_providers.append(choice)
            else:
                providers.append(choice)
        configured_personas = engine_config.get("persona", [])
        if not isinstance(configured_personas, list):
            configured_personas = []
        personas = [
            str(value.get("name") or value.get("id") or "").strip()
            for value in configured_personas
            if isinstance(value, dict) and str(value.get("name") or value.get("id") or "").strip()
        ]
        runtime_schema = _with_runtime_choices(
            schema,
            providers=providers,
            embedding_providers=embedding_providers,
            rerank_providers=rerank_providers,
            personas=personas,
        )
        customer_schema, profile = _project_customer_schema(module_name, runtime_schema)
        clean_schema = _customer_schema(customer_schema, config)
        customer_config = _customer_config(customer_schema, config)
        guide = get_plugin_guide(module_name)
        clean_basic_schema, clean_model_schema = _split_schema(clean_schema)
        return {
            "id": public_id,
            "display_name": item.display_name,
            "enabled": item.enabled,
            "configurable": item.configurable,
            "field_count": item.field_count,
            "model_field_count": item.model_field_count,
            "pages": item.pages,
            "configuration": item.configuration,
            "description": item.description,
            "simplified": item.simplified,
            "configuration_note": profile.note if profile else "",
            "setup_steps": profile.setup_steps if profile else (),
            "guide": guide,
            "advanced_schema": _customer_schema(
                advanced_schema(module_name, runtime_schema), config
            ),
            "advanced_config": _customer_config(
                advanced_schema(module_name, runtime_schema), config
            ),
            "schema": clean_schema,
            "basic_schema": clean_basic_schema,
            "model_schema": clean_model_schema,
            "config": customer_config,
            "revision": _json_revision(stored),
            "providers": providers,
            "embedding_providers": embedding_providers,
            "rerank_providers": rerank_providers,
            "personas": personas,
            "engine_online": engine_online,
        }

    def save(self, public_id: str, config: dict[str, Any], base_revision: str | None) -> dict[str, Any]:
        with _CONFIG_WRITE_LOCK:
            return self._save_locked(public_id, config, base_revision)

    def _save_locked(self, public_id: str, config: dict[str, Any], base_revision: str | None) -> dict[str, Any]:
        item = self._plugin(public_id)
        module_name = item.module_name
        if not item.configurable:
            raise PluginCenterError("这项功能没有可编辑的基础配置。")
        path = _config_path(self.install_root, module_name)
        stored = _read_object(path)
        if base_revision and base_revision != _json_revision(stored):
            raise ConfigConflictError("配置已被其他窗口修改，请刷新后再保存。")
        schema = _read_object(
            _plugin_root(self.install_root) / module_name / "_conf_schema.json"
        )
        merged = _merge_customer_config(schema, config, stored)
        normalized = _normalize_config(schema, _merge_defaults(schema, merged))
        _validate_config(schema, normalized)
        online = self.engine.is_available()
        reloaded = False
        apply_state = "pending_start" if not online else "pending_enable"
        receipt = {}
        if online:
            receipt = self.engine.save_config(module_name, normalized, _json_revision(stored))
            if not isinstance(receipt, dict) or receipt.get("verified") is not True:
                raise PluginCenterError("未收到配置生效确认，请重新读取设置检查结果。")
            apply_state = receipt["state"]
            reloaded = apply_state == "applied"
        else:
            write_json_atomic(path, normalized)
        saved = _read_object(path)
        if receipt and _json_revision(saved) != receipt.get("revision"):
            raise ConfigConflictError("保存后配置发生变化，请重新读取设置确认。")
        return {
            "saved": True,
            "reloaded": reloaded,
            "apply_state": apply_state,
            "revision": _json_revision(saved),
            "message": ("已保存并确认生效，后续请求使用新设置。" if reloaded else
                        "已保存并加载新设置；所需服务尚未就绪，请按使用指引完成配置后再验证。" if apply_state == "pending_setup" else
                        "已保存，启用此功能后使用新设置。" if online else
                        "已保存到本机，尚未确认生效。机器人启动后加载；若仍在运行，请恢复连接后重新保存。"),
        }

    def set_enabled(self, public_id: str, enabled: bool) -> dict[str, Any]:
        with _CONFIG_WRITE_LOCK:
            item = self._plugin(public_id)
            module_name = item.module_name
            online, live_states = self._runtime_plugin_states()
            previous_live = live_states.get(module_name, item.enabled)
            # plugin_set is both the message-pipeline allowlist and the
            # persisted startup state.  Update it before activation so the
            # feature cannot appear enabled while the pipeline still skips it.
            set_plugin_enabled(self.install_root, module_name, enabled)
            if online:
                try:
                    persisted_config = _read_object(_dashboard_path(self.install_root))
                    desired_plugins = persisted_config.get("plugin_set")
                    if not isinstance(desired_plugins, list):
                        raise PluginCenterError("功能启用范围未能保存，请停止机器人后重试。")
                    self.engine.set_plugin_filter(desired_plugins)
                    self.engine.set_enabled(module_name, enabled)
                except PluginCenterError:
                    # Restore both the on-disk allowlist and the running engine.
                    try:
                        set_plugin_enabled(self.install_root, module_name, item.enabled)
                        rollback_config = _read_object(_dashboard_path(self.install_root))
                        rollback_plugins = rollback_config.get("plugin_set")
                        if isinstance(rollback_plugins, list):
                            self.engine.set_plugin_filter(rollback_plugins)
                        self.engine.set_enabled(module_name, previous_live)
                    except (OSError, ValueError, PluginCenterError) as rollback_error:
                        raise PluginCenterError(
                            "功能状态更新失败，且无法恢复原状态；请停止机器人后重试。"
                        ) from rollback_error
                    raise
            persisted = self._plugin(public_id).enabled
            if persisted != enabled:
                raise PluginCenterError("功能开关未能保存，请停止机器人后重试。")
            return {
                "enabled": enabled,
                "applied": online,
                "message": "功能状态已立即更新。" if online else "功能状态已保存，将在下次启动时生效。",
            }

    def reload(self, public_id: str) -> dict[str, Any]:
        engine_online, live_states = self._runtime_plugin_states()
        item = self._plugin(public_id, live_states if engine_online else None)
        module_name = item.module_name
        if not item.enabled:
            raise PluginCenterError("请先启用这项功能，再重新载入设置。")
        self.engine.reload(module_name)
        return {"reloaded": True, "message": "设置已重新载入并生效。"}

    def page_entry(self, public_id: str, page_name: str) -> dict[str, str]:
        item = self._plugin(public_id)
        module_name = item.module_name
        if page_name not in item.pages or not _PAGE_ID.fullmatch(page_name):
            raise PluginCenterError("高级配置页不存在。")
        return {"content_path": self.engine.page_entry(module_name, page_name)}

    def extension_path(self, public_id: str, endpoint: str) -> str:
        engine_online, live_states = self._runtime_plugin_states()
        item = self._plugin(public_id, live_states if engine_online else None)
        if not item.enabled or not item.pages:
            raise PluginCenterError("请先启用这项功能，再使用高级配置页。")
        normalized = _normalize_plugin_extension_endpoint(endpoint)
        encoded_module = quote(item.module_name, safe="")
        encoded_endpoint = quote(normalized, safe="/")
        return f"/api/plug/{encoded_module}/{encoded_endpoint}"


def _asset_root() -> Path:
    candidates = [Path(__file__).with_name("plugin_webui")]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.insert(0, Path(bundle_root) / "wechatrebot_manager" / "plugin_webui")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    raise FileNotFoundError("功能配置中心资源缺失，请重新安装管理器。")


def _handler(service: PluginCenterService):
    assets = _asset_root()

    class Handler(BaseHTTPRequestHandler):
        server_version = "WeChatReBot"
        sys_version = ""

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_error(self, exc: Exception) -> None:
            status = int(getattr(exc, "status_code", HTTPStatus.INTERNAL_SERVER_ERROR))
            message = str(exc) if isinstance(exc, PluginCenterError) else "操作未完成，请稍后重试。"
            self._send_json(status, {"ok": False, "message": message})

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise PluginCenterError("请求长度无效。") from exc
            if length <= 0 or length > _MAX_BODY_BYTES:
                raise PluginCenterError("请求内容为空或过大。")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PluginCenterError("请求内容不是有效的 JSON。") from exc
            if not isinstance(value, dict):
                raise PluginCenterError("请求内容格式不正确。")
            return value

        def _require_same_origin(self) -> None:
            origin = self.headers.get("Origin")
            if not origin:
                return
            parsed = urlsplit(origin)
            host, port = self.server.server_address[:2]
            if (
                parsed.scheme != "http"
                or parsed.hostname != str(host)
                or parsed.port != int(port)
            ):
                raise PluginCenterError("请求来源无效。")

        def _parts(self) -> list[str]:
            return [unquote(part) for part in urlsplit(self.path).path.strip("/").split("/") if part]

        def _serve_asset(self, relative: str, content_type: str) -> None:
            target = assets / relative
            if not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面资源不存在。"})
                return
            raw = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-src 'self'")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _proxy(self) -> None:
            self._require_same_origin()
            split = urlsplit(self.path)
            if not any(split.path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY_BYTES:
                raise PluginCenterError("请求内容过大。")
            body = self.rfile.read(length) if length else None
            upstream_path = split.path + (f"?{split.query}" if split.query else "")
            status, headers, raw = service.engine.raw_request(
                self.command,
                upstream_path,
                body=body,
                content_type=self.headers.get("Content-Type", "application/json"),
                accept_language=self.headers.get("Accept-Language", "zh-CN"),
            )
            normalized_headers = {key.casefold(): value for key, value in headers.items()}
            self.send_response(status)
            self._security_headers()
            for key in ("Content-Type", "Content-Security-Policy", "Permissions-Policy"):
                value = normalized_headers.get(key.casefold())
                if value:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _proxy_extension(self, public_id: str, endpoint: str) -> None:
            self._require_same_origin()
            split = urlsplit(self.path)
            upstream_path = service.extension_path(public_id, endpoint)
            if split.query:
                upstream_path = f"{upstream_path}?{split.query}"
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise PluginCenterError("请求长度无效。") from exc
            if length > _MAX_BODY_BYTES:
                raise PluginCenterError("请求内容过大。")
            body = self.rfile.read(length) if length else None
            status, headers, raw = service.engine.raw_request(
                self.command,
                upstream_path,
                body=body,
                content_type=self.headers.get("Content-Type", "application/json"),
                accept_language=self.headers.get("Accept-Language", "zh-CN"),
            )
            normalized_headers = {key.casefold(): value for key, value in headers.items()}
            self.send_response(status)
            self._security_headers()
            content_type = normalized_headers.get("content-type")
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                split = urlsplit(self.path)
                if split.path in {"/", "/index.html"}:
                    self._serve_asset("index.html", "text/html; charset=utf-8")
                    return
                if split.path == "/assets/app.css":
                    self._serve_asset("app.css", "text/css; charset=utf-8")
                    return
                if split.path == "/assets/app.js":
                    self._serve_asset("app.js", "text/javascript; charset=utf-8")
                    return
                if any(split.path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                parts = self._parts()
                if parts == ["control", "catalog"]:
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": service.catalog()})
                    return
                if len(parts) == 3 and parts[:2] == ["control", "plugins"]:
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": service.detail(parts[2])})
                    return
                if len(parts) == 6 and parts[:2] == ["control", "plugins"] and parts[3] == "pages" and parts[5] == "entry":
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": service.page_entry(parts[2], parts[4])})
                    return
                if len(parts) >= 5 and parts[:2] == ["control", "plugins"] and parts[3] == "extension":
                    self._proxy_extension(parts[2], "/".join(parts[4:]))
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
            except Exception as exc:
                self._send_error(exc)

        def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                self._require_same_origin()
                if any(urlsplit(self.path).path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                parts = self._parts()
                if len(parts) != 4 or parts[:2] != ["control", "plugins"] or parts[3] != "config":
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
                    return
                body = self._body()
                config = body.get("config")
                if not isinstance(config, dict):
                    raise PluginCenterError("配置内容格式不正确。")
                data = service.save(parts[2], config, str(body.get("revision") or "") or None)
                self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
            except Exception as exc:
                self._send_error(exc)

        def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                self._require_same_origin()
                if any(urlsplit(self.path).path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                parts = self._parts()
                if len(parts) != 4 or parts[:2] != ["control", "plugins"] or parts[3] != "enabled":
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
                    return
                enabled = self._body().get("enabled")
                if not isinstance(enabled, bool):
                    raise PluginCenterError("启用状态无效。")
                data = service.set_enabled(parts[2], enabled)
                self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                self._require_same_origin()
                if any(urlsplit(self.path).path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                parts = self._parts()
                if len(parts) >= 5 and parts[:2] == ["control", "plugins"] and parts[3] == "extension":
                    self._proxy_extension(parts[2], "/".join(parts[4:]))
                    return
                if len(parts) != 4 or parts[:2] != ["control", "plugins"] or parts[3] != "reload":
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
                    return
                data = service.reload(parts[2])
                self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
            except Exception as exc:
                self._send_error(exc)

        def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                self._require_same_origin()
                if any(urlsplit(self.path).path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
            except Exception as exc:
                self._send_error(exc)

    return Handler


def plugin_center_command(
    install_root: str | Path, feature_id: str | None = None
) -> list[str]:
    root = str(Path(install_root).expanduser().resolve())
    selected = str(feature_id or "").strip()
    if selected and not _PUBLIC_PLUGIN_ID.fullmatch(selected):
        raise PluginCenterError("功能标识无效。")
    if getattr(sys, "frozen", False):
        command = [sys.executable, "plugin-center", root]
    else:
        command = [sys.executable, "-m", "wechatrebot_manager", "plugin-center", root]
    if selected:
        command.append(selected)
    return command


def launch_plugin_center(
    install_root: str | Path, feature_id: str | None = None
) -> subprocess.Popen[bytes]:
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        plugin_center_command(install_root, feature_id),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )


def run_plugin_center(
    install_root: str | Path, feature_id: str | None = None
) -> int:
    root = Path(install_root).expanduser().resolve()
    selected = str(feature_id or "").strip()
    if selected and not _PUBLIC_PLUGIN_ID.fullmatch(selected):
        raise PluginCenterError("功能标识无效。")
    lock = SingleInstanceLock(f"plugin-center:{root}")
    if not lock.acquire():
        return 3
    server: ThreadingHTTPServer | None = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(PluginCenterService(root)))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        import webview

        brand = load_robot_settings(root).profile
        query = f"?feature={quote(selected, safe='')}" if selected else ""
        url = f"http://127.0.0.1:{server.server_port}/{query}"
        webview.create_window(
            f"{brand.display_name} · 功能配置",
            url,
            width=1180,
            height=760,
            min_size=(960, 640),
            background_color="#F8FAFD",
            text_select=True,
        )
        webview.start(gui="edgechromium", debug=False, private_mode=True)
        return 0
    except ImportError as exc:
        raise PluginCenterError("功能配置窗口组件缺失，请重新安装管理器。") from exc
    except PluginCenterError:
        raise
    except Exception as exc:
        raise PluginCenterError(
            "功能配置窗口无法打开，请检查 Edge WebView2 Runtime 后重试。"
        ) from exc
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        lock.release()


__all__ = [
    "ConfigConflictError",
    "EngineApiClient",
    "EngineUnavailableError",
    "PluginCatalogItem",
    "PluginCenterError",
    "PluginCenterService",
    "launch_plugin_center",
    "plugin_center_command",
    "run_plugin_center",
]
