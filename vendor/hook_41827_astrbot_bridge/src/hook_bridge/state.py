from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass, field
import time
from typing import Any, Mapping

from .models import GroupMemberIdentity, RecallRecord, ReplyTarget


@dataclass(slots=True)
class BridgeState:
    dedup_ttl_seconds: float
    dedup_capacity: int
    history_capacity: int = 500
    bot_wxid: str = ""
    bot_nickname: str = ""
    bot_avatar: str = ""
    friends: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    group_members: dict[str, dict[str, GroupMemberIdentity]] = field(
        default_factory=dict
    )
    _dedup: OrderedDict[str, float] = field(default_factory=OrderedDict, init=False)
    _recalls: dict[int, RecallRecord] = field(default_factory=dict, init=False)
    _latest_by_conversation: dict[str, RecallRecord] = field(default_factory=dict, init=False)
    _group_history: dict[str, deque[dict[str, Any]]] = field(
        default_factory=dict, init=False
    )
    _messages: OrderedDict[int, dict[str, Any]] = field(
        default_factory=OrderedDict, init=False
    )
    _latest_message_by_conversation: dict[str, int] = field(
        default_factory=dict, init=False
    )
    _pending_reply_by_conversation: dict[str, ReplyTarget] = field(
        default_factory=dict, init=False
    )
    _reply_target_by_message_id: OrderedDict[str, ReplyTarget] = field(
        default_factory=OrderedDict, init=False
    )
    _reply_target_conversation_by_message_id: dict[str, str] = field(
        default_factory=dict, init=False
    )
    _group_member_snapshots: set[str] = field(default_factory=set, init=False)
    _message_id: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.dedup_ttl_seconds <= 0:
            raise ValueError("dedup_ttl_seconds must be greater than zero")
        if self.dedup_capacity <= 0:
            raise ValueError("dedup_capacity must be greater than zero")
        if self.history_capacity <= 0:
            raise ValueError("history_capacity must be greater than zero")

    def accept_message(self, message_id: str, now: float | None = None) -> bool:
        if not message_id:
            return True
        current = time.monotonic() if now is None else now
        expired_before = current - self.dedup_ttl_seconds
        while self._dedup:
            oldest_id, timestamp = next(iter(self._dedup.items()))
            if timestamp > expired_before:
                break
            self._dedup.pop(oldest_id)
        if message_id in self._dedup:
            return False
        while len(self._dedup) >= self.dedup_capacity:
            self._dedup.popitem(last=False)
        self._dedup[message_id] = current
        return True

    def set_identity(self, wxid: str, nickname: str = "", avatar: str = "") -> bool:
        changed = wxid != self.bot_wxid
        self.bot_wxid = wxid
        self.bot_nickname = nickname
        self.bot_avatar = avatar
        return changed

    def next_message_id(self) -> int:
        self._message_id += 1
        return self._message_id

    def remember_recall(self, onebot_message_id: int, record: RecallRecord) -> None:
        self._recalls[onebot_message_id] = record
        self._latest_by_conversation[record.to_user_name] = record

    def get_recall(self, onebot_message_id: int) -> RecallRecord | None:
        return self._recalls.get(onebot_message_id)

    def latest_recall(self, conversation_id: str) -> RecallRecord | None:
        return self._latest_by_conversation.get(conversation_id)

    def replace_group_member_snapshot(
        self,
        room_id: str,
        members: Mapping[str, GroupMemberIdentity],
    ) -> None:
        self.group_members[room_id] = dict(members)
        self._group_member_snapshots.add(room_id)

    def has_group_member_snapshot(self, room_id: str) -> bool:
        return room_id in self._group_member_snapshots

    def remember_group_member_identity(
        self,
        room_id: str,
        member_wxid: str,
        identity: GroupMemberIdentity,
    ) -> None:
        if not room_id or not member_wxid:
            return
        self.group_members.setdefault(room_id, {})[member_wxid] = identity

    def group_member_identity(
        self, room_id: str, member_wxid: str
    ) -> GroupMemberIdentity | None:
        return self.group_members.get(room_id, {}).get(member_wxid)

    def forget_recall(self, onebot_message_id: int) -> None:
        record = self._recalls.pop(onebot_message_id, None)
        if record and self._latest_by_conversation.get(record.to_user_name) == record:
            self._latest_by_conversation.pop(record.to_user_name, None)

    def remember_group_message(self, event: Mapping[str, Any]) -> None:
        if event.get("message_type") != "group":
            return
        group_id = str(event.get("group_id", ""))
        if not group_id:
            return
        history = self._group_history.setdefault(
            group_id, deque(maxlen=self.history_capacity)
        )
        history.append(deepcopy(dict(event)))

    def remember_message(self, message_id: int, event: Mapping[str, Any]) -> None:
        """Cache a OneBot message so AstrBot can resolve a Reply segment."""
        if message_id <= 0:
            return
        self._messages[message_id] = deepcopy(dict(event))
        self._messages.move_to_end(message_id)
        while len(self._messages) > self.history_capacity:
            self._messages.popitem(last=False)
        conversation_id = str(
            event.get("group_id")
            or event.get("user_id")
            or event.get("sender", {}).get("user_id", "")
        )
        if conversation_id:
            self._latest_message_by_conversation[conversation_id] = message_id

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        event = self._messages.get(message_id)
        return deepcopy(event) if event is not None else None

    def latest_message_id(self, conversation_id: str) -> int | None:
        return self._latest_message_by_conversation.get(str(conversation_id))

    def remember_reply_target(
        self,
        conversation_id: str,
        target: ReplyTarget,
        message_id: int | str | None = None,
    ) -> None:
        key = str(conversation_id)
        if key:
            self._pending_reply_by_conversation[key] = target
        if message_id in (None, ""):
            return
        message_key = str(message_id)
        self._reply_target_by_message_id[message_key] = target
        self._reply_target_by_message_id.move_to_end(message_key)
        self._reply_target_conversation_by_message_id[message_key] = key
        while len(self._reply_target_by_message_id) > self.history_capacity:
            oldest_key, _ = self._reply_target_by_message_id.popitem(last=False)
            self._reply_target_conversation_by_message_id.pop(oldest_key, None)

    def take_reply_target_by_message_id(
        self, message_id: int | str
    ) -> ReplyTarget | None:
        message_key = str(message_id)
        target = self._reply_target_by_message_id.pop(message_key, None)
        conversation_id = self._reply_target_conversation_by_message_id.pop(
            message_key, None
        )
        if target is None:
            return None
        if conversation_id and self._pending_reply_by_conversation.get(conversation_id) == target:
            self._pending_reply_by_conversation.pop(conversation_id, None)
        return target

    def take_reply_target(self, conversation_id: str) -> ReplyTarget | None:
        return self._pending_reply_by_conversation.pop(str(conversation_id), None)

    def group_history(self, group_id: str, count: int = 100) -> list[dict[str, Any]]:
        items = list(self._group_history.get(group_id, ()))
        if count > 0:
            items = items[-count:]
        return deepcopy(items)
