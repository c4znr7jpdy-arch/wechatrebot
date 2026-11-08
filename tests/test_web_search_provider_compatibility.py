"""Exercise manager configuration through the pinned core's tool selection."""
import asyncio
import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_app_shell import _api
from test_hot_config_runtime import _runtime_function
from wechatrebot_manager import customer_config, model_api


CORE = Path(__file__).parents[1] / "vendor/engine/astrbot/core"


@pytest.fixture
def selected_tools():
    class ToolSet(list):
        def add_tool(self, tool):
            self.append(tool)

    scope = {"ToolSet": ToolSet, "logger": logging.getLogger(__name__)}
    for name in (
        "TavilyWebSearchTool", "TavilyExtractWebPageTool", "BochaWebSearchTool",
        "BraveWebSearchTool", "FirecrawlWebSearchTool", "FirecrawlExtractWebPageTool",
        "BaiduWebSearchTool", "ExaWebSearchTool", "ExaGetContentsTool",
    ):
        scope[name] = name
    _runtime_function(
        (CORE / "tools/web_search_tools.py").read_text("utf-8-sig"),
        "normalize_legacy_web_search_config", scope,
    )
    apply_tools = _runtime_function(
        (CORE / "astr_main_agent.py").read_text("utf-8-sig"),
        "_apply_web_search_tools", scope,
    )

    def select(config):
        req = SimpleNamespace(func_tool=None)
        context = SimpleNamespace(
            get_config=lambda **_kwargs: config,
            get_llm_tool_manager=lambda: SimpleNamespace(get_builtin_tool=lambda tool: tool),
        )
        asyncio.run(apply_tools(SimpleNamespace(unified_msg_origin="test"), req, context))
        return req.func_tool

    return select


@pytest.mark.parametrize("provider,expected", [
    ("tavily", ["TavilyWebSearchTool", "TavilyExtractWebPageTool"]),
    ("bocha", ["BochaWebSearchTool"]),
    ("brave", ["BraveWebSearchTool"]),
    ("firecrawl", ["FirecrawlWebSearchTool", "FirecrawlExtractWebPageTool"]),
    ("baidu_app_builder", ["BaiduWebSearchTool"]),
    ("exa", ["ExaWebSearchTool", "ExaGetContentsTool"]),
])
def test_saved_provider_attaches_its_real_core_tools(tmp_path, selected_tools, provider, expected):
    result = customer_config.save_web_search_settings(tmp_path, {
        "enabled": True, "provider": provider, "link_enabled": True,
        "key": "synthetic-search-key",
    })
    config = json.loads((tmp_path / "app/astrbot/data/cmd_config.json").read_text("utf-8"))
    assert selected_tools(config) == expected
    assert result["provider"] == provider
    assert result["providers"][provider]["configured"] is True
    assert "synthetic-search-key" not in str(result)
    assert customer_config.load_web_search_settings(tmp_path) == result


@pytest.mark.parametrize("stored", ["baidu_app_builder", "baidu_ai_search"])
@pytest.mark.parametrize("values,expected_key", [
    ({}, "synthetic-saved-key"),
    ({"key": "  synthetic-new-key  "}, "synthetic-new-key"),
    ({"clear_key": True}, ""),
])
def test_baidu_readback_and_resave_migrate_legacy_id_without_losing_keys(stored, values, expected_key):
    config = {"provider_settings": {
        "web_search": True, "websearch_provider": stored,
        "websearch_baidu_app_builder_key": "synthetic-saved-key",
        "websearch_tavily_key": ["synthetic-other-key"],
    }}
    public = customer_config.web_search_settings_from_config(config)
    assert public["provider"] == "baidu_app_builder"
    customer_config.apply_web_search_values(config, {
        "enabled": public["enabled"], "provider": public["provider"], **values,
    })
    assert config["provider_settings"]["websearch_provider"] == "baidu_ai_search"
    assert config["provider_settings"]["websearch_baidu_app_builder_key"] == expected_key
    assert config["provider_settings"]["websearch_tavily_key"] == ["synthetic-other-key"]


def test_baidu_hot_save_readback_uses_core_id_and_preserves_public_id(monkeypatch, tmp_path, selected_tools):
    api = _api(tmp_path)
    config = {"provider_settings": {
        "web_search": True, "websearch_provider": "baidu_ai_search",
        "websearch_baidu_app_builder_key": "synthetic-saved-key",
    }}
    calls = []

    class Client:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def request(method, _path, payload=None):
            calls.append(method)
            if method == "PUT":
                assert payload["provider_settings"]["websearch_provider"] == "baidu_ai_search"
                config.update(copy.deepcopy(payload))
            return {"config": copy.deepcopy(config)}

    monkeypatch.setattr(model_api, "EngineApiClient", lambda _root: Client())
    assert api.get_web_search_settings()["data"]["provider"] == "baidu_app_builder"
    result = api.save_web_search_settings({
        "enabled": True, "provider": "baidu_app_builder", "link_enabled": True,
    })
    assert result["ok"] is True
    assert result["data"]["apply_state"] == "applied"
    assert result["data"]["provider"] == "baidu_app_builder"
    assert "synthetic-saved-key" not in str(result)
    assert calls == ["GET", "GET", "PUT", "GET"]
    assert selected_tools(config) == ["BaiduWebSearchTool"]
