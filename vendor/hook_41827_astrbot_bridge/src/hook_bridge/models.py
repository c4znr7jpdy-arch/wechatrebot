from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    message_id: str | None = None
    create_time: int | None = None
    to_user_name: str | None = None
    error: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VideoUpload:
    cdn_video_url: str = field(repr=False)
    encryption_key: str = field(repr=False)
    url_file_key: str = field(default="", repr=False)
    credential_shapes: tuple[str, ...] = ()
    response_keys: tuple[str, ...] = ()
    data_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GroupMemberIdentity:
    wechat_name: str = ""
    group_nickname: str = ""
    avatar: str = ""


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """WeChat metadata needed to render a native type=57 reply."""

    message_id: str
    create_time: int = 0
    sender_id: str = ""
    sender_name: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    kind: str
    account_id: str = ""
    conversation_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    wechat_name: str = ""
    group_nickname: str = ""
    message_type: int = 0
    content: str = ""
    mentions: tuple[str, ...] = ()
    message_id: str = ""
    create_time: int = 0
    media_path: Path | None = None
    quoted_image: bool = False
    quoted_message_type: int = 0
    quoted_content: str = ""
    quoted_sender_id: str = ""
    quoted_sender_name: str = ""
    quoted_message_id: str = ""
    # Group-member callbacks carry these fields outside the normal message
    # envelope.  Keeping them on the normalized event lets the OneBot encoder
    # emit a native notice without making the rest of the bridge inspect raw
    # HOOK payloads.
    group_name: str = ""
    member_avatar: str = ""
    operator_id: str = ""
    notice_sub_type: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def unknown(cls, raw: Mapping[str, Any], event_type: Any = None) -> "NormalizedEvent":
        return cls(kind="unknown", raw=dict(raw))


@dataclass(frozen=True, slots=True)
class OutboundCommand:
    kind: Literal[
        "text",
        "at_text",
        "image",
        "file",
        "voice",
        "video",
        "emotion",
        "app_xml",
        "link_card",
    ]
    target: str
    text: str = ""
    source: str = ""
    mentions: tuple[str, ...] = ()
    app_type: int = 3
    title: str = ""
    description: str = ""
    url: str = ""
    thumb_url: str = ""


@dataclass(frozen=True, slots=True)
class RecallRecord:
    message_id: str
    create_time: int
    to_user_name: str
