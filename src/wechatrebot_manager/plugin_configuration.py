"""Plugin config projection, redaction, validation and conflict-aware merging."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .plugin_errors import PluginCenterError
from .provider import DEFAULT_PROVIDER_MODEL, DEFAULT_PROVIDER_SOURCE_ID


_SECRET_FIELD = re.compile(
    r"(?:^|[^a-z0-9])(?:api.?key|access.?key|authorization|cookie|credential|password|secret|token)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


_LEGACY_IMAGE_MODEL_SENTINELS = {
    "primary": "__legacy_image_primary__",
    "secondary": "__legacy_image_secondary__",
}


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
    if (
        provider_type in {"image", "image_generation"}
        or "image_provider" in normalized
        or "image_model" in normalized
    ):
        return "image"
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
        value = re.sub(r'https?://[^\s）)<>"]*(?:astrbot|onebot)[^\s）)<>"]*', "功能中心", value, flags=re.I)
        value = re.sub(r"(?i)(?:ll)?onebot", "微信连接", value)
        value = re.sub(r"天使(?:之)?心", "机器人核心", value)
        value = value.replace("angel_describe_image", "历史图片描述")
        value = re.sub(r"(?i)angel[-_ ]?heart", "机器人核心", value)
        value = re.sub(r"(?i)kawayiyokami", "核心维护者", value)
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


def _clear_factory_provider_values(value: Any) -> Any:
    """Do not expose AstrBot's factory-only model as a selectable value."""
    factory_id = f"{DEFAULT_PROVIDER_SOURCE_ID}/{DEFAULT_PROVIDER_MODEL}"
    if isinstance(value, dict):
        return {key: _clear_factory_provider_values(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_clear_factory_provider_values(child) for child in value]
    if value == factory_id:
        return ""
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


_MISSING_CUSTOMER_VALUE = object()


def _changed_customer_config(
    schema: dict[str, Any],
    submitted: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Return only customer-visible values changed since the form was read."""
    result: dict[str, Any] = {}
    for key, field in schema.items():
        if (
            not isinstance(field, dict)
            or field.get("invisible") is True
            or key not in submitted
        ):
            continue
        submitted_value = submitted[key]
        baseline_value = baseline.get(key, _MISSING_CUSTOMER_VALUE)
        if field.get("type") == "object" and isinstance(field.get("items"), dict):
            if not isinstance(submitted_value, dict):
                if submitted_value != baseline_value:
                    result[key] = copy.deepcopy(submitted_value)
                continue
            changed = _changed_customer_config(
                field["items"],
                submitted_value,
                baseline_value if isinstance(baseline_value, dict) else {},
            )
            if changed:
                result[key] = changed
            continue
        if (
            baseline_value is _MISSING_CUSTOMER_VALUE
            or submitted_value != baseline_value
        ):
            result[key] = copy.deepcopy(submitted_value)
    return result


def _customer_change_conflicts(
    schema: dict[str, Any],
    changed: dict[str, Any],
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    """Detect whether a changed visible field was also changed externally."""
    for key, submitted_value in changed.items():
        field = schema.get(key)
        if not isinstance(field, dict):
            return True
        if _is_secret_field(str(key), field):
            # Redacted baselines cannot prove whether the stored secret changed.
            return True
        baseline_value = baseline.get(key, _MISSING_CUSTOMER_VALUE)
        current_value = current.get(key, _MISSING_CUSTOMER_VALUE)
        if field.get("type") == "object" and isinstance(field.get("items"), dict):
            if not isinstance(submitted_value, dict):
                if current_value != baseline_value:
                    return True
                continue
            if _customer_change_conflicts(
                field["items"],
                submitted_value,
                baseline_value if isinstance(baseline_value, dict) else {},
                current_value if isinstance(current_value, dict) else {},
            ):
                return True
            continue
        if (
            baseline_value is _MISSING_CUSTOMER_VALUE
            or current_value is _MISSING_CUSTOMER_VALUE
            or current_value != baseline_value
        ):
            return True
    return False


def _with_runtime_choices(
    schema: dict[str, Any],
    *,
    providers: list[dict[str, str]],
    embedding_providers: list[dict[str, str]],
    rerank_providers: list[dict[str, str]],
    image_providers: list[dict[str, str]],
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
                image_providers=image_providers,
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
        elif kind == "image":
            choices = image_providers
        elif kind == "persona":
            choices = [{"value": value, "label": value} for value in personas]
        if kind:
            field["options"] = [""] + [item["value"] for item in choices]
            field["labels"] = [
                str(field.get("_blank_label") or "使用系统默认")
            ] + [item["label"] for item in choices]
    return result


def _configured_provider_service_kind(provider: dict[str, Any]) -> str:
    """Classify provider purpose without mistaking vision input for generation."""
    declared = str(provider.get("service_kind") or "").strip().casefold()
    if declared in {"chat", "embedding", "image", "rerank"}:
        return declared
    legacy_type = " ".join(
        str(provider.get(key) or "")
        for key in ("type", "provider_type", "provider")
    ).casefold()
    if "embedding" in legacy_type:
        return "embedding"
    if "rerank" in legacy_type:
        return "rerank"
    if "image_generation" in legacy_type or "openai_image" in legacy_type:
        return "image"
    modalities = {
        str(value).strip().casefold()
        for value in provider.get("modalities", [])
        if str(value).strip()
    } if isinstance(provider.get("modalities"), list) else set()
    if modalities == {"image"}:
        return "image"
    return "chat"


def _image_provider_bindings(
    sources: list[dict[str, Any]], providers: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Return the private connection details behind image-provider choices."""
    sources_by_id = {
        str(source.get("id") or ""): source
        for source in sources
        if isinstance(source, dict) and str(source.get("id") or "").strip()
    }
    bindings: dict[str, dict[str, str]] = {}
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("enable") is False:
            continue
        provider_id = str(provider.get("id") or "").strip()
        model = str(provider.get("model") or "").strip()
        source_id = str(provider.get("provider_source_id") or "").strip()
        source = sources_by_id.get(source_id)
        if not provider_id or not model or not isinstance(source, dict):
            continue
        if _configured_provider_service_kind(provider) != "image":
            continue
        keys = source.get("key")
        api_key = str(keys[0]) if isinstance(keys, list) and keys else ""
        bindings[provider_id] = {
            "provider_id": provider_id,
            "api_base": str(source.get("api_base") or "").strip().rstrip("/"),
            "model": model,
            "api_key": api_key,
        }
    return bindings


def _resolve_image_provider_config(
    config: dict[str, Any],
    bindings: dict[str, dict[str, str]],
    legacy_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate selected provider IDs into the image plugin's flat config."""
    result = copy.deepcopy(config)
    for prefix in ("primary", "secondary"):
        model_key = f"{prefix}_model"
        if result.get(model_key) == _LEGACY_IMAGE_MODEL_SENTINELS[prefix]:
            if isinstance(legacy_config, dict):
                result[model_key] = legacy_config.get(model_key, "")
            continue
        binding = bindings.get(str(result.get(model_key) or "").strip())
        if not binding:
            continue
        result[model_key] = binding["model"]
        result[f"{prefix}_base_url"] = binding["api_base"]
        result[f"{prefix}_api_key"] = binding["api_key"]
    return result


def _expose_image_provider_config(
    config: dict[str, Any],
    bindings: dict[str, dict[str, str]],
    source_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Show the provider ID in the manager while retaining plugin config on disk."""
    result = copy.deepcopy(config)
    for prefix in ("primary", "secondary"):
        model_key = f"{prefix}_model"
        model = str(result.get(model_key) or "").strip()
        connection = source_config if isinstance(source_config, dict) else result
        api_base = str(connection.get(f"{prefix}_base_url") or "").strip().rstrip("/")
        match = next(
            (
                provider_id
                for provider_id, binding in bindings.items()
                if binding["model"] == model
                and binding["api_base"].rstrip("/") == api_base
            ),
            None,
        )
        if match:
            result[model_key] = match
        elif (
            model
            or str(result.get(f"{prefix}_base_url") or "").strip()
            or str(result.get(f"{prefix}_api_key") or "").strip()
        ):
            result[model_key] = _LEGACY_IMAGE_MODEL_SENTINELS[prefix]
    return result


def _preserve_legacy_image_model_choices(
    schema: dict[str, Any],
    config: dict[str, Any],
    bindings: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], bool]:
    """Keep existing unregistered models selectable until users migrate them."""
    result = copy.deepcopy(schema)
    has_legacy_model = False
    for prefix in ("primary", "secondary"):
        model_key = f"{prefix}_model"
        model = str(config.get(model_key) or "").strip()
        api_base = str(config.get(f"{prefix}_base_url") or "").strip()
        api_key = str(config.get(f"{prefix}_api_key") or "").strip()
        if not model and not api_base and not api_key:
            continue
        api_base = api_base.rstrip("/")
        registered = any(
            binding["model"] == model
            and binding["api_base"].rstrip("/") == api_base
            for binding in bindings.values()
        )
        if registered:
            continue
        has_legacy_model = True
        field = result.get(model_key)
        if not isinstance(field, dict):
            continue
        sentinel = _LEGACY_IMAGE_MODEL_SENTINELS[prefix]
        options = list(field.get("options") or [""])
        labels = list(
            field.get("labels")
            or [str(field.get("_blank_label") or "请选择图片模型")]
        )
        if sentinel not in options:
            options.append(sentinel)
            labels.append("现有独立配置（保留中）")
        field["options"] = options
        field["labels"] = labels
    return result, has_legacy_model


def _validate_image_proxy_port(config: dict[str, Any]) -> None:
    port = str(config.get("local_proxy_port") or "").strip()
    if port and (not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535):
        raise PluginCenterError("本地代理端口请填写 1–65535 的整数，或留空使用已有代理设置。")
    config["local_proxy_port"] = port


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
