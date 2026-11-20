"""Default vectors share the persisted LivingMemory binding and migrate safely."""
import copy
from types import SimpleNamespace

import pytest

from test_app_shell import _api
from wechatrebot_manager import model_api
from wechatrebot_manager.customer_config import load_provider_services
from wechatrebot_manager.embedding_default import (
    EMBEDDING_MODULE, embedding_config_path, read_embedding_config,
)
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.plugin_errors import PluginCenterError
from wechatrebot_manager.provider import OpenAIProviderProfile


def _catalog(root):
    profiles = [OpenAIProviderProfile(
        api_key="synthetic-key", api_base="https://vectors.example.test/v1",
        model=model, source_id="vectors", service_kind=kind,
    ).normalized() for model, kind in (("z-vector", "embedding"), ("a-vector", "embedding"), ("chat", "chat"))]
    write_json_atomic(root / "app/astrbot/data/cmd_config.json", {
        "provider_sources": [profiles[0].astrbot_source()],
        "provider": [item.astrbot_model() for item in profiles],
        "provider_settings": {"default_provider_id": profiles[2].model_id},
    })
    return profiles


def _offline(monkeypatch):
    monkeypatch.setattr(model_api, "EngineApiClient", lambda root: SimpleNamespace(is_available=lambda: False))


def test_implicit_embedding_default_uses_engine_order_before_card_sort(tmp_path):
    profiles = _catalog(tmp_path)
    services = load_provider_services(tmp_path)
    vectors = [item for item in services if item.service_kind == "embedding"]
    assert vectors[0].model == "a-vector"
    assert [item.provider_id for item in vectors if item.is_default_embedding] == [profiles[0].model_id]
    assert [item.provider_id for item in services if item.is_default] == [profiles[2].model_id]
    assert next(item for item in vectors if item.is_default_embedding).to_public_dict()["is_default_embedding"] is True


def test_select_embedding_default_preserves_other_settings_and_reply_model(monkeypatch, tmp_path):
    first, second, chat = _catalog(tmp_path)
    original = {"provider_settings": {"llm_provider_id": chat.model_id}, "recall": {"limit": 7}}
    write_json_atomic(embedding_config_path(tmp_path), original)
    _offline(monkeypatch)
    result = _api(tmp_path).set_default_embedding_provider(second.model_id)
    assert result["data"] == {"id": second.model_id, "apply_state": "saved_for_start"}
    expected = copy.deepcopy(original)
    expected["provider_settings"]["embedding_provider_id"] = second.model_id
    assert read_embedding_config(tmp_path) == expected
    services = load_provider_services(tmp_path)
    assert [item.provider_id for item in services if item.is_default_embedding] == [second.model_id]
    assert [item.provider_id for item in services if item.is_default] == [chat.model_id]
    for invalid in (chat.model_id, "missing"):
        assert _api(tmp_path).set_default_embedding_provider(invalid)["ok"] is False
        assert read_embedding_config(tmp_path) == expected


def test_embedding_default_hot_save_uses_revision_and_verified_receipt(monkeypatch, tmp_path):
    _, second, _ = _catalog(tmp_path)
    snapshot = {"provider_settings": {"embedding_provider_id": "old"}, "keep": [1, 2]}
    calls = []
    class Client:
        def is_available(self):
            return True
        def get_config(self, module):
            assert module == EMBEDDING_MODULE
            return {"config": copy.deepcopy(snapshot), "revision": "revision-1"}
        def save_config(self, module, config, revision):
            calls.append((module, config, revision))
            return {"state": "applied", "revision": "revision-2", "verified": True}
    monkeypatch.setattr(model_api, "EngineApiClient", lambda root: Client())
    result = _api(tmp_path).set_default_embedding_provider(second.model_id)
    assert result["data"]["apply_state"] == "applied"
    assert calls == [(EMBEDDING_MODULE, {"provider_settings": {"embedding_provider_id": second.model_id}, "keep": [1, 2]}, "revision-1")]


@pytest.mark.parametrize("state", ["pending_enable", "pending_setup"])
def test_embedding_default_preserves_pending_plugin_state(monkeypatch, tmp_path, state):
    _, second, _ = _catalog(tmp_path)
    client = SimpleNamespace(
        is_available=lambda: True,
        get_config=lambda module: {"config": {}, "revision": "before"},
        save_config=lambda module, config, revision: {"state": state, "verified": True},
    )
    monkeypatch.setattr(model_api, "EngineApiClient", lambda root: client)
    result = _api(tmp_path).set_default_embedding_provider(second.model_id)
    assert result["ok"] is True
    assert result["data"] == {"id": second.model_id, "apply_state": state}


def test_implicit_default_embedding_delete_requires_replacement(monkeypatch, tmp_path):
    first, second, _ = _catalog(tmp_path)
    _offline(monkeypatch)
    api = _api(tmp_path)
    assert api.delete_provider(first.model_id)["ok"] is False
    assert api.delete_provider(first.model_id, second.model_id)["ok"] is True
    assert read_embedding_config(tmp_path)["provider_settings"]["embedding_provider_id"] == second.model_id
    services = load_provider_services(tmp_path)
    assert first.model_id not in {item.provider_id for item in services}
    assert [item.provider_id for item in services if item.is_default_embedding] == [second.model_id]


def test_failed_offline_embedding_delete_restores_binding(monkeypatch, tmp_path):
    first, second, _ = _catalog(tmp_path)
    original = {"provider_settings": {"embedding_provider_id": first.model_id}, "other": "keep"}
    write_json_atomic(embedding_config_path(tmp_path), original)
    _offline(monkeypatch)
    def reject(*args):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(model_api, "delete_provider_profile", reject)
    assert _api(tmp_path).delete_provider(first.model_id, second.model_id)["ok"] is False
    assert read_embedding_config(tmp_path) == original
    assert first.model_id in {item.provider_id for item in load_provider_services(tmp_path)}


def test_failed_hot_embedding_delete_restores_binding_with_new_revision(monkeypatch, tmp_path):
    first, second, _ = _catalog(tmp_path)
    config = {"provider_settings": {"embedding_provider_id": first.model_id}, "keep": True}
    write_json_atomic(embedding_config_path(tmp_path), config)
    calls = []
    class Client:
        def is_available(self):
            return True
        def get_config(self, module):
            return {"config": copy.deepcopy(config), "revision": "old"}
        def save_config(self, module, updated, base_revision=None):
            calls.append((copy.deepcopy(updated), base_revision))
            return {"revision": "new"}
        def delete_provider(self, *args):
            raise PluginCenterError("synthetic delete failure")
    monkeypatch.setattr(model_api, "EngineApiClient", lambda root: Client())
    assert _api(tmp_path).delete_provider(first.model_id, second.model_id)["ok"] is False
    assert calls == [({"provider_settings": {"embedding_provider_id": second.model_id}, "keep": True}, "old"), (config, "new")]


def test_editing_default_embedding_migrates_id_without_duplicate(monkeypatch, tmp_path):
    first, second, _ = _catalog(tmp_path)
    write_json_atomic(embedding_config_path(tmp_path), {
        "provider_settings": {"embedding_provider_id": first.model_id},
    })
    _offline(monkeypatch)
    result = _api(tmp_path).save_provider({
        "provider_id": first.model_id, "service_kind": "embedding", "api_key": "",
        "api_base": first.api_base, "model": "renamed-vector",
    })
    assert result["ok"] is True
    services = load_provider_services(tmp_path)
    vectors = [item for item in services if item.service_kind == "embedding"]
    assert len(vectors) == 2
    assert first.model_id not in {item.provider_id for item in vectors}
    assert result["data"]["is_default_embedding"] is True
    assert read_embedding_config(tmp_path)["provider_settings"]["embedding_provider_id"] == result["data"]["id"]
    assert second.model_id in {item.provider_id for item in vectors}


def test_corrupt_embedding_settings_are_not_overwritten(monkeypatch, tmp_path):
    _, second, _ = _catalog(tmp_path)
    path = embedding_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("invalid", encoding="utf-8")
    _offline(monkeypatch)
    assert len(load_provider_services(tmp_path)) == 3
    assert _api(tmp_path).set_default_embedding_provider(second.model_id)["ok"] is False
    assert path.read_text(encoding="utf-8") == "invalid"


def test_embedding_default_and_references_reuse_one_config_snapshot(monkeypatch, tmp_path):
    from pathlib import Path

    first, _, _ = _catalog(tmp_path)
    path = embedding_config_path(tmp_path)
    write_json_atomic(path, {"provider_settings": {"embedding_provider_id": first.model_id}})
    read_text = Path.read_text
    reads = []
    def read_once(current, *args, **kwargs):
        if current == path:
            reads.append(current)
        return read_text(current, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_once)
    services = load_provider_services(tmp_path)
    assert len(reads) == 1
    selected = next(item for item in services if item.provider_id == first.model_id)
    assert selected.is_default_embedding is True
    assert selected.reference_modules == (EMBEDDING_MODULE,)
