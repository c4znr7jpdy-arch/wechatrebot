"""Account-scoped, memory-only private contact profiles from WeChat."""
from __future__ import annotations

import copy
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
        self._contacts: dict[str, dict[str, str]] = {}
        self._loaded_contacts: set[str] = set()
        self._contact_retry_at: dict[str, float] = {}

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

    def contacts(self, user_ids) -> dict[str, dict[str, str]]:
        """Return current-account contact portraits without retaining them on disk."""
        requested = self._contact_ids(user_ids)
        if not requested:
            return {}
        with self._lock:
            port, token, account = self._connection()
            if account != self._contact_account:
                self._contact_account = account
                self._contacts = {}
                self._loaded_contacts = set()
                self._contact_retry_at = {}
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
                str(item.get("user_id") or "").strip(): {
                    "user_id": str(item.get("user_id") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                    "avatar": str(item.get("avatar") or "").strip(),
                }
                for item in data.get("items") or []
                if isinstance(item, dict) and str(item.get("user_id") or "").strip()
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
