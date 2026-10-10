"""Model service configuration and dependent feature bindings."""
from __future__ import annotations

import json
from typing import Any

from .customer_config import (
    delete_provider_profile,
    delete_provider_thinking_preference,
    apply_web_search_values,
    load_provider_profile,
    load_provider_services,
    load_web_search_settings,
    provider_reference_modules,
    replace_provider_references,
    replace_provider_value,
    save_provider_profile,
    save_web_search_settings,
    save_provider_thinking_preference,
    set_default_provider,
    set_provider_thinking,
    web_search_settings_from_config,
)
from .json_io import write_json_atomic
from .plugin_center import EngineApiClient, EngineUnavailableError, PluginCenterError
from .provider import (
    DEFAULT_PROVIDER_MODEL,
    DEFAULT_PROVIDER_SOURCE_ID,
    OpenAIProviderProfile,
    discover_openai_models,
    provider_source_id,
    supports_thinking_toggle,
)
from .runtime_config import bind_missing_analysis_model


class AnalysisModelBindingError(PluginCenterError):
    """A provider was saved, but its dependent analysis binding was not confirmed."""


class ModelApiMixin:
    def save_provider(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            services = load_provider_services(self._install_root)
            original_id = str(values.get("provider_id") or "").strip()
            selected = next(
                (item for item in services if item.provider_id == original_id),
                None,
            )
            service_kind = str(
                values.get("service_kind")
                or (getattr(selected, "service_kind", "chat") if selected is not None else "chat")
            ).strip().casefold()
            chat_services = [
                item for item in services
                if getattr(item, "service_kind", "chat") == "chat"
                and not (
                    getattr(item, "is_recommended", False)
                    and str(getattr(item, "model", "")).casefold() == DEFAULT_PROVIDER_MODEL.casefold()
                )
            ]
            make_default = service_kind == "chat" and (
                not chat_services or bool(selected is not None and selected.is_default)
            )
            current = load_provider_profile(self._install_root)
            api_key = str(values.get("api_key") or "").strip()
            if not api_key and selected is not None:
                api_key = selected.api_key
            elif not api_key and not original_id and current is not None:
                api_key = current.api_key
            api_base = str(values.get("api_base") or "").strip()
            model = str(values.get("model") or "").strip()
            profile = OpenAIProviderProfile(
                api_key=api_key,
                api_base=api_base,
                model=model,
                source_id=(
                    selected.source_id
                    if (
                        selected is not None
                        and selected.api_base.rstrip("/") == api_base.rstrip("/")
                    )
                    else provider_source_id(api_base, model)
                ),
                thinking_enabled=(
                    self._requested_thinking(values["thinking_enabled"])
                    if "thinking_enabled" in values else None
                ),
                service_kind=service_kind,
                modalities=tuple(
                    item.strip()
                    for item in str(values.get("modalities") or "").split(",")
                    if item.strip()
                ),
            )
            errors = profile.validate()
            if errors:
                raise ValueError("；".join(errors))
            client = EngineApiClient(self._install_root)
            hot_applied = client.is_available()
            if hot_applied:
                client.save_provider(
                    profile,
                    original_provider_id=(
                        original_id
                        if selected is not None and selected.model == model
                        else None
                    ),
                    make_default=make_default,
                )
                saved = profile.normalized()
                if saved.thinking_enabled is not None:
                    save_provider_thinking_preference(
                        self._install_root, saved.model_id, saved.thinking_enabled
                    )
            else:
                saved = save_provider_profile(
                    self._install_root,
                    profile,
                    make_default=make_default,
                )
            if saved.service_kind == "chat":
                self._bind_group_analysis_model(
                    saved if make_default else current, client, hot_applied
                )
            return self._ok(
                {
                    "id": saved.model_id,
                    "name": next(
                        (
                            item.display_name
                            for item in load_provider_services(self._install_root)
                            if item.provider_id == saved.model_id
                        ),
                        saved.model,
                    ),
                    "configured": True,
                    "api_base": saved.api_base,
                    "model": saved.model,
                    "api_key_configured": bool(saved.api_key),
                    "is_default": make_default,
                    "is_recommended": (
                        saved.source_id == DEFAULT_PROVIDER_SOURCE_ID
                        and saved.model == DEFAULT_PROVIDER_MODEL
                    ),
                    "service_kind": saved.service_kind,
                    "modalities": list(saved.modalities),
                    "modality_label": (
                        "向量" if saved.service_kind == "embedding"
                        else "图片" if saved.service_kind == "image"
                        else "语音" if saved.service_kind == "voice"
                        else "多模态" if set(saved.modalities) & {"image", "audio"}
                        else "文本"
                    ),
                    "thinking_supported": supports_thinking_toggle(saved.api_base, saved.model),
                    "thinking_enabled": profile.thinking_enabled,
                    "apply_state": "applied" if hot_applied else "saved_for_start",
                },
                message=(
                    (
                        "回复模型已热更新并立即生效。"
                        if hot_applied
                        else "回复模型已保存，机器人启动时会直接使用。"
                    )
                    if make_default
                    else "模型服务已保存，当前回复模型保持不变。"
                ),
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def _bind_group_analysis_model(
        self, profile: OpenAIProviderProfile | None,
        client: EngineApiClient, online: bool,
    ) -> None:
        data_root = self._install_root / "app" / "astrbot" / "data"
        module = "astrbot_plugin_angel_heart"
        if profile is None or not (data_root / "plugins" / module).is_dir():
            return
        try:
            path = data_root / "config" / f"{module}_config.json"
            if online:
                snapshot = client.get_config(module)
                config = snapshot["config"]
            else:
                config = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
            if not isinstance(config, dict):
                raise ValueError("机器人核心配置格式不正确。")
            if not bind_missing_analysis_model(config, profile):
                return
            if online:
                client.save_config(module, config, snapshot["revision"])
            else:
                write_json_atomic(path, config)
        except (OSError, ValueError, PluginCenterError) as exc:
            raise AnalysisModelBindingError(
                "模型已保存，但群聊分析模型自动绑定未完成。"
                "请在机器人核心的模型设置中核对绑定，并查看运行记录。"
            ) from exc

    @staticmethod
    def _requested_thinking(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.casefold() in {"true", "false"}:
            return value.casefold() == "true"
        raise ValueError("思考模式只能选择开启或关闭。")

    def get_providers(self) -> dict[str, object]:
        try:
            return self._ok(
                [
                    item.to_public_dict()
                    for item in load_provider_services(self._install_root)
                ]
            )
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def get_web_search_settings(self) -> dict[str, object]:
        try:
            client = EngineApiClient(self._install_root)
            if client.is_available():
                system = client.request("GET", "/api/v1/system-config")
                config = system.get("config") if isinstance(system, dict) else None
                if not isinstance(config, dict):
                    raise PluginCenterError("无法读取当前机器人核心配置，请刷新后重试。")
                settings = web_search_settings_from_config(config)
                settings["apply_state"] = "applied"
                return self._ok(settings)
            return self._ok(load_web_search_settings(self._install_root))
        except (OSError, ValueError, PluginCenterError, EngineUnavailableError) as exc:
            return self._failed(exc, "15001")

    def save_web_search_settings(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            client = EngineApiClient(self._install_root)
            hot_applied = client.is_available()
            if hot_applied:
                system = client.request("GET", "/api/v1/system-config")
                config = system.get("config") if isinstance(system, dict) else None
                if not isinstance(config, dict):
                    raise PluginCenterError("无法读取当前机器人核心配置，请刷新后重试。")
                apply_web_search_values(config, values)
                client.request("PUT", "/api/v1/system-config", config)
                verified = client.request("GET", "/api/v1/system-config")
                verified_config = verified.get("config") if isinstance(verified, dict) else None
                if not isinstance(verified_config, dict):
                    raise PluginCenterError("联网搜索设置已提交，但没有收到生效确认，请刷新后重试。")
                saved = web_search_settings_from_config(verified_config)
            else:
                saved = save_web_search_settings(self._install_root, values)
            saved["apply_state"] = "applied" if hot_applied else "saved_for_start"
            return self._ok(
                saved,
                message=(
                    "联网搜索设置已热更新，下一条消息使用新配置。"
                    if hot_applied
                    else "联网搜索设置已保存，机器人启动时使用。"
                ),
            )
        except (OSError, ValueError, PluginCenterError, EngineUnavailableError) as exc:
            return self._failed(exc, "15001")

    def set_default_provider(self, provider_id: str) -> dict[str, object]:
        try:
            services = load_provider_services(self._install_root)
            selected = next(
                (item for item in services if item.provider_id == provider_id),
                None,
            )
            if selected is None or not selected.configured:
                raise ValueError("所选模型服务不存在或尚未完成配置。")
            if getattr(selected, "service_kind", "chat") != "chat":
                raise ValueError("只有聊天模型可以设为默认回复模型。")
            client = EngineApiClient(self._install_root)
            hot_applied = client.is_available()
            if hot_applied:
                client.set_default_provider(provider_id)
            else:
                set_default_provider(self._install_root, provider_id)
            self._bind_group_analysis_model(
                load_provider_profile(self._install_root), client, hot_applied
            )
            return self._ok(
                {
                    "id": provider_id,
                    "apply_state": "applied" if hot_applied else "saved_for_start",
                },
                message=(
                    "默认回复模型已切换并立即生效。"
                    if hot_applied
                    else "默认回复模型已保存，机器人启动时会直接使用。"
                ),
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def set_provider_thinking(self, provider_id: str, enabled: bool) -> dict[str, object]:
        try:
            requested = self._requested_thinking(enabled)
            client = EngineApiClient(self._install_root)
            hot_applied = client.is_available()
            if hot_applied:
                protocol_applied = client.set_provider_thinking(provider_id, requested)
                save_provider_thinking_preference(
                    self._install_root, provider_id, requested
                )
            else:
                protocol_applied = set_provider_thinking(
                    self._install_root, provider_id, requested
                )
            saved = next(
                (
                    item
                    for item in load_provider_services(self._install_root)
                    if item.provider_id == provider_id
                ),
                None,
            )
            if saved is None or saved.thinking_enabled is not requested:
                raise PluginCenterError("思考模式未能确认保存，请刷新后重试。")
            return self._ok(
                {
                    "id": provider_id,
                    "thinking_enabled": requested,
                    "thinking_supported": protocol_applied,
                    "apply_state": (
                        "applied" if hot_applied and protocol_applied
                        else "saved_for_start" if protocol_applied
                        else "saved_preference"
                    ),
                },
                message=(
                    "思考模式已热更新，下一条消息立即生效。"
                    if hot_applied and protocol_applied
                    else "思考模式已保存，机器人下次启动时生效。"
                    if protocol_applied
                    else "思考模式偏好已保存；当前模型不支持时会自动忽略。"
                ),
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def test_provider(self, provider_id: str) -> dict[str, object]:
        try:
            client = EngineApiClient(self._install_root)
            if not client.is_available():
                raise EngineUnavailableError("请先启动机器人，再测试模型服务。")
            return self._ok(
                client.test_provider(provider_id),
                message="模型服务连接正常。",
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def discover_provider_models(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            provider_id = str(values.get("provider_id") or "").strip()
            selected = next(
                (
                    item
                    for item in load_provider_services(self._install_root)
                    if item.provider_id == provider_id
                ),
                None,
            )
            api_key = str(values.get("api_key") or "").strip()
            if not api_key and selected is not None:
                api_key = selected.api_key
            models = discover_openai_models(
                api_key=api_key,
                api_base=str(values.get("api_base") or "").strip(),
            )
            return self._ok(
                {"items": list(models), "total": len(models)},
                message=f"已读取 {len(models)} 个可用模型。",
            )
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def delete_provider(
        self, provider_id: str, replacement_provider_id: str = ""
    ) -> dict[str, object]:
        try:
            services = load_provider_services(self._install_root)
            selected = next(
                (item for item in services if item.provider_id == provider_id),
                None,
            )
            if selected is None:
                raise ValueError("所选模型服务不存在，请刷新后重试。")
            reference_modules = provider_reference_modules(
                self._install_root, provider_id, strict=True
            )
            needs_replacement = selected.is_default or bool(reference_modules)
            if needs_replacement:
                replacement = next(
                    (
                        item
                        for item in services
                        if item.provider_id == replacement_provider_id
                        and item.provider_id != provider_id
                        and item.configured
                        and getattr(item, "service_kind", "chat") == getattr(selected, "service_kind", "chat")
                    ),
                    None,
                )
                if replacement is None:
                    raise ValueError("请先选择一个已配置的模型接替当前服务及相关功能。")
            client = EngineApiClient(self._install_root)
            if client.is_available():
                snapshots: dict[str, dict[str, Any]] = {}
                prepared: dict[str, dict[str, Any]] = {}
                for module_name in reference_modules:
                    snapshot = client.get_config(module_name)
                    updated, count = replace_provider_value(
                        snapshot["config"], provider_id, replacement_provider_id
                    )
                    if count:
                        snapshots[module_name] = snapshot
                        prepared[module_name] = updated

                applied: list[tuple[str, str]] = []
                try:
                    for module_name, updated in prepared.items():
                        receipt = client.save_config(
                            module_name,
                            updated,
                            base_revision=str(snapshots[module_name]["revision"]),
                        )
                        applied.append(
                            (module_name, str(receipt.get("revision") or ""))
                        )
                except PluginCenterError:
                    for module_name, revision in reversed(applied):
                        try:
                            client.save_config(
                                module_name,
                                snapshots[module_name]["config"],
                                base_revision=revision or None,
                            )
                        except PluginCenterError:
                            pass
                    raise
                if selected.is_default:
                    client.set_default_provider(replacement_provider_id)
                client.delete_provider(provider_id, selected.source_id)
                delete_provider_thinking_preference(
                    self._install_root, provider_id
                )
                hot_applied = True
            else:
                if reference_modules:
                    replace_provider_references(
                        self._install_root,
                        provider_id,
                        replacement_provider_id,
                    )
                if selected.is_default:
                    set_default_provider(self._install_root, replacement_provider_id)
                delete_provider_profile(self._install_root, provider_id)
                hot_applied = False
            if hot_applied and reference_modules:
                message = "模型服务已删除，相关功能已切换并立即生效。"
            elif hot_applied:
                message = "模型服务已删除并立即生效。"
            elif reference_modules:
                message = "模型服务已删除，相关功能已切换。"
            else:
                message = "模型服务已删除。"
            return self._ok(
                {
                    "id": provider_id,
                    "updated_features": list(reference_modules),
                },
                message=message,
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")
