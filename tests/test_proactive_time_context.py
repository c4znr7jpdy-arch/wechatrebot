"""Check the time actually sent to proactive models without contacting a provider."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from test_plugin_portability import _patched


PLUGIN = "astrbot_plugin_proactive_chat"
PACKAGE = "proactive_time_fixture"
SESSION = "wechat:GroupMessage:room@chatroom"
CURRENT_TIME = "2026年11月16日 星期一 15:47 (Asia/Shanghai, UTC+08:00)"
PERSONA = "保留既有角色设定和表达方式。"


@pytest.fixture(scope="module")
def checkout(tmp_path_factory):
    directory = tmp_path_factory.mktemp("proactive-time")
    _patched(directory, PLUGIN, "core/llm_adapter.py", all_patches=True)
    return directory


def _load(monkeypatch, name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(checkout, monkeypatch):
    for name, path in ((PACKAGE, checkout), (f"{PACKAGE}.core", checkout / "core"),
                       (f"{PACKAGE}.utils", checkout / "utils")):
        package = ModuleType(name)
        package.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "astrbot.api", NS(logger=Mock()))
    time_utils = _load(monkeypatch, f"{PACKAGE}.utils.time_utils",
                       checkout / "utils/time_utils.py")
    adapter = _load(monkeypatch, f"{PACKAGE}.core.llm_adapter",
                    checkout / "core/llm_adapter.py")

    class Clock:
        instant = datetime(2026, 11, 16, 7, 47, tzinfo=timezone.utc)
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            if tz is None:
                return NS(astimezone=lambda: cls.instant.astimezone(ZoneInfo("Asia/Shanghai")))
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(time_utils, "datetime", Clock)
    owner = adapter.LlmMixin()
    owner.timezone = ZoneInfo("Asia/Shanghai")
    owner.telemetry = None
    owner.session_data = {}
    owner._normalize_session_id = lambda value: value
    owner._get_chat_provider_candidate_ids = AsyncMock(return_value=["primary"])
    provider = NS(text_chat=AsyncMock(return_value=NS(completion_text="换个轻松的话题吧。")))
    owner.context = NS(
        llm_generate=AsyncMock(return_value=NS(completion_text="换个轻松的话题吧。")),
        get_using_provider=Mock(return_value=provider),
    )
    return NS(owner=owner, clock=Clock, time_utils=time_utils, provider=provider)


@pytest.mark.parametrize("instant,zone,expected", [
    (datetime(2026, 11, 16, 7, 47, tzinfo=timezone.utc), "Asia/Shanghai", CURRENT_TIME),
    (datetime(2026, 11, 15, 16, 5, tzinfo=timezone.utc), "Asia/Shanghai",
     "2026年11月16日 星期一 00:05 (Asia/Shanghai, UTC+08:00)"),
    (datetime(2026, 11, 15, 16, 5, tzinfo=timezone.utc), "America/Los_Angeles",
     "2026年11月15日 星期日 08:05 (America/Los_Angeles, UTC-08:00)"),
    (datetime(2026, 11, 16, 7, 47, tzinfo=timezone.utc), "Asia/Kolkata",
     "2026年11月16日 星期一 13:17 (Asia/Kolkata, UTC+05:30)"),
])
def test_weekday_uses_the_configured_local_date(runtime, instant, zone, expected):
    runtime.clock.instant = instant
    assert runtime.time_utils.format_current_time(ZoneInfo(zone)) == expected


def test_missing_timezone_uses_an_aware_system_local_time(runtime):
    assert runtime.time_utils.format_current_time(None) == CURRENT_TIME


@pytest.mark.parametrize("template", ["", "自然地聊一句。", "现在是{{current_time}}，未回复{{unanswered_count}}次。"])
def test_every_template_gets_authoritative_time_without_changing_persona_or_history(runtime, template):
    owner = runtime.owner
    history = [{"role": "assistant", "content": "昨天的旧消息：周日下午，周末快结束了。"}]
    original = copy.deepcopy(history)
    config = {"proactive_prompt": template}
    response, user_prompt = asyncio.run(owner._generate_llm_response(
        SESSION, config, history, PERSONA, 2))
    request = owner.context.llm_generate.call_args.kwargs
    assert response == "换个轻松的话题吧。"
    assert request["system_prompt"].startswith(PERSONA + "\n\n")
    assert CURRENT_TIME in request["system_prompt"]
    assert "历史对话、平台聊天流水、示例和近期主动消息" in request["system_prompt"]
    assert "不代表本次的当前时间" in request["system_prompt"]
    assert request["contexts"] == original == history
    assert config == {"proactive_prompt": template}
    assert user_prompt == template.replace("{{current_time}}", CURRENT_TIME).replace("{{unanswered_count}}", "2")
    assert request["prompt"] == user_prompt


@pytest.mark.parametrize("legacy", [False, True])
def test_provider_fallbacks_keep_the_same_time_snapshot(runtime, legacy):
    owner = runtime.owner
    owner._get_chat_provider_candidate_ids.return_value = ["primary", "backup"]

    async def generate(**request):
        runtime.clock.instant += timedelta(days=1)
        if legacy or request["chat_provider_id"] == "primary":
            raise TimeoutError("test provider timeout")
        return NS(completion_text="换个轻松的话题吧。")

    owner.context.llm_generate.side_effect = generate
    result, _ = asyncio.run(owner._generate_llm_response(
        SESSION, {"proactive_prompt": "{{current_time}}"}, [], PERSONA, 0))
    requests = [call.kwargs for call in owner.context.llm_generate.call_args_list]
    requests.extend(call.kwargs for call in runtime.provider.text_chat.call_args_list)
    assert result == "换个轻松的话题吧。"
    assert [request["chat_provider_id"] for request in requests[:2]] == ["primary", "backup"]
    assert len(requests) == (3 if legacy else 2)
    assert runtime.clock.calls == 1
    for request in requests:
        assert CURRENT_TIME in request["system_prompt"]
        assert request["prompt"] == CURRENT_TIME


@pytest.mark.parametrize("legacy", [False, True])
def test_repetition_regeneration_keeps_current_time_despite_old_weekend_examples(runtime, legacy):
    owner = runtime.owner
    previous = "周日下午，周末快结束了。"
    owner.session_data = {SESSION: {"recent_proactive_messages": [previous]}}
    if legacy:
        owner.context.llm_generate.side_effect = TimeoutError("test provider timeout")
        generate = runtime.provider.text_chat
    else:
        generate = owner.context.llm_generate

    async def respond(**request):
        runtime.clock.instant += timedelta(days=1)
        text = previous if generate.await_count <= 2 else "来分享一首最近喜欢的歌吧。"
        return NS(completion_text=text)

    generate.side_effect = respond
    result, _ = asyncio.run(owner._generate_llm_response(
        SESSION, {"proactive_prompt": "自然地聊一句。"}, [], PERSONA, 0))
    assert result == "来分享一首最近喜欢的歌吧。"
    assert generate.await_count == 3
    assert runtime.clock.calls == 1
    for call in generate.call_args_list:
        request = call.kwargs
        assert CURRENT_TIME in request["system_prompt"]
        assert "不代表本次的当前时间" in request["system_prompt"]
        assert previous in request["prompt"]
    assert "[重新生成]" in generate.call_args.kwargs["prompt"]


def test_platform_history_uses_the_same_date_weekday_and_timezone(runtime):
    context, count, _ = runtime.owner._format_platform_history_as_context(
        [{"sender_name": "test-user", "content": "聊点什么？"}],
        include_bot_messages=True,
    )
    assert count == 1
    assert CURRENT_TIME in context["content"]
