from __future__ import annotations

import base64
import hashlib
import hmac
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wechatrebot_manager.brand import BrandProfile, save_brand_profile
from wechatrebot_manager.plugin_center import (
    ConfigConflictError,
    EngineApiClient,
    PluginCenterError,
    PluginCenterService,
    _asset_root,
    _handler,
    _model_field_kind,
    _normalize_config,
    _json_revision,
    _normalize_plugin_extension_endpoint,
    _project_customer_schema,
    plugin_center_command,
)
from wechatrebot_manager.runtime_config import write_runtime_configs
from wechatrebot_manager.provider import OpenAIProviderProfile
from wechatrebot_manager.customer_config import load_plugins, set_plugin_enabled


def test_common_plugin_model_field_names_use_unified_provider_catalog() -> None:
    assert _model_field_kind("llm_provider_id", {}) == "chat"
    assert _model_field_kind("LLM_providers", {"type": "list"}) == "chat"
    assert _model_field_kind("embedding_provider_id", {}) == "embedding"
    assert _model_field_kind("rerank_provider_id", {}) == "rerank"


def test_angel_heart_exposes_curated_basics_and_keeps_full_page() -> None:
    schema = {
        "analyzer_model": {
            "type": "string",
            "_special": "select_provider",
            "default": "",
        },
        "image_caption_provider_id": {
            "type": "string",
            "_special": "select_provider",
            "default": "",
        },
        "is_reasoning_model": {"type": "bool", "default": True},
        "personality": {
            "type": "object",
            "items": {
                "ai_self_identity": {"type": "text", "default": "identity"},
                "reply_strategy_guide": {"type": "text", "default": "guide"},
            },
        },
        "wake_interaction": {
            "type": "object",
            "items": {
                "alias": {"type": "string", "default": "AI|助手"},
                "enter_on_mention_only": {"type": "bool", "default": True},
                "direct_wake_fast_path": {"type": "bool", "default": True},
                "silence_duration": {"type": "int", "default": 600},
            },
        },
        "timing": {
            "type": "object",
            "items": {
                "assistant_debounce_time": {"type": "float", "default": 1.0},
                "waiting_time": {"type": "float", "default": 30.0},
                "observation_timeout": {"type": "int", "default": 60},
                "secretary_debounce_time": {"type": "float", "default": 30.0},
            },
        },
        "reply_length": {
            "type": "object",
            "items": {
                "normal_reply_max_chars": {"type": "int", "default": 20},
                "focus_reply_max_chars": {"type": "int", "default": 200},
                "focus_instructions": {"type": "string", "default": "分析"},
            },
        },
        "access_control": {
            "type": "object",
            "items": {
                "group_chat_enhancement": {"type": "bool", "default": True},
                "takeover_private_chat_context": {"type": "bool", "default": False},
            },
        },
        "energy": {
            "type": "object",
            "items": {"initial_energy": {"type": "float", "default": 100.0}},
        },
    }

    projected, profile = _project_customer_schema(
        "astrbot_plugin_angel_heart", schema
    )

    assert profile is not None
    assert list(projected) == [
        "analyzer_model",
        "image_caption_provider_id",
        "personality",
        "wake_interaction",
        "timing",
        "reply_length",
        "access_control",
    ]
    assert projected["analyzer_model"]["_blank_label"].startswith("请选择模型")
    assert list(projected["wake_interaction"]["items"]) == [
        "alias",
        "enter_on_mention_only",
        "direct_wake_fast_path",
    ]
    assert "secretary_debounce_time" not in projected["timing"]["items"]
    assert "energy" not in projected
    assert "完整配置页" in profile.note


def _install(tmp_path: Path) -> tuple[Path, str]:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    save_brand_profile(
        install / "config" / "brand.json",
        BrandProfile("群聊管家", "管家", "小管家").normalized(),
    )
    write_runtime_configs(install, wechat)
    engine_config_path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    engine_config = json.loads(engine_config_path.read_text(encoding="utf-8"))
    engine_config["provider"] = [
        {"id": "vendor/reasoning-model", "model": "推理模型", "enable": True},
        {"id": "vector_private", "type": "openai_embedding", "enable": True},
        {"id": "rank_private", "type": "vllm_rerank", "enable": True},
        {"id": "disabled/model", "model": "停用模型", "enable": False},
    ]
    engine_config["persona"] = [{"name": "客服人格"}]
    engine_config_path.write_text(
        json.dumps(engine_config, ensure_ascii=False), encoding="utf-8"
    )

    module_name = "sample_configurable_feature"
    plugin = install / "app" / "astrbot" / "data" / "plugins" / module_name
    (plugin / "pages" / "dashboard").mkdir(parents=True)
    (plugin / "pages" / "dashboard" / "index.html").write_text(
        "<!doctype html><title>dashboard</title>", encoding="utf-8"
    )
    schema = {
        "model_group": {
            "description": "AstrBot 模型配置",
            "hint": "在 WebUI Dashboard 中选择，不使用 NapCat 或 QQ 号。",
            "type": "object",
            "items": {
                "provider_id": {
                    "description": "思考模型",
                    "type": "string",
                    "default": None,
                    "_special": "select_provider",
                },
                "embedding_provider_id": {
                    "description": "记忆检索模型",
                    "type": "string",
                    "default": None,
                },
                "api_secret": {
                    "description": "服务密钥",
                    "type": "string",
                    "default": "",
                    "_secret": True,
                },
                "enc_params": {
                    "description": "内部加密参数",
                    "type": "string",
                    "default": "",
                    "invisible": True,
                },
            },
        },
        "AstrBot_internal_key": {
            "description": "内部兼容值",
            "type": "string",
            "default": "keep",
        },
    }
    (plugin / "_conf_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    config_path = install / "app" / "astrbot" / "data" / "config"
    config_path.mkdir(parents=True, exist_ok=True)
    (config_path / f"{module_name}_config.json").write_text(
        json.dumps(
            {
                "unknown": {"keep": True},
                "model_group": {
                    "api_secret": "stored-secret",
                    "enc_params": "stored-encrypted-value",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    public_id = service.catalog()["plugins"][0]["id"]
    return install, public_id


class _ProviderHotApplyClient(EngineApiClient):
    def __init__(self, install_root: Path) -> None:
        super().__init__(install_root)
        self.sources = [
            {
                "id": "jyjapi",
                "api_base": "https://old.example.test/v1",
                "key": ["old-key"],
                "custom_headers": {"X-Route": "keep"},
            }
        ]
        self.providers = [
            {
                "id": "jyjapi/grok-4.6",
                "provider_source_id": "jyjapi",
                "model": "grok-4.6",
                "enable": True,
                "custom_extra_body": {"thinking": True},
                "key": ["merged-source-key"],
                "api_base": "https://old.example.test/v1",
            }
        ]
        self.config = {
            "provider_settings": {
                "default_provider_id": "jyjapi/grok-4.6",
                "fallback_chat_models": ["legacy/slow-a", "legacy/slow-b"],
            },
            "unrelated": {"keep": True},
        }

    def request(self, method: str, path: str, payload=None):  # type: ignore[override]
        if method == "GET" and path == "/api/v1/provider-sources":
            return {"provider_sources": self.sources}
        if method == "GET" and path == "/api/v1/providers":
            return {"providers": self.providers}
        if method == "GET" and path == "/api/v1/system-config":
            return {"config": self.config}
        if method == "PUT" and path == "/api/v1/provider-sources/by-id":
            self.sources = [payload["config"]]
            return {}
        if method == "PUT" and path == "/api/v1/providers/by-id":
            self.providers = [payload["config"]]
            return {}
        if method == "PUT" and path == "/api/v1/system-config":
            self.config = payload
            return {}
        if method == "DELETE" and path.startswith("/api/v1/providers/by-id?"):
            self.providers = []
            return {}
        if method == "DELETE" and path.startswith(
            "/api/v1/provider-sources/by-id?"
        ):
            self.sources = []
            return {}
        raise AssertionError((method, path, payload))


def test_provider_hot_apply_preserves_extended_fields_and_verifies_default(
    tmp_path: Path,
) -> None:
    client = _ProviderHotApplyClient(tmp_path)
    client.save_provider(
        OpenAIProviderProfile(
            api_key="new-key",
            api_base="https://api.xiuxianjyj.xin/v1",
            model="grok-4.6",
            source_id="jyjapi",
        )
    )

    assert client.sources[0]["key"] == ["new-key"]
    assert client.sources[0]["custom_headers"] == {"X-Route": "keep"}
    assert client.providers[0]["custom_extra_body"] == {"thinking": True}
    assert "key" not in client.providers[0]
    assert "api_base" not in client.providers[0]
    assert client.config["provider_settings"]["default_provider_id"] == (
        "jyjapi/grok-4.6"
    )
    assert client.config["provider_settings"]["request_max_retries"] == 1
    assert client.config["provider_settings"]["fallback_chat_models"] == []
    assert client.config["unrelated"] == {"keep": True}


def test_provider_hot_delete_is_read_back_and_cleans_unused_source(
    tmp_path: Path,
) -> None:
    client = _ProviderHotApplyClient(tmp_path)

    client.delete_provider("jyjapi/grok-4.6", "jyjapi")

    assert client.providers == []
    assert client.sources == []


def test_catalog_uses_opaque_ids_and_detail_white_labels_schema(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]

    catalog = service.catalog()
    assert catalog["product_name"] == "群聊管家"
    assert catalog["plugins"] == [
        {
            "id": public_id,
            "display_name": "Sample Configurable Feature",
            "enabled": True,
            "configurable": True,
            "field_count": 2,
            "model_field_count": 2,
            "pages": ("dashboard",),
            "configuration": "schema-and-page",
                "description": "",
                "simplified": False,
                "audience": "按需使用",
                "readiness": "请按说明准备",
            }
    ]
    assert public_id.startswith("feature-")
    assert "module_name" not in catalog["plugins"][0]

    detail = service.detail(public_id)
    assert "AstrBot_internal_key" in detail["schema"]
    customer_copy = json.dumps(
        {
            "description": detail["schema"]["model_group"]["description"],
            "hint": detail["schema"]["model_group"]["hint"],
        },
        ensure_ascii=False,
    ).casefold()
    assert "astrbot" not in customer_copy
    assert "napcat" not in customer_copy
    assert "webui" not in customer_copy
    assert "qq" not in customer_copy
    assert "unknown" not in detail["config"]
    assert detail["config"]["model_group"]["provider_id"] is None
    assert detail["config"]["model_group"]["api_secret"] == ""
    assert "enc_params" not in detail["schema"]["model_group"]["items"]
    assert detail["schema"]["model_group"]["items"]["api_secret"]["secret"] is True
    assert detail["schema"]["model_group"]["items"]["api_secret"]["_secret_configured"] is True
    assert detail["providers"] == [
        {
            "value": "vendor/reasoning-model",
            "label": "推理模型",
        }
    ]
    assert detail["embedding_providers"] == [
        {"value": "vector_private", "label": "vector_private"}
    ]
    assert detail["rerank_providers"] == [
        {"value": "rank_private", "label": "rank_private"}
    ]
    assert detail["personas"] == ["客服人格"]
    assert "provider_id" in detail["model_schema"]["model_group"]["items"]
    assert "embedding_provider_id" in detail["model_schema"]["model_group"]["items"]
    assert "provider_id" not in detail["basic_schema"]["model_group"]["items"]
    assert "api_secret" in detail["basic_schema"]["model_group"]["items"]
    assert detail["model_schema"]["model_group"]["items"]["provider_id"]["options"] == [
        "",
        "vendor/reasoning-model",
    ]
    assert detail["model_schema"]["model_group"]["items"]["embedding_provider_id"]["options"] == [
        "",
        "vector_private",
    ]


def test_group_analysis_uses_beginner_profile_and_preserves_advanced_values(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    write_runtime_configs(install, wechat)
    module_name = "astrbot_plugin_qq_group_daily_analysis"
    plugin = install / "app" / "astrbot" / "data" / "plugins" / module_name
    plugin.mkdir(parents=True)
    schema = {
        "basic": {
            "description": "基础设置",
            "type": "object",
            "items": {
                "report_template": {
                    "description": "报告模板",
                    "type": "string",
                    "default": "scrapbook",
                    "options": ["scrapbook", "simple"],
                },
                "analysis_days": {"type": "int", "default": 1},
                "min_messages_threshold": {"type": "int", "default": 200},
                "max_messages": {"type": "int", "default": 1000},
            },
        },
        "auto_analysis": {
            "description": "自动分析",
            "type": "object",
            "items": {
                "auto_analysis_time": {
                    "type": "list",
                    "items": {"type": "string"},
                    "default": ["23:00"],
                },
                "scheduled_group_list_mode": {
                    "type": "string",
                    "default": "whitelist",
                },
                "scheduled_group_list": {
                    "type": "list",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
        },
        "analysis_features": {
            "description": "分析内容",
            "type": "object",
            "items": {
                "topic_analysis_enabled": {"type": "bool", "default": True},
                "user_title_analysis_enabled": {"type": "bool", "default": True},
                "golden_quote_analysis_enabled": {"type": "bool", "default": True},
                "chat_quality_analysis_enabled": {"type": "bool", "default": True},
                "max_topics": {"type": "int", "default": 5},
            },
        },
        "llm": {
            "description": "模型设置",
            "type": "object",
            "items": {
                "llm_provider_id": {
                    "type": "string",
                    "default": "",
                    "_special": "select_provider",
                },
                "topic_provider_id": {
                    "type": "string",
                    "default": "",
                    "_special": "select_provider",
                },
                "llm_retries": {"type": "int", "default": 2},
            },
        },
        "prompts": {
            "description": "提示词",
            "type": "object",
            "items": {"topic_prompt": {"type": "text", "default": "advanced"}},
        },
    }
    (plugin / "_conf_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    config_path = install / "app" / "astrbot" / "data" / "config"
    config_path.mkdir(parents=True, exist_ok=True)
    stored_path = config_path / f"{module_name}_config.json"
    stored_path.write_text(
        json.dumps(
            {
                "basic": {
                    "report_template": "scrapbook",
                    "analysis_days": 1,
                    "min_messages_threshold": 200,
                    "max_messages": 4321,
                },
                "auto_analysis": {
                    "auto_analysis_time": ["23:00"],
                    "scheduled_group_list_mode": "whitelist",
                    "scheduled_group_list": ["123456@chatroom"],
                },
                "analysis_features": {
                    "topic_analysis_enabled": True,
                    "user_title_analysis_enabled": True,
                    "golden_quote_analysis_enabled": True,
                    "chat_quality_analysis_enabled": True,
                    "max_topics": 9,
                },
                "llm": {
                    "llm_provider_id": "",
                    "topic_provider_id": "special/topic-model",
                    "llm_retries": 3,
                },
                "prompts": {"topic_prompt": "keep this advanced prompt"},
                "future_setting": {"keep": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]

    catalog_item = service.catalog()["plugins"][0]
    detail = service.detail(catalog_item["id"])

    assert catalog_item["display_name"] == "群聊分析"
    assert catalog_item["field_count"] == 9
    assert catalog_item["model_field_count"] == 1
    assert catalog_item["simplified"] is True
    assert list(detail["basic_schema"]) == [
        "basic",
        "auto_analysis",
        "analysis_features",
    ]
    assert list(detail["model_schema"]) == ["llm"]
    assert list(detail["basic_schema"]["basic"]["items"]) == [
        "report_template",
        "analysis_days",
        "min_messages_threshold",
    ]
    assert detail["basic_schema"]["basic"]["items"]["report_template"][
        "labels"
    ] == ["手账风（示例样式）", "简洁风"]
    assert (
        detail["basic_schema"]["auto_analysis"]["items"]["scheduled_group_list"][
            "_ui_control"
        ]
        == "line-list"
    )
    assert (
        detail["basic_schema"]["auto_analysis"]["items"]["scheduled_group_list"][
            "_group_selector"
        ]
        is True
    )
    assert "/sid" not in detail["setup_steps"][0]
    assert (
        detail["basic_schema"]["auto_analysis"]["items"]["auto_analysis_time"][
            "_ui_control"
        ]
        == "time-list"
    )
    assert "prompts" not in detail["schema"]
    assert "专业参数" in detail["configuration_note"]
    assert len(detail["setup_steps"]) == 3

    detail["config"]["basic"]["min_messages_threshold"] = 300
    service.save(catalog_item["id"], detail["config"], detail["revision"])
    saved = json.loads(stored_path.read_text(encoding="utf-8"))

    assert saved["basic"]["min_messages_threshold"] == 300
    assert saved["basic"]["max_messages"] == 4321
    assert saved["auto_analysis"]["scheduled_group_list_mode"] == "whitelist"
    assert saved["analysis_features"]["max_topics"] == 9
    assert saved["llm"]["topic_provider_id"] == "special/topic-model"
    assert saved["prompts"]["topic_prompt"] == "keep this advanced prompt"
    assert saved["future_setting"] == {"keep": True}


def test_delivery_policy_hides_legacy_and_technical_configuration(tmp_path: Path) -> None:
    install = tmp_path / "install"
    plugin_root = install / "app" / "astrbot" / "data" / "plugins"
    legal = install / "legal"
    legal.mkdir(parents=True)
    modules = {
        "astrbot_plugin_angel_heart": ("chat-config",),
        "astrbot_plugin_music": (),
        "jiang_short_drama": ("recommendation-schedules",),
        "astrbot_plugin_livingmemory": (),
        "astrbot_plugin_self_learning": (),
    }
    for module, pages in modules.items():
        directory = plugin_root / module
        directory.mkdir(parents=True)
        schema = {"technical_value": {"type": "string", "default": "hidden"}}
        if module == "astrbot_plugin_angel_heart":
            schema.update(
                {
                    "analyzer_model": {
                        "type": "string",
                        "default": "",
                        "_special": "select_provider",
                    },
                    "personality": {
                        "type": "object",
                        "items": {
                            "ai_self_identity": {
                                "type": "text",
                                "default": "identity",
                            },
                            "reply_strategy_guide": {
                                "type": "text",
                                "default": "guide",
                            },
                        },
                    },
                }
            )
        (directory / "_conf_schema.json").write_text(
            json.dumps(schema),
            encoding="utf-8",
        )
        for page in pages:
            page_root = directory / "pages" / page
            page_root.mkdir(parents=True)
            (page_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (legal / "feature-set.json").write_text(
        json.dumps(
            {
                "features": [
                    {
                        "module": "astrbot_plugin_angel_heart",
                        "display_name": "长期记忆",
                        "configuration": "page",
                        "description": "原生上下文与天使之心",
                    },
                    {
                        "module": "astrbot_plugin_music",
                        "display_name": "点歌",
                        "configuration": "none",
                    },
                    {
                        "module": "jiang_short_drama",
                        "display_name": "短剧",
                        "configuration": "page-and-global-schedule-center",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]

    catalog = service.catalog()["plugins"]

    assert [item["display_name"] for item in catalog] == ["长期记忆", "点歌", "短剧"]
    by_name = {item["display_name"]: item for item in catalog}
    assert by_name["长期记忆"]["configurable"] is True
    assert by_name["长期记忆"]["model_field_count"] == 1
    assert by_name["点歌"]["configurable"] is False
    assert by_name["短剧"]["configurable"] is False
    assert by_name["点歌"]["pages"] == ()
    assert by_name["短剧"]["pages"] == ("recommendation-schedules",)
    assert by_name["长期记忆"]["pages"] == ("chat-config",)
    angel = service.detail(by_name["长期记忆"]["id"])
    music = service.detail(by_name["点歌"]["id"])
    drama = service.detail(by_name["短剧"]["id"])
    assert list(angel["model_schema"]) == ["analyzer_model"]
    assert list(angel["basic_schema"]) == ["personality"]
    assert music["schema"] == music["config"] == {}
    assert drama["schema"] == drama["config"] == {}
    with pytest.raises(PluginCenterError, match="没有可编辑"):
        service.save(by_name["点歌"]["id"], {}, music["revision"])


def test_plugin_center_command_can_open_selected_feature(tmp_path: Path) -> None:
    command = plugin_center_command(tmp_path, "feature-0123456789abcdef")

    assert command[-1] == "feature-0123456789abcdef"
    with pytest.raises(PluginCenterError, match="标识无效"):
        plugin_center_command(tmp_path, "../not-a-feature")


def test_offline_save_preserves_unknown_values_and_detects_conflict(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    updated = detail["config"]
    updated["model_group"]["provider_id"] = "vendor/reasoning-model"

    result = service.save(public_id, updated, detail["revision"])
    assert result["saved"] is True
    assert result["reloaded"] is False
    saved = json.loads(
        (
            install
            / "app"
            / "astrbot"
            / "data"
            / "config"
            / "sample_configurable_feature_config.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["unknown"] == {"keep": True}
    assert saved["model_group"]["provider_id"] == "vendor/reasoning-model"
    assert saved["model_group"]["api_secret"] == "stored-secret"
    assert saved["model_group"]["enc_params"] == "stored-encrypted-value"

    with pytest.raises(ConfigConflictError, match="其他窗口"):
        service.save(public_id, updated, detail["revision"])


def test_offline_save_validates_field_types(tmp_path: Path) -> None:
    _, public_id = _install(tmp_path)
    service = PluginCenterService(tmp_path / "install")
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["model_group"]["provider_id"] = ["not-a-string"]
    with pytest.raises(PluginCenterError, match="类型不正确"):
        service.save(public_id, detail["config"], detail["revision"])


def test_online_save_enable_and_reload_use_live_engine_contract(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    config_path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "sample_configurable_feature_config.json"
    )
    calls: list[tuple[object, ...]] = []
    service.engine.is_available = lambda: True  # type: ignore[method-assign]
    service.engine.plugin_states = lambda: {  # type: ignore[method-assign]
        "sample_configurable_feature": True
    }

    def save_config(module_name: str, config: dict, base_revision=None) -> dict:
        calls.append(("save", module_name, config))
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return {"verified": True, "state": "applied", "revision": _json_revision(config)}

    service.engine.save_config = save_config  # type: ignore[method-assign]
    service.engine.set_plugin_filter = lambda names: calls.append(  # type: ignore[method-assign]
        ("filter", tuple(names))
    )
    service.engine.set_enabled = lambda module, enabled: calls.append(  # type: ignore[method-assign]
        ("enabled", module, enabled)
    )
    service.engine.reload = lambda module: calls.append(("reload", module))  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["model_group"]["provider_id"] = "vendor/reasoning-model"

    saved = service.save(public_id, detail["config"], detail["revision"])
    enabled = service.set_enabled(public_id, True)
    reloaded = service.reload(public_id)

    assert saved["saved"] is True
    assert saved["reloaded"] is True
    assert enabled == {
        "enabled": True,
        "applied": True,
        "message": "功能状态已立即更新。",
    }
    assert reloaded["reloaded"] is True
    assert [call[:2] for call in calls if call[0] != "filter"] == [
        ("save", "sample_configurable_feature"),
        ("enabled", "sample_configurable_feature"),
        ("reload", "sample_configurable_feature"),
    ]
    filter_call = next(call for call in calls if call[0] == "filter")
    assert "sample_configurable_feature" in filter_call[1]


def test_engine_api_routes_match_installed_runtime_contract(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    client = EngineApiClient(install)
    calls: list[tuple[str, str, object]] = []
    active = True
    system_config = {"plugin_set": ["example"]}

    def request(method: str, path: str, body=None):
        nonlocal active, system_config
        calls.append((method, path, body))
        if path == "/api/v1/plugins":
            return [{"root_dir_name": "example", "name": "registered-example", "activated": active}]
        if path == "/api/v1/system-config":
            if method == "PUT":
                system_config = dict(body)
            return {"config": dict(system_config)}
        if path.endswith("/config"):
            if method == "PUT":
                return {"hot_apply": {"verified": True, "state": "applied", "revision": _json_revision({"enabled": True})}}
            return {"hot_apply_supported": True, "config": {"enabled": True}}
        if path.endswith("/enabled"):
            active = body["enabled"]
        if path.endswith("/pages/settings"):
            return {"content_path": "/api/v1/plugins/example/pages/settings/content/index.html"}
        return {}

    client.request = request  # type: ignore[method-assign]
    assert client.plugin_states() == {
        "example": True,
        "registered-example": True,
    }
    client.set_plugin_filter(["registered-example", "example", "example"])
    client.save_config("example", {"enabled": True})
    client.reload("example")
    client.set_enabled("example", False)
    page = client.page_entry("example", "settings")

    assert [call for call in calls if call[0] != "GET"] == [
        ("PUT", "/api/v1/system-config", {"plugin_set": ["example", "registered-example"]}),
        ("PUT", "/api/v1/plugins/registered-example/config", {"config": {"enabled": True}, "base_revision": _json_revision({"enabled": True})}),
        ("POST", "/api/v1/plugins/registered-example/reload", None),
        ("PATCH", "/api/v1/plugins/registered-example/enabled", {"enabled": False}),
    ]
    assert page.endswith("/content/index.html")


def test_plugin_extension_path_is_scoped_to_selected_feature(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]

    assert service.extension_path(public_id, "page/schedules") == (
        "/api/plug/sample_configurable_feature/page/schedules"
    )
    assert _normalize_plugin_extension_endpoint("profiles/update") == "profiles/update"
    for endpoint in ("", "../config", "page//config", "https://example.invalid"):
        with pytest.raises(PluginCenterError, match="请求无效"):
            service.extension_path(public_id, endpoint)


def test_online_plugin_state_is_authoritative_and_toggle_persists_restart_state(
    tmp_path: Path,
) -> None:
    install, public_id = _install(tmp_path)
    module_name = "sample_configurable_feature"
    service = PluginCenterService(install)
    set_plugin_enabled(install, module_name, False)
    live = True

    service.engine.is_available = lambda: True  # type: ignore[method-assign]
    service.engine.plugin_states = lambda: {module_name: live}  # type: ignore[method-assign]

    def set_live(_module_name: str, enabled: bool) -> None:
        nonlocal live
        live = enabled

    service.engine.set_enabled = set_live  # type: ignore[method-assign]
    filters: list[list[str]] = []
    service.engine.set_plugin_filter = lambda names: filters.append(list(names))  # type: ignore[method-assign]

    assert service.catalog()["plugins"][0]["enabled"] is True
    assert service.detail(public_id)["enabled"] is True
    assert service.extension_path(public_id, "page/schedules").endswith(
        "/page/schedules"
    )

    service.set_enabled(public_id, False)
    assert live is False
    assert load_plugins(install)[0].enabled is False
    assert module_name not in filters[-1]
    with pytest.raises(PluginCenterError, match="请先启用"):
        service.extension_path(public_id, "page/schedules")

    service.set_enabled(public_id, True)
    assert live is True
    assert load_plugins(install)[0].enabled is True
    assert module_name in filters[-1]


def test_live_state_requires_activation_and_plugin_filter_membership(
    tmp_path: Path,
) -> None:
    client = EngineApiClient(tmp_path)
    configured: list[str] = []

    def request(method: str, path: str, body=None):
        if path == "/api/v1/plugins":
            return [
                {
                    "root_dir_name": "folder-name",
                    "name": "registered-name",
                    "activated": True,
                }
            ]
        if path == "/api/v1/system-config":
            return {"config": {"plugin_set": configured}}
        return {}

    client.request = request  # type: ignore[method-assign]
    assert client.plugin_states() == {
        "folder-name": False,
        "registered-name": False,
    }
    configured.append("folder-name")
    assert client.plugin_states() == {
        "folder-name": True,
        "registered-name": True,
    }


def test_online_toggle_rolls_back_filter_and_persisted_state_on_failure(
    tmp_path: Path,
) -> None:
    install, public_id = _install(tmp_path)
    module_name = "sample_configurable_feature"
    service = PluginCenterService(install)
    set_plugin_enabled(install, module_name, False)
    filters: list[list[str]] = []
    activations: list[bool] = []

    service.engine.is_available = lambda: True  # type: ignore[method-assign]
    service.engine.plugin_states = lambda: {module_name: False}  # type: ignore[method-assign]
    service.engine.set_plugin_filter = lambda names: filters.append(list(names))  # type: ignore[method-assign]

    def set_live(_module_name: str, enabled: bool) -> None:
        activations.append(enabled)
        if enabled:
            raise PluginCenterError("模拟运行态更新失败")

    service.engine.set_enabled = set_live  # type: ignore[method-assign]

    with pytest.raises(PluginCenterError, match="模拟运行态更新失败"):
        service.set_enabled(public_id, True)

    assert load_plugins(install)[0].enabled is False
    assert module_name in filters[0]
    assert module_name not in filters[-1]
    assert activations == [True, False]


def test_offline_save_can_replace_and_explicitly_clear_secret(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["model_group"]["api_secret"] = "replacement-secret"
    first = service.save(public_id, detail["config"], detail["revision"])
    path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "sample_configurable_feature_config.json"
    )
    assert json.loads(path.read_text(encoding="utf-8"))["model_group"]["api_secret"] == "replacement-secret"

    refreshed = service.detail(public_id)
    refreshed["config"]["model_group"]["api_secret"] = None
    service.save(public_id, refreshed["config"], first["revision"])
    assert json.loads(path.read_text(encoding="utf-8"))["model_group"]["api_secret"] == ""


def test_offline_save_validates_template_list_items(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    plugin = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "plugins"
        / "sample_configurable_feature"
    )
    schema = json.loads((plugin / "_conf_schema.json").read_text(encoding="utf-8"))
    schema["workers"] = {
        "description": "任务列表",
        "type": "template_list",
        "default": [],
        "templates": {
            "worker": {
                "name": "任务",
                "items": {
                    "retries": {"description": "重试次数", "type": "int", "default": 2}
                },
            }
        },
    }
    (plugin / "_conf_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["workers"] = [
        {"__template_key": "worker", "retries": "two"}
    ]

    with pytest.raises(PluginCenterError, match="类型不正确"):
        service.save(public_id, detail["config"], detail["revision"])


def test_template_list_credentials_are_redacted_and_preserved(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    plugin = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "plugins"
        / "sample_configurable_feature"
    )
    schema = json.loads((plugin / "_conf_schema.json").read_text(encoding="utf-8"))
    schema["workers"] = {
        "description": "任务列表",
        "type": "template_list",
        "default": [],
        "templates": {
            "worker": {
                "name": "任务",
                "items": {
                    "name": {"description": "名称", "type": "string", "default": ""},
                    "api_token": {"description": "令牌", "type": "string", "default": ""},
                },
            }
        },
    }
    (plugin / "_conf_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    config_path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "sample_configurable_feature_config.json"
    )
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    stored["workers"] = [
        {"__template_key": "worker", "name": "日报", "api_token": "row-secret"}
    ]
    config_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]

    detail = service.detail(public_id)

    row = detail["config"]["workers"][0]
    assert row["api_token"] == ""
    assert row["__manager_index"] == 0
    token_schema = detail["schema"]["workers"]["templates"]["worker"]["items"]["api_token"]
    assert token_schema["secret"] is True
    assert token_schema["_secret_configured"] is True
    service.save(public_id, detail["config"], detail["revision"])
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["workers"][0]["api_token"] == "row-secret"
    assert "__manager_index" not in saved["workers"][0]


def test_legacy_scalar_values_are_normalized_for_schema_forms() -> None:
    normalized = _normalize_config(
        {
            "probability": {"type": "string"},
            "retries": {"type": "int"},
            "enabled": {"type": "bool"},
        },
        {"probability": 0.3, "retries": "2", "enabled": "true"},
    )

    assert normalized == {"probability": "0.3", "retries": 2, "enabled": True}


def test_dashboard_token_is_short_lived_and_signed(tmp_path: Path) -> None:
    install = tmp_path / "install"
    path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "dashboard": {
                    "jwt_secret": "local-signing-secret",
                    "username": "local-console",
                    "port": 6185,
                }
            }
        ),
        encoding="utf-8",
    )
    token = EngineApiClient(install)._token()
    header, body, signature = token.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    )
    expected = base64.urlsafe_b64encode(
        hmac.new(
            b"local-signing-secret",
            f"{header}.{body}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode("ascii")
    assert signature == expected
    assert payload["username"] == "local-console"
    assert 0 < payload["exp"] - int(time.time()) <= 300


def test_plugin_web_assets_are_packaged_and_customer_facing() -> None:
    root = _asset_root()
    html = (root / "index.html").read_text(encoding="utf-8")
    javascript = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "app.css").read_text(encoding="utf-8")
    visible = f"{html}\n{javascript}\n{css}".casefold()
    assert "重新载入设置" in visible
    assert "保存并立即应用" in visible
    assert "打开完整配置页" in visible
    assert "热重载" not in visible
    assert "模型绑定" in visible
    assert "template-card" in visible
    assert "astrbot" not in visible.replace("astrbot-plugin-page", "")
    assert "window.alert(" not in visible
    assert "window.confirm(" not in visible
    assert "window.prompt(" not in visible
    assert "astrbot-plugin-page" in javascript
    assert 'message.action === "api:get"' in javascript
    assert 'message.action === "api:post"' in javascript
    assert "async function refreshCurrentPluginState()" in javascript
    assert 'window.addEventListener("focus", () => { refreshCurrentPluginState(); });' in javascript


def test_original_engine_home_is_replaced_with_neutral_local_page() -> None:
    project = Path(__file__).parents[1]
    page = (project / "assets" / "engine-webui" / "index.html").read_text(
        encoding="utf-8"
    )
    version = (
        project / "assets" / "engine-webui" / "assets" / "version"
    ).read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (project / "release" / "source-manifest.json").read_text(encoding="utf-8")
    )
    assert "astrbot" not in page.casefold()
    assert version == "v4.26.6"
    assert any(
        item.get("target") == "app/astrbot/data/dist"
        and item.get("source") == "assets/engine-webui"
        for item in manifest["entries"]
    )


def test_local_web_handler_serves_assets_and_rejects_foreign_mutation() -> None:
    class FakeService:
        def catalog(self):
            return {"plugins": [], "engine_online": False, "product_name": "测试机器人"}

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(FakeService()))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/", timeout=2) as response:
            assert response.status == 200
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
            assert "功能配置中心" in response.read().decode("utf-8")
        with urlopen(f"{base}/control/catalog", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["data"]["product_name"] == "测试机器人"
        request = Request(
            f"{base}/control/plugins/feature-0000000000000000/enabled",
            data=b'{"enabled":true}',
            method="PATCH",
            headers={"Content-Type": "application/json", "Origin": "https://example.invalid"},
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(request, timeout=2)
        assert rejected.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_web_handler_hosts_plugin_page_bridge_and_preserves_content_type() -> None:
    calls: list[tuple[str, str, bytes | None]] = []

    class FakeEngine:
        def raw_request(self, method, path, *, body=None, **_kwargs):
            calls.append((method, path, body))
            if path.startswith("/api/plugin/page/"):
                return 200, {"content-type": "text/html; charset=utf-8"}, b"<h1>page</h1>"
            return 200, {"content-type": "application/json; charset=utf-8"}, b'{"status":"ok","data":{"saved":true}}'

    class FakeService:
        engine = FakeEngine()

        def extension_path(self, public_id: str, endpoint: str) -> str:
            assert public_id == "feature-0000000000000000"
            assert endpoint == "page/schedules"
            return "/api/plug/example/page/schedules"

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(FakeService()))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/plugin/page/content/example/dashboard/", timeout=2) as response:
            assert response.headers.get_content_type() == "text/html"
            assert response.read() == b"<h1>page</h1>"

        with urlopen(
            f"{base}/control/plugins/feature-0000000000000000/extension/page/schedules?kind=short",
            timeout=2,
        ) as response:
            assert response.headers.get_content_type() == "application/json"
            assert json.loads(response.read())["data"]["saved"] is True

        request = Request(
            f"{base}/control/plugins/feature-0000000000000000/extension/page/schedules",
            data=b'{"enabled":true}',
            method="POST",
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with urlopen(request, timeout=2) as response:
            assert response.status == 200
        assert calls == [
            ("GET", "/api/plugin/page/content/example/dashboard/", None),
            ("GET", "/api/plug/example/page/schedules?kind=short", None),
            ("POST", "/api/plug/example/page/schedules", b'{"enabled":true}'),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
