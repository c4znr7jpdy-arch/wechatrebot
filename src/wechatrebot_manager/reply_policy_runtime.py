"""Per-message reply policy, copied into the pinned AstrBot engine patch."""
from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


WEB_SEARCH_TURN_EXTRA = "_wechatrebot_web_search_turn"
_AUDIO_EXTENSIONS = frozenset({
    ".aac", ".aiff", ".alac", ".amr", ".flac", ".m4a", ".mp3", ".ogg",
    ".opus", ".pcm", ".silk", ".slik", ".wav", ".wma",
})


def mark_web_search_turn(
    event: Any,
    called_tools: Iterable[str],
    search_tools: Iterable[str],
) -> None:
    """Keep search replies textual throughout this message and its subagents.

    Callers pass only tools selected for execution, never the registered tool
    catalog or conversation history. Mark the whole batch before executing its
    first tool so a voice tool listed before search cannot race this decision.
    A later non-search batch must not clear an earlier search in the same turn.
    """
    if set(called_tools).intersection(search_tools):
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter(WEB_SEARCH_TURN_EXTRA, True)


def voice_reply_allowed(event: Any) -> bool:
    """Search suppression belongs to the incoming event, never to a chat."""
    getter = getattr(event, "get_extra", None)
    return not (callable(getter) and getter(WEB_SEARCH_TURN_EXTRA))


def is_web_search_turn(event: Any) -> bool:
    """Return whether this exact incoming event used a web-search tool."""
    return not voice_reply_allowed(event)


def is_voice_message_component(message: Any) -> bool:
    """Recognize voice bubbles and audio-file fallbacks before media I/O."""
    if not isinstance(message, dict):
        return False
    kind = str(message.get("type", "")).lower()
    if kind == "record":
        return True
    if kind != "file":
        return False
    for field in ("path", "url", "text"):
        value = str(message.get(field) or "").replace("\\", "/")
        try:
            value = urlsplit(value).path
        except ValueError:
            # Invalid URLs still pass through the normal send-tool validation.
            pass
        filename = unquote(value).rsplit("/", 1)[-1].lower()
        if any(filename.endswith(extension) for extension in _AUDIO_EXTENSIONS):
            return True
    return False
