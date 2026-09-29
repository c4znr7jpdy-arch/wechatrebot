import asyncio
import copy
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from test_hot_config_runtime import _runtime_function
from wechatrebot_manager.provider import OpenAIProviderProfile, repair_embedding_provider_types

ROOT = Path(__file__).parents[1]


def _patched_variant(tmp_path, module, variant):
    shutil.copytree(ROOT / "tests/fixtures/patch_sources/data/plugins" / module, tmp_path, dirs_exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "apply", "--ignore-space-change", str(ROOT / f"patches/astrbot-plugin-{module}--{variant}.patch")],
                   cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_embedding_adapter_and_credentials_preserve_shared_image_source():
    profile = OpenAIProviderProfile(api_key="test-secret", api_base="https://example.test/v1",
                                    model="vector-model", source_id="shared", service_kind="embedding")
    merged = {**profile.astrbot_source(), **profile.astrbot_model()}
    assert merged["type"] == "openai_embedding"
    assert merged["provider_type"] == "embedding"
    assert merged["embedding_model"] == "vector-model"
    assert merged["embedding_api_base"] == "https://example.test/v1"
    assert merged["embedding_api_key"] == "test-secret"
    source = {"id": "shared", "type": "openai_chat_completion", "key": ["test-secret"], "api_base": "https://example.test/v1"}
    image = {"id": "shared/image", "service_kind": "image", "provider_source_id": "shared"}
    vector = {"id": "shared/vector", "model": "vector", "service_kind": "embedding", "provider_source_id": "shared"}
    config = {"provider_sources": [source], "provider": [image, vector]}
    before = copy.deepcopy(image)
    assert repair_embedding_provider_types(config)
    assert not repair_embedding_provider_types(config)
    assert image == before
    assert source["type"] == "openai_chat_completion"
    assert vector["id"] == "shared/vector" and vector["embedding_model"] == "vector"


@pytest.fixture
def debounce(tmp_path, monkeypatch):
    root = _patched_variant(tmp_path, "astrbot_plugin_angel_heart", "mention-latency")
    spec = importlib.util.spec_from_file_location("debounce_regression", root / "core/debounce_manager.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    config = NS(waiting_time=30, secretary_debounce_time=30, assistant_debounce_time=0.05,
                accelerate_debounce_time=0.05)
    return module.DebounceManager(NS(for_chat=lambda _: config))


@pytest.mark.parametrize("busy", [False, True])
def test_direct_followup_skips_rest_and_retries_busy_work_quickly(debounce, busy):
    async def run():
        await debounce.start_assistant_rest("room", 60)
        running = [NS(status="running", kind="assistant")] if busy else []
        debounce.work_ledger = NS(get_active_works=lambda _: running)
        event = NS(set_extra=lambda *args: None)
        future = await debounce.schedule(chat_id="room", event=event, sender_id="user",
                                         message_id="new", is_wake=True, is_present=True)
        if busy:
            await asyncio.sleep(0.08)
            assert not future.done()
            assert debounce._assistant[("room", "user")].delay < 1
            running.clear()
        assert await asyncio.wait_for(future, 0.3) == "PROCESS"
        await debounce.cleanup()
    asyncio.run(run())


def test_ordinary_chatter_still_obeys_rest(debounce):
    async def run():
        await debounce.start_assistant_rest("room", 60)
        future = await debounce.schedule(chat_id="room", event=NS(), sender_id="user",
                                         message_id="new", is_wake=False, is_present=True)
        assert future is None or not future.done()
        await debounce.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize("dimension,vector", [(3, [1.0]), (0, [0.1, 0.2, 0.3])])
def test_memory_discovers_unknown_dimension_before_opening_indexes(tmp_path, dimension, vector):
    root = _patched_variant(tmp_path, "astrbot_plugin_livingmemory", "provider-readiness")
    method = _runtime_function((root / "core/plugin_initializer.py").read_text("utf-8"),
                               "_ensure_embedding_dimension", {"asyncio": asyncio, "ProviderNotReadyError": RuntimeError})
    provider = NS(get_dim=lambda: dimension, get_embedding=AsyncMock(return_value=vector), provider_config={})
    asyncio.run(method(NS(embedding_provider=provider)))
    if dimension:
        provider.get_embedding.assert_not_awaited()
    else:
        assert provider.provider_config["embedding_dimensions"] == len(vector)


def test_failed_embedding_probe_keeps_existing_configuration_and_hides_secrets(tmp_path):
    root = _patched_variant(tmp_path, "astrbot_plugin_livingmemory", "provider-readiness")
    method = _runtime_function((root / "core/plugin_initializer.py").read_text("utf-8"),
                               "_ensure_embedding_dimension", {"asyncio": asyncio, "ProviderNotReadyError": RuntimeError})
    provider = NS(get_dim=lambda: 0, get_embedding=AsyncMock(side_effect=ValueError("secret-key")), provider_config={})
    with pytest.raises(RuntimeError) as error:
        asyncio.run(method(NS(embedding_provider=provider)))
    assert "secret-key" not in str(error.value)
    assert provider.provider_config == {}


def test_meme_catalog_network_fetch_does_not_block_chat_event_loop(tmp_path):
    import threading
    root = _patched_variant(tmp_path, "meme_manager", "z-catalog-readiness")
    main_thread = threading.get_ident()
    worker_threads = []
    def fetch(_url):
        worker_threads.append(threading.get_ident())
        return {"index": {"packs": [{"id": "example"}]}}
    method = _runtime_function((root / "mixins/web_api.py").read_text("utf-8"),
        "_api_fetch_community_index", {"asyncio": asyncio, "COMMUNITY_INDEX_URL": "https://example.test/index",
        "fetch_and_cache_community_index": fetch, "jsonify": lambda value: value})
    result, status = asyncio.run(method(NS()))
    assert status == 200 and result["pack_count"] == 1
    assert worker_threads and worker_threads[0] != main_thread
