"""Plugin catalog, configuration transactions and standalone window entry points.

Transport, schema and page helpers keep one implementation in their own modules.
Their existing imports from this module remain available for manager consumers.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http.server import ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any
from urllib.parse import quote

from .customer_config import PluginEntry, load_plugins, load_robot_settings, set_plugin_enabled
from .engine_api import EngineApiClient as EngineApiClient
from .feature_policy import DeliveryFeature, load_delivery_features
from .group_analysis_settings import group_analysis_ui_schema as _group_analysis_ui_schema
from .json_io import write_json_atomic
from .plugin_center_web import (
    _PAGE_ID,
    _asset_root as _asset_root,
    _extension_body_limit as _extension_body_limit,
    _handler as _handler,
    _normalize_plugin_extension_endpoint,
    _white_label_core_page_content as _white_label_core_page_content,
)
from .plugin_configuration import (
    _changed_customer_config,
    _clear_factory_provider_values,
    _config_path,
    _configured_provider_service_kind,
    _customer_change_conflicts,
    _customer_config,
    _customer_schema,
    _dashboard_path,
    _expose_image_provider_config,
    _field_count,
    _image_provider_bindings,
    _is_secret_field as _is_secret_field,
    _json_revision,
    _merge_customer_config,
    _merge_defaults,
    _model_field_kind as _model_field_kind,
    _normalize_config,
    _plugin_root,
    _preserve_legacy_image_model_choices,
    _read_object,
    _resolve_image_provider_config,
    _split_schema,
    _validate_config,
    _validate_image_proxy_port,
    _white_label_schema,
    _with_runtime_choices,
)
from .plugin_errors import (
    ConfigConflictError as ConfigConflictError,
    EngineUnavailableError as EngineUnavailableError,
    PluginCenterError as PluginCenterError,
)
from .plugin_guides import advanced_schema, get_plugin_guide
from .plugin_schema_profiles import (
    CustomerSchemaProfile as CustomerSchemaProfile,
    _ANGEL_HEART_MODULE,
    _GROUP_ANALYSIS_MODULE,
    _MANAGER_HIDDEN_PAGE_MODULES,
    _MANAGER_NO_CONFIGURATION_MODULES,
    _MANAGER_PAGE_ONLY_MODULES,
    _MANAGER_SCHEMA_POLICY_OVERRIDES,
    _SETU_BACKING_MODULE,
    _SETU_FEATURE_KEY,
    _group_analysis_schedule_schema,
    _hide_customer_internal_schema,
    _project_customer_schema,
    _setu_schema,
    _with_setu_schema,
    _without_group_analysis_schedule,
)
from .provider import DEFAULT_PROVIDER_MODEL, DEFAULT_PROVIDER_SOURCE_ID, provider_display_name
from .single_instance import SingleInstanceLock


_PUBLIC_PLUGIN_ID = re.compile(r"^feature-[a-f0-9]{16}$")


_CONFIG_WRITE_LOCK = threading.RLock()


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
    feature_key: str = ""

    @property
    def catalog_key(self) -> str:
        return self.feature_key or self.module_name


def _public_plugin_id(module_name: str) -> str:
    digest = hashlib.sha256(module_name.encode("utf-8")).hexdigest()[:16]
    return f"feature-{digest}"


ROBOT_CORE_FEATURE_ID = _public_plugin_id(_ANGEL_HEART_MODULE)


def _scheduled_action(module_name: str) -> str:
    if module_name == "jiang_short_drama":
        return "short_drama"
    if module_name == _GROUP_ANALYSIS_MODULE:
        # 群分析有自己的按群日报调度器，不能走统一内容任务运行时。
        return "group_analysis"
    if module_name in {"jiang_commands", "jiang_schedule"}:
        return "weather"
    return ""


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
            exposes_schema = (
                module_name not in _MANAGER_PAGE_ONLY_MODULES
                and (
                    policy is None
                    or policy.exposes_schema
                    or module_name in _MANAGER_SCHEMA_POLICY_OVERRIDES
                )
            )
            exposes_pages = (
                (policy is None or policy.exposes_pages)
                and module_name not in _MANAGER_HIDDEN_PAGE_MODULES
            )
            schema, profile = _project_customer_schema(
                module_name, raw_schema if exposes_schema else {}
            )
            basic_schema, model_schema = _split_schema(schema)
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
                    field_count=_field_count(basic_schema),
                    model_field_count=_field_count(model_schema),
                    pages=pages,
                    configuration=(
                        "none"
                        if module_name in _MANAGER_NO_CONFIGURATION_MODULES
                        else "schema"
                        if module_name in _MANAGER_HIDDEN_PAGE_MODULES
                        else
                        "page"
                        if module_name in _MANAGER_PAGE_ONLY_MODULES
                        else (policy.configuration if policy else "schema-and-page")
                    ),
                    description=(policy.description if policy else ""),
                    simplified=profile is not None,
                )
            )
            if module_name == _SETU_BACKING_MODULE:
                setu_schema = _setu_schema(raw_schema)
                result.append(
                    PluginCatalogItem(
                        module_name=module_name,
                        display_name="涩图",
                        enabled=(
                            enabled_overrides.get(module_name, False)
                            if enabled_overrides is not None
                            else bool(raw_plugin.enabled)
                        ),
                        configurable=True,
                        field_count=_field_count(setu_schema),
                        model_field_count=0,
                        pages=(),
                        configuration="schema",
                        description="发送随机图片，并可设置发送后自动撤回的等待时间。",
                        simplified=True,
                        feature_key=_SETU_FEATURE_KEY,
                    )
                )
        return tuple(result)

    def catalog(self) -> dict[str, Any]:
        engine_online, live_states = self._runtime_plugin_states()
        entries = []
        for plugin in self._catalog_items(live_states if engine_online else None):
            if plugin.module_name == _ANGEL_HEART_MODULE:
                continue
            guide = get_plugin_guide(plugin.catalog_key)
            entries.append(
                {
                    "id": _public_plugin_id(plugin.catalog_key),
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
                    "guide": guide,
                    "scheduled_action": _scheduled_action(plugin.catalog_key),
                    "schedule_provider": plugin.catalog_key in {"jiang_commands", "jiang_short_drama", _GROUP_ANALYSIS_MODULE},
                }
            )
        return {
            "plugins": entries,
            "engine_online": engine_online,
            "product_name": "WxRebot",
        }

    def _plugin(
        self,
        public_id: str,
        enabled_overrides: dict[str, bool] | None = None,
    ) -> PluginCatalogItem:
        if not _PUBLIC_PLUGIN_ID.fullmatch(public_id):
            raise PluginCenterError("功能标识无效。")
        for plugin in self._catalog_items(enabled_overrides):
            if _public_plugin_id(plugin.catalog_key) == public_id:
                return plugin
        raise PluginCenterError("所选功能不存在或尚未交付。")

    def detail(self, public_id: str) -> dict[str, Any]:
        engine_online, live_states = self._runtime_plugin_states()
        item = self._plugin(public_id, live_states if engine_online else None)
        module_name = item.module_name
        raw_schema = _read_object(
            _plugin_root(self.install_root) / module_name / "_conf_schema.json"
        )
        storage_schema = (
            _with_setu_schema(raw_schema)
            if item.catalog_key == _SETU_FEATURE_KEY
            else raw_schema
        )
        schema = (
            _setu_schema(storage_schema)
            if item.catalog_key == _SETU_FEATURE_KEY
            else (raw_schema if item.configurable else {})
        )
        stored = _read_object(_config_path(self.install_root, module_name))
        live_revision = ""
        if engine_online and item.configurable:
            # The running engine is authoritative after a hot reload.  Its
            # in-memory config can briefly differ from the file on disk while
            # the manager is starting or another settings window is saving.
            try:
                live = self.engine.get_config(module_name)
            except (EngineUnavailableError, PluginCenterError):
                live = None
            if isinstance(live, dict) and isinstance(live.get("config"), dict):
                stored = live["config"]
                live_revision = str(live.get("revision") or "")
        config = _normalize_config(
            storage_schema, _merge_defaults(storage_schema, stored)
        )
        engine_config = _read_object(_dashboard_path(self.install_root))
        providers: list[dict[str, str]] = []
        embedding_providers: list[dict[str, str]] = []
        rerank_providers: list[dict[str, str]] = []
        image_providers: list[dict[str, str]] = []
        configured_providers = engine_config.get("provider", [])
        configured_sources = engine_config.get("provider_sources", [])
        if not isinstance(configured_providers, list):
            configured_providers = []
        source_records = configured_sources if isinstance(configured_sources, list) else []
        source_ids = {
            str(source.get("id") or "")
            for source in (
                source_records
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
            # The factory Grok entry is supplied by AstrBot itself. It is not a
            # customer-added service and must not appear in model pickers.
            if source_id == DEFAULT_PROVIDER_SOURCE_ID and model_name.casefold() == DEFAULT_PROVIDER_MODEL.casefold():
                continue
            label = provider_display_name(
                source_id if source_id in source_ids else "",
                model_name or provider_id,
            )
            choice = {"value": provider_id, "label": str(_white_label_schema(label))}
            provider_kind = _configured_provider_service_kind(provider)
            if provider_kind == "embedding":
                embedding_providers.append(choice)
            elif provider_kind == "image":
                image_providers.append({**choice, "label": f"{choice['label']} · 图片模型"})
            elif provider_kind == "rerank":
                rerank_providers.append(choice)
            else:
                providers.append(choice)
        image_bindings = _image_provider_bindings(source_records, configured_providers)
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
            image_providers=image_providers,
            personas=personas,
        )
        runtime_schema = _hide_customer_internal_schema(module_name, runtime_schema)
        if module_name == "jiang_image":
            for model_key in ("primary_model", "secondary_model"):
                field = runtime_schema.get(model_key)
                if isinstance(field, dict):
                    field["_provider_type"] = "image"
            runtime_schema = _with_runtime_choices(
                runtime_schema,
                providers=providers,
                embedding_providers=embedding_providers,
                rerank_providers=rerank_providers,
                image_providers=image_providers,
                personas=personas,
            )
        if module_name == _GROUP_ANALYSIS_MODULE:
            runtime_schema = _group_analysis_ui_schema(runtime_schema)
        schema_key = item.catalog_key
        customer_schema, profile = _project_customer_schema(schema_key, runtime_schema)
        clean_schema = _customer_schema(customer_schema, config)
        has_legacy_image_model = False
        customer_config = _clear_factory_provider_values(
            _customer_config(customer_schema, config)
        )
        if module_name == "jiang_image":
            clean_schema, has_legacy_image_model = _preserve_legacy_image_model_choices(
                clean_schema, config, image_bindings
            )
            customer_config = _expose_image_provider_config(
                customer_config, image_bindings, config
            )
        guide = get_plugin_guide(schema_key)
        clean_basic_schema, clean_model_schema = _split_schema(clean_schema)
        if module_name == "jiang_image":
            clean_basic_schema = clean_schema
            clean_model_schema = {}
        group_schedule_schema = (
            _group_analysis_schedule_schema(runtime_schema)
            if module_name == _GROUP_ANALYSIS_MODULE
            else {}
        )
        visible_advanced_schema = advanced_schema(schema_key, runtime_schema)
        if module_name == _GROUP_ANALYSIS_MODULE:
            visible_advanced_schema = _without_group_analysis_schedule(
                visible_advanced_schema
            )
        configuration_profile = ""
        configuration_note = profile.note if profile else ""
        if module_name == _GROUP_ANALYSIS_MODULE:
            clean_basic_schema = clean_schema
            clean_model_schema = {}
            configuration_profile = "group-analysis-simple"
            configuration_note = "模型与全局模型服务共用，在常用设置调整使用范围和报告内容；自动日报统一在主定时任务标签管理。专业设置按需展开。"
        if module_name == "jiang_image":
            configuration_profile = "image-service-selection"
            configuration_note = (
                "检测到旧版独立图片配置，原配置仍保留；添加模型服务后选择对应服务即可迁移。"
                if has_legacy_image_model
                else "图片服务地址、模型名称和密钥统一在模型服务管理；此页只选择主/备用模型与代理。"
            )
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
            "configuration_profile": configuration_profile,
            "configuration_note": configuration_note,
            "has_legacy_image_model": has_legacy_image_model,
            "setup_steps": profile.setup_steps if profile else (),
            "guide": guide,
            "advanced_schema": _customer_schema(
                visible_advanced_schema, config
            ),
            "advanced_config": (
                _expose_image_provider_config(
                    _clear_factory_provider_values(
                        _customer_config(visible_advanced_schema, config)
                    ),
                    image_bindings,
                )
                if module_name == "jiang_image"
                else _clear_factory_provider_values(
                    _customer_config(visible_advanced_schema, config)
                )
            ),
            "schedule_schema": _customer_schema(group_schedule_schema, config),
            "schedule_config": _customer_config(group_schedule_schema, config),
            "schema": clean_schema,
            "basic_schema": clean_basic_schema,
            "model_schema": clean_model_schema,
            "config": customer_config,
            "revision": live_revision or _json_revision(stored),
            "providers": providers,
            "embedding_providers": embedding_providers,
            "rerank_providers": rerank_providers,
            "image_providers": image_providers,
            "personas": personas,
            "engine_online": engine_online,
            "scheduled_action": _scheduled_action(schema_key),
            "schedule_provider": schema_key in {"jiang_commands", "jiang_short_drama", _GROUP_ANALYSIS_MODULE},
        }

    def save(
        self,
        public_id: str,
        config: dict[str, Any],
        base_revision: str | None,
        baseline: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with _CONFIG_WRITE_LOCK:
            return self._save_locked(public_id, config, base_revision, baseline)

    def _save_locked(
        self,
        public_id: str,
        config: dict[str, Any],
        base_revision: str | None,
        baseline: dict[str, Any] | None,
    ) -> dict[str, Any]:
        item = self._plugin(public_id)
        module_name = item.module_name
        if not item.configurable:
            raise PluginCenterError("这项功能没有可编辑的基础配置。")
        path = _config_path(self.install_root, module_name)
        stored = _read_object(path)
        schema = _read_object(
            _plugin_root(self.install_root) / module_name / "_conf_schema.json"
        )
        if item.catalog_key == _SETU_FEATURE_KEY:
            schema = _with_setu_schema(schema)
        online = self.engine.is_available()
        live_revision = ""
        if online:
            # Compare against the live engine snapshot, not a stale file that
            # may predate startup normalization or a previous hot reload.
            live = self.engine.get_config(module_name)
            if not isinstance(live.get("config"), dict):
                raise PluginCenterError("无法读取当前功能设置，请刷新后重试。")
            stored = live["config"]
            live_revision = str(live.get("revision") or "")
        current_revision = live_revision or _json_revision(stored)
        submitted = config
        if base_revision and base_revision != current_revision:
            if not isinstance(baseline, dict):
                raise ConfigConflictError("配置已被其他窗口修改，请刷新后再保存。")
            changed = _changed_customer_config(schema, config, baseline)
            normalized_current = _normalize_config(
                schema, _merge_defaults(schema, stored)
            )
            current = _customer_config(schema, normalized_current)
            if module_name == "jiang_image":
                current_engine = _read_object(_dashboard_path(self.install_root))
                current_sources = current_engine.get("provider_sources", [])
                current_providers = current_engine.get("provider", [])
                current_bindings = _image_provider_bindings(
                    current_sources if isinstance(current_sources, list) else [],
                    current_providers if isinstance(current_providers, list) else [],
                )
                current = _expose_image_provider_config(
                    current, current_bindings, normalized_current
                )
            if _customer_change_conflicts(schema, changed, baseline, current):
                raise ConfigConflictError(
                    "同一项设置已在其他窗口修改，请重新读取后确认再保存。"
                )
            submitted = changed
        merged = _merge_customer_config(schema, submitted, stored)
        if module_name == "jiang_image":
            engine_config = _read_object(_dashboard_path(self.install_root))
            configured_sources = engine_config.get("provider_sources", [])
            configured_providers = engine_config.get("provider", [])
            image_bindings = _image_provider_bindings(
                configured_sources if isinstance(configured_sources, list) else [],
                configured_providers if isinstance(configured_providers, list) else [],
            )
            merged = _resolve_image_provider_config(merged, image_bindings, stored)
        normalized = _normalize_config(schema, _merge_defaults(schema, merged))
        _validate_config(schema, normalized)
        if module_name == "jiang_image" and "local_proxy_port" in schema:
            _validate_image_proxy_port(normalized)
        reloaded = False
        apply_state = "pending_start" if not online else "pending_enable"
        receipt = {}
        if online:
            receipt = self.engine.save_config(module_name, normalized, current_revision)
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
                        "已保存；运行环境准备完成后使用新设置。" if online else
                        "已保存到本机，尚未确认生效。机器人启动后加载；若仍在运行，请恢复连接后重新保存。"),
        }

    def set_enabled(self, public_id: str, enabled: bool) -> dict[str, Any]:
        if enabled is not True:
            raise PluginCenterError(
                "功能统一保持加载；请在群聊&私聊管理中按聊天取消对应功能。"
            )
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

    def meme_pack_storage(self, public_id: str) -> dict[str, str]:
        if self._plugin(public_id).module_name != "meme_manager":
            raise PluginCenterError("当前功能没有本地表情包目录。")
        packs_dir = self.install_root / "app" / "astrbot" / "data" / "plugin_data" / "meme_manager" / "packs"
        packs_dir.mkdir(parents=True, exist_ok=True)
        return {"path": str(packs_dir)}


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
    process = subprocess.Popen(
        plugin_center_command(install_root, feature_id),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    try:
        exit_code = process.wait(timeout=0.35)
    except subprocess.TimeoutExpired:
        return process
    if exit_code == 0:
        return process
    if exit_code == 3:
        raise PluginCenterError("配置窗口已打开，但未能切换到前台；请在任务栏中打开对应窗口。")
    raise PluginCenterError("配置窗口启动失败，请检查管理器安装后重试。")


def _plugin_center_window_title(root: Path, selected: str) -> str:
    brand = load_robot_settings(root).profile.display_name
    if not selected:
        return f"{brand} · 功能配置"
    for feature in load_delivery_features(root) or ():
        if _public_plugin_id(feature.module) == selected:
            return f"{brand} · {feature.display_name}配置"
    return f"{brand} · 功能配置 · {selected}"


def _focus_plugin_center_window(title: str) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return bool(user32.SetForegroundWindow(hwnd))


def run_plugin_center(
    install_root: str | Path, feature_id: str | None = None
) -> int:
    root = Path(install_root).expanduser().resolve()
    selected = str(feature_id or "").strip()
    if selected and not _PUBLIC_PLUGIN_ID.fullmatch(selected):
        raise PluginCenterError("功能标识无效。")
    title = _plugin_center_window_title(root, selected)
    lock = SingleInstanceLock(f"plugin-center:{root}:{selected or 'all'}")
    if not lock.acquire():
        return 0 if _focus_plugin_center_window(title) else 3
    server: ThreadingHTTPServer | None = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(PluginCenterService(root)))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        import webview

        query = f"?feature={quote(selected, safe='')}" if selected else ""
        url = f"http://127.0.0.1:{server.server_port}/{query}"
        webview.create_window(
            title,
            url,
            width=1180,
            height=760,
            min_size=(960, 640),
            background_color="#F3F2EE",
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
    "ROBOT_CORE_FEATURE_ID",
    "launch_plugin_center",
    "plugin_center_command",
    "run_plugin_center",
]
