"""Runtime-configurable image generation backends for ``jiang_image``."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import tempfile
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


PLUGIN_NAME = "jiang_image"
CONFIG_VERSION = 1
SUPPORTED_PROTOCOLS = {"openai_images", "openai_chat", "minimax"}
SUPPORTED_AUTH_TYPES = {"bearer", "header", "none"}
SUPPORTED_SIZE_MODES = {"pixels", "ratio_size", "aspect_ratio", "omit"}
PROFILE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")

ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "4:3": "1024x768",
    "3:4": "768x1024",
    "3:2": "1024x683",
    "2:3": "683x1024",
}


class ImageModelConfigError(ValueError):
    """Raised when a model profile is invalid."""


class ImageModelManager:
    """Persist model profiles and expose an atomic, hot-reloadable runtime."""

    def __init__(self, plugin_dir: Path, data_dir: Path, logger) -> None:
        self.plugin_dir = Path(plugin_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir = self.data_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "models.json"
        self.project_dir = self.plugin_dir.parent.parent.parent
        self.cmd_config_path = self.project_dir / "data" / "cmd_config.json"
        self.env_file = self.project_dir / "ai_plugin" / ".env.prod"
        self.logger = logger
        self._config: dict[str, Any] = {}

    def initialize(self) -> None:
        if not self.config_path.exists():
            self._write_config(self._default_config())
        self.reload()
        self.cleanup_expired()

    def reload(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.config_path.read_text("utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ImageModelConfigError(f"读取模型配置失败: {exc}") from exc
        self._config = self._validate_config(raw)
        return self.public_config()

    def public_config(self) -> dict[str, Any]:
        result = deepcopy(self._config)
        for profile in result.get("models", []):
            secret = self._resolve_api_key(profile)
            profile.pop("api_key", None)
            profile["has_api_key"] = bool(secret)
            profile["api_key_mask"] = self._mask_secret(secret)
        result["config_path"] = str(self.config_path)
        return result

    def active_profile(self) -> dict[str, Any] | None:
        return self.get_profile(str(self._config.get("active_model", "")))

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        for profile in self._config.get("models", []):
            if profile.get("id") == profile_id:
                return deepcopy(profile)
        return None

    def generation_chain(self) -> list[dict[str, Any]]:
        active_id = str(self._config.get("active_model", ""))
        enabled = [
            deepcopy(item)
            for item in self._config.get("models", [])
            if bool(item.get("enabled", True))
        ]
        active = [item for item in enabled if item["id"] == active_id]
        fallbacks = sorted(
            (
                item
                for item in enabled
                if item["id"] != active_id and int(item.get("fallback_priority", 0)) > 0
            ),
            key=lambda item: (int(item.get("fallback_priority", 0)), item["name"]),
        )
        return active + fallbacks

    def upsert_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ImageModelConfigError("模型配置必须是 JSON 对象")
        existing = self.get_profile(str(payload.get("id", "")).strip())
        normalized = self._validate_profile(payload, existing=existing)
        models = [
            item
            for item in self._config.get("models", [])
            if item.get("id") != normalized["id"]
        ]
        models.append(normalized)
        updated = deepcopy(self._config)
        updated["models"] = models
        if not updated.get("active_model"):
            updated["active_model"] = normalized["id"]
        self._commit(updated)
        return self.public_config()

    def delete_profile(self, profile_id: str) -> dict[str, Any]:
        profile_id = str(profile_id or "").strip()
        models = [
            item for item in self._config.get("models", []) if item.get("id") != profile_id
        ]
        if len(models) == len(self._config.get("models", [])):
            raise ImageModelConfigError(f"模型不存在: {profile_id}")
        if not models:
            raise ImageModelConfigError("至少保留一个模型")
        updated = deepcopy(self._config)
        updated["models"] = models
        if updated.get("active_model") == profile_id:
            enabled = [item for item in models if item.get("enabled", True)]
            updated["active_model"] = (enabled or models)[0]["id"]
        self._commit(updated)
        return self.public_config()

    def set_active(self, profile_id: str) -> dict[str, Any]:
        profile = self.get_profile(str(profile_id or "").strip())
        if profile is None:
            raise ImageModelConfigError(f"模型不存在: {profile_id}")
        if not profile.get("enabled", True):
            raise ImageModelConfigError("不能将已停用模型设为默认")
        updated = deepcopy(self._config)
        updated["active_model"] = profile["id"]
        self._commit(updated)
        return self.public_config()

    async def test_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise ImageModelConfigError(f"模型不存在: {profile_id}")
        api_key = self._require_api_key(profile)
        path = str(profile.get("models_path") or "models")
        url = self._endpoint(profile["base_url"], path)
        started = time.monotonic()
        async with self._client(profile, timeout_cap=45) as client:
            response = await client.get(url, headers=self._headers(profile, api_key))
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if response.status_code != 200:
            raise RuntimeError(
                f"连接检查失败: HTTP {response.status_code} {self._safe_response_text(response)}"
            )
        data = response.json()
        model_ids = self._extract_model_ids(data)
        configured_model = str(profile["model"])
        return {
            "ok": True,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "configured_model": configured_model,
            "model_found": configured_model in model_ids if model_ids else None,
            "model_count": len(model_ids),
            "image_models": [
                item
                for item in model_ids
                if re.search(
                    r"image|banana|seedream|flux|dall|midjourney|kolors|imagine",
                    item,
                    re.IGNORECASE,
                )
            ][:50],
        }

    async def generate(
        self,
        profile: dict[str, Any],
        prompt: str,
        n: int = 1,
        aspect_ratio: str = "1:1",
    ) -> list[str]:
        protocol = profile["protocol"]
        if protocol == "openai_images":
            data = await self._generate_openai_images(profile, prompt, n, aspect_ratio)
        elif protocol == "openai_chat":
            data = await self._generate_openai_chat(profile, prompt, n, aspect_ratio)
        elif protocol == "minimax":
            data = await self._generate_minimax(profile, prompt, n, aspect_ratio)
        else:
            raise ImageModelConfigError(f"不支持的生图协议: {protocol}")
        # ``n`` is the number requested by the caller.  Some OpenAI-compatible
        # providers ignore it and return more images, so enforce the same
        # contract when materializing the response as local files.
        paths = await self._save_response_images(data, max_images=n)
        if not paths:
            raise RuntimeError("接口返回成功，但没有找到图片 URL 或 base64 数据")
        return paths

    async def edit(
        self,
        profile: dict[str, Any],
        image_path: str,
        prompt: str,
        n: int = 1,
        aspect_ratio: str = "1:1",
    ) -> list[str]:
        if not profile.get("supports_edit", False):
            raise ImageModelConfigError(f"模型 {profile['name']} 未启用图生图")
        protocol = profile["protocol"]
        if protocol == "openai_images":
            data = await self._edit_openai_images(
                profile, image_path, prompt, n, aspect_ratio
            )
        elif protocol == "openai_chat":
            data = await self._edit_openai_chat(
                profile, image_path, prompt, n, aspect_ratio
            )
        elif protocol == "minimax":
            data = await self._edit_minimax(
                profile, image_path, prompt, n, aspect_ratio
            )
        else:
            raise ImageModelConfigError(f"不支持的图生图协议: {protocol}")
        paths = await self._save_response_images(data, max_images=n)
        if not paths:
            raise RuntimeError("接口返回成功，但没有找到编辑后的图片")
        return paths

    async def _generate_openai_images(
        self, profile: dict[str, Any], prompt: str, n: int, aspect_ratio: str
    ) -> dict[str, Any]:
        api_key = self._require_api_key(profile)
        payload: dict[str, Any] = {
            "model": profile["model"],
            "prompt": prompt,
            "n": n,
        }
        self._apply_size(payload, profile, aspect_ratio)
        response_format = str(profile.get("response_format") or "").strip()
        if response_format:
            payload["response_format"] = response_format
        payload.update(deepcopy(profile.get("extra_body") or {}))
        # extra_body is user-configurable, but must not override the per-call
        # image count (the plugin currently requests one image).
        payload["n"] = n
        url = self._endpoint(
            profile["base_url"],
            str(profile.get("generation_path") or "images/generations"),
        )
        return await self._post_json(profile, url, api_key, payload)

    async def _generate_openai_chat(
        self, profile: dict[str, Any], prompt: str, n: int, aspect_ratio: str
    ) -> dict[str, Any]:
        api_key = self._require_api_key(profile)
        payload: dict[str, Any] = {
            "model": profile["model"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        self._apply_size(payload, profile, aspect_ratio)
        payload.update(deepcopy(profile.get("extra_body") or {}))
        if n > 1:
            payload["n"] = n
        else:
            # One image is the endpoint default; remove a stale n from
            # extra_body without forcing chat providers to parse n=1.
            payload.pop("n", None)
        url = self._endpoint(
            profile["base_url"],
            str(profile.get("generation_path") or "chat/completions"),
        )
        return await self._post_json(profile, url, api_key, payload)

    async def _generate_minimax(
        self, profile: dict[str, Any], prompt: str, n: int, aspect_ratio: str
    ) -> dict[str, Any]:
        api_key = self._require_api_key(profile)
        payload: dict[str, Any] = {
            "model": profile["model"],
            "prompt": prompt,
            "n": n,
            "aspect_ratio": aspect_ratio,
            "response_format": profile.get("response_format") or "url",
            "prompt_optimizer": True,
        }
        payload.update(deepcopy(profile.get("extra_body") or {}))
        payload["n"] = n
        url = self._endpoint(
            profile["base_url"],
            str(profile.get("generation_path") or "image_generation"),
        )
        return await self._post_json(profile, url, api_key, payload)

    async def _edit_openai_images(
        self,
        profile: dict[str, Any],
        image_path: str,
        prompt: str,
        n: int,
        aspect_ratio: str,
    ) -> dict[str, Any]:
        api_key = self._require_api_key(profile)
        form: dict[str, str] = {
            "model": str(profile["model"]),
            "prompt": prompt,
            "n": str(n),
        }
        size_mode = profile.get("size_mode", "pixels")
        if size_mode == "pixels":
            form["size"] = ASPECT_TO_SIZE.get(aspect_ratio, "1024x1024")
        elif size_mode == "ratio_size":
            form["size"] = aspect_ratio
        elif size_mode == "aspect_ratio":
            form["aspect_ratio"] = aspect_ratio
        response_format = str(profile.get("response_format") or "").strip()
        if response_format:
            form["response_format"] = response_format
        for key, value in (profile.get("extra_body") or {}).items():
            form[str(key)] = value if isinstance(value, str) else json.dumps(value)
        form["n"] = str(n)
        url = self._endpoint(
            profile["base_url"],
            str(profile.get("edit_path") or "images/edits"),
        )
        mime = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
        with open(image_path, "rb") as image_file:
            files = {"image": (Path(image_path).name, image_file, mime)}
            async with self._client(profile) as client:
                response = await client.post(
                    url,
                    headers=self._headers(profile, api_key),
                    data=form,
                    files=files,
                )
        return self._response_json(response, "图片编辑")

    async def _edit_openai_chat(
        self,
        profile: dict[str, Any],
        image_path: str,
        prompt: str,
        n: int,
        aspect_ratio: str,
    ) -> dict[str, Any]:
        api_key = self._require_api_key(profile)
        image_uri = self._encode_image_data_uri(Path(image_path))
        payload: dict[str, Any] = {
            "model": profile["model"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_uri}},
                    ],
                }
            ],
            "stream": False,
        }
        self._apply_size(payload, profile, aspect_ratio)
        payload.update(deepcopy(profile.get("extra_body") or {}))
        if n > 1:
            payload["n"] = n
        else:
            payload.pop("n", None)
        url = self._endpoint(
            profile["base_url"],
            str(profile.get("edit_path") or profile.get("generation_path") or "chat/completions"),
        )
        return await self._post_json(profile, url, api_key, payload)

    async def _edit_minimax(
        self,
        profile: dict[str, Any],
        image_path: str,
        prompt: str,
        n: int,
        aspect_ratio: str,
    ) -> dict[str, Any]:
        api_key = self._require_api_key(profile)
        payload: dict[str, Any] = {
            "model": profile["model"],
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "subject_reference": [
                {
                    "type": "character",
                    "image_file": self._encode_image_data_uri(Path(image_path)),
                }
            ],
            "n": n,
            "response_format": profile.get("response_format") or "url",
        }
        payload.update(deepcopy(profile.get("extra_body") or {}))
        payload["n"] = n
        url = self._endpoint(
            profile["base_url"],
            str(profile.get("edit_path") or profile.get("generation_path") or "image_generation"),
        )
        return await self._post_json(profile, url, api_key, payload)

    async def _post_json(
        self,
        profile: dict[str, Any],
        url: str,
        api_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._client(profile) as client:
            response = await client.post(
                url,
                headers={
                    **self._headers(profile, api_key),
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        return self._response_json(response, "图片生成")

    def _client(
        self, profile: dict[str, Any], timeout_cap: int | None = None
    ) -> httpx.AsyncClient:
        timeout = int(profile.get("timeout_seconds", 300))
        if timeout_cap is not None:
            timeout = min(timeout, timeout_cap)
        return httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout), connect=min(float(timeout), 15.0)),
            follow_redirects=True,
            trust_env=bool(profile.get("trust_env", False)),
        )

    def _headers(self, profile: dict[str, Any], api_key: str) -> dict[str, str]:
        headers = {
            str(key): str(value)
            for key, value in (profile.get("extra_headers") or {}).items()
        }
        auth_type = profile.get("auth_type", "bearer")
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_type == "header":
            headers[str(profile.get("api_key_header") or "X-API-Key")] = api_key
        return headers

    def _require_api_key(self, profile: dict[str, Any]) -> str:
        if profile.get("auth_type") == "none":
            return ""
        api_key = self._resolve_api_key(profile)
        if not api_key:
            source = profile.get("api_key_source", {})
            raise ImageModelConfigError(
                f"模型 {profile['name']} 没有可用 API Key（来源: {source.get('type', 'stored')}）"
            )
        return api_key

    def _resolve_api_key(self, profile: dict[str, Any]) -> str:
        source = profile.get("api_key_source") or {"type": "stored"}
        source_type = source.get("type", "stored")
        if source_type == "stored":
            return str(profile.get("api_key") or "").strip()
        if source_type == "environment":
            env_name = str(source.get("env_name") or "").strip()
            if not env_name:
                return ""
            if os.getenv(env_name):
                return str(os.environ[env_name]).strip()
            return self._load_env_file().get(env_name, "")
        if source_type == "astrbot_provider":
            provider_id = str(source.get("provider_id") or "").strip()
            return self._provider_api_key(provider_id)
        return ""

    def _provider_api_key(self, provider_id: str) -> str:
        try:
            data = json.loads(self.cmd_config_path.read_text("utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return ""
        for provider in data.get("provider", []):
            if not isinstance(provider, dict) or str(provider.get("id")) != provider_id:
                continue
            for key_name in ("api_key", "key", "embedding_api_key"):
                value = provider.get(key_name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _load_env_file(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if not self.env_file.exists():
            return values
        for line in self.env_file.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    def _default_config(self) -> dict[str, Any]:
        env = self._load_env_file()
        models: list[dict[str, Any]] = [
            {
                "id": "hostcentral-gpt-image-2",
                "name": "HostCentral GPT Image 2",
                "enabled": True,
                "protocol": "openai_images",
                "base_url": "https://api.hostcentral.cc/",
                "model": "gpt-image-2",
                "generation_path": "images/generations",
                "edit_path": "images/edits",
                "models_path": "models",
                "supports_edit": True,
                "size_mode": "pixels",
                "response_format": "url",
                "timeout_seconds": 300,
                "trust_env": False,
                "auth_type": "bearer",
                "api_key_header": "X-API-Key",
                "api_key_source": {
                    "type": "environment",
                    "env_name": "GPT_IMAGE_API_KEY",
                },
                "api_key": "",
                "extra_headers": {},
                "extra_body": {},
                "fallback_priority": 0,
            },
            {
                "id": "gemai-gpt-image-2k",
                "name": "GemAI GPT Image 2K",
                "enabled": True,
                "protocol": "openai_images",
                "base_url": "https://api.gemai.cc/v1",
                "model": "gpt-image-2-2K",
                "generation_path": "images/generations",
                "edit_path": "images/edits",
                "models_path": "models",
                "supports_edit": True,
                "size_mode": "ratio_size",
                "response_format": "",
                "timeout_seconds": 300,
                "trust_env": False,
                "auth_type": "bearer",
                "api_key_header": "X-API-Key",
                "api_key_source": {
                    "type": "astrbot_provider",
                    "provider_id": "openai_embedding",
                },
                "api_key": "",
                "extra_headers": {},
                "extra_body": {},
                "fallback_priority": 1,
            }
        ]
        old_base = env.get("GPT_IMAGE_BASE_URL", "").rstrip("/")
        old_key = env.get("GPT_IMAGE_API_KEY", "")
        if old_key and old_base and "gemai.cc" not in old_base.lower():
            models.append(
                {
                    "id": "legacy-gpt-image",
                    "name": "旧 GPT Image 后端",
                    "enabled": True,
                    "protocol": "openai_images",
                    "base_url": old_base,
                    "model": env.get("GPT_IMAGE_MODEL", "gpt-image-2"),
                    "generation_path": "images/generations",
                    "edit_path": "images/edits",
                    "models_path": "models",
                    "supports_edit": True,
                    "size_mode": "pixels",
                    "response_format": "url",
                    "timeout_seconds": 300,
                    "trust_env": False,
                    "auth_type": "bearer",
                    "api_key_header": "X-API-Key",
                    "api_key_source": {
                        "type": "environment",
                        "env_name": "GPT_IMAGE_API_KEY",
                    },
                    "api_key": "",
                    "extra_headers": {},
                    "extra_body": {},
                    "fallback_priority": 2,
                }
            )
        if env.get("MINIMAX_API_KEY"):
            models.append(
                {
                    "id": "minimax-image-01",
                    "name": "MiniMax image-01",
                    "enabled": True,
                    "protocol": "minimax",
                    "base_url": env.get(
                        "MINIMAX_API_BASE_URL", "https://api.minimaxi.com/v1"
                    ),
                    "model": env.get("MINIMAX_IMAGE_MODEL", "image-01"),
                    "generation_path": "image_generation",
                    "edit_path": "image_generation",
                    "models_path": "models",
                    "supports_edit": True,
                    "size_mode": "aspect_ratio",
                    "response_format": "url",
                    "timeout_seconds": 180,
                    "trust_env": True,
                    "auth_type": "bearer",
                    "api_key_header": "X-API-Key",
                    "api_key_source": {
                        "type": "environment",
                        "env_name": "MINIMAX_API_KEY",
                    },
                    "api_key": "",
                    "extra_headers": {},
                    "extra_body": {},
                    "fallback_priority": 3,
                }
            )
        return {
            "version": CONFIG_VERSION,
            "active_model": "hostcentral-gpt-image-2",
            "retention_hours": 2,
            "models": models,
        }

    def _validate_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ImageModelConfigError("模型配置根节点必须是 JSON 对象")
        models_raw = raw.get("models")
        if not isinstance(models_raw, list) or not models_raw:
            raise ImageModelConfigError("models 必须是非空数组")
        models: list[dict[str, Any]] = []
        ids: set[str] = set()
        for item in models_raw:
            normalized = self._validate_profile(item)
            if normalized["id"] in ids:
                raise ImageModelConfigError(f"模型 ID 重复: {normalized['id']}")
            ids.add(normalized["id"])
            models.append(normalized)
        active = str(raw.get("active_model") or "").strip()
        active_profile = next((item for item in models if item["id"] == active), None)
        if active_profile is None or not active_profile.get("enabled", True):
            active = next(
                (item["id"] for item in models if item.get("enabled", True)),
                models[0]["id"],
            )
        retention = self._bounded_int(raw.get("retention_hours", 2), 1, 720, "保留时长")
        return {
            "version": CONFIG_VERSION,
            "active_model": active,
            "retention_hours": retention,
            "models": models,
        }

    def _validate_profile(
        self, raw: dict[str, Any], existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ImageModelConfigError("单个模型配置必须是 JSON 对象")
        profile_id = str(raw.get("id") or "").strip()
        if not PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise ImageModelConfigError(
                "模型 ID 仅允许字母、数字、点、下划线和短横线，最长 64 个字符"
            )
        name = str(raw.get("name") or "").strip()
        if not name or len(name) > 100:
            raise ImageModelConfigError("显示名称不能为空且不能超过 100 个字符")
        protocol = str(raw.get("protocol") or "openai_images").strip()
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ImageModelConfigError(f"不支持的协议: {protocol}")
        base_url = str(raw.get("base_url") or "").strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ImageModelConfigError("API Base URL 必须是有效的 http/https 地址")
        if parsed.username or parsed.password:
            raise ImageModelConfigError("API Base URL 不能包含用户名或密码")
        model = str(raw.get("model") or "").strip()
        if not model or len(model) > 200:
            raise ImageModelConfigError("模型名不能为空且不能超过 200 个字符")
        auth_type = str(raw.get("auth_type") or "bearer").strip()
        if auth_type not in SUPPORTED_AUTH_TYPES:
            raise ImageModelConfigError(f"不支持的认证方式: {auth_type}")
        source = raw.get("api_key_source") or {"type": "stored"}
        if not isinstance(source, dict):
            raise ImageModelConfigError("API Key 来源配置必须是对象")
        source_type = str(source.get("type") or "stored").strip()
        if source_type not in {"stored", "environment", "astrbot_provider"}:
            raise ImageModelConfigError(f"不支持的 API Key 来源: {source_type}")
        normalized_source: dict[str, str] = {"type": source_type}
        if source_type == "environment":
            env_name = str(source.get("env_name") or "").strip()
            if not ENV_NAME_PATTERN.fullmatch(env_name):
                raise ImageModelConfigError("环境变量名格式不正确")
            normalized_source["env_name"] = env_name
        elif source_type == "astrbot_provider":
            provider_id = str(source.get("provider_id") or "").strip()
            if not provider_id or len(provider_id) > 128:
                raise ImageModelConfigError("AstrBot Provider ID 不能为空")
            normalized_source["provider_id"] = provider_id
        api_key = str(raw.get("api_key") or "").strip()
        if source_type == "stored" and not api_key and existing:
            api_key = str(existing.get("api_key") or "").strip()
        extra_headers = self._json_object(raw.get("extra_headers"), "额外请求头")
        extra_body = self._json_object(raw.get("extra_body"), "额外请求参数")
        size_mode = str(raw.get("size_mode") or "pixels").strip()
        if size_mode not in SUPPORTED_SIZE_MODES:
            raise ImageModelConfigError(f"不支持的尺寸模式: {size_mode}")
        return {
            "id": profile_id,
            "name": name,
            "enabled": bool(raw.get("enabled", True)),
            "protocol": protocol,
            "base_url": base_url,
            "model": model,
            "generation_path": self._clean_path(
                raw.get("generation_path"),
                "chat/completions" if protocol == "openai_chat" else (
                    "image_generation" if protocol == "minimax" else "images/generations"
                ),
            ),
            "edit_path": self._clean_path(
                raw.get("edit_path"),
                "image_generation" if protocol == "minimax" else (
                    "chat/completions" if protocol == "openai_chat" else "images/edits"
                ),
            ),
            "models_path": self._clean_path(raw.get("models_path"), "models"),
            "supports_edit": bool(raw.get("supports_edit", False)),
            "size_mode": size_mode,
            "response_format": str(raw.get("response_format") or "").strip(),
            "timeout_seconds": self._bounded_int(
                raw.get("timeout_seconds", 300), 5, 900, "超时时间"
            ),
            "trust_env": bool(raw.get("trust_env", False)),
            "auth_type": auth_type,
            "api_key_header": str(raw.get("api_key_header") or "X-API-Key").strip(),
            "api_key_source": normalized_source,
            "api_key": api_key,
            "extra_headers": extra_headers,
            "extra_body": extra_body,
            "fallback_priority": self._bounded_int(
                raw.get("fallback_priority", 0), 0, 99, "回退优先级"
            ),
        }

    @staticmethod
    def _json_object(value: Any, label: str) -> dict[str, Any]:
        if value in (None, ""):
            return {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ImageModelConfigError(f"{label}不是合法 JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ImageModelConfigError(f"{label}必须是 JSON 对象")
        return deepcopy(value)

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ImageModelConfigError(f"{label}必须是整数") from exc
        if result < minimum or result > maximum:
            raise ImageModelConfigError(f"{label}必须在 {minimum}-{maximum} 之间")
        return result

    @staticmethod
    def _clean_path(value: Any, default: str) -> str:
        path = str(value or default).strip()
        if not path or len(path) > 500:
            raise ImageModelConfigError("API 路径为空或过长")
        if any(char in path for char in ("\r", "\n")):
            raise ImageModelConfigError("API 路径包含非法字符")
        return path

    def _commit(self, config: dict[str, Any]) -> None:
        validated = self._validate_config(config)
        self._write_config(validated)
        self._config = validated
        self.logger.info(
            "[IMAGE] 模型配置已热更新: active=%s, models=%d",
            validated["active_model"],
            len(validated["models"]),
        )

    def _write_config(self, config: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix="models-", suffix=".tmp", dir=str(self.data_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.config_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _endpoint(base_url: str, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        base = base_url.rstrip("/")
        clean_path = path.lstrip("/")
        if clean_path.startswith("v1/"):
            if base.endswith("/v1"):
                base = base[:-3].rstrip("/")
            return f"{base}/{clean_path}"
        if base.endswith("/v1"):
            return f"{base}/{clean_path}"
        return f"{base}/v1/{clean_path}"

    @staticmethod
    def _apply_size(
        payload: dict[str, Any], profile: dict[str, Any], aspect_ratio: str
    ) -> None:
        mode = profile.get("size_mode", "pixels")
        if mode == "pixels":
            payload["size"] = ASPECT_TO_SIZE.get(aspect_ratio, "1024x1024")
        elif mode == "ratio_size":
            payload["size"] = aspect_ratio
        elif mode == "aspect_ratio":
            payload["aspect_ratio"] = aspect_ratio

    @staticmethod
    def _response_json(response: httpx.Response, action: str) -> dict[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(
                f"{action}失败: HTTP {response.status_code} "
                f"{ImageModelManager._safe_response_text(response)}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{action}接口没有返回 JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"{action}接口返回结构不是 JSON 对象")
        base_resp = data.get("base_resp")
        if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0):
            raise RuntimeError(
                f"{action}失败: {base_resp.get('status_msg') or base_resp}"
            )
        return data

    @staticmethod
    def _safe_response_text(response: httpx.Response) -> str:
        text = response.text.replace("\r", " ").replace("\n", " ").strip()
        return text[:800]

    @staticmethod
    def _extract_model_ids(data: Any) -> list[str]:
        if isinstance(data, dict):
            items = data.get("data")
            if isinstance(items, list):
                return sorted(
                    {
                        str(item.get("id"))
                        for item in items
                        if isinstance(item, dict) and item.get("id")
                    }
                )
        return []

    async def _save_response_images(
        self, data: dict[str, Any], max_images: int | None = None
    ) -> list[str]:
        candidates = self._extract_image_candidates(data)
        if max_images is not None:
            max_images = max(1, int(max_images))
            if len(candidates) > max_images:
                self.logger.info(
                    "[IMAGE] 上游返回 %d 张图片，按请求数量截取前 %d 张",
                    len(candidates),
                    max_images,
                )
                candidates = candidates[:max_images]
        paths: list[str] = []
        for index, candidate in enumerate(candidates):
            try:
                paths.append(await self._save_candidate(candidate, index))
            except Exception as exc:
                self.logger.warning("[IMAGE] 保存第 %d 张返回图片失败: %s", index + 1, exc)
        return paths

    @classmethod
    def _extract_image_candidates(cls, data: Any) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(kind: str, value: str) -> None:
            value = value.strip()
            if not value or value in seen:
                return
            seen.add(value)
            found.append({"kind": kind, "value": value})

        def walk(value: Any, key: str = "") -> None:
            key_lower = key.lower()
            if isinstance(value, dict):
                for child_key, child in value.items():
                    walk(child, str(child_key))
                return
            if isinstance(value, list):
                for child in value:
                    walk(child, key)
                return
            if not isinstance(value, str):
                return
            text = value.strip()
            if key_lower in {"b64_json", "base64", "image_base64"} and len(text) > 100:
                add("base64", text)
            if text.startswith("data:image/"):
                add("data_uri", text)
            if text.startswith(("http://", "https://")) and (
                any(token in key_lower for token in ("url", "image", "output", "result"))
                or re.search(r"\.(png|jpe?g|webp|gif)(?:\?|$)", text, re.IGNORECASE)
            ):
                add("url", text)
            for match in re.finditer(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", text):
                add("url", match.group(1))
            for match in re.finditer(r"(data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+)", text):
                add("data_uri", re.sub(r"\s+", "", match.group(1)))

        walk(data)
        return found

    async def _save_candidate(self, candidate: dict[str, str], index: int) -> str:
        kind = candidate["kind"]
        value = candidate["value"]
        if kind == "url":
            async with httpx.AsyncClient(
                timeout=120.0, follow_redirects=True
            ) as client:
                response = await client.get(value)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                suffix = mimetypes.guess_extension(content_type) or Path(
                    urlparse(value).path
                ).suffix
                suffix = suffix if suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else ".png"
                return self._write_image_bytes(response.content, suffix, index)
        if kind == "data_uri":
            header, encoded = value.split(",", 1)
            mime = header.split(";", 1)[0].split(":", 1)[-1]
            suffix = mimetypes.guess_extension(mime) or ".png"
            return self._write_image_bytes(base64.b64decode(encoded), suffix, index)
        decoded = base64.b64decode(value)
        suffix = self._guess_image_suffix(decoded)
        return self._write_image_bytes(decoded, suffix, index)

    def _write_image_bytes(self, content: bytes, suffix: str, index: int) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{uuid.uuid4().hex[:8]}_{index}{suffix.lower()}"
        path = self.image_dir / filename
        path.write_bytes(content)
        return str(path)

    @staticmethod
    def _guess_image_suffix(content: bytes) -> str:
        if content.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return ".webp"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        return ".png"

    @staticmethod
    def _encode_image_data_uri(path: Path) -> str:
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    def cleanup_expired(self) -> int:
        retention = int(self._config.get("retention_hours", 2) or 2)
        cutoff = datetime.now() - timedelta(hours=retention)
        deleted = 0
        if not self.image_dir.exists():
            return deleted
        for path in self.image_dir.iterdir():
            if not path.is_file():
                continue
            try:
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError as exc:
                self.logger.warning("[IMAGE] 清理图片失败 %s: %s", path, exc)
        return deleted

    @staticmethod
    def _mask_secret(secret: str) -> str:
        if not secret:
            return ""
        if len(secret) <= 8:
            return "••••••••"
        return f"{secret[:3]}••••••{secret[-4:]}"
