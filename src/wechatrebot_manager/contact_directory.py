"""Account-scoped, memory-only private contact profiles from WeChat."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from urllib.parse import quote

from .group_directory import GroupDirectoryError, GroupDirectoryService


class ContactDirectoryService(GroupDirectoryService):
    _BATCH_SIZE = 40
    _RETRY_SECONDS = 30.0

    def __init__(self, install_root: str | Path):
        super().__init__(install_root)
        self._contact_account = ""
        self._contacts: dict[str, dict[str, object]] = {}
        self._directory_snapshot: dict[str, object] | None = None
        self._loaded_contacts: set[str] = set()
        self._contact_retry_at: dict[str, float] = {}

    def _reset_contact_state(self, account: str) -> None:
        self._contact_account = account
        self._contacts = {}
        self._directory_snapshot = None
        self._loaded_contacts = set()
        self._contact_retry_at = {}

    @staticmethod
    def _contact_message(message: str) -> str:
        return (
            message.replace("群目录", "联系人目录")
            .replace("群列表", "联系人列表")
            .replace("群信息", "联系人信息")
        )

    def _connection(self):
        try:
            return super()._connection()
        except GroupDirectoryError as exc:
            raise GroupDirectoryError(self._contact_message(str(exc))) from None

    def _request(self, port, token, account, path, *, state_locked: bool = False):
        try:
            return super()._request(
                port,
                token,
                account,
                path,
                state_locked=state_locked,
            )
        except GroupDirectoryError as exc:
            raise GroupDirectoryError(self._contact_message(str(exc))) from None

    @staticmethod
    def _public_contact(item: object) -> dict[str, object] | None:
        if not isinstance(item, dict):
            return None
        user_id = str(item.get("user_id") or "").strip()
        if not ContactDirectoryService._contact_ids([user_id]):
            return None
        text_fields = (
            "name",
            "nickname",
            "remark",
            "alias",
            "avatar",
            "gender",
            "province",
            "city",
            "country",
            "signature",
            "contact_type",
        )
        result: dict[str, object] = {"user_id": user_id}
        for field in text_fields:
            result[field] = str(item.get(field) or "").strip()
        gender_code = item.get("gender_code")
        result["gender_code"] = (
            gender_code
            if isinstance(gender_code, int) and not isinstance(gender_code, bool)
            else None
        )
        return result

    def list(self, refresh: bool = False) -> dict[str, object]:
        """Return the active account's private-contact directory in memory only."""
        with self._lock:
            try:
                port, token, account = self._connection()
                if account != self._contact_account:
                    self._reset_contact_state(account)
                if refresh or self._directory_snapshot is None:
                    suffix = "&refresh=1" if refresh else ""
                    data = self._request(
                        port,
                        token,
                        account,
                        f"/api/manager/contacts?directory=1{suffix}",
                        state_locked=True,
                    )
                    items = [
                        normalized
                        for item in data.get("items") or []
                        if (normalized := self._public_contact(item)) is not None
                    ]
                    self._directory_snapshot = {
                        **data,
                        "items": items,
                        "total": int(data.get("total") or len(items)),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self._contacts.update(
                        {str(item["user_id"]): copy.deepcopy(item) for item in items}
                    )
                    self._loaded_contacts.update(str(item["user_id"]) for item in items)
                return {
                    **copy.deepcopy(self._directory_snapshot),
                    "stale": False,
                    "message": "读取的是当前微信通讯录；查看和刷新不会向联系人发送消息。",
                }
            except (GroupDirectoryError, OSError, ValueError) as exc:
                if self._account != self._contact_account:
                    self._reset_contact_state(self._account)
                fallback = self._directory_snapshot or {
                    "items": [],
                    "total": 0,
                    "account_id": self._contact_account,
                    "updated_at": None,
                    "truncated": False,
                }
                return {
                    **copy.deepcopy(fallback),
                    "stale": True,
                    "message": str(exc),
                }

    def profile(self, user_id: str) -> dict[str, object]:
        """Read a current contact profile after validating the active directory."""
        requested = self._contact_ids([user_id])
        if not requested:
            raise GroupDirectoryError("联系人标识格式不正确，请从联系人列表重新选择。")
        contact_id = requested[0]
        with self._lock:
            port, token, account = self._connection()
            if account != self._contact_account:
                self._reset_contact_state(account)
            items = (
                self._directory_snapshot.get("items", [])
                if self._directory_snapshot
                else []
            )
            if not any(
                isinstance(item, dict) and item.get("user_id") == contact_id
                for item in items
            ):
                raise GroupDirectoryError("请先刷新联系人列表，再选择当前账号的联系人。")
        data = self._request(
            port,
            token,
            account,
            f"/api/manager/contacts/{quote(contact_id, safe='')}",
        )
        item = self._public_contact(data.get("item"))
        if item is None or item.get("user_id") != contact_id:
            raise GroupDirectoryError("联系人详情格式不正确，请稍后重试。")
        with self._lock:
            if self._account != account or self._contact_account != account:
                raise GroupDirectoryError("微信账号已变化，请重新刷新联系人。")
            self._contacts[contact_id] = copy.deepcopy(item)
            self._loaded_contacts.add(contact_id)
            if self._directory_snapshot:
                self._directory_snapshot["items"] = [
                    copy.deepcopy(item)
                    if isinstance(current, dict)
                    and current.get("user_id") == contact_id
                    else current
                    for current in self._directory_snapshot.get("items", [])
                ]
        return {**data, "item": item}

    @staticmethod
    def _contact_ids(values) -> list[str]:
        result: list[str] = []
        for raw in values:
            value = str(raw or "").strip()
            if (
                not value
                or len(value) > 128
                or re.fullmatch(r"[A-Za-z0-9_.@-]+", value) is None
                or value.endswith("@chatroom")
                or value in result
            ):
                continue
            result.append(value)
        return result

    def contacts(self, user_ids) -> dict[str, dict[str, object]]:
        """Return current-account contact portraits without retaining them on disk."""
        requested = self._contact_ids(user_ids)
        if not requested:
            return {}
        with self._lock:
            port, token, account = self._connection()
            if account != self._contact_account:
                self._reset_contact_state(account)
            now = time.monotonic()
            missing = [
                user_id
                for user_id in requested
                if user_id not in self._loaded_contacts
                and self._contact_retry_at.get(user_id, 0.0) <= now
            ]

        for offset in range(0, len(missing), self._BATCH_SIZE):
            batch = missing[offset : offset + self._BATCH_SIZE]
            query = "&".join(f"id={quote(user_id, safe='')}" for user_id in batch)
            data = self._request(
                port,
                token,
                account,
                f"/api/manager/contacts?{query}",
            )
            received = {
                str(normalized["user_id"]): normalized
                for item in data.get("items") or []
                if (normalized := self._public_contact(item)) is not None
            }
            with self._lock:
                if self._account != account:
                    raise GroupDirectoryError("微信账号已变化，请重新刷新聊天记录。")
                self._contacts.update(received)
                self._loaded_contacts.update(received)
                retry_at = time.monotonic() + self._RETRY_SECONDS
                for user_id in batch:
                    if user_id in received:
                        self._contact_retry_at.pop(user_id, None)
                    else:
                        self._contact_retry_at[user_id] = retry_at

        with self._lock:
            return {
                user_id: copy.deepcopy(self._contacts[user_id])
                for user_id in requested
                if user_id in self._contacts
            }


__all__ = ["ContactDirectoryService"]
