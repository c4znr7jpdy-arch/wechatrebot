"""Exercise delivered video transcription methods with isolated providers."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_hot_config_runtime import _runtime_function
from test_plugin_portability import _patched


def _provider(provider_id, modalities):
    return SimpleNamespace(
        meta=lambda: SimpleNamespace(id=provider_id),
        provider_config={"modalities": modalities},
    )


@pytest.fixture(scope="module")
def audio_methods(tmp_path_factory):
    source = _patched(
        tmp_path_factory.mktemp("video-audio-provider"),
        "jiang_video_understanding", "main.py", all_patches=True,
    )
    namespace = {"asyncio": asyncio, "VideoProcessingError": RuntimeError}
    for name in ("_safe_float", "_find_audio_chat_provider", "_transcribe_audio"):
        _runtime_function(source, name, namespace)
    return namespace


@pytest.fixture
def audio(audio_methods):
    stt = SimpleNamespace(get_text=AsyncMock(return_value=" STT 转写 "))
    context = SimpleNamespace(
        get_using_stt_provider=Mock(return_value=stt),
        get_all_providers=Mock(return_value=[
            _provider("text-model", ["text"]),
            _provider("audio-model", ["text", "audio"]),
        ]),
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text=" 音频模型转写 ")),
    )
    plugin = SimpleNamespace(config={}, context=context)
    plugin._find_audio_chat_provider = lambda: audio_methods["_find_audio_chat_provider"](plugin)
    event = SimpleNamespace(unified_msg_origin="wechat:GroupMessage:test-room")
    path = Path("video-audio.wav")
    return SimpleNamespace(
        plugin=plugin, context=context, stt=stt, event=event, path=path,
        transcribe=lambda: asyncio.run(audio_methods["_transcribe_audio"](plugin, event, path)),
    )


@pytest.mark.parametrize("configured", ["audio-model", " \t audio-model \n "])
def test_explicit_audio_model_takes_priority_over_stt(audio, configured):
    audio.plugin.config["audio_provider_id"] = configured

    assert audio.transcribe() == "音频模型转写"
    audio.context.get_using_stt_provider.assert_not_called()
    audio.stt.get_text.assert_not_awaited()
    assert audio.context.llm_generate.await_args.kwargs["chat_provider_id"] == "audio-model"
    assert audio.context.llm_generate.await_args.kwargs["audio_urls"] == [str(audio.path)]


@pytest.mark.parametrize("configured", [None, "", " \t\n "])
def test_blank_audio_model_keeps_stt_first(audio, configured):
    audio.plugin.config["audio_provider_id"] = configured

    assert audio.transcribe() == "STT 转写"
    audio.context.get_using_stt_provider.assert_called_once_with(audio.event.unified_msg_origin)
    audio.stt.get_text.assert_awaited_once_with(str(audio.path))
    audio.context.get_all_providers.assert_not_called()
    audio.context.llm_generate.assert_not_awaited()


def test_without_stt_automatically_selects_audio_chat_model(audio):
    audio.context.get_using_stt_provider.return_value = None

    assert audio.transcribe() == "音频模型转写"
    assert audio.context.llm_generate.await_args.kwargs["chat_provider_id"] == "audio-model"


def test_without_stt_or_audio_model_keeps_empty_transcript(audio):
    audio.context.get_using_stt_provider.return_value = None
    audio.context.get_all_providers.return_value = [_provider("text-model", ["text"])]

    assert audio.transcribe() == ""
    audio.context.llm_generate.assert_not_awaited()


@pytest.mark.parametrize("configured", ["missing-model", "stt-only-model"])
def test_unavailable_explicit_model_does_not_fall_back(audio, configured):
    audio.plugin.config["audio_provider_id"] = configured
    audio.stt.meta = lambda: SimpleNamespace(id="stt-only-model")
    audio.stt.provider_config = {"modalities": ["audio"]}

    with pytest.raises(RuntimeError, match="音频理解聊天模型不可用"):
        audio.transcribe()

    audio.context.get_using_stt_provider.assert_not_called()
    audio.stt.get_text.assert_not_awaited()
    audio.context.llm_generate.assert_not_awaited()


@pytest.mark.parametrize("modalities", [None, [], ["text"], ["image"]])
def test_explicit_model_without_audio_capability_does_not_fall_back(audio, modalities):
    audio.plugin.config["audio_provider_id"] = "selected-model"
    audio.context.get_all_providers.return_value.append(_provider("selected-model", modalities))

    with pytest.raises(RuntimeError, match="未声明音频能力"):
        audio.transcribe()

    audio.context.get_using_stt_provider.assert_not_called()
    audio.stt.get_text.assert_not_awaited()
    audio.context.llm_generate.assert_not_awaited()


@pytest.mark.parametrize("selected", ["stt", "audio"])
def test_transcription_timeout_keeps_existing_error(audio, selected):
    if selected == "audio":
        audio.plugin.config["audio_provider_id"] = "audio-model"
        audio.context.llm_generate.side_effect = TimeoutError
    else:
        audio.stt.get_text.side_effect = TimeoutError

    with pytest.raises(RuntimeError, match="音频转写响应超时"):
        audio.transcribe()

    if selected == "audio":
        audio.stt.get_text.assert_not_awaited()
    else:
        audio.context.llm_generate.assert_not_awaited()


def test_explicit_model_request_failure_does_not_fall_back(audio):
    audio.plugin.config["audio_provider_id"] = "audio-model"
    audio.context.llm_generate.side_effect = ConnectionError("provider unavailable")

    with pytest.raises(ConnectionError, match="provider unavailable"):
        audio.transcribe()

    audio.context.get_using_stt_provider.assert_not_called()
    audio.stt.get_text.assert_not_awaited()
