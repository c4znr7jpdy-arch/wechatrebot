from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

DEFAULT_PROVIDER_SOURCE_ID = "jyjapi"
DEFAULT_PROVIDER_API_BASE = "https://api.xiuxianjyj.xin/v1"
DEFAULT_PROVIDER_MODEL = "grok-4.6"
DEFAULT_PROVIDER_MODEL_ID = (
    f"{DEFAULT_PROVIDER_SOURCE_ID}/{DEFAULT_PROVIDER_MODEL}"
)
DEEPSEEK_PROVIDER_SOURCE_ID = "deepseek"
DEEPSEEK_PROVIDER_API_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_PROVIDER_MODEL = "deepseek-v4-flash"
PROVIDER_REQUEST_TIMEOUT_SECONDS = 35
# AstrBot's provider layer retries connection errors on top of the OpenAI SDK.
# Keep one product-level attempt so a failed endpoint cannot hold a group reply
# for several minutes. The SDK retry is disabled by the bundled runtime patch.
PROVIDER_REQUEST_MAX_ATTEMPTS = 1


def thinking_mode_adapter(api_base: str, model: str) -> str | None:
    """Return the safe request mapping for a known OpenAI-compatible service."""
    host = (urlsplit(api_base.strip()).hostname or "").casefold()
    normalized_model = model.strip().casefold()
    if (
        host == "xiaomimimo.com" or host.endswith(".xiaomimimo.com")
    ) and normalized_model in {"mimo-v2.5", "mimo-v2.5-pro"}:
        return "thinking"
    if (
        host == "deepseek.com" or host.endswith(".deepseek.com")
    ) and normalized_model.startswith(
        ("deepseek-chat", "deepseek-flash", "deepseek-v3", "deepseek-v4")
    ):
        return "thinking"
    if (
        host == "dashscope.aliyuncs.com"
        or host.endswith(".dashscope.aliyuncs.com")
    ) and "thinking" not in normalized_model and normalized_model.startswith(
        ("qwen-plus", "qwen3", "deepseek-v3", "deepseek-v4", "kimi-k2", "glm-")
    ):
        return "enable_thinking"
    if (
        host == "x.ai"
        or host.endswith(".x.ai")
        or host == "xiuxianjyj.xin"
        or host.endswith(".xiuxianjyj.xin")
    ) and normalized_model.startswith(("grok-4.5", "grok-4.6")):
        # Current Grok reasoning models cannot fully disable reasoning. Treat
        # the off position as the lowest supported effort instead.
        return "reasoning_effort_low"
    if (
        host == "googleapis.com" or host.endswith(".googleapis.com")
    ) and normalized_model.startswith(
        ("gemini-2.5-flash", "gemini-2.5-flash-lite")
    ):
        return "reasoning_effort_none"
    openai_match = re.match(r"^gpt-5\.(\d+)", normalized_model)
    if (
        host == "openai.com" or host.endswith(".openai.com")
    ) and openai_match is not None and int(openai_match.group(1)) >= 1 and (
        "pro" not in normalized_model
    ):
        return "reasoning_effort_none"
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
    elif adapter in {"reasoning_effort_low", "reasoning_effort_none"}:
        merged.pop("reasoning_effort", None)
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
    return body.get("reasoning_effort") == ("high" if enabled else "none")


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


@dataclass(frozen=True, slots=True)
class OpenAIProviderProfile:
    """Customer-supplied OpenAI-compatible chat provider settings."""

    api_key: str
    api_base: str
    model: str
    source_id: str = "commercial_openai"
    thinking_enabled: bool | None = None

    def normalized(self) -> "OpenAIProviderProfile":
        return OpenAIProviderProfile(
            api_key=self.api_key.strip(),
            api_base=self.api_base.strip().rstrip("/"),
            model=self.model.strip(),
            source_id=self.source_id.strip().casefold(),
            thinking_enabled=self.thinking_enabled,
        )

    def validate(self) -> tuple[str, ...]:
        profile = self.normalized()
        errors: list[str] = []
        if not profile.api_key:
            errors.append("模型 API Key 不能为空。")
        elif len(profile.api_key) > 4096 or any(
            ord(character) < 32 for character in profile.api_key
        ):
            errors.append("模型 API Key 格式无效。")
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
            "provider_type": "chat_completion",
            "key": [profile.api_key],
            "api_base": profile.api_base,
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
        modalities = ["text", "tool_use"]
        if (
            profile.source_id == DEFAULT_PROVIDER_SOURCE_ID
            and profile.model == DEFAULT_PROVIDER_MODEL
        ):
            modalities = ["text", "image", "audio", "tool_use"]
        return {
            "id": profile.model_id,
            "enable": True,
            "provider_source_id": profile.source_id,
            "model": profile.model,
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
            raise ValueError("服务密钥未通过验证，请检查后重试。") from None
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
    "DEFAULT_PROVIDER_API_BASE",
    "DEFAULT_PROVIDER_MODEL",
    "DEFAULT_PROVIDER_MODEL_ID",
    "DEFAULT_PROVIDER_SOURCE_ID",
    "OpenAIProviderProfile",
    "PROVIDER_REQUEST_TIMEOUT_SECONDS",
    "discover_openai_models",
    "provider_display_name",
    "provider_source_id",
    "infer_thinking_mode",
    "supports_thinking_toggle",
    "thinking_mode_adapter",
    "thinking_mode_matches",
    "without_thinking_mode",
    "with_thinking_mode",
]
