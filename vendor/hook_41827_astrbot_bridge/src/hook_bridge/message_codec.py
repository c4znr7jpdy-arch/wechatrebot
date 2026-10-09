from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import time
from typing import Any

from .models import NormalizedEvent, OutboundCommand


class UnsupportedSegmentError(ValueError):
    """Raised when the HOOK has no safe equivalent for a OneBot segment."""


def _flush_text(
    commands: list[OutboundCommand],
    target: str,
    text_parts: list[str],
    mentions: list[str],
) -> None:
    text = "".join(text_parts)
    if not text and not mentions:
        return
    commands.append(
        OutboundCommand(
            kind="at_text" if mentions else "text",
            target=target,
            text=text,
            mentions=tuple(mentions),
        )
    )
    text_parts.clear()
    mentions.clear()


def decode_outbound(
    message: str | Sequence[Mapping[str, Any]], target: str, is_group: bool
) -> list[OutboundCommand]:
    if isinstance(message, str):
        return [OutboundCommand(kind="text", target=target, text=message)]
    if not isinstance(message, Sequence):
        raise UnsupportedSegmentError("message must be text or a segment list")

    commands: list[OutboundCommand] = []
    text_parts: list[str] = []
    mentions: list[str] = []
    for segment in message:
        if not isinstance(segment, Mapping):
            raise UnsupportedSegmentError("segment must be an object")
        segment_type = str(segment.get("type", ""))
        raw_data = segment.get("data")
        data = raw_data if isinstance(raw_data, Mapping) else {}
        if segment_type == "text":
            text_parts.append(str(data.get("text", "")))
            continue
        if segment_type in {"at", "mention"}:
            if not is_group:
                raise UnsupportedSegmentError("at is only supported for group messages")
            wxid = str(data.get("qq", data.get("user_id", "")))
            if wxid:
                mentions.append("notify@all" if wxid == "all" else wxid)
            continue
        if segment_type == "reply":
            continue

        _flush_text(commands, target, text_parts, mentions)
        source = str(data.get("file", data.get("file_id", data.get("url", ""))))
        if segment_type == "image":
            subtype = str(
                data.get("sub_type", data.get("subtype", segment.get("sub_type", "")))
            ).lower()
            kind = "emotion" if subtype in {"emoji", "meme", "2"} else "image"
            commands.append(OutboundCommand(kind=kind, target=target, source=source))
        elif segment_type in {"record", "voice"}:
            commands.append(OutboundCommand(kind="voice", target=target, source=source))
        elif segment_type == "file":
            commands.append(OutboundCommand(kind="file", target=target, source=source))
        elif segment_type in {"xml", "app_xml"}:
            content = str(data.get("data", data.get("content", "")))
            commands.append(
                OutboundCommand(
                    kind="app_xml",
                    target=target,
                    text=content,
                    app_type=int(data.get("type", 3)),
                )
            )
        elif segment_type in {"wechat_link_card", "share"}:
            description = data.get("desc", "")
            thumb_url = data.get("image_url", "")
            if segment_type == "share":
                description = data.get("content") or description
                thumb_url = data.get("image") or thumb_url
            commands.append(
                OutboundCommand(
                    kind="link_card",
                    target=target,
                    title=str(data.get("title", "")),
                    description=str(description),
                    url=str(data.get("url", "")),
                    thumb_url=str(thumb_url),
                )
            )
        elif segment_type == "video":
            commands.append(
                OutboundCommand(kind="video", target=target, source=source)
            )
        else:
            raise UnsupportedSegmentError(f"unsupported segment: {segment_type or '<empty>'}")

    _flush_text(commands, target, text_parts, mentions)
    return commands


def _inbound_segments(
    event: NormalizedEvent, quoted_reply_id: int | None = None
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = [
        {"type": "at", "data": {"qq": wxid, "name": ""}}
        for wxid in event.mentions
    ]
    if event.quoted_message_type in {1, 3} or event.quoted_image:
        if quoted_reply_id is not None:
            segments.append(
                {"type": "reply", "data": {"id": str(quoted_reply_id)}}
            )
            if event.quoted_message_type == 3 and not event.media_path:
                failure = "[引用图片下载失败，无法识图，请勿猜测图片内容]"
                text = f"{failure}\n{event.content}" if event.content else failure
                segments.append({"type": "text", "data": {"text": text}})
            elif event.content:
                segments.append({"type": "text", "data": {"text": event.content}})
        elif event.quoted_image:
            # Backward-compatible encoding for callers that do not provide a
            # cached OneBot reply id (primarily older unit/integration users).
            if event.media_path:
                segments.append(
                    {"type": "image", "data": {"file": event.media_path.resolve().as_uri()}}
                )
                if event.content:
                    segments.append({"type": "text", "data": {"text": event.content}})
            else:
                failure = "[引用图片下载失败，无法识图，请勿猜测图片内容]"
                text = f"{failure}\n{event.content}" if event.content else failure
                segments.append({"type": "text", "data": {"text": text}})
        elif event.content:
            segments.append({"type": "text", "data": {"text": event.content}})
    elif event.message_type == 1:
        segments.append({"type": "text", "data": {"text": event.content}})
    elif event.message_type == 3 and event.media_path:
        segments.append(
            {"type": "image", "data": {"file": event.media_path.resolve().as_uri()}}
        )
    elif event.message_type == 34 and event.media_path:
        segments.append(
            {"type": "record", "data": {"file": event.media_path.resolve().as_uri()}}
        )
    elif event.message_type == 43:
        if event.media_path:
            segments.append(
                {"type": "video", "data": {"file": str(event.media_path.resolve())}}
            )
        else:
            segments.append({"type": "text", "data": {"text": "[视频]"}})
    elif event.message_type == 47 and event.media_path:
        segments.append(
            {
                "type": "image",
                "data": {
                    "file": event.media_path.resolve().as_uri(),
                    "sub_type": "emoji",
                },
            }
        )
    elif event.message_type == 49 and event.media_path:
        segments.append(
            {"type": "file", "data": {"file": event.media_path.resolve().as_uri()}}
        )
    elif event.message_type == 3:
        segments.append(
            {
                "type": "text",
                "data": {"text": "[图片下载失败，无法识图，请勿猜测图片内容]"},
            }
        )
    elif event.message_type == 34:
        segments.append({"type": "text", "data": {"text": "[语音]"}})
    elif event.message_type == 47:
        segments.append({"type": "text", "data": {"text": "[表情]"}})
    elif event.message_type == 49:
        segments.append({"type": "text", "data": {"text": "[文件或应用消息]"}})
    elif event.content:
        segments.append({"type": "text", "data": {"text": event.content}})
    else:
        segments.append(
            {
                "type": "text",
                "data": {"text": f"[不支持的消息类型:{event.message_type}]"},
            }
        )
    return segments


def encode_inbound(
    event: NormalizedEvent,
    self_id: str,
    message_id: int,
    quoted_reply_id: int | None = None,
) -> dict[str, Any]:
    if event.kind != "message":
        raise ValueError("only normalized message events can be encoded")
    is_group = event.conversation_id.endswith("@chatroom")
    segments = _inbound_segments(event, quoted_reply_id)
    raw_message = event.content
    if not raw_message:
        text_segments = [
            str(segment["data"].get("text", ""))
            for segment in segments
            if segment["type"] == "text"
        ]
        raw_message = "".join(text_segments)
    wechat_name = event.wechat_name.strip()
    group_nickname = event.group_nickname.strip()
    visible_name = (group_nickname or wechat_name) if is_group else wechat_name
    result: dict[str, Any] = {
        "time": event.create_time or int(time.time()),
        "self_id": self_id,
        "post_type": "message",
        "message_type": "group" if is_group else "private",
        "sub_type": "normal" if is_group else "friend",
        "message_id": message_id,
        "user_id": event.sender_id,
        "message": segments,
        "raw_message": raw_message,
        "font": 0,
        "sender": {
            "user_id": event.sender_id,
            "nickname": wechat_name,
            "display_name": visible_name,
            "card": visible_name if is_group else "",
            "sex": "unknown",
            "age": 0,
        },
        "wx_nickname": wechat_name,
        "wx_display_name": group_nickname,
        "wx_group_name": event.group_name.strip(),
    }
    if is_group:
        result["group_id"] = event.conversation_id
    return result


def encode_group_member_notice(
    event: NormalizedEvent, self_id: str
) -> dict[str, Any]:
    """Encode a normalized HOOK member callback as a OneBot V11 notice."""
    if event.kind not in {"group_increase", "group_decrease"}:
        raise ValueError("event is not a group member notice")
    operator_id = event.operator_id or event.sender_id
    subtype = event.notice_sub_type
    if event.kind == "group_increase":
        subtype = subtype or ("invite" if event.operator_id else "approve")
    else:
        subtype = subtype or ("kick" if event.operator_id and event.operator_id != event.sender_id else "leave")
    return {
        "time": event.create_time or int(time.time()),
        "self_id": self_id or event.account_id or "wechat_bot",
        "post_type": "notice",
        "notice_type": event.kind,
        "sub_type": subtype,
        "group_id": event.conversation_id,
        "user_id": event.sender_id,
        "operator_id": operator_id,
        "wx_nickname": event.wechat_name,
        "wx_display_name": event.group_nickname,
        "wx_group_name": event.group_name,
        "wx_avatar": event.member_avatar,
    }
