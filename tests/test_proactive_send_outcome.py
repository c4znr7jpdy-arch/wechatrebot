"""Keep rejected proactive sends out of delivered history and reply counters."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time
import traceback
from types import MethodType, ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_background_plugin_permissions import background  # noqa: F401
from test_hot_config_runtime import _runtime_function
from test_plugin_portability import _patched


PLUGIN = "astrbot_plugin_proactive_chat"
ROOT = Path(__file__).parents[1]
SESSION = "wechat:GroupMessage:room@chatroom"
RESPONSE = "Generated proactive message"


@pytest.fixture
def sender(tmp_path, background, monkeypatch):
    source = _patched(tmp_path, PLUGIN, "core/message_sender.py", all_patches=True)
    for relative in ("core/chat_flow.py", "core/message_events.py"):
        path = tmp_path / relative
        if not path.exists():
            path.write_bytes((ROOT / "vendor/data/plugins" / PLUGIN / relative).read_bytes())
    package_name = "proactive_outcome_fixture.core"
    for name, path in (("proactive_outcome_fixture", tmp_path),
                       (package_name, tmp_path / "core")):
        package = ModuleType(name)
        package.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "astrbot.api", NS(logger=Mock()))
    monkeypatch.setitem(sys.modules, "astrbot.core.platform.platform", NS(
        PlatformStatus=NS(RUNNING="running")))
    helper_name = f"{package_name}.wechat_group_scope"
    helper_spec = importlib.util.spec_from_file_location(
        helper_name, tmp_path / "core/wechat_group_scope.py")
    helper = importlib.util.module_from_spec(helper_spec)
    monkeypatch.setitem(sys.modules, helper_name, helper)
    helper_spec.loader.exec_module(helper)

    scope = {"__package__": package_name, "asyncio": asyncio, "time": time,
             "traceback": traceback, "logger": Mock(),
             "PlatformStatus": NS(RUNNING="running"), "MessageChain": list,
             "Plain": lambda **kwargs: NS(**kwargs), "Record": lambda **kwargs: NS(**kwargs)}
    config = {"enable": True, "tts_settings": {"enable_tts": False},
              "segmented_reply_settings": {"enable": False}}
    owner = NS(context=background.context, telemetry=None,
               session_data={SESSION: {"self_id": "wxid_bot"}},
               _parse_session_id=lambda session: tuple(session.split(":", 2)),
               _get_session_config=lambda session: config,
               _get_session_log_str=lambda *args: "test-session",
               _trigger_decorating_hooks=AsyncMock(side_effect=lambda session, chain, **kwargs: chain),
               _persist_proactive_message_to_platform_history=AsyncMock(),
               _reset_group_silence_timer=AsyncMock(),
               _split_text=lambda text, settings: ["first", "second"],
               _calc_interval=AsyncMock(return_value=0))
    for name in ("_send_chain_with_hooks", "_send_proactive_message"):
        setattr(owner, name, MethodType(_runtime_function(source, name, scope), owner))
    return owner, config, scope


@pytest.mark.parametrize("outcome", ["accepted", "permission", "bridge-rejected", "bridge-error", "empty-decoration"])
def test_send_chain_reports_the_real_background_result(sender, background, outcome):
    owner, _config, _scope = sender
    if outcome == "permission":
        background.save([])
    elif outcome == "bridge-rejected":
        background.bot.send_group_msg.return_value = {"retcode": 1403}
    elif outcome == "bridge-error":
        background.bot.send_group_msg.side_effect = OSError("test transport failure")
    elif outcome == "empty-decoration":
        owner._trigger_decorating_hooks = AsyncMock(return_value=[])
    accepted = asyncio.run(owner._send_chain_with_hooks(SESSION, ["text"]))
    assert accepted is (outcome == "accepted")
    assert owner._persist_proactive_message_to_platform_history.await_count == int(outcome == "accepted")


@pytest.mark.parametrize("mode", ["text", "tts-only", "tts-fallback", "segments", "partial-segments",
                                 "disabled-session", "no-delivery", "tts-all-rejected",
                                 "tts-with-text-failure", "segment-first-rejected"])
def test_send_message_requires_a_complete_text_or_voice_delivery(sender, background, mode):
    owner, config, _scope = sender
    calls = []

    async def send(session, components):
        calls.append(components)
        return (mode not in {"no-delivery", "tts-all-rejected", "segment-first-rejected"}
                and (mode not in {"partial-segments", "tts-with-text-failure"} or len(calls) == 1))

    owner._send_chain_with_hooks = send
    if mode in {"tts-only", "tts-fallback", "tts-all-rejected", "tts-with-text-failure"}:
        config["tts_settings"] = {"enable_tts": True, "always_send_text": mode == "tts-with-text-failure"}
        background.context.get_using_tts_provider = lambda **kwargs: NS(get_audio=AsyncMock(return_value="audio.wav"))
        if mode == "tts-fallback":
            async def tts_then_text(session, components):
                calls.append(components)
                return len(calls) > 1
            owner._send_chain_with_hooks = tts_then_text
    elif mode in {"segments", "partial-segments", "segment-first-rejected"}:
        config["segmented_reply_settings"] = {"enable": True, "words_count_threshold": 150}
    elif mode == "disabled-session":
        config["enable"] = False
    accepted = asyncio.run(owner._send_proactive_message(SESSION, RESPONSE))
    assert accepted is (mode not in {"partial-segments", "disabled-session", "no-delivery",
                                     "tts-all-rejected", "segment-first-rejected"})
    assert len(calls) == {"text": 1, "tts-only": 1, "tts-fallback": 2,
                          "segments": 2, "partial-segments": 2, "disabled-session": 0,
                          "no-delivery": 1, "tts-all-rejected": 2, "tts-with-text-failure": 2,
                          "segment-first-rejected": 1}[mode]
    assert owner._reset_group_silence_timer.await_count == int(bool(accepted))


def _flow(owner, scope, plugin_root: Path, session=SESSION):
    owner._normalize_session_id = lambda session: session
    owner._is_chat_allowed = AsyncMock(return_value=(True, "allowed"))
    owner.data_lock = asyncio.Lock()
    owner.session_data = {session: {"self_id": "wxid_bot"}}
    owner.last_message_times = {}
    owner._prepare_llm_request = AsyncMock(return_value={"conv_id": "conversation", "history": [],
                                                        "system_prompt": "", "session_id": session})
    owner._generate_llm_response = AsyncMock(return_value=(RESPONSE, "generated prompt"))
    owner._schedule_next_chat_and_save = AsyncMock()
    owner._finalize_and_reschedule = AsyncMock()
    owner._parse_session_id = lambda session: ("wechat", "GroupMessage", "room@chatroom")
    owner._clear_session_schedule_state = Mock(return_value=False)
    owner._clear_manual_trigger_state = AsyncMock()
    owner._save_data_internal = AsyncMock()
    source = (plugin_root / "core/chat_flow.py").read_text("utf-8")
    owner.check_and_chat = MethodType(_runtime_function(source, "check_and_chat", scope), owner)


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("outcome", ["accepted", "permission", "bridge-rejected", "bridge-error"])
def test_scheduled_flow_only_finalizes_delivered_messages(sender, background, tmp_path, monkeypatch, outcome, private):
    owner, _config, scope = sender
    # Sender and the caller use the same formally applied plugin files.
    session = "wechat:FriendMessage:friend" if private else SESSION
    _flow(owner, scope, tmp_path, session)
    if private:
        owner._parse_session_id = lambda session: ("wechat", "FriendMessage", "friend")
        policy = Path.cwd().parent.parent / "data/manager/private-chat-policies.json"
        policy.write_text(json.dumps({"schema_version": 1, "accounts": {"wxid_bot": {"contacts": {
            "friend": {"allowed_features": [] if outcome == "permission" else [PLUGIN]}
        }}}}), encoding="utf-8")

        async def private_send(**kwargs):
            await kwargs["bot"].send_private_msg(user_id=kwargs["session_id"], message=kwargs["message_chain"])

        transport = sys.modules["astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"]
        monkeypatch.setattr(transport.AiocqhttpMessageEvent, "send_message", private_send)
    if outcome == "permission":
        background.save([])
    elif outcome == "bridge-rejected":
        background.bot.send_group_msg.return_value = {"retcode": 1403}
        background.bot.send_private_msg.return_value = {"retcode": 1403}
    elif outcome == "bridge-error":
        background.bot.send_group_msg.side_effect = OSError("test transport failure")
        background.bot.send_private_msg.side_effect = OSError("test transport failure")
    asyncio.run(owner.check_and_chat(session))
    assert owner._finalize_and_reschedule.await_count == int(outcome == "accepted"), scope["logger"].mock_calls
    assert owner._schedule_next_chat_and_save.await_count == int(private and outcome != "accepted")
    owner._clear_manual_trigger_state.assert_awaited_once_with(session)


@pytest.mark.parametrize("accepted", [True, False])
def test_live_interjection_only_archives_delivered_messages(sender, tmp_path, accepted):
    owner, config, scope = sender
    _flow(owner, scope, tmp_path)
    owner.live_interject_sessions = set()
    owner._build_live_interject_context = AsyncMock(return_value="recent messages")
    owner._archive_live_interject_pair = AsyncMock()
    owner._send_proactive_message = AsyncMock(return_value=accepted)
    source = (tmp_path / "core/message_events.py").read_text("utf-8")
    run = _runtime_function(source, "_run_live_interject", scope)
    asyncio.run(run(owner, SESSION, config, "last message", time.time(),
                    {"max_response_delay_seconds": 60, "prompt": "{{live_context}}"}))
    assert owner._archive_live_interject_pair.await_count == int(accepted)
    assert owner.live_interject_sessions == set()


def test_session_disabled_during_generation_is_not_sent_or_rescheduled(sender, tmp_path):
    owner, config, scope = sender
    _flow(owner, scope, tmp_path)
    owner._parse_session_id = lambda session: ("wechat", "FriendMessage", "friend")

    async def generate(*args):
        config["enable"] = False
        return RESPONSE, "generated prompt"

    owner._generate_llm_response = generate
    asyncio.run(owner.check_and_chat(SESSION))
    owner._finalize_and_reschedule.assert_not_awaited()
    owner._schedule_next_chat_and_save.assert_not_awaited()
    owner._persist_proactive_message_to_platform_history.assert_not_awaited()


@pytest.mark.parametrize("accepted", [True, False])
def test_real_finalizer_preserves_counter_and_history_when_send_was_denied(sender, tmp_path, accepted):
    owner, _config, scope = sender
    _flow(owner, scope, tmp_path)
    owner.session_data[SESSION] = {"self_id": "wxid_bot", "unanswered_count": 2,
                                   "recent_proactive_messages": ["previous"]}
    owner.context.conversation_manager = NS(add_message_pair=AsyncMock())
    owner.RECENT_PROACTIVE_MESSAGES_LIMIT = 20
    owner._send_proactive_message = AsyncMock(return_value=accepted)
    scope.update(UserMessageSegment=NS, AssistantMessageSegment=NS, TextPart=NS)
    source = (tmp_path / "core/chat_flow.py").read_text("utf-8")
    owner._finalize_and_reschedule = MethodType(_runtime_function(source, "_finalize_and_reschedule", scope), owner)
    asyncio.run(owner.check_and_chat(SESSION))
    assert owner.session_data[SESSION]["unanswered_count"] == 2 + int(accepted)
    assert owner.session_data[SESSION]["recent_proactive_messages"] == (["previous", RESPONSE] if accepted else ["previous"])
    assert owner.context.conversation_manager.add_message_pair.await_count == int(accepted)
