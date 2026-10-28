"""Authenticated local engine requests and verified runtime configuration changes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from pathlib import Path
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .plugin_configuration import _dashboard_path, _json_revision, _read_object, _white_label_schema
from .plugin_errors import ConfigConflictError, EngineUnavailableError, PluginCenterError, ProviderTestError
from .customer_logs import sanitize_log_text
from .error_catalog import provider_error_code
from .provider import (
    OpenAIProviderProfile,
    PROVIDER_REQUEST_MAX_ATTEMPTS,
    supports_thinking_toggle,
    thinking_mode_matches,
    without_thinking_mode,
    with_thinking_mode,
)


_LONG_MEME_REQUEST_PATHS = frozenset({
    "/api/plug/meme_manager/community/install",
    "/api/plug/meme_manager/community/install_official_first",
    "/api/plug/meme_manager/packs/import",
    "/api/plug/meme_manager/settings/backup/import",
})

_PROVIDER_TEST_KINDS = frozenset({"chat", "embedding", "image", "voice", "stt", "tts", "rerank"})


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
            timeout = (
                max(self.timeout, 600)
                if method == "POST" and urlsplit(path).path in _LONG_MEME_REQUEST_PATHS
                else max(self.timeout, 60) if method in {"PUT", "PATCH", "POST"}
                else self.timeout
            )
            with urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()
        except (TimeoutError, socket.timeout) as exc:
            raise EngineUnavailableError("操作等待超时，后台可能仍在处理；请刷新已安装列表确认结果。") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise EngineUnavailableError("操作等待超时，后台可能仍在处理；请刷新已安装列表确认结果。") from exc
            raise EngineUnavailableError("回复引擎尚未在线，请先在运行中心启动机器人。") from exc
        except OSError as exc:
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
                    "embedding_api_base": normalized.api_base,
                    "embedding_api_key": normalized.api_key,
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
        expected_modalities = list(model_payload["modalities"])
        if target:
            current_model = dict(
                next(item for item in providers if item.get("id") == target)
            )
            previous_source = next(
                (
                    item for item in sources
                    if item.get("id") == current_model.get("provider_source_id")
                ),
                None,
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
                "service_kind",
                "embedding_model",
                "embedding_api_base",
                "embedding_api_key",
            ):
                current_model.pop(inherited_field, None)
            model_payload = {**model_payload, **current_model}
            model_payload.update(
                {
                    "id": normalized.model_id,
                    "provider_source_id": normalized.source_id,
                    "model": normalized.model,
                    "service_kind": normalized.service_kind,
                    "modalities": expected_modalities,
                    "enable": True,
                }
            )
            if normalized.thinking_enabled is not None:
                extra_body = current_model.get("custom_extra_body")
                if isinstance(previous_source, dict):
                    extra_body = without_thinking_mode(
                        extra_body,
                        api_base=str(previous_source.get("api_base") or ""),
                        model=str(current_model.get("model") or ""),
                    )
                model_payload["custom_extra_body"] = with_thinking_mode(
                    extra_body, normalized.thinking_enabled,
                    api_base=normalized.api_base, model=normalized.model,
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
        raw_modalities = provider.get("modalities") if isinstance(provider, dict) else None
        verified_modalities = (
            {
                str(item).strip().casefold()
                for item in raw_modalities
                if str(item).strip()
            }
            if isinstance(raw_modalities, list)
            else set()
        )
        if (
            not isinstance(source, dict)
            or str(source.get("api_base") or "").rstrip("/") != normalized.api_base
            or not isinstance(provider, dict)
            or str(provider.get("model") or "") != normalized.model
            or str(provider.get("service_kind") or "chat") != normalized.service_kind
            or verified_modalities != set(expected_modalities)
            or (
                normalized.thinking_enabled is not None
                and not thinking_mode_matches(
                    provider.get("custom_extra_body"), normalized.thinking_enabled,
                    api_base=normalized.api_base, model=normalized.model,
                )
            )
        ):
            raise PluginCenterError("模型服务已提交，但没有收到生效确认，请刷新后重试。")

    def set_provider_thinking(self, provider_id: str, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("思考模式只能选择开启或关闭。")
        sources, providers = self._provider_records()
        model = next(
            (item for item in providers if item.get("id") == provider_id), None
        )
        source = next(
            (item for item in sources if isinstance(model, dict) and item.get("id") == model.get("provider_source_id")),
            None,
        )
        if not isinstance(model, dict) or not isinstance(source, dict):
            raise ValueError("所选模型服务不存在，请刷新后重试。")
        if not supports_thinking_toggle(
            str(source.get("api_base") or ""), str(model.get("model") or "")
        ):
            return False
        api_base = str(source.get("api_base") or "")
        model_name = str(model.get("model") or "")
        updated = dict(model)
        updated["custom_extra_body"] = with_thinking_mode(
            model.get("custom_extra_body"), enabled,
            api_base=api_base, model=model_name,
        )
        self.request(
            "PUT", "/api/v1/providers/by-id",
            {"provider_id": provider_id, "config": updated},
        )
        _sources, verified = self._provider_records()
        actual = next((item for item in verified if item.get("id") == provider_id), None)
        body = actual.get("custom_extra_body") if isinstance(actual, dict) else None
        if not thinking_mode_matches(
            body, enabled, api_base=api_base, model=model_name
        ):
            raise PluginCenterError("思考模式已提交，但没有收到生效确认，请刷新后重试。")
        return True

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ProviderTestError("请选择需要测试的模型服务。", code="16001")
        result = self.request(
            "POST",
            "/api/v1/providers/test",
            {"provider_id": provider_id},
        )
        if not isinstance(result, dict) or result.get("id") != provider_id:
            raise ProviderTestError("模型测试没有返回对应服务的结果，请刷新后重试。")
        status = result.get("status")
        if not isinstance(status, str) or status not in {"available", "unavailable"}:
            raise ProviderTestError("模型测试结果格式无法识别，未确认模型可用。")
        if result["status"] != "available" or result.get("error") not in (None, ""):
            code = provider_error_code(result.get("error_type", ""), result.get("status_code")) or "19000"
            detail = str(result.get("error") or "模型没有完成本次测试。")
            raise ProviderTestError(f"模型 {provider_id} 测试失败：{detail}", code=code)
        kind = result.get("test_kind")
        if not isinstance(kind, str) or kind not in _PROVIDER_TEST_KINDS or (kind == "chat" and "response_text" not in result):
            raise ProviderTestError("当前运行核心未返回真实模型测试结果，请更新机器人后重试。")
        response = result.get("response_text", "")
        if not isinstance(response, str) or (kind == "chat" and not response.strip()):
            raise ProviderTestError(f"模型 {provider_id} 返回了空回复，未确认模型可用。")
        elapsed = result.get("elapsed_ms")
        if (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                or elapsed < 0 or (isinstance(elapsed, float) and not math.isfinite(elapsed))):
            raise ProviderTestError("模型测试没有返回有效的耗时记录，请更新机器人后重试。")
        model = result.get("model")
        if not isinstance(model, str) or (kind == "chat" and not model.strip()):
            raise ProviderTestError("模型测试没有返回有效的模型名称，未确认模型可用。")
        return {
            "id": provider_id, "model": sanitize_log_text(model).strip()[:128],
            "status": "available", "test_kind": kind,
            "response_text": sanitize_log_text(response).strip()[:2000],
            "elapsed_ms": elapsed,
        }

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
