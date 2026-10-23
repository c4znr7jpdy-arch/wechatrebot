"""Exercise assembled emoji voice hooks against the shared live permission policy."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_group_feature_runtime import permission_runtime  # noqa: F401
from test_plugin_portability import _patched
from wechatrebot_manager import group_feature_runtime, reply_policy_runtime


MODULES = ("jiang_mimo_emoji_voice", "astrbot_plugin_mimo_tts_clone", "meme_manager")


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    pass


class Record:
    @classmethod
    def fromFileSystem(cls, path):
        return cls()


def save_policy(root, modules):
    (root / "data/manager/group-policies.json").write_text(json.dumps({
        "schema_version": 1,
        "accounts": {"wxid_bot": {"groups": {"room@chatroom": {
            "allowed_features": list(modules), "bot_enabled": True,
        }}}},
    }), encoding="utf-8")


@pytest.fixture
def voice_runtime(permission_runtime, tmp_path, monkeypatch):
    root, disabled, stars, event, config, _, context = permission_runtime
    stars[:] = [SimpleNamespace(root_dir_name=name, name=name, activated=True,
                                reserved=False) for name in MODULES]
    event.unified_msg_origin = "wechat:GroupMessage:room@chatroom"
    event.get_group_id = lambda: "room@chatroom"
    extras = {"meme_manager_pending_images": [Image()]}
    event.get_extra = lambda key, default=None: extras.get(key, default)
    event.set_extra = extras.__setitem__
    result = SimpleNamespace(chain=[Plain("这是一条需要保留的文字回复")], is_llm_result=lambda: True)
    event.get_result = lambda: result
    save_policy(root, MODULES)

    class Star:
        def __init__(self, context):
            pass

    decorators = SimpleNamespace(on_decorating_result=lambda **kwargs: lambda fn: fn,
                                 after_message_sent=lambda **kwargs: lambda fn: fn)
    for name, module in {
        "astrbot.api": SimpleNamespace(AstrBotConfig=dict, logger=logging.getLogger(__name__)),
        "astrbot.api.event": SimpleNamespace(AstrMessageEvent=object, filter=decorators),
        "astrbot.api.message_components": SimpleNamespace(Plain=Plain, Image=Image, Record=Record),
        "astrbot.api.star": SimpleNamespace(Context=object, Star=Star),
        "astrbot.core.platform.wechat_group_policy": group_feature_runtime,
        "astrbot.core.wechat_reply_policy": reply_policy_runtime,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    plugin_dir = tmp_path / "emoji"
    plugin_dir.mkdir()
    _patched(plugin_dir, MODULES[0], "main.py", all_patches=True)
    path = plugin_dir / "main.py"
    spec = importlib.util.spec_from_file_location("tested_emoji_voice", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter = module.JiangMimoEmojiVoice(context)
    wav = root / "mimo_tts_test.wav"
    wav.write_bytes(b"clone-owned-output")
    clone = SimpleNamespace(synthesize_text=AsyncMock(return_value=wav))
    stars[1].star_cls = clone
    context.get_registered_star = lambda name: next((star for star in stars if star.name == name), None)
    silk = root / "jiang_mimo_emoji_voice_test.silk"

    async def transcode(path):
        silk.write_bytes(b"temporary-voice")
        return silk

    adapter._wav_to_silk = AsyncMock(side_effect=transcode)
    return SimpleNamespace(root=root, disabled=disabled, stars=stars, config=config,
                           event=event, extras=extras, result=result, adapter=adapter,
                           clone=clone, silk=silk, wav=wav, transcode=transcode)


def revoke(runtime, module, scope):
    if scope == "chat":
        save_policy(runtime.root, set(MODULES) - {module})
    elif scope == "global":
        runtime.config["plugin_set"] = list(set(MODULES) - {module})
    elif scope == "session":
        runtime.disabled.add(module)
    elif scope == "inactive":
        next(star for star in runtime.stars if star.name == module).activated = False
    elif scope == "event":
        runtime.event.plugins_name = list(set(MODULES) - {module})


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("scope", ["chat", "global", "session", "inactive", "event"])
def test_dependency_denial_preserves_text_without_synthesis(voice_runtime, module, scope):
    runtime = voice_runtime
    original = runtime.result.chain
    revoke(runtime, module, scope)
    asyncio.run(runtime.adapter.replace_text_with_voice(runtime.event))
    assert runtime.result.chain is original
    runtime.clone.synthesize_text.assert_not_awaited()
    runtime.adapter._wav_to_silk.assert_not_awaited()
    assert not runtime.extras.get("jiang_mimo_emoji_voice_done")


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("scope", ["chat", "global", "session", "inactive", "event"])
def test_revocation_during_synthesis_discards_temp_voice_and_keeps_text(voice_runtime, module, scope):
    runtime = voice_runtime
    original = runtime.result.chain

    async def synthesize(*args, **kwargs):
        revoke(runtime, module, scope)
        return runtime.wav

    runtime.clone.synthesize_text.side_effect = synthesize
    asyncio.run(runtime.adapter.replace_text_with_voice(runtime.event))
    runtime.clone.synthesize_text.assert_awaited_once()
    assert runtime.result.chain is original
    assert not runtime.silk.exists()
    assert runtime.wav.exists()  # Clone-owned outputs remain under its own retention policy.
    assert not runtime.extras.get("jiang_mimo_emoji_voice_done")


def test_search_turn_skips_voice_but_next_turn_can_speak(voice_runtime):
    runtime = voice_runtime
    original = runtime.result.chain
    reply_policy_runtime.mark_web_search_turn(runtime.event, ["web_search"], ["web_search"])
    asyncio.run(runtime.adapter.replace_text_with_voice(runtime.event))
    assert runtime.result.chain is original
    runtime.clone.synthesize_text.assert_not_awaited()

    next_event = SimpleNamespace(**vars(runtime.event))
    next_extras = {"meme_manager_pending_images": [Image()]}
    next_event.get_extra = lambda key, default=None: next_extras.get(key, default)
    next_event.set_extra = next_extras.__setitem__
    asyncio.run(runtime.adapter.replace_text_with_voice(next_event))
    assert isinstance(runtime.result.chain[0], Record)
    assert next_extras["jiang_mimo_emoji_voice_done"] is True
    assert runtime.silk.exists()
    asyncio.run(runtime.adapter.cleanup_transcoded_audio(next_event))
    assert not runtime.silk.exists()
    assert next_extras["jiang_mimo_emoji_voice_audio"] is None


def test_search_marked_during_conversion_discards_audio_and_preserves_text(voice_runtime):
    runtime = voice_runtime
    original = runtime.result.chain

    async def transcode(path):
        output = await runtime.transcode(path)
        reply_policy_runtime.mark_web_search_turn(runtime.event, ["web_search"], ["web_search"])
        return output

    runtime.adapter._wav_to_silk.side_effect = transcode
    asyncio.run(runtime.adapter.replace_text_with_voice(runtime.event))
    assert runtime.result.chain is original
    assert not runtime.silk.exists()
    assert not runtime.extras.get("jiang_mimo_emoji_voice_done")


def test_synthesis_failure_keeps_original_text(voice_runtime):
    runtime = voice_runtime
    original = runtime.result.chain
    runtime.clone.synthesize_text.side_effect = RuntimeError("upstream unavailable")
    asyncio.run(runtime.adapter.replace_text_with_voice(runtime.event))
    assert runtime.result.chain is original
    runtime.adapter._wav_to_silk.assert_not_awaited()


def test_cleanup_failure_is_logged_without_replacing_text(voice_runtime, monkeypatch, caplog):
    runtime = voice_runtime
    original = runtime.result.chain

    async def transcode(path):
        output = await runtime.transcode(path)
        revoke(runtime, MODULES[1], "chat")
        return output

    def cannot_remove(path, **kwargs):
        raise OSError("in use")

    runtime.adapter._wav_to_silk.side_effect = transcode
    monkeypatch.setattr(type(runtime.silk), "unlink", cannot_remove)
    asyncio.run(runtime.adapter.replace_text_with_voice(runtime.event))
    assert runtime.result.chain is original
    assert "临时语音清理失败" in caplog.text
