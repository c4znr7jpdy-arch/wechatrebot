from __future__ import annotations

import base64
import hashlib
import hmac
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wechatrebot_manager import plugin_center

from wechatrebot_manager.brand import BrandProfile, save_brand_profile
from wechatrebot_manager.plugin_center import (
    ConfigConflictError,
    EngineApiClient,
    PluginCenterError,
    PluginCenterService,
    _asset_root,
    _handler,
    _hide_customer_internal_schema,
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
from wechatrebot_manager.feature_policy import DeliveryFeature


def test_common_plugin_model_field_names_use_unified_provider_catalog() -> None:
    assert _model_field_kind("llm_provider_id", {}) == "chat"
    assert _model_field_kind("LLM_providers", {"type": "list"}) == "chat"
    assert _model_field_kind("embedding_provider_id", {}) == "embedding"
    assert _model_field_kind("rerank_provider_id", {}) == "rerank"


def test_secret_detection_does_not_treat_secretary_timing_as_a_password() -> None:
    assert plugin_center._is_secret_field("secretary_debounce_time", {}) is False
    assert plugin_center._is_secret_field("api_secret", {}) is True
    assert plugin_center._is_secret_field("access_token", {}) is True


def test_meme_prompt_schema_is_hidden_from_customer_configuration() -> None:
    schema = {
        "generation": {
            "type": "object",
            "items": {
                "prompt": {
                    "type": "object",
                    "items": {"head": {"type": "text", "default": "internal"}},
                },
                "emotion": {"type": "object", "items": {}},
            },
        },
        "storage": {
            "type": "object",
            "items": {"provider": {"type": "string", "default": "local"}},
        },
    }

    visible_schema = _hide_customer_internal_schema("meme_manager", schema)
    projected, _profile = _project_customer_schema("meme_manager", schema)

    assert "prompt" not in json.dumps(visible_schema, ensure_ascii=False)
    assert "prompt" not in json.dumps(projected, ensure_ascii=False)
    assert "prompt" in schema["generation"]["items"]


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


def test_group_notice_exposes_join_leave_and_profile_sections() -> None:
    schema = {
        key: {"type": "bool" if key.startswith("notify_") else "string", "default": True if key.startswith("notify_") else key}
        for key in (
            "notify_join", "notify_leave", "notify_profile_change",
            "welcome_title", "welcome_tail", "leave_title", "leave_body",
            "profile_change_title", "profile_change_body",
        )
    }

    projected, profile = _project_customer_schema("jiang_group_notice", schema)

    assert profile is not None
    assert list(projected) == list(schema)
    assert projected["notify_join"]["_ui_group"] == "要提示的成员事件"
    assert projected["welcome_title"]["_ui_group"] == "成员加入"
    assert projected["leave_title"]["_ui_group"] == "成员退出"
    assert projected["profile_change_title"]["_ui_group"] == "群昵称变化"
    assert "加入、退出和群昵称变化" in profile.note


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
                "modalities": ["text", "tool_use"],
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
    assert client.providers[0]["modalities"] == [
        "text", "image", "audio", "tool_use",
    ]
    assert "key" not in client.providers[0]
    assert "api_base" not in client.providers[0]
    assert client.config["provider_settings"]["default_provider_id"] == (
        "jyjapi/grok-4.6"
    )
    assert client.config["provider_settings"]["request_max_retries"] == 1
    assert client.config["provider_settings"]["fallback_chat_models"] == []
    assert client.config["unrelated"] == {"keep": True}


def test_provider_hot_apply_handles_null_modalities_as_unverified(
    tmp_path: Path,
) -> None:
    client = _ProviderHotApplyClient(tmp_path)
    original_request = client.request

    def request_without_modalities(method: str, path: str, payload=None):
        result = original_request(method, path, payload)
        if method == "PUT" and path == "/api/v1/providers/by-id":
            client.providers[0]["modalities"] = None
        return result

    client.request = request_without_modalities  # type: ignore[method-assign]

    with pytest.raises(PluginCenterError, match="没有收到生效确认"):
        client.save_provider(
            OpenAIProviderProfile(
                api_key="new-key",
                api_base="https://api.xiuxianjyj.xin/v1",
                model="grok-4.6",
                source_id="jyjapi",
            )
        )


def test_mimo_thinking_hot_switch_preserves_model_and_reads_back(tmp_path: Path) -> None:
    client = _ProviderHotApplyClient(tmp_path)
    client.sources[0].update({
        "id": "custom_mimo", "api_base": "https://token-plan-cn.xiaomimimo.com/v1",
    })
    client.providers[0].update({
        "id": "custom_mimo/mimo-v2.5", "provider_source_id": "custom_mimo",
        "model": "mimo-v2.5", "custom_extra_body": {"safe_extra": "keep"},
    })
    client.set_provider_thinking("custom_mimo/mimo-v2.5", False)
    assert client.providers[0]["custom_extra_body"] == {
        "safe_extra": "keep", "thinking": {"type": "disabled"},
    }
    assert client.providers[0]["model"] == "mimo-v2.5"
    client.set_provider_thinking("custom_mimo/mimo-v2.5", True)
    assert client.providers[0]["custom_extra_body"]["thinking"] == {"type": "enabled"}


def test_thinking_hot_switch_maps_grok_and_ignores_unknown_protocols(tmp_path: Path) -> None:
    grok = _ProviderHotApplyClient(tmp_path)
    grok.sources[0]["api_base"] = "https://api.x.ai/v1"
    assert grok.set_provider_thinking("jyjapi/grok-4.6", False) is True
    assert grok.providers[0]["custom_extra_body"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }

    unknown = _ProviderHotApplyClient(tmp_path)
    unknown.sources[0]["api_base"] = "https://example.com/v1"
    before = json.loads(json.dumps(unknown.providers))
    assert unknown.set_provider_thinking("jyjapi/grok-4.6", False) is False
    assert unknown.providers == before


def test_openrouter_thinking_hot_switch_preserves_nested_reasoning_options(
    tmp_path: Path,
) -> None:
    client = _ProviderHotApplyClient(tmp_path)
    client.sources[0]["api_base"] = "https://openrouter.ai/api/v1"
    client.providers[0].update({
        "id": "jyjapi/anthropic/claude-sonnet-4.7",
        "model": "anthropic/claude-sonnet-4.7",
        "custom_extra_body": {
            "reasoning": {"max_tokens": 4096},
            "safe_extra": "keep",
        },
    })

    assert client.set_provider_thinking(
        "jyjapi/anthropic/claude-sonnet-4.7", False
    ) is True
    assert client.providers[0]["custom_extra_body"] == {
        "reasoning": {"max_tokens": 4096, "effort": "none"},
        "safe_extra": "keep",
    }


def test_editing_to_unknown_protocol_removes_only_managed_thinking_field(
    tmp_path: Path,
) -> None:
    client = _ProviderHotApplyClient(tmp_path)
    client.sources[0]["api_base"] = "https://api.xiaomimimo.com/v1"
    client.providers[0].update({
        "id": "jyjapi/mimo-v2.5",
        "model": "mimo-v2.5",
        "custom_extra_body": {
            "thinking": {"type": "enabled"},
            "safe_extra": "keep",
        },
    })

    client.save_provider(
        OpenAIProviderProfile(
            api_key="new-key",
            api_base="https://example.com/v1",
            model="mimo-v2.5",
            source_id="jyjapi",
            thinking_enabled=False,
        ),
        original_provider_id="jyjapi/mimo-v2.5",
    )

    assert client.providers[0]["custom_extra_body"] == {"safe_extra": "keep"}


def test_mimo_thinking_hot_switch_rejects_missing_readback(tmp_path: Path) -> None:
    class NoWriteClient(_ProviderHotApplyClient):
        def request(self, method, path, payload=None):
            if method == "PUT" and path == "/api/v1/providers/by-id":
                return {}
            return super().request(method, path, payload)

    client = NoWriteClient(tmp_path)
    client.sources[0].update({
        "id": "custom_mimo", "api_base": "https://token-plan-cn.xiaomimimo.com/v1",
    })
    client.providers[0].update({
        "id": "custom_mimo/mimo-v2.5", "provider_source_id": "custom_mimo",
        "model": "mimo-v2.5", "custom_extra_body": {},
    })
    with pytest.raises(PluginCenterError, match="生效确认"):
        client.set_provider_thinking("custom_mimo/mimo-v2.5", False)


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
    assert catalog["product_name"] == "WxRebot"
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
                "guide": {},
            "scheduled_action": "",
            "schedule_provider": False,
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


def test_group_analysis_uses_professional_settings_and_preserves_values(
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
    assert catalog_item["scheduled_action"] == "group_analysis"
    assert catalog_item["schedule_provider"] is True
    assert detail["scheduled_action"] == "group_analysis"
    assert detail["schedule_provider"] is True
    assert catalog_item["field_count"] == 7
    assert catalog_item["model_field_count"] == 1
    assert catalog_item["simplified"] is True
    assert detail["basic_schema"] == detail["model_schema"] == {}
    assert detail["configuration_profile"] == "advanced-only"
    assert "llm" in detail["advanced_schema"]
    assert "max_messages" in detail["advanced_schema"]["basic"]["items"]
    assert detail["schedule_schema"]["auto_analysis"]["items"]["scheduled_group_list"]["_ui_control"] == "line-list"
    assert detail["schedule_schema"]["auto_analysis"]["items"]["scheduled_group_list"]["_group_selector"] is True
    assert "/sid" not in detail["setup_steps"][0]
    assert detail["schedule_schema"]["auto_analysis"]["items"]["auto_analysis_time"]["_ui_control"] == "time-list"
    assert "prompts" not in detail["schema"]
    assert "专业设置" in detail["configuration_note"]
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


def test_image_service_preserves_legacy_config_without_exposing_credentials(tmp_path: Path) -> None:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    write_runtime_configs(install, wechat)
    plugin = install / "app/astrbot/data/plugins/jiang_image"
    plugin.mkdir(parents=True)
    schema = {
        "primary_base_url": {"type": "string", "default": "https://images.example/v1"},
        "primary_model": {"type": "string", "default": "image-default"},
        "primary_api_key": {"type": "string", "default": ""},
        "use_system_proxy": {"type": "bool", "default": True},
        "local_proxy_port": {"type": "string", "default": ""},
    }
    (plugin / "_conf_schema.json").write_text(json.dumps(schema), encoding="utf-8")
    config_path = install / "app/astrbot/data/config/jiang_image_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({
        "primary_base_url": "https://saved.example/v1", "primary_model": "image-saved",
        "primary_api_key": "stored-secret", "use_system_proxy": True,
    }), encoding="utf-8")
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False
    public_id = service.catalog()["plugins"][0]["id"]
    detail = service.detail(public_id)
    assert detail["configuration_profile"] == "image-service-selection"
    assert detail["has_legacy_image_model"] is True
    assert detail["pages"] == ()
    assert detail["configuration"] == "schema"
    assert detail["model_schema"] == {}
    assert detail["config"]["primary_model"] == "__legacy_image_primary__"
    assert "primary_base_url" not in detail["config"]
    assert "primary_api_key" not in detail["config"]
    assert detail["advanced_config"] == {}

    edited = {**detail["config"], "local_proxy_port": "70000"}
    with pytest.raises(PluginCenterError, match="本地代理端口"):
        service.save(public_id, edited, detail["revision"])
    assert json.loads(config_path.read_text("utf-8"))["primary_model"] == "image-saved"

    edited["local_proxy_port"] = "7890"
    service.save(public_id, edited, detail["revision"])
    reread = service.detail(public_id)
    assert reread["config"]["primary_model"] == "__legacy_image_primary__"
    assert reread["has_legacy_image_model"] is True
    assert reread["config"]["local_proxy_port"] == "7890"
    assert json.loads(config_path.read_text("utf-8"))["primary_base_url"] == "https://saved.example/v1"
    assert json.loads(config_path.read_text("utf-8"))["primary_api_key"] == "stored-secret"


def test_image_service_can_select_a_configured_image_provider(tmp_path: Path) -> None:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    write_runtime_configs(install, wechat)
    plugin = install / "app/astrbot/data/plugins/jiang_image"
    plugin.mkdir(parents=True)
    schema = {
        "primary_base_url": {"type": "string", "default": "https://old.example/v1"},
        "primary_model": {"type": "string", "default": "old-image"},
        "primary_api_key": {"type": "string", "default": ""},
    }
    (plugin / "_conf_schema.json").write_text(json.dumps(schema), encoding="utf-8")
    config_path = install / "app/astrbot/data/config/jiang_image_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({
        "primary_base_url": "https://old.example/v1",
        "primary_model": "old-image",
        "primary_api_key": "old-secret",
    }), encoding="utf-8")
    engine_path = install / "app/astrbot/data/cmd_config.json"
    engine = json.loads(engine_path.read_text(encoding="utf-8"))
    engine["provider_sources"].append({
        "id": "image_vendor", "api_base": "https://images.example/v1",
        "key": ["image-secret"], "enable": True,
    })
    engine["provider_sources"].append({
        "id": "vision_vendor", "api_base": "https://vision.example/v1",
        "key": ["vision-secret"], "enable": True,
    })
    engine["provider"].append({
        "id": "image_vendor/image-v2", "provider_source_id": "image_vendor",
        "model": "image-v2", "service_kind": "image", "modalities": ["image"],
        "enable": True,
    })
    engine["provider"].append({
        "id": "vision_vendor/vision-chat", "provider_source_id": "vision_vendor",
        "model": "vision-chat", "service_kind": "chat",
        "modalities": ["text", "image"], "enable": True,
    })
    engine_path.write_text(json.dumps(engine), encoding="utf-8")
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False
    public_id = service.catalog()["plugins"][0]["id"]

    detail = service.detail(public_id)
    assert detail["image_providers"] == [
        {"value": "image_vendor/image-v2", "label": "image-v2 · 支持图片 · 图片模型"}
    ]
    assert {item["value"] for item in detail["providers"]} == {
        "vision_vendor/vision-chat"
    }
    assert detail["model_schema"] == {}
    assert detail["basic_schema"]["primary_model"]["_provider_type"] == "image"

    detail["config"]["primary_model"] = "image_vendor/image-v2"
    service.save(public_id, detail["config"], detail["revision"])

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["primary_base_url"] == "https://images.example/v1"
    assert saved["primary_model"] == "image-v2"
    assert saved["primary_api_key"] == "image-secret"
    reread_detail = service.detail(public_id)
    assert reread_detail["config"]["primary_model"] == "image_vendor/image-v2"
    assert service.detail(public_id)["has_legacy_image_model"] is False

    engine["provider_sources"][0]["key"] = []
    engine_path.write_text(json.dumps(engine), encoding="utf-8")
    detail_without_key = service.detail(public_id)
    service.save(public_id, detail_without_key["config"], detail_without_key["revision"])
    assert json.loads(config_path.read_text("utf-8"))["primary_api_key"] == ""


def test_group_analysis_access_fields_in_professional_form(tmp_path: Path) -> None:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    write_runtime_configs(install, wechat)
    module = "astrbot_plugin_qq_group_daily_analysis"
    plugin = install / "app/astrbot/data/plugins" / module
    plugin.mkdir(parents=True)
    group_list = {"type": "list", "items": {"type": "string"}, "default": [], "hint": "onebot:GroupMessage:123456"}
    schema = {
        "basic": {"type": "object", "items": {
            "admin_ids": group_list,
            "bot_self_ids": group_list,
            "group_list": group_list,
            "group_list_mode": {"type": "string", "options": ["whitelist", "blacklist", "none"], "default": "none"},
        }},
        "auto_analysis": {"type": "object", "items": {"scheduled_group_list": group_list}},
        "incremental": {"type": "object", "items": {"incremental_group_list": group_list}},
    }
    (plugin / "_conf_schema.json").write_text(json.dumps(schema), encoding="utf-8")
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False
    public_id = service.catalog()["plugins"][0]["id"]
    detail = service.detail(public_id)
    assert detail["basic_schema"] == detail["model_schema"] == {}
    for form in (detail["advanced_schema"],):
        items = form["basic"]["items"]
        for key in ("admin_ids", "bot_self_ids", "group_list"):
            assert items[key]["_ui_control"] == "line-list"
            assert "onebot" not in items[key]["hint"]
        assert items["admin_ids"]["_ui_tint"] is True
        assert items["bot_self_ids"]["_ui_tint"] is True
        assert items["group_list"]["_group_selector"] is True
    assert detail["schedule_schema"]["auto_analysis"]["items"]["scheduled_group_list"]["_group_selector"] is True
    assert detail["advanced_schema"]["incremental"]["items"]["incremental_group_list"]["_group_selector"] is True
    detail["advanced_config"]["basic"].update({
        "admin_ids": ["wxid_analysis_admin"], "bot_self_ids": ["wxid_excluded"],
        "group_list_mode": "whitelist", "group_list": ["123456@chatroom"],
    })
    service.save(public_id, detail["advanced_config"], detail["revision"])
    saved = service.detail(public_id)["config"]["basic"]
    assert saved["admin_ids"] == ["wxid_analysis_admin"]
    assert saved["bot_self_ids"] == ["wxid_excluded"]
    assert saved["group_list"] == ["123456@chatroom"]
    assert saved["group_list_mode"] == "whitelist"


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

    assert [item["display_name"] for item in catalog] == ["点歌", "短剧"]
    by_name = {item["display_name"]: item for item in catalog}
    assert by_name["点歌"]["configurable"] is False
    assert by_name["短剧"]["configurable"] is False
    assert by_name["短剧"]["configuration"] == "none"
    assert by_name["短剧"]["scheduled_action"] == "short_drama"
    assert by_name["短剧"]["schedule_provider"] is True
    assert by_name["点歌"]["pages"] == ()
    assert by_name["短剧"]["pages"] == ()
    angel = service.detail(plugin_center.ROBOT_CORE_FEATURE_ID)
    assert angel["configurable"] is True
    assert angel["model_field_count"] == 1
    assert angel["pages"] == ("chat-config",)
    music = service.detail(by_name["点歌"]["id"])
    drama = service.detail(by_name["短剧"]["id"])
    assert list(angel["model_schema"]) == ["analyzer_model"]
    assert list(angel["basic_schema"]) == ["personality"]
    assert music["schema"] == music["config"] == {}
    assert drama["schema"] == drama["config"] == {}
    assert drama["configuration"] == "none"
    assert drama["pages"] == ()
    assert drama["scheduled_action"] == "short_drama"
    with pytest.raises(PluginCenterError, match="没有可编辑"):
        service.save(by_name["点歌"]["id"], {}, music["revision"])


def test_voice_clone_uses_only_its_page_even_with_old_schema_delivery(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    module = "astrbot_plugin_mimo_tts_clone"
    plugin = install / "app" / "astrbot" / "data" / "plugins" / module
    page = plugin / "pages" / "settings"
    page.mkdir(parents=True)
    (page / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (plugin / "_conf_schema.json").write_text(
        json.dumps({
            "api_key": {"type": "string", "default": ""},
            "ai_style_director_provider_id": {"type": "string", "default": ""},
        }),
        encoding="utf-8",
    )
    legal = install / "legal"
    legal.mkdir(parents=True, exist_ok=True)
    (legal / "feature-set.json").write_text(
        json.dumps({"features": [{
            "module": module, "display_name": "语音克隆",
            "configuration": "schema-and-page",
        }]}, ensure_ascii=False),
        encoding="utf-8",
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    entry = service.catalog()["plugins"][0]
    detail = service.detail(entry["id"])

    assert entry["configurable"] is False
    assert entry["field_count"] == entry["model_field_count"] == 0
    assert detail["basic_schema"] == detail["model_schema"] == detail["advanced_schema"] == {}
    assert detail["pages"] == ("settings",)
    with pytest.raises(PluginCenterError, match="没有可编辑"):
        service.save(entry["id"], {}, detail["revision"])


def test_twitter_uses_only_its_subscription_page_even_with_old_schema_delivery(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    module = "astrbot_plugin_twitter"
    plugin = install / "app" / "astrbot" / "data" / "plugins" / module
    page = plugin / "pages" / "subscriptions"
    page.mkdir(parents=True)
    (page / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (plugin / "_conf_schema.json").write_text(
        json.dumps({"twitter_data_provider": {"type": "string", "default": "fxtwitter"}}),
        encoding="utf-8",
    )
    legal = install / "legal"
    legal.mkdir(parents=True, exist_ok=True)
    (legal / "feature-set.json").write_text(
        json.dumps({"features": [{
            "module": module, "display_name": "推特推送",
            "configuration": "schema-and-page",
        }]}, ensure_ascii=False),
        encoding="utf-8",
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    entry = service.catalog()["plugins"][0]
    detail = service.detail(entry["id"])

    assert entry["configuration"] == "page"
    assert entry["configurable"] is False
    assert entry["field_count"] == entry["model_field_count"] == 0
    assert detail["basic_schema"] == detail["model_schema"] == detail["advanced_schema"] == {}
    assert detail["pages"] == ("subscriptions",)
    with pytest.raises(PluginCenterError, match="没有可编辑"):
        service.save(entry["id"], {}, detail["revision"])


def test_plugin_center_command_can_open_selected_feature(tmp_path: Path) -> None:
    command = plugin_center_command(tmp_path, "feature-0123456789abcdef")

    assert command[-1] == "feature-0123456789abcdef"
    with pytest.raises(PluginCenterError, match="标识无效"):
        plugin_center_command(tmp_path, "../not-a-feature")


def test_plugin_center_uses_a_separate_window_for_each_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice = plugin_center._public_plugin_id("astrbot_plugin_mimo_tts_clone")
    meme = plugin_center._public_plugin_id("meme_manager")
    monkeypatch.setattr(
        plugin_center, "load_robot_settings",
        lambda root: SimpleNamespace(profile=SimpleNamespace(display_name="小妹")),
    )
    monkeypatch.setattr(
        plugin_center, "load_delivery_features",
        lambda root: (
            DeliveryFeature("astrbot_plugin_mimo_tts_clone", "语音克隆", "page"),
            DeliveryFeature("meme_manager", "表情管理", "schema-and-page"),
        ),
    )
    locks: list[str] = []
    focused: list[str] = []

    class ExistingWindowLock:
        def __init__(self, key: str) -> None:
            locks.append(key)

        def acquire(self) -> bool:
            return False

    monkeypatch.setattr(plugin_center, "SingleInstanceLock", ExistingWindowLock)
    monkeypatch.setattr(
        plugin_center, "_focus_plugin_center_window",
        lambda title: focused.append(title) or True,
    )

    assert plugin_center.run_plugin_center(tmp_path, voice) == 0
    assert plugin_center.run_plugin_center(tmp_path, meme) == 0
    assert locks == [
        f"plugin-center:{tmp_path.resolve()}:{voice}",
        f"plugin-center:{tmp_path.resolve()}:{meme}",
    ]
    assert focused == ["小妹 · 语音克隆配置", "小妹 · 表情管理配置"]


def test_plugin_center_launch_reports_an_existing_window_that_cannot_be_focused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        def wait(self, *, timeout: float) -> int:
            assert timeout > 0
            return 3

    monkeypatch.setattr(plugin_center.subprocess, "Popen", lambda *args, **kwargs: ExitedProcess())
    with pytest.raises(PluginCenterError, match="任务栏"):
        plugin_center.launch_plugin_center(tmp_path, "feature-0123456789abcdef")


def test_meme_market_requests_wait_for_install_and_other_posts_keep_short_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineApiClient(tmp_path)
    monkeypatch.setattr(client, "_token", lambda: "test-token")
    monkeypatch.setattr(client, "_url", lambda path: f"http://127.0.0.1{path}")
    timeouts: list[float] = []

    class Response:
        status = 200
        headers: dict[str, str] = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(_request, *, timeout: float):
        timeouts.append(timeout)
        return Response()

    monkeypatch.setattr(plugin_center, "urlopen", fake_urlopen)
    client.raw_request("POST", "/api/plug/meme_manager/community/install", body=b"{}")
    client.raw_request("POST", "/api/plug/another_plugin/settings", body=b"{}")
    assert timeouts == [600, 60]


def test_meme_pack_upload_has_its_own_limit_and_storage_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    multipart = "multipart/form-data; boundary=test"
    assert plugin_center._extension_body_limit(
        "/api/plug/meme_manager/packs/import", multipart,
    ) == 120 * 1024 * 1024
    assert plugin_center._extension_body_limit(
        "/api/plug/meme_manager/emoji/add/happy", multipart,
    ) == 12 * 1024 * 1024
    assert plugin_center._extension_body_limit(
        "/api/plug/meme_manager/packs/import", "application/json",
    ) == 4 * 1024 * 1024

    service = PluginCenterService(tmp_path)
    monkeypatch.setattr(service, "_plugin", lambda _id: SimpleNamespace(module_name="meme_manager"))
    path = Path(service.meme_pack_storage("feature-0123456789abcdef")["path"])
    assert path == tmp_path / "app" / "astrbot" / "data" / "plugin_data" / "meme_manager" / "packs"
    assert path.is_dir()
    monkeypatch.setattr(service, "_plugin", lambda _id: SimpleNamespace(module_name="another_plugin"))
    with pytest.raises(PluginCenterError, match="没有本地表情包目录"):
        service.meme_pack_storage("feature-0123456789abcdef")


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


def test_stale_form_rebases_visible_change_over_unrelated_runtime_write(
    tmp_path: Path,
) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    baseline = json.loads(json.dumps(detail["config"], ensure_ascii=False))
    config_path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "sample_configurable_feature_config.json"
    )
    external = json.loads(config_path.read_text(encoding="utf-8"))
    external["unknown"]["runtime_default"] = "created-after-prefetch"
    config_path.write_text(
        json.dumps(external, ensure_ascii=False), encoding="utf-8"
    )
    edited = json.loads(json.dumps(baseline, ensure_ascii=False))
    edited["model_group"]["provider_id"] = "vendor/reasoning-model"

    service.save(public_id, edited, detail["revision"], baseline)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["unknown"]["runtime_default"] == "created-after-prefetch"
    assert saved["model_group"]["provider_id"] == "vendor/reasoning-model"


def test_stale_form_still_rejects_same_visible_field_conflict(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    baseline = json.loads(json.dumps(detail["config"], ensure_ascii=False))
    config_path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "sample_configurable_feature_config.json"
    )
    external = json.loads(config_path.read_text(encoding="utf-8"))
    external.setdefault("model_group", {})["provider_id"] = "external/model"
    config_path.write_text(
        json.dumps(external, ensure_ascii=False), encoding="utf-8"
    )
    edited = json.loads(json.dumps(baseline, ensure_ascii=False))
    edited["model_group"]["provider_id"] = "vendor/reasoning-model"

    with pytest.raises(ConfigConflictError, match="同一项设置"):
        service.save(public_id, edited, detail["revision"], baseline)


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
    service.engine.get_config = lambda _module: {  # type: ignore[method-assign]
        "config": json.loads(config_path.read_text(encoding="utf-8")),
        "revision": _json_revision(json.loads(config_path.read_text(encoding="utf-8"))),
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


def test_online_group_save_uses_live_config_when_disk_snapshot_is_stale(tmp_path: Path) -> None:
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
    disk_config = json.loads(config_path.read_text(encoding="utf-8"))
    live_config = json.loads(json.dumps(disk_config))
    live_config["model_group"]["provider_id"] = "live/vendor-model"
    config_path.write_text(json.dumps(disk_config, ensure_ascii=False), encoding="utf-8")
    service.engine.is_available = lambda: True  # type: ignore[method-assign]
    service.engine.plugin_states = lambda: {"sample_configurable_feature": True}  # type: ignore[method-assign]
    service.engine.get_config = lambda _module: {  # type: ignore[method-assign]
        "config": live_config,
        "revision": _json_revision(live_config),
    }

    def save_config(_module: str, config: dict, base_revision=None) -> dict:
        assert config["model_group"]["provider_id"] == "new/vendor-model"
        assert base_revision == _json_revision(live_config)
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return {"verified": True, "state": "applied", "revision": _json_revision(config)}

    service.engine.save_config = save_config  # type: ignore[method-assign]
    detail = service.detail(public_id)
    assert detail["config"]["model_group"]["provider_id"] == "live/vendor-model"
    assert detail["revision"] == _json_revision(live_config)
    detail["config"]["model_group"]["provider_id"] = "new/vendor-model"

    saved = service.save(public_id, detail["config"], detail["revision"])

    assert saved["saved"] is True
    assert json.loads(config_path.read_text(encoding="utf-8"))["model_group"]["provider_id"] == "new/vendor-model"


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


def test_customer_feature_cannot_be_globally_disabled(
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

    with pytest.raises(PluginCenterError, match="按聊天取消"):
        service.set_enabled(public_id, False)
    assert live is True
    assert load_plugins(install)[0].enabled is False
    assert filters == []

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
    assert 'message.action === "files:upload"' in javascript
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
        for weight in ("Regular", "Bold"):
            font_url = f"{base}/assets/AlibabaPuHuiTi2-{weight}.woff2"
            with urlopen(font_url, timeout=2) as response:
                assert response.status == 200
                assert response.headers["Content-Type"] == "font/woff2"
                assert response.read(4) == b"wOF2"
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


@pytest.mark.parametrize("frozen", [False, True])
def test_dedicated_window_serves_shared_theme_dependencies(tmp_path: Path, monkeypatch, frozen: bool) -> None:
    source = Path(plugin_center.__file__).with_name("app_webui")
    dependencies = {
        "theme-tokens.css": "text/css",
        "feature-page-theme.js": "text/javascript",
        "feature-page-overrides.css": "text/css",
        "design-overrides.css": "text/css",
        "assets/AlimamaShuHeiTi-Bold.woff2": "font/woff2",
        "assets/AlibabaPuHuiTi2-Regular.woff2": "font/woff2",
        "assets/AlibabaPuHuiTi2-Bold.woff2": "font/woff2",
    }
    if frozen:
        bundle = tmp_path / "bundle" / "wechatrebot_manager"
        for directory in ("plugin_webui", "app_webui"):
            for relative in ("index.html", *dependencies):
                original = Path(plugin_center.__file__).with_name(directory) / relative
                if original.is_file():
                    target = bundle / directory / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original.read_bytes())
        monkeypatch.setattr(plugin_center.sys, "_MEIPASS", str(bundle.parent), raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(object()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base, timeout=2) as response:
            page = response.read().decode("utf-8")
            assert page.index('/feature-page-theme.js') < page.index('/assets/app.js')
            assert "<script>" not in page  # Standalone CSP rejects inline initialization.
        for relative, content_type in dependencies.items():
            with urlopen(f"{base}/{relative}", timeout=2) as response:
                assert response.headers.get_content_type() == content_type
                assert response.headers["Cache-Control"] == "no-store"
                assert response.read() == (source / relative).read_bytes()
        with pytest.raises(HTTPError) as rejected:
            urlopen(f"{base}/assets/../index.html", timeout=2)
        assert rejected.value.code == 404
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


def test_manager_web_handler_serves_main_ui_from_same_origin(tmp_path: Path) -> None:
    assets = tmp_path / "app_webui"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<script src='app.js'></script>", encoding="utf-8")
    (assets / "app.css").write_text("body{}", encoding="utf-8")
    (assets / "design-overrides.css").write_text(".design-ready{}", encoding="utf-8")
    (assets / "app.js").write_text("window.loaded = true;", encoding="utf-8")
    (assets / "core-settings.js").write_text("window.core = true;", encoding="utf-8")
    (assets / "assets" / "app-icon.png").write_bytes(b"PNG")

    class FakeService:
        def catalog(self):
            return {"plugins": [], "engine_online": False, "product_name": "测试机器人"}

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _handler(FakeService(), manager_assets=assets)  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/", timeout=2) as response:
            assert response.status == 200
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
            content_security_policy = response.headers["Content-Security-Policy"]
            assert "default-src 'self'" in content_security_policy
            assert (
                "img-src 'self' data: blob: https://wx.qlogo.cn"
                in content_security_policy
            )
        for path, expected in (
            ("/app.css", b"body{}"),
            ("/design-overrides.css", b".design-ready{}"),
            ("/app.js", b"window.loaded = true;"),
            ("/core-settings.js", b"window.core = true;"),
            ("/assets/app-icon.png", b"PNG"),
        ):
            with urlopen(f"{base}{path}", timeout=2) as response:
                assert response.status == 200
                assert response.read() == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_plugin_page_upload_forwards_large_audio_to_the_selected_feature() -> None:
    calls: list[tuple[str, str, int, str]] = []

    class FakeEngine:
        def raw_request(self, method, path, *, body=None, content_type="", **_kwargs):
            calls.append((method, path, len(body or b""), content_type))
            return 200, {"content-type": "application/json"}, b'{"status":"ok","data":{"success":true}}'

    class FakeService:
        engine = FakeEngine()

        def extension_path(self, public_id: str, endpoint: str) -> str:
            assert public_id == "feature-0000000000000000"
            assert endpoint == "upload_voice_sample"
            return "/api/plug/astrbot_plugin_mimo_tts_clone/upload_voice_sample"

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(FakeService()))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = b"--audio\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample.wav\"\r\n\r\n"
        body += b"a" * (5 * 1024 * 1024)
        body += b"\r\n--audio--\r\n"
        request = Request(
            f"{base}/control/plugins/feature-0000000000000000/extension/upload_voice_sample",
            data=body,
            method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=audio", "Origin": base},
        )
        with urlopen(request, timeout=5) as response:
            assert json.loads(response.read())["data"]["success"] is True
        assert calls == [(
            "POST", "/api/plug/astrbot_plugin_mimo_tts_clone/upload_voice_sample",
            len(body), "multipart/form-data; boundary=audio",
        )]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
