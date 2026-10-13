import ast
import asyncio
import __future__
import copy
import inspect
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from openai.resources.chat.completions import AsyncCompletions
import pytest


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
