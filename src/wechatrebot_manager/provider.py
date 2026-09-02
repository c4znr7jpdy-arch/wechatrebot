from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


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
            "timeout": 120,
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
        return {
            "id": profile.model_id,
            "enable": True,
            "provider_source_id": profile.source_id,
            "model": profile.model,
            "modalities": ["text", "tool_use"],
            "custom_extra_body": {},
            "max_context_tokens": 131072,
        }
