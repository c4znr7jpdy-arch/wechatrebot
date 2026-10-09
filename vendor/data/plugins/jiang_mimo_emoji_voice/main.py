from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star


_MEME_PATH_RE = re.compile(r"(?:^|/)meme_manager/(?:packs/[^/]+/)?memes/", re.I)
_MARKUP_RE = re.compile(r"&&[^&]+&&")
_URL_RE = re.compile(r"https?://\S+", re.I)

_EMOTION_MAP = {
    "happy": "happy",
    "like": "happy",
    "morning": "happy",
    "color": "happy",
    "givemoney": "excited",
    "excited": "excited",
    "playful": "happy",
    "baka": "playful",
    "fool": "playful",
    "meow": "playful",
    "sad": "sad",
    "sigh": "sad",
    "angry": "angry",
    "confused": "surprised",
    "surprised": "surprised",
    "shy": "tender",
    "sleep": "lazy",
    "work": "serious",
    "see": "calm",
}


def _components(chain: Any) -> list[Any]:
    if isinstance(chain, list):
        return list(chain)
    value = getattr(chain, "chain", None)
    if isinstance(value, list):
        return list(value)
    if isinstance(chain, str):
        return [Plain(chain)]
    return []


def _component_source(component: Any) -> str:
    return " ".join(
        str(getattr(component, name, "") or "")
        for name in ("path", "file", "url")
    )


def is_meme_image(component: Any) -> bool:
    """识别 meme_manager 生成的表情，不把普通图片当成语音触发器。"""
    if not isinstance(component, Image):
        return False
    source = _component_source(component).replace("\\", "/")
    return bool(_MEME_PATH_RE.search(source))


def has_meme_trigger(event: Any, chain: Any) -> bool:
    pending = event.get_extra("meme_manager_pending_images")
    if isinstance(pending, (list, tuple, set)) and bool(pending):
        return True
    components = _components(chain)
    if any(is_meme_image(component) for component in components):
        return True
    # send_image_as_base64=true 时，表情图片没有可识别的本地路径；
    # 由更高优先级钩子保存 found_emotions，再要求当前链确实出现 Image。
    candidates = event.get_extra("jiang_mimo_emoji_voice_candidate_emotions")
    return bool(candidates) and any(isinstance(component, Image) for component in components)


def extract_speech_text(chain: Any) -> str:
    text = "".join(
        component.text
        for component in _components(chain)
        if isinstance(component, Plain) and isinstance(component.text, str)
    )
    text = _MARKUP_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_emotion(value: str) -> str:
    candidate = str(value or "").strip().lower()
    return _EMOTION_MAP.get(candidate, "neutral")


def infer_emotion(chain: Any, event: Any) -> str:
    candidates_from_manager = event.get_extra(
        "jiang_mimo_emoji_voice_candidate_emotions"
    )
    if isinstance(candidates_from_manager, (list, tuple)):
        for candidate in candidates_from_manager:
            mapped = _EMOTION_MAP.get(str(candidate or "").strip().lower())
            if mapped:
                return mapped

    pending = event.get_extra("meme_manager_pending_images")
    candidates: Iterable[Any]
    if isinstance(pending, (list, tuple, set)):
        candidates = list(pending) + _components(chain)
    else:
        candidates = _components(chain)

    for component in candidates:
        source = _component_source(component).replace("\\", "/").lower()
        for token, emotion in _EMOTION_MAP.items():
            if f"/{token}/" in source or source.endswith(f"/{token}"):
                return emotion

    return "neutral"


class JiangMimoEmojiVoice(Star):
    """在 meme_manager 完成表情选择后，把 Plain 替换为 MiMo Record。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        raw = dict(config or {})
        self.enabled = bool(raw.get("enabled", True))
        self.max_text_chars = max(1, int(raw.get("max_text_chars", 120) or 120))
        self.min_text_chars = max(0, int(raw.get("min_text_chars", 1) or 1))
        self.use_style_director = bool(raw.get("use_style_director", False))
        self.ffmpeg_path = str(raw.get("ffmpeg_path", "") or "").strip()
        self.keep_meme = bool(raw.get("keep_meme", True))

    def _mimo_plugin(self) -> Any | None:
        metadata = self.context.get_registered_star("astrbot_plugin_mimo_tts_clone")
        plugin = getattr(metadata, "star_cls", None) if metadata else None
        if plugin is None:
            logger.warning(
                "[jiang_mimo_emoji_voice] MiMo TTS Clone 未加载，保留原文字"
            )
        return plugin

    @filter.on_decorating_result(priority=100000)
    async def capture_meme_candidate(self, event: AstrMessageEvent):
        """在 meme_manager 清空 found_emotions 前保存候选，兼容 base64 图片。"""
        emotions = event.get_extra("found_emotions")
        if isinstance(emotions, (list, tuple)) and emotions:
            event.set_extra(
                "jiang_mimo_emoji_voice_candidate_emotions",
                [str(item) for item in emotions if str(item).strip()],
            )

    def _ffmpeg(self) -> str | None:
        return self.ffmpeg_path or shutil.which("ffmpeg")

    async def _wav_to_silk(self, wav_path: Path) -> Path:
        """Convert MiMo's WAV output to Tencent SILK for a WeChat voice bubble.

        The legacy HOOK accepts a SILK/SLIK payload for its voice endpoint;
        passing WAV/MP3 through that endpoint can appear as a regular file card.
        Keep the conversion in this adapter so the upstream MiMo plugin remains
        unchanged and both old and new HOOKs receive the same portable format.
        """
        ffmpeg = self._ffmpeg()
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法将 MiMo WAV 转为 SILK")

        pcm_handle = tempfile.NamedTemporaryFile(
            prefix="jiang_mimo_emoji_voice_",
            suffix=".pcm",
            delete=False,
        )
        pcm_path = Path(pcm_handle.name)
        pcm_handle.close()
        silk_handle = tempfile.NamedTemporaryFile(
            prefix="jiang_mimo_emoji_voice_",
            suffix=".silk",
            delete=False,
        )
        silk_path = Path(silk_handle.name)
        silk_handle.close()
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "24000",
            str(pcm_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not pcm_path.is_file() or pcm_path.stat().st_size <= 0:
            pcm_path.unlink(missing_ok=True)
            silk_path.unlink(missing_ok=True)
            detail = stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise RuntimeError(f"ffmpeg WAV→PCM 转码失败{(': ' + detail) if detail else ''}")

        try:
            import pilk

            duration_ms = await asyncio.to_thread(
                pilk.encode,
                str(pcm_path),
                str(silk_path),
                pcm_rate=24000,
                tencent=True,
            )
        except Exception as exc:
            silk_path.unlink(missing_ok=True)
            raise RuntimeError(f"PCM→SILK 转码失败: {exc}") from exc
        finally:
            pcm_path.unlink(missing_ok=True)

        if not silk_path.is_file() or silk_path.stat().st_size <= 0:
            silk_path.unlink(missing_ok=True)
            raise RuntimeError("PCM→SILK 转码未生成有效文件")
        logger.debug(
            "[jiang_mimo_emoji_voice] WAV→SILK 完成 duration_ms=%s file=%s",
            duration_ms,
            silk_path.name,
        )
        return silk_path

    async def _synthesize(self, plugin: Any, event: AstrMessageEvent, text: str, emotion: str) -> Path:
        sender_id = str(event.get_sender_id() or "")
        group_id = str(getattr(event, "unified_msg_origin", "") or "")
        output = await plugin.synthesize_text(
            text,
            emotion=emotion,
            context="",
            user_id=sender_id,
            group_id=group_id,
            split=False,
            style_director_enabled=self.use_style_director,
        )
        wav_path = Path(output)
        if wav_path.suffix.lower() in {".mp3", ".silk", ".slik"}:
            return wav_path
        return await self._wav_to_silk(wav_path)

    @filter.on_decorating_result(priority=99998)
    async def replace_text_with_voice(self, event: AstrMessageEvent):
        if not self.enabled:
            return

        result = event.get_result()
        if result is None or not result.chain:
            return
        if hasattr(result, "is_llm_result") and callable(result.is_llm_result):
            if not result.is_llm_result():
                return
        if event.get_extra("jiang_mimo_emoji_voice_done"):
            return
        if not has_meme_trigger(event, result.chain):
            return

        text = extract_speech_text(result.chain)
        if not (self.min_text_chars <= len(text) <= self.max_text_chars):
            logger.info(
                "[jiang_mimo_emoji_voice] 跳过表情语音：文本长度=%d，上限=%d",
                len(text),
                self.max_text_chars,
            )
            return

        plugin = self._mimo_plugin()
        if plugin is None or not callable(getattr(plugin, "synthesize_text", None)):
            return

        emotion = infer_emotion(result.chain, event)
        try:
            audio_path = await self._synthesize(plugin, event, text, emotion)
        except Exception as exc:
            logger.warning(
                "[jiang_mimo_emoji_voice] 合成失败，回退原文字：%s",
                exc,
            )
            return

        audio_component = Record.fromFileSystem(str(audio_path))
        retained = [
            component
            for component in _components(result.chain)
            if not isinstance(component, Plain)
        ]
        # 表情包由 meme_manager 的 pending_images/图片组件继续发送；这里只替换 Plain。
        result.chain = [audio_component, *retained]
        event.set_extra("jiang_mimo_emoji_voice_done", True)
        event.set_extra("jiang_mimo_emoji_voice_audio", str(audio_path))
        logger.info(
            "[jiang_mimo_emoji_voice] 表情触发语音：emotion=%s chars=%d audio=%s",
            emotion,
            len(text),
            audio_path.name,
        )

    @filter.after_message_sent()
    async def cleanup_transcoded_audio(self, event: AstrMessageEvent):
        path_text = event.get_extra("jiang_mimo_emoji_voice_audio")
        if not path_text:
            return
        path = Path(str(path_text))
        # MiMo 插件自己的 WAV 由它管理；这里只清理本适配层生成的临时 SILK。
        if path.suffix.lower() in {".silk", ".slik"} and path.name.startswith("jiang_mimo_emoji_voice_"):
            try:
                path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning(
                    "[jiang_mimo_emoji_voice] 临时 MP3 清理失败 path=%s error=%s",
                    path,
                    exc,
                )
        event.set_extra("jiang_mimo_emoji_voice_audio", None)
