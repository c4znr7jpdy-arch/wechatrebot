"""Exercise production retry and SSE parsing with HTTPX replacing network I/O."""
import __future__
import ast
import asyncio
import copy
from dataclasses import dataclass, field
import inspect
import json
import logging
from pathlib import Path
import random
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from openai import AsyncOpenAI, BadRequestError, RateLimitError
from openai.lib.streaming.chat._completions import ChatCompletionStreamState
from openai.types.chat import ChatCompletion, ChatCompletionChunk
import pytest

from test_hot_config_runtime import _runtime_function
from test_provider_runtime_patch import patched_provider_sources  # noqa: F401


CORE = Path(__file__).parents[1] / "vendor/engine/astrbot/core"


@pytest.fixture
def provider_class(patched_provider_sources):
    source, _ = patched_provider_sources

    class Plain:
        def __init__(self, text):
            self.text = text

    async def request_once(_label, request, **options):
        # Nested provider retries have separate regression coverage. These
        # tests isolate the outer key-rotation loop, as a real caller can do.
        assert options["max_attempts"] == 1
        return await request()

    scope = {"__name__": __name__, "dataclass": dataclass, "field": field,
             "Plain": Plain, "Comp": SimpleNamespace(Plain=Plain), "copy": copy,
             "json": json, "random": random, "re": re,
             "asyncio": SimpleNamespace(sleep=AsyncMock()),
             "ChatCompletion": ChatCompletion, "ChatCompletionChunk": ChatCompletionChunk,
             "ChatCompletionStreamState": ChatCompletionStreamState,
             "EmptyModelOutputError": ValueError, "retry_provider_request": request_once,
             "is_connection_error": lambda _error: False,
             "logger": logging.getLogger(__name__), "is_checkpoint_message": lambda _value: False,
             "Message": type("UnusedMessage", (), {})}
    provider_base = (CORE / "provider/provider.py").read_text("utf-8")
    scope["Provider"] = type("ProviderDependency", (), {
        name: _runtime_function(provider_base, name, scope)
        for name in ("get_model", "_ensure_message_to_dicts", "pop_record")
    })
    scope["normalize_and_dedupe_strings"] = _runtime_function(
        (CORE / "utils/string_utils.py").read_text("utf-8"), "normalize_and_dedupe_strings", scope)
    # Execute the complete production provider class, including its error
    # handler, request builder and SSE parser, without importing an engine or
    # executing the constructor that registers shared application resources.
    provider = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.ClassDef) and node.name == "ProviderOpenAIOfficial")
    provider.decorator_list = []
    chain = next(node for node in ast.parse((CORE / "message/message_event_result.py").read_text("utf-8")).body
                 if isinstance(node, ast.ClassDef) and node.name == "MessageChain")
    chain.body = [node for node in chain.body if isinstance(node, ast.AnnAssign)
                  or isinstance(node, ast.FunctionDef) and node.name in {"message", "get_plain_text"}]
    entities = [node for node in ast.parse((CORE / "provider/entities.py").read_text("utf-8")).body
                if isinstance(node, ast.ClassDef) and node.name in {"TokenUsage", "LLMResponse"}]
    exec(compile(ast.Module(body=[chain, *entities, provider], type_ignores=[]), "production-provider", "exec",
                 flags=__future__.annotations.compiler_flag), scope)
    return scope["ProviderOpenAIOfficial"]


def _successful_response(stream):
    choice = {"index": 0, "finish_reason": "stop"}
    choice["delta" if stream else "message"] = {"role": "assistant", "content": "complete"}
    payload = {"id": "offline-completion", "created": 0, "model": "test-model",
               "object": "chat.completion.chunk" if stream else "chat.completion", "choices": [choice]}
    if stream:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode())
    return httpx.Response(200, json=payload)


def _provider(provider_class, respond):
    provider = object.__new__(provider_class)
    provider.provider_config = {}
    provider.model_name = "test-model"
    provider.reasoning_key = "reasoning_content"
    provider.api_keys = [f"synthetic-test-key-{index}" for index in range(11)]
    provider.client = AsyncOpenAI(api_key="synthetic-unused-key", base_url="https://offline.invalid/v1",
                                  max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    provider.default_params = inspect.signature(provider.client.chat.completions.create).parameters
    return provider


async def _consume(provider, stream, contexts=None):
    options = {"contexts": contexts or [{"role": "user", "content": "hello"}], "request_max_retries": 1}
    if stream:
        return [(response.is_chunk, response.completion_text)
                async for response in provider.text_chat_stream(**options)]
    response = await provider.text_chat(**options)
    return [(response.is_chunk, response.completion_text)]


@pytest.mark.parametrize("success_attempt", [1, 9, 10, None])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("recovery", ["rate_limit", "context_length"])
def test_success_and_exhaustion_at_recovery_boundaries(provider_class, success_attempt, stream, recovery):
    requests = []

    async def respond(request):
        requests.append(request)
        if success_attempt is None or len(requests) < success_attempt:
            if recovery == "context_length":
                return httpx.Response(400, json={"error": {"message": "maximum context length", "type": "invalid_request_error"}})
            return httpx.Response(429, json={"error": {"message": "test rate limit", "type": "rate_limit_error"}})
        return _successful_response(stream)

    async def run():
        provider = _provider(provider_class, respond)
        if recovery == "context_length":
            provider.api_keys = provider.api_keys[:1]
        contexts = [{"role": "user", "content": f"message {index}"} for index in range(30)]
        try:
            if success_attempt is None:
                with pytest.raises(RateLimitError if recovery == "rate_limit" else BadRequestError):
                    await _consume(provider, stream, contexts)
            else:
                assert (await _consume(provider, stream, contexts))[-1] == (False, "complete")
        finally:
            await provider.terminate()

    asyncio.run(run())
    expected = success_attempt or 10
    assert len(requests) == expected
    assert len({request.headers["authorization"] for request in requests}) == (expected if recovery == "rate_limit" else 1)
    assert all(json.loads(request.content)["stream"] is stream for request in requests)
    if recovery == "context_length":
        assert [len(json.loads(request.content)["messages"]) for request in requests] == [30 - 2 * index for index in range(expected)]


@pytest.mark.parametrize("stream", [False, True])
def test_nonretryable_error_is_preserved_without_extra_requests(provider_class, stream):
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"message": "invalid request", "type": "invalid_request_error"}})

    async def run():
        provider = _provider(provider_class, respond)
        try:
            with pytest.raises(BadRequestError):
                await _consume(provider, stream)
        finally:
            await provider.terminate()

    asyncio.run(run())
    assert len(requests) == 1
