"""Execute the patched production completion parser without an engine process."""
import __future__
import ast
import asyncio
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
from types import MethodType, SimpleNamespace

from openai.types.chat import ChatCompletion, ChatCompletionChunk
import pytest

from test_hot_config_runtime import _runtime_function
from test_provider_runtime_patch import patched_provider_sources  # noqa: F401


ROOT = Path(__file__).parents[1] / "vendor/engine/astrbot/core"
MISSING = object()


@pytest.fixture
def parser(patched_provider_sources):
    source, _ = patched_provider_sources

    class Plain:
        def __init__(self, text):
            self.text = text

    # Keep the actual response container and text-chain methods. Only the Plain
    # component dependency is isolated, so this never imports an engine runtime.
    scope = {"__name__": __name__, "dataclass": dataclass, "field": field,
             "Plain": Plain, "Comp": SimpleNamespace(Plain=Plain),
             "json": json, "re": re, "ChatCompletion": ChatCompletion,
             "ChatCompletionChunk": ChatCompletionChunk,
             "EmptyModelOutputError": ValueError, "logger": logging.getLogger(__name__)}
    chain = next(node for node in ast.parse((ROOT / "message/message_event_result.py").read_text("utf-8")).body
                 if isinstance(node, ast.ClassDef) and node.name == "MessageChain")
    chain.body = [node for node in chain.body if isinstance(node, ast.AnnAssign)
                  or isinstance(node, ast.FunctionDef) and node.name in {"message", "get_plain_text"}]
    entities = [node for node in ast.parse((ROOT / "provider/entities.py").read_text("utf-8")).body
                if isinstance(node, ast.ClassDef) and node.name in {"TokenUsage", "LLMResponse"}]
    exec(compile(ast.Module(body=[chain, *entities], type_ignores=[]), "production-response", "exec",
                 flags=__future__.annotations.compiler_flag), scope)
    provider = SimpleNamespace(reasoning_key="reasoning_content")
    for name in ["_normalize_content", "_extract_reasoning_content", "_extract_usage", "_parse_openai_completion"]:
        method = _runtime_function(source, name, scope)
        setattr(provider, name, method if name == "_normalize_content" else MethodType(method, provider))

    def parse(content="Answer", reasoning_field=MISSING, **message_values):
        message = {"role": "assistant", "content": content if isinstance(content, str) else None, **message_values}
        if reasoning_field is not MISSING:
            message["reasoning_content"] = reasoning_field
        completion = ChatCompletion.model_validate({
            "id": "reasoning-tags", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        })
        # Compatible gateways may deliver the non-string formats already
        # supported by _normalize_content, outside the strict SDK schema.
        completion.choices[0].message.content = content
        return asyncio.run(provider._parse_openai_completion(completion, None)), completion

    return parse


# Boundary cases from official AstrBot #10116, kept against our pinned patch.
@pytest.mark.parametrize("content,structured,answer,reasoning", [
    ("<think>First\nsecond</think>Answer", MISSING, "Answer", "First\nsecond"),
    ("<thinking>First\nsecond</thinking>Answer", MISSING, "Answer", "First\nsecond"),
    ("<thinking></thinking>Answer", MISSING, "Answer", ""),
    ("<thinking>Only reasoning</thinking>", MISSING, "", "Only reasoning"),
    ("<think>One</think><thinking>Two</thinking>Answer", MISSING, "Answer", "One\nTwo"),
    ("Answer</think>", MISSING, "Answer", None),
    ("Answer</thinking>  ", MISSING, "Answer", None),
    ("Ordinary thinking and reasoning", MISSING, "Ordinary thinking and reasoning", None),
    ("<thinker>Example</thinker>", MISSING, "<thinker>Example</thinker>", None),
    ("<thinking>Inline</thinking>Answer", "Structured", "Answer", "Structured"),
    ("<thinking>Inline</thinking>Answer", "", "Answer", ""),
    ("<thinking>Unfinished", MISSING, "<thinking>Unfinished", None),
    ("<think>Example</thinking>Answer", MISSING, "<think>Example</thinking>Answer", None),
])
def test_reasoning_tags_keep_existing_compatible_completion_contract(parser, content, structured, answer, reasoning):
    response, completion = parser(content, structured)
    assert response.completion_text == answer
    assert response.reasoning_content == reasoning
    assert response.raw_completion is completion
    assert response.id == "reasoning-tags"
    assert response.usage.total == 11


@pytest.mark.parametrize("content", [
    {"type": "text", "text": "<thinking>Hidden</thinking>Answer"},
    [{"type": "text", "text": "<thinking>Hidden</thinking>"}, {"type": "text", "text": "Answer"}],
    '[{"type":"text","text":"<thinking>Hidden</thinking>Answer"}]',
])
def test_tags_are_extracted_after_existing_content_normalization(parser, content):
    response, _ = parser(content)
    assert response.completion_text == "Answer"
    assert response.reasoning_content == "Hidden"


@pytest.mark.parametrize("content", ["<thinking>Example</think>Answer", "Answer <thinking", "<THINKING>Literal</THINKING>"])
def test_unmatched_or_unrecognized_tag_forms_keep_existing_literal_behavior(parser, content):
    response, _ = parser(content)
    assert response.completion_text == content
    assert response.reasoning_content is None


def test_explicit_null_structured_reasoning_remains_authoritative(parser):
    response, _ = parser("<thinking>Inline</thinking>Answer", None)
    assert response.completion_text == "Answer"
    assert response.reasoning_content == ""


def test_refusal_text_keeps_existing_path(parser):
    response, _ = parser(None, refusal="Cannot answer")
    assert response.completion_text == "Cannot answer"
    assert response.reasoning_content is None


def test_empty_output_is_still_rejected(parser):
    with pytest.raises(ValueError, match="no usable output"):
        parser("")
