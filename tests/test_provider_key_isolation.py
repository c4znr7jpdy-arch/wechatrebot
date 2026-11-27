"""Model cards must keep credentials isolated when an endpoint is shared."""
import copy
import json

import pytest

from test_app_shell import _api
from test_plugin_center import _ProviderHotApplyClient
from wechatrebot_manager import model_api
from wechatrebot_manager.customer_config import load_provider_services
from wechatrebot_manager.embedding_default import embedding_config_path
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.plugin_errors import PluginCenterError
from wechatrebot_manager.provider import OpenAIProviderProfile, provider_source_id


BASE = "https://models.example.test/v1"
SOURCE_ID = provider_source_id(BASE, "chat-a")
OLD_KEY = "synthetic-original-key"
NEW_KEY = "synthetic-second-key"


def _engine_path(root):
    return root / "app/astrbot/data/cmd_config.json"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_shared_models(root, *, disabled_sibling=False):
    profiles = [OpenAIProviderProfile(
        api_key=OLD_KEY, api_base=BASE, model=model, source_id=SOURCE_ID,
        service_kind=kind,
    ).normalized() for model, kind in (
        ("chat-a", "chat"), ("chat-b", "chat"), ("vector", "embedding"),
    )]
    source = profiles[0].astrbot_source()
    source.update(
        proxy="http://proxy.example.test:8080", custom_headers={"X-Route": "keep"},
        api_version="synthetic-api-version",
    )
    providers = [profile.astrbot_model() for profile in profiles]
    providers[0].update(key=[OLD_KEY], api_base=BASE, embedding_api_key=OLD_KEY)
    providers[1]["enable"] = not disabled_sibling
    write_json_atomic(_engine_path(root), {
        "provider_sources": [source], "provider": providers,
        "provider_settings": {
            "default_provider_id": profiles[0].model_id,
            "llm_compress_provider_id": profiles[0].model_id,
        },
    })
    data = root / "app/astrbot/data"
    (data / "plugins/astrbot_plugin_angel_heart").mkdir(parents=True)
    write_json_atomic(data / "config/astrbot_plugin_angel_heart_config.json", {
        "analyzer_model": profiles[0].model_id,
        "image_caption_provider_id": profiles[0].model_id,
    })
    write_json_atomic(embedding_config_path(root), {
        "provider_settings": {
            "embedding_provider_id": profiles[2].model_id,
            "llm_provider_id": profiles[0].model_id,
        },
    })
    return profiles


class _MultiModelClient(_ProviderHotApplyClient):
    """Extend the existing hot-save fake to persist multiple model records."""

    def __init__(self, root, online):
        super().__init__(root)
        self.root = root
        self.online = online
        self.config = _read(_engine_path(root))
        self.sources = self.config.pop("provider_sources")
        self.providers = self.config.pop("provider")
        self.ignore = ""
        self.fail_model_write = False

    def is_available(self):
        return self.online

    def request(self, method, path, payload=None):
        source_route = path in {"/api/v1/provider-sources", "/api/v1/provider-sources/by-id"}
        provider_route = path in {"/api/v1/providers", "/api/v1/providers/by-id"}
        if method in {"POST", "PUT"} and (source_route or provider_route):
            if provider_route and self.fail_model_write:
                raise PluginCenterError("Synthetic model write failure")
            records = self.sources if source_route else self.providers
            record = copy.deepcopy(payload["config"])
            target_id = payload.get("source_id" if source_route else "provider_id", record["id"])
            old = next((item for item in records if item["id"] == target_id), None)
            if source_route and self.ignore == "key":
                record["key"] = [OLD_KEY]
                record["embedding_api_key"] = OLD_KEY
            if provider_route and self.ignore == "binding":
                record["provider_source_id"] = SOURCE_ID
            if old is None:
                records.append(record)
            else:
                records[records.index(old)] = record
            result = {}
        else:
            result = super().request(method, path, copy.deepcopy(payload))
        if method != "GET":
            write_json_atomic(_engine_path(self.root), {
                **self.config, "provider_sources": self.sources, "provider": self.providers,
            })
        return copy.deepcopy(result)

    def get_config(self, module):
        path = self.root / f"app/astrbot/data/config/{module}_config.json"
        return {"config": _read(path), "revision": "synthetic-revision"}

    def save_config(self, module, config, revision):
        assert revision == "synthetic-revision"
        write_json_atomic(self.root / f"app/astrbot/data/config/{module}_config.json", config)
        return {"verified": True, "state": "applied"}


def _setup(monkeypatch, root, online, **seed_options):
    profiles = _seed_shared_models(root, **seed_options)
    client = _MultiModelClient(root, online)
    monkeypatch.setattr(model_api, "EngineApiClient", lambda _root: client)
    return _api(root), client, profiles


def _effective_models(root):
    config = _read(_engine_path(root))
    sources = {item["id"]: item for item in config["provider_sources"]}
    return {item["id"]: (item, sources[item["provider_source_id"]])
            for item in config["provider"]}


def _save(api, model, key, provider_id="", kind="chat"):
    return api.save_provider({
        "provider_id": provider_id, "api_base": BASE, "model": model,
        "api_key": key, "service_kind": kind,
    })


@pytest.mark.parametrize("online", [False, True], ids=["cold", "hot"])
@pytest.mark.parametrize("new_model", ["chat-c", "chat-a"], ids=["different-model", "same-model"])
def test_adding_same_endpoint_with_another_key_keeps_both_cards(
    monkeypatch, tmp_path, online, new_model,
):
    api, _client, profiles = _setup(monkeypatch, tmp_path, online)
    before = _effective_models(tmp_path)
    result = _save(api, new_model, NEW_KEY)
    assert result["ok"] is True, result
    new_id = result["data"]["id"]
    after = _effective_models(tmp_path)
    assert new_id not in before
    assert len(after) == len(before) + 1
    assert after[new_id][1]["key"] == [NEW_KEY]
    assert after[new_id][0]["provider_source_id"] != SOURCE_ID
    for field in ("proxy", "custom_headers", "api_version"):
        assert after[new_id][1][field] == before[profiles[0].model_id][1][field]
    for provider_id, records in before.items():
        assert after[provider_id] == records
    assert _read(_engine_path(tmp_path))["provider_settings"]["default_provider_id"] == profiles[0].model_id
    assert {service.provider_id: service.api_key for service in load_provider_services(tmp_path)}[new_id] == NEW_KEY


@pytest.mark.parametrize("online", [False, True], ids=["cold", "hot"])
@pytest.mark.parametrize("disabled_sibling", [False, True], ids=["enabled-sibling", "disabled-sibling"])
@pytest.mark.parametrize("target_kind", ["chat", "embedding"])
def test_editing_shared_key_preserves_siblings_ids_and_plugin_bindings(
    monkeypatch, tmp_path, online, disabled_sibling, target_kind,
):
    api, _client, profiles = _setup(monkeypatch, tmp_path, online, disabled_sibling=disabled_sibling)
    target = profiles[0] if target_kind == "chat" else profiles[2]
    before = _effective_models(tmp_path)
    config_paths = [
        tmp_path / "app/astrbot/data/config/astrbot_plugin_angel_heart_config.json",
        embedding_config_path(tmp_path),
    ]
    references = [_read(path) for path in config_paths]
    for key in (NEW_KEY, "synthetic-rotated-key", ""):
        result = _save(api, target.model, key, target.model_id, target_kind)
        assert result["ok"] is True, result
        assert result["data"]["id"] == target.model_id
        after = _effective_models(tmp_path)
        assert set(after) == set(before)
        model, source = after[target.model_id]
        assert source["key"] == [key or "synthetic-rotated-key"]
        assert model["provider_source_id"] != SOURCE_ID
        assert source["custom_headers"] == before[target.model_id][1]["custom_headers"]
        assert source["proxy"] == before[target.model_id][1]["proxy"]
        assert source["api_version"] == before[target.model_id][1]["api_version"]
        assert not {"key", "api_base", "embedding_api_key"} & model.keys()
        for provider_id in set(before) - {target.model_id}:
            assert after[provider_id] == before[provider_id]
        for path, reference in zip(config_paths, references):
            actual = _read(path)
            assert {field: actual[field] for field in reference} == reference
        settings = _read(_engine_path(tmp_path))["provider_settings"]
        assert settings["default_provider_id"] == profiles[0].model_id
        assert settings["llm_compress_provider_id"] == profiles[0].model_id
        assert len(_read(_engine_path(tmp_path))["provider_sources"]) == 2
    services = {item.provider_id: item for item in load_provider_services(tmp_path)}
    assert services[profiles[0].model_id].is_default is True
    assert services[profiles[2].model_id].is_default_embedding is True


@pytest.mark.parametrize("online", [False, True], ids=["cold", "hot"])
def test_disabled_model_alone_still_protects_its_shared_key(monkeypatch, tmp_path, online):
    profiles = _seed_shared_models(tmp_path, disabled_sibling=True)
    config = _read(_engine_path(tmp_path))
    config["provider"] = config["provider"][:2]
    write_json_atomic(_engine_path(tmp_path), config)
    client = _MultiModelClient(tmp_path, online)
    monkeypatch.setattr(model_api, "EngineApiClient", lambda _root: client)
    result = _save(_api(tmp_path), profiles[0].model, NEW_KEY, profiles[0].model_id)
    assert result["ok"] is True, result
    after = _effective_models(tmp_path)
    assert after[profiles[0].model_id][1]["key"] == [NEW_KEY]
    disabled, source = after[profiles[1].model_id]
    assert disabled["enable"] is False
    assert disabled["provider_source_id"] == SOURCE_ID
    assert source["key"] == [OLD_KEY]


@pytest.mark.parametrize("editing", [False, True], ids=["add", "edit"])
def test_hot_model_write_failure_never_replaces_sibling_credentials(monkeypatch, tmp_path, editing):
    api, client, profiles = _setup(monkeypatch, tmp_path, True)
    before = _effective_models(tmp_path)
    client.fail_model_write = True
    result = _save(
        api, profiles[0].model if editing else "chat-c", NEW_KEY,
        profiles[0].model_id if editing else "",
    )
    assert result["ok"] is False
    assert _effective_models(tmp_path) == before
    assert NEW_KEY not in json.dumps(result)


@pytest.mark.parametrize("ignored_field", ["key", "binding"])
def test_hot_save_rejects_runtime_that_ignores_new_key_or_source_binding(
    monkeypatch, tmp_path, ignored_field,
):
    api, client, profiles = _setup(monkeypatch, tmp_path, True)
    client.ignore = ignored_field
    result = _save(api, profiles[0].model, NEW_KEY, profiles[0].model_id)
    assert result["ok"] is False
    assert result["error"]["code"] == "15001"
    assert NEW_KEY not in json.dumps(result)
