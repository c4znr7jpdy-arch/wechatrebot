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

    def normalized(self) -> "OpenAIProviderProfile":
        return OpenAIProviderProfile(
            api_key=self.api_key.strip(),
            api_base=self.api_base.strip().rstrip("/"),
            model=self.model.strip(),
            source_id=self.source_id.strip().casefold(),
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
            "custom_extra_body": {},
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
]
