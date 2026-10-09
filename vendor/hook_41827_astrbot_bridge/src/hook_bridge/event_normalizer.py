from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any
import xml.etree.ElementTree as ET

from .models import NormalizedEvent


IGNORED_PRIVATE_CONTACTS = frozenset({"weixin"})
INBOUND_MESSAGE_TYPES = frozenset({"群聊消息", "私聊消息"})


def _normalized_label(value: Any) -> str:
    """Normalize HOOK callback labels for stable event-name matching."""
    return re.sub(r"[\s_\-:：/\\()（）\[\]【】]+", "", scalar(value)).lower()


def _field(mapping: Mapping[str, Any], *names: str) -> Any:
    expected = {_normalized_label(name) for name in names}
    for key, value in mapping.items():
        if _normalized_label(key) in expected:
            return value
    return None


def _member_scalar(mapping: Mapping[str, Any], *names: str) -> str:
    value = _field(mapping, *names)
    if isinstance(value, Mapping):
        return scalar(
            value.get("String")
            or value.get("string")
            or value.get("value")
            or value.get("Value")
        ).strip()
    return scalar(value).strip()


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_mappings(nested)


def _extract_member_id_from_raw(
    payload: Mapping[str, Any], room_id: str, account_id: str, operator_id: str
) -> str:
    """Recover a member wxid when HOOK leaves ``memberlist.userName`` blank."""
    id_names = (
        "member_wxid",
        "memberWxid",
        "user_wxid",
        "userWxid",
        "target_wxid",
        "targetWxid",
        "user_id",
        "userId",
        "username",
        "userName",
        "user_name",
        "member_id",
        "memberId",
    )
    excluded = {item for item in (room_id, account_id, operator_id) if item}
    for mapping in _walk_mappings(payload):
        for name in id_names:
            candidate = _member_scalar(mapping, name)
            if candidate and candidate not in excluded and not candidate.endswith("@chatroom"):
                return candidate

    raw = json.dumps(payload, ensure_ascii=False, default=str)
    # The documented addChatRoomSceneNewXml uses <link><username>...</username>
    # and some builds emit the same field with underscores/case changes.
    for match in re.finditer(
        r"<(?:user_?name|member_?wxid|user_?wxid|target_?wxid)\b[^>]*>\s*"
        r"(?:<!\[CDATA\[)?\s*([^<\]]+?)\s*(?:\]\]>)?\s*</",
        raw,
        flags=re.IGNORECASE,
    ):
        candidate = match.group(1).strip()
        if candidate and candidate not in excluded and not candidate.endswith("@chatroom"):
            return candidate

    candidates = []
    for candidate in re.findall(r"\bwxid_[A-Za-z0-9_-]+\b", raw):
        if candidate not in excluded and candidate not in candidates:
            candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else ""


def _group_member_callback_kind(payload: Mapping[str, Any], data: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return ``(notice_type, subtype)`` for a group member callback.

    The 4.1.8.27 documentation exposes separate join/leave callback routes but
    does not promise a numeric ``event_type``.  In practice builds have used
    ``event_type``, ``event_desc``, or the longer ``messageType`` label.  Match
    those labels first and retain the legacy 11098/11099/11101 numeric aliases
    for installations that still expose the old callback numbers.
    """
    labels = [
        payload.get("event_type"),
        payload.get("even_type"),
        payload.get("messageType"),
        payload.get("event_desc"),
        payload.get("eventDesc"),
        payload.get("messageDesc"),
        payload.get("even_desc"),
        data.get("event_type"),
        data.get("event_desc"),
    ]
    normalized = [_normalized_label(value) for value in labels if value not in (None, "")]
    joined = "|".join(normalized)

    # Keep the more specific kick wording ahead of the generic leave tokens.
    if any(token in joined for token in ("被踢", "移除群成员", "群成员被删除", "groupmemberkick", "memberkick")):
        return "group_decrease", "kick"
    if any(token in joined for token in ("退群", "离群", "离开群聊", "成员删除", "删除群成员", "群成员退出", "groupmemberleave", "memberleave", "groupmemberdecrease", "memberdecrease")):
        return "group_decrease", "leave"
    if any(token in joined for token in ("进群", "入群", "加入群聊", "成员新增", "新增群成员", "群成员加入", "groupmemberjoin", "memberjoin", "groupmemberincrease", "memberincrease")):
        return "group_increase", "invite"

    numeric_types = {
        integer(value, -1)
        for value in labels
        if value not in (None, "") and str(value).strip().lstrip("+-").isdigit()
    }
    if 11099 in numeric_types:
        return "group_decrease", "leave"
    if 11101 in numeric_types:
        return "group_decrease", "kick"
    if 11098 in numeric_types:
        return "group_increase", "invite"
    return None


def _group_member_mapping(data: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _field(
        data,
        "memberlist",
        "memberList",
        "member_info",
        "memberInfo",
        "member",
    )
    if value in (None, ""):
        nested = _as_mapping(_field(data, "newChatroomData", "new_chatroom_data"))
        if nested is not None:
            value = _field(nested, "chatRoomMember", "chatroomMember", "memberlist")
    mapped = _as_mapping(value)
    if mapped is not None:
        return mapped
    if isinstance(value, list):
        for item in value:
            mapped = _as_mapping(item)
            if mapped is not None:
                return mapped
    if any(
        _field(data, name) not in (None, "")
        for name in ("member_wxid", "user_wxid", "user_id", "target_wxid", "wxid", "username", "userName")
    ):
        return data
    return {}


def scalar(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("String", ""))
    return "" if value is None else str(value)


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_at_user_list(msg_source: str) -> tuple[str, ...]:
    if not msg_source.strip():
        return ()
    try:
        root = ET.fromstring(msg_source)
    except ET.ParseError:
        return ()
    node = root.find(".//atuserlist")
    if node is None or not node.text:
        return ()
    values = re.split(r"[,;\s]+", node.text.strip())
    return tuple(value for value in values if value)


def _xml_name(value: Any) -> str:
    local_name = str(value).rsplit("}", 1)[-1]
    return re.sub(r"[_-]", "", local_name).lower()


def _xml_attribute(node: ET.Element, name: str) -> str:
    expected = _xml_name(name)
    for key, value in node.attrib.items():
        if _xml_name(key) == expected:
            return str(value)
    return ""


def extract_pat_participants(content: str) -> tuple[str, str] | None:
    try:
        root = ET.fromstring(content.strip())
    except ET.ParseError:
        return None

    pat_nodes: list[ET.Element] = []
    for node in root.iter():
        if _xml_name(node.tag) != "sysmsg":
            continue
        if _xml_attribute(node, "type").strip().lower() != "pat":
            continue
        pat_nodes.extend(
            child for child in node.iter() if _xml_name(child.tag) == "pat"
        )
    if len(pat_nodes) != 1:
        return None

    senders: set[str] = set()
    targets: set[str] = set()
    for node in pat_nodes[0].iter():
        value = (node.text or "").strip()
        if not value:
            continue
        name = _xml_name(node.tag)
        if name == "fromusername":
            senders.add(value)
        elif name == "pattedusername":
            targets.add(value)
    if len(targets) != 1 or len(senders) > 1:
        return None
    sender = next(iter(senders), "")
    return sender, next(iter(targets))


def _message_sender(data: Mapping[str, Any], conversation_id: str) -> str:
    if not conversation_id.endswith("@chatroom"):
        return conversation_id
    member = data.get("member_info")
    if isinstance(member, Mapping):
        member_id = scalar(member.get("userName") or member.get("wxid"))
        if member_id:
            return member_id
    content = scalar(data.get("content"))
    match = re.match(r"^([^:\s]{1,128}):\s*(?:\r?\n)?", content)
    return match.group(1) if match else ""


def _strip_sender_prefix(content: str, sender_id: str) -> str:
    if sender_id and content.startswith(f"{sender_id}:"):
        return content[len(sender_id) + 1 :].lstrip("\r\n ")
    return content


def _message_content(data: Mapping[str, Any], sender_id: str) -> str:
    real_content = scalar(data.get("real_content"))
    if real_content:
        return real_content
    return _strip_sender_prefix(scalar(data.get("content")), sender_id)


def normalize_event(payload: Mapping[str, Any]) -> NormalizedEvent:
    embedded = payload.get("data")
    data: Mapping[str, Any] = embedded if isinstance(embedded, Mapping) else payload
    message_type_name = scalar(payload.get("messageType")).strip()
    envelope_value = payload.get(
        "event_type", payload.get("even_type", payload.get("messageType"))
    )
    if envelope_value is None and isinstance(embedded, Mapping):
        envelope_value = payload.get("msgType")
    envelope_type = (
        2004
        if message_type_name in INBOUND_MESSAGE_TYPES
        else integer(envelope_value)
    )
    if envelope_type == 2000 and (
        data.get("fromUserName") is not None
        or data.get("from_user") is not None
    ):
        envelope_type = 2004

    group_callback = _group_member_callback_kind(payload, data)
    if group_callback is not None:
        kind, notice_sub_type = group_callback
        member = _group_member_mapping(data)
        if not member and data is not payload:
            member = _group_member_mapping(payload)
        room_value = _field(
            data,
            "roomid",
            "roomId",
            "room_wxid",
            "room_id",
            "group_id",
            "groupId",
            "chatroomUserName",
            "chatroomId",
            "chatRoomId",
            "room",
        )
        if room_value in (None, ""):
            room_value = _field(
                payload,
                "roomid",
                "roomId",
                "room_wxid",
                "room_id",
                "group_id",
                "groupId",
                "chatroomUserName",
                "chatroomId",
                "chatRoomId",
                "room",
            )
        room_id = scalar(room_value)
        account_id = scalar(
            payload.get("account_wxid")
            or _field(data, "account_wxid", "accountWxid", "toUserName")
        )
        operator_id = _member_scalar(
            member,
            "inviterUserName",
            "inviter_username",
            "inviterWxid",
            "operator_id",
            "operatorId",
            "operator_wxid",
            "operatorWxid",
            "manager_wxid",
            "managerWxid",
        )
        if not operator_id:
            operator_id = _member_scalar(
                data,
                "inviterUserName",
                "inviter_username",
                "inviterWxid",
                "operator_id",
                "operatorId",
                "operator_wxid",
                "operatorWxid",
                "manager_wxid",
                "managerWxid",
            )
        member_id = _member_scalar(
            member,
            "userName",
            "username",
            "user_name",
            "wxid",
            "user_id",
            "userId",
            "member_wxid",
            "memberWxid",
            "member_id",
            "memberId",
        )
        if not member_id:
            member_id = _member_scalar(
                data,
                "userName",
                "username",
                "user_name",
                "wxid",
                "user_id",
                "userId",
                "member_wxid",
                "memberWxid",
                "member_id",
                "memberId",
            )
        if not member_id:
            member_id = _extract_member_id_from_raw(
                payload, room_id, account_id, operator_id
            )
        if kind == "group_increase":
            notice_sub_type = "invite" if operator_id else "approve"
        create_time = integer(
            data.get("createtime")
            or data.get("createTime")
            or payload.get("createtime")
            or payload.get("createTime")
        )
        return NormalizedEvent(
            kind=kind,
            account_id=account_id,
            conversation_id=room_id,
            sender_id=member_id,
            sender_name=_member_scalar(member, "nickName", "nickname", "nick_name", "name"),
            wechat_name=_member_scalar(
                member, "nickName", "nickname", "nick_name", "name"
            ),
            group_nickname=_member_scalar(
                member, "displayName", "display_name", "displayname", "card"
            ),
            message_id=":".join(
                value for value in (kind, room_id, member_id, str(create_time)) if value
            ),
            create_time=create_time,
            group_name=scalar(
                _field(
                    data,
                    "roomname",
                    "roomName",
                    "nickname",
                    "group_name",
                    "groupName",
                )
                or _field(
                    payload,
                    "roomname",
                    "roomName",
                    "nickname",
                    "group_name",
                    "groupName",
                )
            ).strip(),
            member_avatar=_member_scalar(
                member,
                "smallHeadImgUrl",
                "small_head_img_url",
                "bigHeadImgUrl",
                "big_head_img_url",
                "avatar",
                "avatar_url",
            ),
            operator_id=operator_id,
            notice_sub_type=notice_sub_type,
            raw=dict(payload),
        )

    if envelope_type == 1002:
        return NormalizedEvent(
            kind="login",
            account_id=scalar(data.get("wxid") or payload.get("account_wxid")),
            sender_name=scalar(data.get("nickname") or data.get("nickName")),
            raw=dict(payload),
        )
    if envelope_type == 1:
        return NormalizedEvent(kind="outgoing", raw=dict(payload))
    if envelope_type == 7:
        return NormalizedEvent(kind="send_receipt", raw=dict(payload))
    if envelope_type == 1012:
        member_value = data.get("memberlist")
        member = member_value if isinstance(member_value, Mapping) else {}
        return NormalizedEvent(
            kind="group_member_update",
            conversation_id=scalar(data.get("roomid") or data.get("roomId")),
            sender_id=scalar(member.get("userName") or member.get("wxid")),
            wechat_name=scalar(member.get("nickName")).strip(),
            group_nickname=scalar(member.get("displayName")).strip(),
            raw=dict(payload),
        )
    if envelope_type != 2004:
        return NormalizedEvent.unknown(payload, envelope_type)

    conversation_id = scalar(data.get("fromUserName") or data.get("from_user"))
    sender_id = _message_sender(data, conversation_id)
    member = data.get("member_info")
    member_mapping = member if isinstance(member, Mapping) else {}
    sender_name = scalar(
        data.get("sender_nick")
        or member_mapping.get("nickName")
        or member_mapping.get("displayName")
    )
    is_group = conversation_id.endswith("@chatroom")
    profile = data.get("sender_profile")
    profile_mapping = profile if isinstance(profile, Mapping) else {}
    if is_group:
        wechat_name = scalar(
            member_mapping.get("nickName") or data.get("sender_nick")
        ).strip()
        group_nickname = scalar(member_mapping.get("displayName")).strip()
    else:
        wechat_name = scalar(
            profile_mapping.get("nickName") or data.get("sender_nick")
        ).strip()
        group_nickname = ""
    account_id = scalar(payload.get("account_wxid") or data.get("toUserName"))
    message_type = integer(data.get("msgType"))
    content = _message_content(data, sender_id)
    mentions = extract_at_user_list(scalar(data.get("msgSource")))
    kind = (
        "ignored"
        if conversation_id.lower() in IGNORED_PRIVATE_CONTACTS
        else "message"
    )

    if message_type in {10000, 10002}:
        mentions = ()
        pat = extract_pat_participants(content)
        raw_content = _strip_sender_prefix(scalar(data.get("content")), sender_id)
        if pat is None and raw_content != content:
            pat = extract_pat_participants(raw_content)
        if pat is not None and account_id and pat[1] == account_id:
            pat_sender, patted_username = pat
            sender_id = pat_sender or sender_id
            message_type = 1
            content = "[拍一拍]"
            mentions = (
                (account_id,) if conversation_id.endswith("@chatroom") else ()
            )
        else:
            # 10000/10002 是微信系统消息（入群、退群、拍其他人等），不应
            # 作为普通聊天文本转交给 AstrBot。只有结构化确认“拍到机器人”
            # 时，才转换成可唤醒的 [拍一拍] 消息。
            kind = "ignored"
    if account_id and sender_id == account_id:
        kind = "ignored"
    return NormalizedEvent(
        kind=kind,
        account_id=account_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        sender_name=sender_name,
        wechat_name=wechat_name,
        group_nickname=group_nickname,
        message_type=message_type,
        content=content,
        mentions=mentions,
        message_id=scalar(data.get("newMsgId") or data.get("NewMsgId") or data.get("msgId")),
        create_time=integer(data.get("createTime")),
        raw=dict(payload),
    )
