import ast
import asyncio
import __future__
import copy
import inspect
from pathlib import Path
import shutil
import subprocess
import sys
from types import MethodType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from openai.resources.chat.completions import AsyncCompletions
import pytest

from test_hot_config_runtime import _patched_runtime_source, _runtime_function
from wechatrebot_manager.engine_api import EngineApiClient


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "vendor/engine"
PATCH = ROOT / "patches" / "astrbot-4.26.6-wechat.patch"


@pytest.fixture
def patched_provider_sources(tmp_path: Path):
    relatives = (
        Path("astrbot/core/provider/sources/openai_source.py"),
        Path("astrbot/core/provider/sources/request_retry.py"),
    )
    if not all((SOURCE / relative).is_file() for relative in relatives):
        pytest.fail("Pinned AstrBot source unavailable")
    for relative in relatives:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / relative, target)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "apply",
            "-p2",
            *[f"--include={relative.as_posix()}" for relative in relatives],
            str(PATCH),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    openai = (tmp_path / relatives[0]).read_text(encoding="utf-8-sig")
    retry = (tmp_path / relatives[1]).read_text(encoding="utf-8-sig")
    return openai, retry


def test_provider_runtime_uses_one_bounded_attempt(patched_provider_sources) -> None:
    openai, retry = patched_provider_sources
    assert openai.count("max_retries=0") == 2
    assert "REQUEST_RETRY_ATTEMPTS = 1" in retry


@pytest.fixture
def provider_request(patched_provider_sources):
    source, _ = patched_provider_sources
    methods = [node for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.AsyncFunctionDef)
               and node.name in {"_prepare_chat_payload", "_query"}]
    requests = []

    async def retry_request(_label, request, **_kwargs):
        return await request()

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace()

    async def parse(_completion, _tools):
        return "generated report"

    scope = {"copy": copy, "retry_provider_request": retry_request,
             "ChatCompletion": SimpleNamespace,
             "logger": SimpleNamespace(debug=lambda *_args: None)}
    exec(compile(ast.Module(body=methods, type_ignores=[]), "patched-provider", "exec",
                 flags=__future__.annotations.compiler_flag), scope)
    provider = SimpleNamespace(
        timeout=35, provider_config={"timeout": 35},
        get_model=lambda: "analysis-model", _ensure_message_to_dicts=lambda value: value,
        _context_contains_image=lambda _value: False,
        _finally_convert_payload=lambda _payload: None,
        _apply_provider_specific_request_overrides=lambda *_args: None,
        _sanitize_assistant_messages=lambda _payload: None,
        default_params=set(inspect.signature(AsyncCompletions.create).parameters),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        _parse_openai_completion=parse,
    )

    async def request(**kwargs):
        payload, _ = await scope["_prepare_chat_payload"](provider, None, **kwargs)
        return await scope["_query"](provider, payload, None)

    return provider, requests, request


def test_analysis_timeout_reaches_sdk_without_changing_shared_provider(provider_request):
    provider, requests, request = provider_request

    async def run():
        return await asyncio.gather(request(timeout=90), request())

    assert asyncio.run(run()) == ["generated report", "generated report"]
    assert requests[0]["timeout"] == 90.0
    assert "timeout" not in requests[0]["extra_body"]
    assert "timeout" not in requests[1]
    assert provider.timeout == provider.provider_config["timeout"] == 35


@pytest.mark.parametrize("timeout", [True, 0, -1, "90", float("inf"), float("nan")])
def test_provider_rejects_invalid_per_request_timeout(provider_request, timeout):
    _provider, requests, request = provider_request
    with pytest.raises(ValueError, match="超时必须为有限正数"):
        asyncio.run(request(timeout=timeout))
    assert requests == []


@pytest.fixture
def provider_catalog(tmp_path, monkeypatch):
    manager_source = _patched_runtime_source(
        tmp_path, Path("astrbot/core/provider/manager.py"),
    )
    service_source = _patched_runtime_source(
        tmp_path, Path("astrbot/dashboard/services/config_service.py"),
    )
    saved = []

    class Config(dict):
        def save_config(self):
            saved.append(copy.deepcopy(self))

    config = Config(
        provider=[
            {"id": f"{source}/{model}", "model": model, "provider_source_id": source,
             "provider_type": kind, "enable": True}
            for source, model, kind in (
                ("shared", "qwen3-embedding-8b", "embedding"),
                ("shared", "gemini-embedding-2-preview", "embedding"),
                ("other", "reply-model", "chat_completion"),
            )
        ],
        provider_sources=[{"id": source} for source in ("shared", "other")],
    )
    manager = SimpleNamespace(
        acm=SimpleNamespace(default_conf=config),
        providers_config=config["provider"],
        provider_sources_config=config["provider_sources"],
        resource_lock=asyncio.Lock(), reload_lock=asyncio.Lock(),
        inst_map={item["id"]: copy.deepcopy(item) for item in config["provider"]},
        provider_insts=[], stt_provider_insts=[], tts_provider_insts=[],
    )

    async def terminate(provider_id):
        manager.inst_map.pop(provider_id, None)

    async def load(provider):
        manager.inst_map[provider["id"]] = copy.deepcopy(provider)

    manager.terminate_provider, manager.load_provider = terminate, load
    scope = {"copy": copy, "astrbot_config": config,
             "logger": SimpleNamespace(info=lambda *_args: None)}
    for name in ("delete_provider", "create_provider", "update_provider", "reload",
                 "get_provider_config_by_id", "get_merged_provider_config"):
        setattr(manager, name, MethodType(_runtime_function(manager_source, name, scope), manager))
    monkeypatch.setitem(sys.modules, "astrbot.core.utils.llm_metadata", SimpleNamespace(LLM_METADATAS={}))
    service = SimpleNamespace(config=config, provider_manager=manager, CAPABILITY_TO_PROVIDER_TYPE={})
    scope = {"copy": copy, "save_config": lambda *_args, **_kwargs: config.save_config()}
    for name in ("list_providers", "list_provider_sources", "_resolve_provider_type",
                 "delete_provider", "delete_provider_source"):
        setattr(service, name, MethodType(_runtime_function(service_source, name, scope), service))
    client = EngineApiClient(tmp_path)
    requests = []

    def request(method, path, payload=None):
        requests.append((method, path))
        parsed = urlsplit(path)
        if method == "GET" and parsed.path == "/api/v1/providers":
            return service.list_providers()
        if method == "GET" and parsed.path == "/api/v1/provider-sources":
            return service.list_provider_sources()
        query = parse_qs(parsed.query)
        if method == "DELETE" and parsed.path == "/api/v1/providers/by-id":
            asyncio.run(service.delete_provider(query["provider_id"][0]))
            return {}
        if method == "DELETE" and parsed.path == "/api/v1/provider-sources/by-id":
            asyncio.run(service.delete_provider_source(query["source_id"][0]))
            return {}
        raise AssertionError((method, path))

    client.request = request
    return SimpleNamespace(manager=manager, service=service, client=client,
                           config=config, saved=saved, requests=requests)


def test_embedding_delete_readback_preserves_shared_source_and_second_model(provider_catalog):
    catalog = provider_catalog
    target, remaining, other = [item["id"] for item in catalog.config["provider"]]

    catalog.client.delete_provider(target, "shared")

    assert [item["id"] for item in catalog.service.list_providers()["providers"]] == [remaining, other]
    assert catalog.manager.get_provider_config_by_id(target) is None
    assert catalog.manager.get_provider_config_by_id(remaining)["model"] == "gemini-embedding-2-preview"
    assert target not in catalog.manager.inst_map
    assert remaining in catalog.manager.inst_map
    assert catalog.config == catalog.saved[-1]
    assert [item["id"] for item in catalog.config["provider_sources"]] == ["shared", "other"]
    assert [(method, path) for method, path in catalog.requests if method == "DELETE"] == [
        ("DELETE", "/api/v1/providers/by-id?provider_id=shared%2Fqwen3-embedding-8b"),
    ]


def test_last_embedding_delete_cleans_only_its_unused_source(provider_catalog):
    catalog = provider_catalog
    first, second, other = [item["id"] for item in catalog.config["provider"]]
    catalog.client.delete_provider(first, "shared")
    catalog.client.delete_provider(second, "shared")

    assert [item["id"] for item in catalog.service.list_providers()["providers"]] == [other]
    assert [item["id"] for item in catalog.config["provider_sources"]] == ["other"]
    assert catalog.manager.get_provider_config_by_id(second) is None
    assert catalog.config == catalog.saved[-1]


def test_source_delete_after_single_model_delete_uses_current_catalog(provider_catalog):
    catalog = provider_catalog
    first, second, other = [item["id"] for item in catalog.config["provider"]]
    catalog.client.delete_provider(first, "shared")
    asyncio.run(catalog.service.delete_provider_source("shared"))

    assert [item["id"] for item in catalog.service.list_providers()["providers"]] == [other]
    assert catalog.manager.get_provider_config_by_id(second) is None
    assert set(catalog.manager.inst_map) == {other}
    assert [item["id"] for item in catalog.config["provider_sources"]] == ["other"]


def test_deleted_embedding_can_be_recreated_and_updated_without_stale_records(provider_catalog):
    catalog = provider_catalog
    target = copy.deepcopy(catalog.config["provider"][0])
    catalog.client.delete_provider(target["id"], "shared")
    asyncio.run(catalog.manager.create_provider(target))
    updated = {**target, "embedding_dimensions": 1024}
    asyncio.run(catalog.manager.update_provider(target["id"], updated))

    records = catalog.service.list_providers()["providers"]
    matches = [item for item in records if item["id"] == target["id"]]
    assert len(matches) == 1
    assert matches[0]["embedding_dimensions"] == 1024
    assert catalog.manager.get_provider_config_by_id(target["id"]) == updated
    assert catalog.manager.inst_map[target["id"]] == updated
    assert catalog.config == catalog.saved[-1]


@pytest.fixture
def runtime_model_test(tmp_path):
    import time
    source = _patched_runtime_source(tmp_path, Path("astrbot/dashboard/services/config_service.py"))
    function = _runtime_function(source, "test_provider", {"asyncio": asyncio, "time": time})
    calls = []
    provider = SimpleNamespace()
    provider.meta = lambda: SimpleNamespace(id="saved/model", model="model", provider_type=SimpleNamespace(value="chat_completion"))

    async def text_chat(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text="PONG", reasoning_content="private reasoning")

    provider.text_chat = text_chat
    service = SimpleNamespace(provider_manager=SimpleNamespace(inst_map={"saved/model": provider}))
    return lambda: asyncio.run(function(service, "saved/model")), provider, calls


def test_runtime_model_probe_returns_actual_answer_and_duration(runtime_model_test):
    run, provider, calls = runtime_model_test
    result = run()
    assert result["status"] == "available" and result["response_text"] == "PONG"
    assert result["test_kind"] == "chat" and result["id"] == "saved/model"
    assert result["elapsed_ms"] >= 0
    assert calls == [{"prompt": "REPLY `PONG` ONLY", "request_max_retries": 1}]
    assert "private reasoning" not in str(result)


@pytest.mark.parametrize("content", [None, "", "   ", [], 12])
def test_runtime_model_probe_rejects_empty_or_invalid_answers(runtime_model_test, content):
    run, provider, _calls = runtime_model_test

    async def empty(**kwargs):
        return SimpleNamespace(completion_text=content, reasoning_content="thinking only")

    provider.text_chat = empty
    result = run()
    assert result["status"] == "unavailable"
    assert result["error_type"] == "EmptyModelResponseError"
    assert "response_text" not in result and result["elapsed_ms"] >= 0


def test_runtime_model_probe_preserves_typed_timeout(runtime_model_test):
    run, provider, _calls = runtime_model_test

    async def fail(**kwargs):
        raise TimeoutError()

    provider.text_chat = fail
    result = run()
    assert result["status"] == "unavailable" and result["error_type"] == "TimeoutError"
    assert result["error"] and result["elapsed_ms"] >= 0


def test_runtime_model_probe_caps_displayed_answer(runtime_model_test):
    run, provider, _calls = runtime_model_test

    async def long_answer(**kwargs):
        return SimpleNamespace(completion_text="x" * 3000)

    provider.text_chat = long_answer
    assert len(run()["response_text"]) == 2000


def test_final_provider_failure_is_logged_without_request_details(patched_provider_sources):
    _source, retry_source = patched_provider_sources
    logs, requests = [], []

    class Attempt:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    async def attempts():
        yield Attempt()

    async def request():
        requests.append(1)
        raise TimeoutError("private upstream key=secret response")

    scope = {"logger": SimpleNamespace(error=logs.append), "_build_retrying": lambda *a, **k: attempts(),
             "_get_status_code": lambda error: None}
    function = _runtime_function(retry_source, "retry_provider_request", scope)
    with pytest.raises(TimeoutError):
        asyncio.run(function("OpenAI", request))
    assert requests == [1]
    assert logs == ["MODEL_REQUEST_FAILED error_type=TimeoutError status_code=none"]
    assert "secret" not in str(logs)
