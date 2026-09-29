from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import Request, urlopen


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

DEFAULT_PROVIDER_SOURCE_ID = "jyjapi"
DEFAULT_PROVIDER_API_BASE = "https://api.xiuxianjyj.xin/v1"
DEFAULT_PROVIDER_MODEL = "grok-4.6"
DEFAULT_PROVIDER_MODEL_ID = (
    f"{DEFAULT_PROVIDER_SOURCE_ID}/{DEFAULT_PROVIDER_MODEL}"
)
DEEPSEEK_PROVIDER_SOURCE_ID = "deepseek"
DEEPSEEK_PROVIDER_API_BASE = "https://api.deepseek.com"
DEEPSEEK_PROVIDER_MODEL = "deepseek-flash"
PROVIDER_REQUEST_TIMEOUT_SECONDS = 35
# AstrBot's provider layer retries connection errors on top of the OpenAI SDK.
# Keep one product-level attempt so a failed endpoint cannot hold a group reply
# for several minutes. The SDK retry is disabled by the bundled runtime patch.
PROVIDER_REQUEST_MAX_ATTEMPTS = 1

# OpenAI-compatible entry points that are useful when adding a service from
# the manager. Values are intentionally editable and are only starting points;
# the provider's current documentation remains authoritative.
MODEL_PROVIDER_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "api_base": DEEPSEEK_PROVIDER_API_BASE,
        "model": DEEPSEEK_PROVIDER_MODEL,
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "DeepSeek 官方兼容接口；默认填入 DeepSeek Flash。",
    },
    {
        "id": "glm",
        "name": "智谱 GLM",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.6v",
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "智谱 GLM 兼容接口；默认填入 GLM-4.6V。",
    },
    {
        "id": "xiaomi_mimo",
        "name": "小米 MiMo",
        "api_base": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5",
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "小米 MiMo 兼容接口；默认填入 MiMo-V2.5。",
    },
    {
        "id": "qwen",
        "name": "通义千问 Qwen",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "阿里云百炼兼容接口；默认填入 qwen-plus。",
    },
    {
        "id": "volcengine",
        "name": "火山引擎豆包",
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-2-0-lite-260215",
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "火山方舟兼容接口；默认填入豆包 Seed 2.0。",
    },
    {
        "id": "hunyuan",
        "name": "腾讯混元",
        "api_base": "https://tokenhub.tencentmaas.com/v1",
        "model": "hy3-preview",
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "腾讯 TokenHub 混元兼容接口；默认填入 hy3-preview。",
    },
    {
        "id": "gemini_proxy",
        "name": "Gemini",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.8-flash",
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "Gemini 官方兼容接口；默认填入 gemini-3.8-flash。",
    },
    {
        "id": "sub2",
        "name": "Sub2 兼容服务",
        "api_base": "",
        "model": "",
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "填写 Sub2 兼容接口地址和模型名称；聊天模型默认按多模态请求。",
    },
    {
        "id": "embedding_custom",
        "name": "向量模型（兼容接口）",
        "api_base": "",
        "model": "",
        "service_kind": "embedding",
        "modalities": ("embedding",),
        "description": "新增 Embedding 文本向量模型，供长期记忆等功能选择。",
    },
    {
        "id": "image_custom",
        "name": "图片模型（兼容接口）",
        "api_base": "",
        "model": "",
        "service_kind": "image",
        "modalities": ("image",),
        "description": "新增图片生成模型，供功能中心图片生成选择。",
    },
    {
        "id": "voice_custom",
        "name": "语音模型（兼容接口）",
        "api_base": "",
        "model": "",
        "service_kind": "voice",
        "modalities": ("audio",),
        "description": "新增语音模型，供语音回复或转写功能选择。",
    },
    {
        "id": "custom",
        "name": "其他兼容服务",
        "api_base": "",
        "model": "",
        "service_kind": "chat",
        "modalities": ("text", "image", "audio"),
        "description": "填写中转站或其他兼容接口地址、模型名称和凭据。",
    },
)


@dataclass(frozen=True)
class _ThinkingModeRule:
    adapter: str
    endpoint_matches: Callable[[SplitResult], bool]
    model_matches: Callable[[str], bool]


def _host_matches(host: str, *domains: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _domain_endpoint(*domains: str) -> Callable[[SplitResult], bool]:
    return lambda endpoint: _host_matches(
        (endpoint.hostname or "").casefold(), *domains
    )


def _model_leaf(model: str) -> str:
    return model.rsplit("/", 1)[-1]


def _starts_with(model: str, *prefixes: str) -> bool:
    return _model_leaf(model).startswith(prefixes)


def _xiaomi_hybrid_model(model: str) -> bool:
    return _model_leaf(model) in {"mimo-v2.5", "mimo-v2.5-pro"}


def _deepseek_hybrid_model(model: str) -> bool:
    name = _model_leaf(model)
    return not any(
        marker in name for marker in ("reasoner", "speciale", "-r1", "r1-")
    ) and name.startswith(
        ("deepseek-chat", "deepseek-flash", "deepseek-v3", "deepseek-v4")
    )


def _qwen_hybrid_model(model: str) -> bool:
    name = _model_leaf(model)
    return (
        name.startswith(("qwen3", "qwen-3"))
        and "thinking" not in name
        and "qwq" not in name
    )


def _glm_hybrid_model(model: str) -> bool:
    return re.match(r"^glm-(?:4\.(?:5|6|7)|5(?:[.\-]|$))", _model_leaf(model)) is not None


def _kimi_hybrid_model(model: str) -> bool:
    return _starts_with(model, "kimi-k2.5", "kimi-k2.6")


def _doubao_hybrid_model(model: str) -> bool:
    return _starts_with(model, "doubao-seed-1-6", "doubao-seed-2")


def _dashscope_hybrid_model(model: str) -> bool:
    name = _model_leaf(model)
    return "thinking" not in name and (
        name.startswith("qwen-plus")
        or _qwen_hybrid_model(name)
        or _deepseek_hybrid_model(name)
        or _kimi_hybrid_model(name)
        or _glm_hybrid_model(name)
    )


def _siliconflow_hybrid_model(model: str) -> bool:
    name = _model_leaf(model)
    return (
        _qwen_hybrid_model(name)
        or _deepseek_hybrid_model(name)
        or _glm_hybrid_model(name)
        or name.startswith("hunyuan-a13b")
    )


def _fireworks_hybrid_model(model: str) -> bool:
    return (
        _qwen_hybrid_model(model)
        or _deepseek_hybrid_model(model)
        or _glm_hybrid_model(model)
    )


def _together_hybrid_model(model: str) -> bool:
    name = _model_leaf(model)
    return name.startswith(("qwen3.5", "qwen-3.5", "deepseek-v4"))


def _openai_hybrid_model(model: str) -> bool:
    name = _model_leaf(model)
    match = re.match(r"^gpt-5\.(\d+)", name)
    return match is not None and int(match.group(1)) >= 1 and "pro" not in name


def _openrouter_hybrid_model(model: str) -> bool:
    name = _model_leaf(model)
    claude_optional = re.match(
        r"^claude-(?:3\.7|(?:opus|sonnet|haiku)-4(?:[.\-]|$))", name
    ) is not None
    return (
        _qwen_hybrid_model(model)
        or _deepseek_hybrid_model(model)
        or _glm_hybrid_model(model)
        or _kimi_hybrid_model(model)
        or _openai_hybrid_model(model)
        or _gemini_hybrid_model(model)
        or claude_optional
    )


def _gemini_hybrid_model(model: str) -> bool:
    return _starts_with(
        model,
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.8-flash",
    )


def _ollama_endpoint(endpoint: SplitResult) -> bool:
    host = (endpoint.hostname or "").casefold()
    try:
        port = endpoint.port
    except ValueError:
        return False
    return port == 11434 or host in {"localhost", "127.0.0.1", "::1"}


def _ollama_hybrid_model(model: str) -> bool:
    name = _model_leaf(model)
    return "gpt-oss" not in name and name.startswith(
        ("qwen3", "qwen-3", "deepseek-v3.1", "deepseek-r1", "glm-4.7")
    )


_THINKING_MODE_RULES = (
    _ThinkingModeRule(
        "thinking", _domain_endpoint("xiaomimimo.com"), _xiaomi_hybrid_model
    ),
    _ThinkingModeRule(
        "thinking", _domain_endpoint("deepseek.com"), _deepseek_hybrid_model
    ),
    _ThinkingModeRule(
        "enable_thinking",
        _domain_endpoint("dashscope.aliyuncs.com"),
        _dashscope_hybrid_model,
    ),
    _ThinkingModeRule(
        "thinking", _domain_endpoint("volces.com"), _doubao_hybrid_model
    ),
    _ThinkingModeRule(
        "thinking", _domain_endpoint("bigmodel.cn", "z.ai"), _glm_hybrid_model
    ),
    _ThinkingModeRule(
        "enable_thinking",
        _domain_endpoint("siliconflow.cn", "siliconflow.com"),
        _siliconflow_hybrid_model,
    ),
    _ThinkingModeRule(
        "thinking",
        _domain_endpoint("moonshot.cn", "moonshot.ai"),
        _kimi_hybrid_model,
    ),
    _ThinkingModeRule(
        "reasoning_effort_object",
        _domain_endpoint("openrouter.ai"),
        _openrouter_hybrid_model,
    ),
    _ThinkingModeRule(
        "reasoning_enabled",
        _domain_endpoint("together.xyz", "together.ai"),
        _together_hybrid_model,
    ),
    _ThinkingModeRule(
        "reasoning_effort_none",
        _domain_endpoint("fireworks.ai"),
        _fireworks_hybrid_model,
    ),
    _ThinkingModeRule(
        "reasoning_effort_default",
        _domain_endpoint("groq.com"),
        lambda model: _starts_with(model, "qwen3.6-27b", "qwen3.8-27b"),
    ),
    _ThinkingModeRule(
        "reasoning_effort_none",
        _domain_endpoint("cerebras.ai"),
        lambda model: _model_leaf(model) == "zai-glm-4.7",
    ),
    _ThinkingModeRule(
        "reasoning_effort_none", _ollama_endpoint, _ollama_hybrid_model
    ),
    _ThinkingModeRule(
        "reasoning_effort_low",
        _domain_endpoint("x.ai", "xiuxianjyj.xin"),
        lambda model: _starts_with(model, "grok-4.5", "grok-4.6"),
    ),
    _ThinkingModeRule(
        "reasoning_effort_none",
        _domain_endpoint("googleapis.com"),
        _gemini_hybrid_model,
    ),
    _ThinkingModeRule(
        "reasoning_effort_none",
        _domain_endpoint(
            "openai.com",
            "openai.azure.com",
            "services.ai.azure.com",
            "models.inference.ai.azure.com",
        ),
        _openai_hybrid_model,
    ),
)


def thinking_mode_adapter(api_base: str, model: str) -> str | None:
    """Return the safe request mapping for a known OpenAI-compatible service."""
    endpoint = urlsplit(api_base.strip())
    normalized_model = model.strip().casefold()
    for rule in _THINKING_MODE_RULES:
        if rule.endpoint_matches(endpoint) and rule.model_matches(normalized_model):
            return rule.adapter
    return None


def supports_thinking_toggle(api_base: str, model: str) -> bool:
    """Whether the manager knows how to apply the preference to requests."""
    return thinking_mode_adapter(api_base, model) is not None


def without_thinking_mode(
    extra_body: object,
    *,
    api_base: str,
    model: str,
) -> dict[str, object]:
    """Remove only the request field managed for the previous adapter."""
    merged = dict(extra_body) if isinstance(extra_body, dict) else {}
    adapter = thinking_mode_adapter(api_base, model)
    if adapter == "thinking":
        merged.pop("thinking", None)
    elif adapter == "enable_thinking":
        merged.pop("enable_thinking", None)
    elif adapter in {
        "reasoning_effort_low",
        "reasoning_effort_none",
        "reasoning_effort_default",
    }:
        merged.pop("reasoning_effort", None)
    elif adapter in {"reasoning_effort_object", "reasoning_enabled"}:
        reasoning = merged.get("reasoning")
        if isinstance(reasoning, dict):
            cleaned = dict(reasoning)
            cleaned.pop(
                "effort" if adapter == "reasoning_effort_object" else "enabled",
                None,
            )
            if cleaned:
                merged["reasoning"] = cleaned
            else:
                merged.pop("reasoning", None)
    return merged


def with_thinking_mode(
    extra_body: object,
    enabled: bool,
    *,
    api_base: str,
    model: str,
) -> dict[str, object]:
    """Apply a known thinking protocol and preserve unrelated parameters."""
    merged = dict(extra_body) if isinstance(extra_body, dict) else {}
    adapter = thinking_mode_adapter(api_base, model)
    if adapter == "thinking":
        merged["thinking"] = {"type": "enabled" if enabled else "disabled"}
    elif adapter == "enable_thinking":
        merged["enable_thinking"] = enabled
    elif adapter == "reasoning_effort_low":
        merged["reasoning_effort"] = "high" if enabled else "low"
    elif adapter == "reasoning_effort_none":
        merged["reasoning_effort"] = "high" if enabled else "none"
    elif adapter == "reasoning_effort_default":
        merged["reasoning_effort"] = "default" if enabled else "none"
    elif adapter in {"reasoning_effort_object", "reasoning_enabled"}:
        reasoning = merged.get("reasoning")
        nested = dict(reasoning) if isinstance(reasoning, dict) else {}
        if adapter == "reasoning_effort_object":
            nested["effort"] = "high" if enabled else "none"
        else:
            nested["enabled"] = enabled
        merged["reasoning"] = nested
    return merged


def thinking_mode_matches(
    extra_body: object,
    enabled: bool,
    *,
    api_base: str,
    model: str,
) -> bool:
    """Confirm a known adapter was persisted; unknown adapters are no-ops."""
    adapter = thinking_mode_adapter(api_base, model)
    if adapter is None:
        return True
    body = extra_body if isinstance(extra_body, dict) else {}
    if adapter == "thinking":
        return body.get("thinking") == {
            "type": "enabled" if enabled else "disabled"
        }
    if adapter == "enable_thinking":
        return body.get("enable_thinking") is enabled
    if adapter == "reasoning_effort_low":
        return body.get("reasoning_effort") == ("high" if enabled else "low")
    if adapter == "reasoning_effort_none":
        return body.get("reasoning_effort") == ("high" if enabled else "none")
    if adapter == "reasoning_effort_default":
        return body.get("reasoning_effort") == ("default" if enabled else "none")
    reasoning = body.get("reasoning")
    nested = reasoning if isinstance(reasoning, dict) else {}
    if adapter == "reasoning_effort_object":
        return nested.get("effort") == ("high" if enabled else "none")
    return nested.get("enabled") is enabled


def infer_thinking_mode(
    extra_body: object,
    *,
    api_base: str,
    model: str,
) -> bool:
    """Infer an existing setting when no manager preference was saved yet."""
    adapter = thinking_mode_adapter(api_base, model)
    body = extra_body if isinstance(extra_body, dict) else {}
    if adapter == "thinking":
        value = body.get("thinking")
        return not (isinstance(value, dict) and value.get("type") == "disabled")
    if adapter == "enable_thinking" and isinstance(body.get("enable_thinking"), bool):
        return bool(body["enable_thinking"])
    effort = str(body.get("reasoning_effort") or "").casefold()
    if adapter == "reasoning_effort_low":
        return effort not in {"low", "none"}
    if adapter == "reasoning_effort_none":
        return effort != "none"
    if adapter == "reasoning_effort_default":
        return effort != "none"
    reasoning = body.get("reasoning")
    nested = reasoning if isinstance(reasoning, dict) else {}
    if adapter == "reasoning_effort_object":
        return str(nested.get("effort") or "").casefold() != "none"
    if adapter == "reasoning_enabled" and isinstance(nested.get("enabled"), bool):
        return bool(nested["enabled"])
    return True


def provider_source_id(api_base: str, model: str) -> str:
    """Return a stable internal source id without exposing it in customer UI."""
    normalized_base = api_base.strip().rstrip("/").casefold()
    normalized_model = model.strip().casefold()
    if (
        normalized_base == DEFAULT_PROVIDER_API_BASE.casefold()
        and normalized_model == DEFAULT_PROVIDER_MODEL.casefold()
    ):
        return DEFAULT_PROVIDER_SOURCE_ID
    if normalized_base == DEEPSEEK_PROVIDER_API_BASE.casefold():
        return DEEPSEEK_PROVIDER_SOURCE_ID
    digest = hashlib.sha256(normalized_base.encode("utf-8")).hexdigest()[:10]
    return f"custom_{digest}"


def provider_display_name(source_id: str, model: str) -> str:
    """Build a beginner-friendly label for a configured chat model."""
    clean_source = source_id.strip().casefold()
    clean_model = model.strip()
    if (
        clean_source == DEFAULT_PROVIDER_SOURCE_ID
        and clean_model.casefold() == DEFAULT_PROVIDER_MODEL.casefold()
    ):
        return "免费 Grok 4.6"
    if clean_source == DEEPSEEK_PROVIDER_SOURCE_ID or clean_model.casefold().startswith(
        "deepseek"
    ):
        return f"DeepSeek · {clean_model.removeprefix('deepseek-')}"
    return clean_model or "自定义模型"


def normalize_service_kind(value: str | None) -> str:
    kind = str(value or "chat").strip().casefold()
    aliases = {
        "chat_completion": "chat",
        "openai_chat_completion": "chat",
        "conversation": "chat",
        "vector": "embedding",
        "openai_embedding": "embedding",
        "image_generation": "image",
        "openai_image": "image",
        "vision": "image",
        "audio": "voice",
        "speech": "voice",
        "tts": "voice",
        "asr": "voice",
    }
    kind = aliases.get(kind, kind)
    if kind not in {"chat", "embedding", "image", "voice"}:
        raise ValueError("模型服务类型不受支持，请重新选择。")
    return kind


def infer_provider_modalities(
    service_kind: str, model: str, declared: object = None,
) -> tuple[str, ...]:
    kind = normalize_service_kind(service_kind)
    if kind == "embedding":
        return ("embedding",)
    if kind == "image":
        return ("image",)
    if kind == "voice":
        return ("audio",)
    # Chat requests always include text, image, and audio inputs. Unsupported
    # media is reported by the configured provider when a request is made.
    return ("text", "image", "audio")


def provider_runtime_fields(service_kind: str) -> dict[str, str]:
    """Model overrides must select the adapter, even with a shared API source."""
    embedding = normalize_service_kind(service_kind) == "embedding"
    return {
        "type": "openai_embedding" if embedding else "openai_chat_completion",
        "provider_type": "embedding" if embedding else "chat_completion",
    }


def repair_embedding_provider_types(payload: dict) -> bool:
    """Repair previously saved vector models without changing IDs or credentials."""
    changed = False
    sources = {item.get("id"): item for item in payload.get("provider_sources", [])
               if isinstance(item, dict)}
    for model in payload.get("provider", []):
        if not isinstance(model, dict) or model.get("service_kind") != "embedding":
            continue
        for key, value in {**provider_runtime_fields("embedding"), "embedding_model": model.get("model", "")}.items():
            if model.get(key) != value:
                model[key] = value
                changed = True
        source = sources.get(model.get("provider_source_id"), model)
        keys = source.get("key", [])
        api_key = keys[0] if isinstance(keys, list) and keys else keys if isinstance(keys, str) else ""
        for key, value in {"embedding_api_base": source.get("api_base", ""), "embedding_api_key": api_key}.items():
            if value and source.get(key) != value:
                source[key] = value
                changed = True
    return changed


@dataclass(frozen=True, slots=True)
class OpenAIProviderProfile:
    """Customer-supplied OpenAI-compatible model service settings."""

    api_key: str
    api_base: str
    model: str
    source_id: str = "commercial_openai"
    thinking_enabled: bool | None = None
    service_kind: str = "chat"
    modalities: tuple[str, ...] = ()

    def normalized(self) -> "OpenAIProviderProfile":
        return OpenAIProviderProfile(
            api_key=self.api_key.strip(),
            api_base=self.api_base.strip().rstrip("/"),
            model=self.model.strip(),
            source_id=self.source_id.strip().casefold(),
            thinking_enabled=self.thinking_enabled,
            service_kind=normalize_service_kind(self.service_kind),
            modalities=infer_provider_modalities(
                self.service_kind, self.model, self.modalities
            ),
        )

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        try:
            profile = self.normalized()
        except ValueError as exc:
            errors.append(str(exc))
            profile = OpenAIProviderProfile(
                api_key=self.api_key,
                api_base=self.api_base,
                model=self.model,
                source_id=self.source_id,
                thinking_enabled=self.thinking_enabled,
                service_kind="chat",
                modalities=self.modalities,
            ).normalized()
        if not profile.api_key:
            errors.append("模型 API Key / Token Plan 不能为空。")
        elif len(profile.api_key) > 4096 or any(
            ord(character) < 32 for character in profile.api_key
        ):
            errors.append("模型 API Key / Token Plan 格式无效。")
        if not profile.model or len(profile.model) > 128:
            errors.append("模型名称不能为空且不能超过 128 个字符。")
        if not _SAFE_ID.fullmatch(profile.source_id):
            errors.append("模型来源 ID 格式无效。")
        if profile.thinking_enabled is not None:
            if not isinstance(profile.thinking_enabled, bool):
                errors.append("思考模式只能选择开启或关闭。")
        parsed = urlsplit(profile.api_base)
        loopback = (parsed.hostname or "").casefold() in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            errors.append("API 地址必须是有效的 HTTP(S) 绝对地址。")
        elif parsed.scheme != "https" and not loopback:
            errors.append("非本机 API 地址必须使用 HTTPS。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            errors.append("API 地址不能包含账号、密码、查询参数或片段。")
        return tuple(errors)

    @property
    def model_id(self) -> str:
        profile = self.normalized()
        return f"{profile.source_id}/{profile.model}"

    def astrbot_source(self) -> dict[str, object]:
        profile = self.normalized()
        errors = profile.validate()
        if errors:
            raise ValueError("；".join(errors))
        return {
            "provider": "openai_chat_completion",
            "type": "openai_chat_completion",
            # AstrBot's provider source remains the OpenAI-compatible transport.
            # The individual model record carries its actual product purpose.
            "provider_type": "chat_completion",
            "key": [profile.api_key],
            "api_base": profile.api_base,
            "embedding_api_base": profile.api_base,
            "embedding_api_key": profile.api_key,
            "timeout": PROVIDER_REQUEST_TIMEOUT_SECONDS,
            "proxy": "",
            "custom_headers": {},
            "id": profile.source_id,
            "enable": True,
        }

    def astrbot_model(self) -> dict[str, object]:
        profile = self.normalized()
        errors = profile.validate()
        if errors:
            raise ValueError("；".join(errors))
        modalities = list(infer_provider_modalities(
            profile.service_kind, profile.model, profile.modalities
        ))
        if profile.service_kind == "chat" and "tool_use" not in modalities:
            modalities.append("tool_use")
        return {
            "id": profile.model_id,
            **provider_runtime_fields(profile.service_kind),
            "enable": True,
            "provider_source_id": profile.source_id,
            "model": profile.model,
            **({"embedding_model": profile.model} if profile.service_kind == "embedding" else {}),
            "service_kind": profile.service_kind,
            "modalities": modalities,
            "custom_extra_body": (
                with_thinking_mode(
                    {}, profile.thinking_enabled,
                    api_base=profile.api_base, model=profile.model,
                )
                if profile.thinking_enabled is not None else {}
            ),
            "max_context_tokens": 131072,
        }


def discover_openai_models(
    *, api_key: str, api_base: str, timeout_seconds: float = 15
) -> tuple[str, ...]:
    """Read an OpenAI-compatible model directory without saving credentials."""
    profile = OpenAIProviderProfile(
        api_key=api_key,
        api_base=api_base,
        model="model-discovery",
        source_id="model_discovery",
    ).normalized()
    errors = profile.validate()
    if errors:
        raise ValueError("；".join(errors))
    endpoint = (
        profile.api_base
        if profile.api_base.casefold().endswith("/models")
        else f"{profile.api_base}/models"
    )
    request = Request(
        endpoint,
        headers={
            "Authorization": f"Bearer {profile.api_key}",
            "Accept": "application/json",
            "User-Agent": "WeChatReBot-Manager/1",
        },
    )
    try:
        with urlopen(request, timeout=max(3.0, min(float(timeout_seconds), 30.0))) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise ValueError("API Key / Token Plan 未通过验证，请检查后重试。") from None
        if exc.code == 404:
            raise ValueError("这个服务没有提供模型列表接口，请手动填写模型名称。") from None
        raise ValueError(f"模型列表读取失败（HTTP {exc.code}），请稍后重试。") from None
    except (OSError, URLError, TimeoutError):
        raise ValueError("模型列表读取超时，请检查接口地址和网络后重试。") from None
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("模型列表过大，已停止读取；请手动填写模型名称。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("模型服务返回的列表格式无法识别。") from None
    candidates: object = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and not isinstance(candidates, list):
        candidates = payload.get("models")
    if not isinstance(candidates, list):
        raise ValueError("模型服务没有返回可选择的模型列表。")
    models: set[str] = set()
    for item in candidates:
        value = item.get("id") if isinstance(item, dict) else item
        model = str(value or "").strip()
        if model and len(model) <= 128 and not any(ord(character) < 32 for character in model):
            models.add(model)
    if not models:
        raise ValueError("模型服务没有返回可选择的模型；请手动填写模型名称。")
    return tuple(sorted(models, key=str.casefold)[:500])


__all__ = [
    "DEEPSEEK_PROVIDER_API_BASE",
    "DEEPSEEK_PROVIDER_MODEL",
    "DEEPSEEK_PROVIDER_SOURCE_ID",
    "MODEL_PROVIDER_PRESETS",
    "DEFAULT_PROVIDER_API_BASE",
    "DEFAULT_PROVIDER_MODEL",
    "DEFAULT_PROVIDER_MODEL_ID",
    "DEFAULT_PROVIDER_SOURCE_ID",
    "OpenAIProviderProfile",
    "PROVIDER_REQUEST_TIMEOUT_SECONDS",
    "discover_openai_models",
    "provider_display_name",
    "normalize_service_kind",
    "infer_provider_modalities",
    "provider_source_id",
    "infer_thinking_mode",
    "supports_thinking_toggle",
    "thinking_mode_adapter",
    "thinking_mode_matches",
    "without_thinking_mode",
    "with_thinking_mode",
]
