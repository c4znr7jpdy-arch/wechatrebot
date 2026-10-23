"""Verify assembled clone entry points and delayed delivery without a robot."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from dataclasses import asdict
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_group_feature_runtime import permission_runtime, _private_policy
from test_voice_tool_delivery import (
    PLUGIN, delivery, patched_method, patched_plugins, plugin_for, speak,
)
from wechatrebot_manager.group_feature_runtime import plugin_allowed, session_plugin_allowed
from wechatrebot_manager.reply_policy_runtime import voice_reply_allowed


@pytest.fixture
def clone(permission_runtime, patched_plugins, tmp_path):
    root, disabled, stars, event, config, platform, context = permission_runtime
    stars[:] = [SimpleNamespace(root_dir_name=PLUGIN, name=PLUGIN, activated=True, reserved=False)]
    _private_policy(root, allowed_features=[PLUGIN])
    extras = {}
    event.get_extra = lambda key, default=None: extras.get(key, default)
    event.set_extra = extras.__setitem__
    event.clear_result = Mock()
    plugin = plugin_for(tmp_path / "speech.wav")
    plugin.context = context
    plugin._voice_allowed = MethodType(patched_method(
        patched_plugins, "_voice_allowed", voice_reply_allowed=voice_reply_allowed,
        plugin_allowed=plugin_allowed,
    ), plugin)
    plugin.plugin_config = SimpleNamespace(
        auto_tts_enabled=True, reply_mode="audio_only", delivery_mode="sync",
        auto_tts_probability=1.0, audio_transport="local_path",
    )
    return plugin, event, root, disabled, stars, config, platform


@pytest.mark.parametrize("denial", ["search", "chat", "global", "session", "inactive"])
def test_tool_does_not_synthesize_without_current_permission(clone, delivery, denial):
    plugin, event, root, disabled, stars, config, _ = clone
    if denial == "search":
        event.set_extra("_wechatrebot_web_search_turn", True)
    elif denial == "chat":
        _private_policy(root, allowed_features=[])
    elif denial == "global":
        config["plugin_set"] = []
    elif denial == "session":
        disabled.add(PLUGIN)
    else:
        stars[0].activated = False
    receipt = asyncio.run(speak(delivery, plugin, event))
    assert receipt["status"] == "blocked"
    plugin.synthesize_text.assert_not_awaited()
    plugin._send_audio_result.assert_not_awaited()
    event.clear_result.assert_not_called()


def test_tool_revoked_during_synthesis_retains_text_and_blocks_audio_file_retry(clone, delivery):
    plugin, event, root, *_ = clone
    output = plugin.synthesize_text.return_value

    async def synthesize(*args, **kwargs):
        _private_policy(root, allowed_features=[])
        return output

    plugin.synthesize_text.side_effect = synthesize
    receipt = asyncio.run(speak(delivery, plugin, event))
    assert receipt["status"] == "blocked"
    assert str(output) not in json.dumps(receipt)
    plugin._send_audio_result.assert_not_awaited()
    plugin._discard_voice_output.assert_awaited_once_with(output)
    event.clear_result.assert_not_called()


@pytest.mark.parametrize("entry", ["auto_tts_reply", "tts_command", "_submit_background_job", "submit_tts_after_text_sent"])
def test_search_turn_skips_all_automatic_explicit_and_deferred_entries(clone, patched_plugins, entry):
    plugin, event, *_ = clone
    event.set_extra("_wechatrebot_web_search_turn", True)
    event.set_extra("pending", {"text": "reply"})
    method = patched_method(patched_plugins, entry, _PENDING_BACKGROUND_JOB_EXTRA="pending")

    async def run():
        if entry == "tts_command":
            assert [value async for value in method(plugin, event)] == []
        elif entry == "_submit_background_job":
            with pytest.raises(PermissionError):
                await method(plugin, event=event, text="reply", source="auto")
        else:
            await method(plugin, event)

    asyncio.run(run())
    plugin.synthesize_text.assert_not_awaited()
    plugin._send_audio_result.assert_not_awaited()
    if entry == "submit_tts_after_text_sent":
        assert event.get_extra("pending") is None


@pytest.mark.parametrize("revoke", [True, False])
def test_auto_voice_keeps_text_if_permission_changes_during_synthesis(clone, patched_plugins, revoke):
    plugin, event, root, *_ = clone
    text, record = object(), object()
    result = SimpleNamespace(chain=[text], is_llm_result=lambda: True, get_plain_text=lambda: "reply")
    event.get_result = lambda: result
    plugin._auto_tts_access_decision = lambda e: {"allowed": True, "reason": "allowed", "scope": "private", "matched_rule": ""}
    plugin._is_plain_component = lambda comp: comp is text
    plugin._audio_component = lambda path: record
    output = plugin.synthesize_text.return_value

    async def synthesize(*args, **kwargs):
        if revoke:
            _private_policy(root, allowed_features=[])
        return output

    plugin.synthesize_text.side_effect = synthesize
    auto = patched_method(patched_plugins, "auto_tts_reply", random=SimpleNamespace(random=lambda: 0),
                          clean_tts_text=lambda text: text, clip_log_text=lambda text: text)
    asyncio.run(auto(plugin, event))
    assert result.chain == ([text] if revoke else [record])
    assert plugin._discard_voice_output.await_count == int(revoke)


@pytest.fixture
def job_type(patched_plugins):
    path = patched_plugins / PLUGIN / "core/tts_jobs.py"
    spec = importlib.util.spec_from_file_location("tested_permission_jobs", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module.TTSJob
    finally:
        sys.modules.pop(spec.name, None)


def test_queue_persists_account_and_authorization_but_old_jobs_are_not_trusted(job_type):
    job = job_type(session="wechat:FriendMessage:friend", text="reply", account_id="wxid_bot", voice_authorized=True)
    assert job_type.from_dict(asdict(job)) == job
    old = job_type.from_dict({"session": job.session, "text": "old"})
    assert old.account_id == ""
    assert old.voice_authorized is False
    assert not job_type.from_dict({**asdict(job), "voice_authorized": "false"}).voice_authorized


@pytest.mark.parametrize("denial", ["old", "account", "chat", "session", "disabled", "none"])
def test_persisted_job_rechecks_identity_and_permissions_before_synthesis(clone, patched_plugins, job_type, denial):
    plugin, event, root, disabled, _, _, platform = clone
    job = job_type(session=event.unified_msg_origin, text="reply", user_id="friend",
                   account_id="wxid_bot", voice_authorized=denial != "old")
    if denial == "account":
        platform.bot.get_login_info = AsyncMock(return_value={"user_id": "another_bot"})
    elif denial == "chat":
        _private_policy(root, allowed_features=[])
    elif denial == "session":
        disabled.add(PLUGIN)
    elif denial == "disabled":
        plugin.plugin_config.auto_tts_enabled = False
    plugin._job_voice_allowed = MethodType(patched_method(patched_plugins, "_job_voice_allowed",
        TTSJob=job_type, session_plugin_allowed=session_plugin_allowed), plugin)
    process = patched_method(patched_plugins, "_process_tts_job", TTSJob=job_type)
    if denial == "none":
        asyncio.run(process(plugin, job))
        plugin.synthesize_text.assert_awaited_once()
    else:
        with pytest.raises(PermissionError):
            asyncio.run(process(plugin, job))
        plugin.synthesize_text.assert_not_awaited()


def test_revoked_queue_job_does_not_deliver_later_segments(clone, patched_plugins, job_type):
    plugin, event, *_ = clone
    job = job_type(session=event.unified_msg_origin, text="reply", account_id="wxid_bot", voice_authorized=True)
    paths = ["first.wav", "second.wav"]
    plugin._job_voice_allowed = AsyncMock(side_effect=[True, False])
    plugin._preflight_session = Mock()
    plugin._delivery_audio_paths = lambda output: paths
    plugin._send_audio_path_to_session = AsyncMock()
    deliver = patched_method(patched_plugins, "_deliver_tts_job", TTSJob=job_type)
    with pytest.raises(PermissionError):
        asyncio.run(deliver(plugin, job, "manifest.json"))
    plugin._send_audio_path_to_session.assert_awaited_once_with(job.session, paths[0], account_id="wxid_bot")
    plugin._discard_voice_output.assert_awaited_once_with("manifest.json")


def test_permission_denial_does_not_send_background_failure_notice(clone, patched_plugins, job_type):
    plugin, event, *_ = clone
    job = job_type(session=event.unified_msg_origin, text="reply", notify_on_failure=True)
    plugin._send_to_session = AsyncMock()
    failure = patched_method(patched_plugins, "_handle_tts_job_failure", TTSJob=job_type,
                             Plain=str, clip_log_text=str)
    asyncio.run(failure(plugin, job, PermissionError("revoked")))
    plugin._send_to_session.assert_not_awaited()


@pytest.mark.parametrize("group,target", [(True, "room@chatroom"), (False, "wxid_friend")])
def test_shared_path_background_preserves_wechat_target_and_account(clone, patched_plugins, group, target):
    plugin, event, *_ = clone
    plugin.plugin_config.audio_transport = "shared_path"
    plugin._transport_source = lambda path: ("C:/speech/voice.wav", False)
    plugin._call_onebot_message = AsyncMock()
    authorized = SimpleNamespace(get_group_id=lambda: target if group else "",
                                 get_sender_id=lambda: target, get_self_id=lambda: "wxid_bot")
    platform = SimpleNamespace(bot=object())
    check = AsyncMock(return_value=(platform, authorized))
    send = patched_method(patched_plugins, "_send_audio_path_to_session", background_event=check)
    asyncio.run(send(plugin, event.unified_msg_origin, "voice.wav", account_id="wxid_bot"))
    plugin._call_onebot_message.assert_awaited_once_with(
        platform.bot, is_group=group, target_id=target, source="C:/speech/voice.wav", self_id="wxid_bot",
    )
    check.assert_awaited_once_with(plugin.context, event.unified_msg_origin, PLUGIN, account_id="wxid_bot")
