"""Chat directories, policies and account-scoped message history."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .group_directory import GroupDirectoryError
from .error_catalog import AccountStatusUnavailableError
from .customer_config import load_robot_settings
from .group_policy import GroupPolicyError
from .launcher import installation_services_stopped
from .private_policy import PrivatePolicyError


class ChatApiMixin:
    @staticmethod
    def _date_timestamp(value: object, *, end: bool = False) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text)
        if end:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        return parsed.astimezone().timestamp()

    def _history_account_id(self) -> str:
        """Return the bot account verified by a fresh runtime probe."""
        try:
            runtime = self._lifecycle.fresh_runtime_snapshot()
        except (AttributeError, OSError, ValueError) as exc:
            raise AccountStatusUnavailableError("微信账号状态暂时不可用，请稍后重试。") from exc
        if runtime is None or getattr(runtime, "account_status_available", True) is False:
            raise AccountStatusUnavailableError("微信账号状态暂时不可用，请稍后重试。")
        return str(getattr(runtime, "wechat_identity", "") or "").strip()

    def _offline_history_account(self, view) -> str:
        """A signed device binding grants data access, never a live login identity."""
        runtime = getattr(view, "runtime", None)
        if (runtime is None or getattr(runtime, "connection_ready", None) is not False
                or getattr(runtime, "engine_ready", None) is not False
                or getattr(view, "can_stop", False)
                or any(getattr(runtime, field, None) for field in (
                    "wechat_identity", "hook_ready", "upstream_connected", "heartbeat_fresh"))):
            return ""
        status = self._license_client().snapshot()
        if (status.get("enabled") and status.get("device_id") and status.get("license_id")
                and status.get("account") and installation_services_stopped(self._install_root)):
            # Signature/device validation still applies to expired entitlements.
            # An invalid local cache returns no bound account.
            return str(status.get("account") or "")
        return ""

    def _history_data_identity(self) -> tuple[str, bool]:
        failure = None
        try:
            actual = self._history_account_id()
            if actual:
                return actual, False
        except OSError as exc:
            failure = exc
        if not self._license_client().snapshot().get("account"):
            if failure is not None:
                raise failure
            return "", False
        try:
            account = self._offline_history_account(self._lifecycle.refresh())
            if account:
                return account, True
        except (AttributeError, OSError, ValueError) as exc:
            if failure is not None:
                raise failure
            raise OSError("无法确认本机历史记录的数据身份，请稍后重试。") from exc
        if failure is not None:
            raise failure
        return "", False

    def _history_contacts(self, user_ids: list[str]) -> dict[str, dict[str, str]]:
        resolver = getattr(getattr(self, "_contacts", None), "contacts", None)
        if not callable(resolver) or not user_ids:
            return {}
        try:
            return resolver(user_ids)
        except (OSError, ValueError, GroupDirectoryError):
            # History remains usable while WeChat is offline; stored portraits
            # or the stable text fallback continue to render in that state.
            return {}

    def get_conversations(self, filters: dict[str, Any] | None = None) -> dict[str, object]:
        try:
            values = filters or {}
            kind = str(values.get("kind") or "all")
            search = str(values.get("search") or "").strip()
            expected_account = str(values.get("account_id") or "").strip()
            start_time = self._date_timestamp(values.get("start"))
            end_time = self._date_timestamp(values.get("end"), end=True)
            runtime_account, offline = self._history_data_identity()
            empty = {
                "messages": 0,
                "conversations": 0,
                "private_messages": 0,
                "group_messages": 0,
                "visible_conversations": 0,
                "directory_groups": 0,
            }
            if not runtime_account:
                return self._ok({
                    "items": [],
                    "summary": empty,
                    "account_id": "",
                    "requires_login": True,
                })
            if expected_account and expected_account != runtime_account:
                return self._ok({
                    "items": [],
                    "summary": empty,
                    "account_id": runtime_account,
                    "account_changed": True,
                })
            directory = {"items": [], "account_id": runtime_account}
            if kind in {"all", "group"} and not offline:
                directory = self._groups.list(values.get("refresh_groups") is True)
            directory_account = str(directory.get("account_id") or "").strip()
            if (
                kind in {"all", "group"}
                and directory_account != runtime_account
            ):
                return self._ok({
                    "items": [],
                    "summary": empty,
                    "account_id": runtime_account,
                    "account_changed": True,
                })
            account_id = runtime_account
            history = self._history.conversations(
                kind=kind,
                search=search,
                start_time=start_time,
                end_time=end_time,
                account_id=account_id,
            )
            items = list(history.get("items") or [])
            private_contacts = {} if offline else self._history_contacts(
                [
                    str(item.get("conversation_id") or "")
                    for item in items
                    if isinstance(item, dict) and item.get("type") == "private"
                ]
            )
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "private":
                    continue
                contact = private_contacts.get(
                    str(item.get("conversation_id") or ""), {}
                )
                item["avatar"] = str(
                    contact.get("avatar") or item.get("avatar") or ""
                )
            if kind in {"all", "group"}:
                known = {
                    str(item.get("conversation_id") or ""): item
                    for item in items
                    if isinstance(item, dict)
                }
                keyword = search.casefold()
                for group in directory.get("items") or []:
                    if not isinstance(group, dict):
                        continue
                    group_id = str(group.get("group_id") or "").strip()
                    group_name = str(group.get("name") or "").strip()
                    if not group_id or (
                        keyword
                        and keyword not in group_id.casefold()
                        and keyword not in group_name.casefold()
                    ):
                        continue
                    if group_id in known:
                        known[group_id]["display_name"] = group_name or known[group_id].get("display_name")
                        # History rows carry the sender's portrait. A group may
                        # only use the portrait returned for that group.
                        known[group_id]["avatar"] = str(group.get("avatar") or known[group_id].get("avatar") or "")
                        known[group_id]["member_count"] = group.get("member_count")
                        continue
                    if start_time is not None or end_time is not None:
                        continue
                    item = {
                        "conversation_id": group_id,
                        "type": "group",
                        "display_name": group_name or f"微信群 · {group_id.removesuffix('@chatroom')[-8:]}",
                        "message_count": 0,
                        "last_timestamp": 0,
                        "avatar": str(group.get("avatar") or ""),
                        "member_count": group.get("member_count"),
                    }
                    items.append(item)
                    known[group_id] = item
                items.sort(
                    key=lambda item: (
                        -float(item.get("last_timestamp") or 0),
                        str(item.get("display_name") or "").casefold(),
                    )
                )
            summary = dict(history.get("summary") or {})
            summary["visible_conversations"] = len(items)
            summary["directory_groups"] = len(directory.get("items") or [])
            return self._ok({
                "items": items,
                "summary": summary,
                "account_id": account_id,
                **({"offline": True} if offline else {}),
            })
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def get_groups(self, refresh: bool = False) -> dict[str, object]:
        if refresh:
            getattr(self, "_history_group_members", {}).clear()
        try:
            return self._ok(self._groups.list(bool(refresh)))
        except GroupDirectoryError as exc:
            return self._failed(exc)

    def get_group_members(self, group_id: str) -> dict[str, object]:
        try:
            return self._ok(self._groups.members(group_id))
        except GroupDirectoryError as exc:
            return {"ok": False, "error": {"title": "成员信息未更新", "message": str(exc)}}

    def get_group_qrcode(self, account_id: str, group_id: str) -> dict[str, object]:
        try:
            return self._ok(self._groups.qrcode(account_id, group_id))
        except GroupDirectoryError as exc:
            return {"ok": False, "error": {"title": "群二维码暂不可用", "message": str(exc)}}

    def get_group_policy(self, account_id: str, group_id: str) -> dict[str, object]:
        try:
            return self._ok(self._group_policy_service().overview(account_id, group_id))
        except (OSError, ValueError, GroupPolicyError) as exc:
            return self._failed(exc, "15001")

    def save_group_policy(
        self,
        account_id: str,
        group_id: str,
        values: dict[str, Any],
        revision: str = "",
    ) -> dict[str, object]:
        try:
            saved = self._group_policy_service().save(
                account_id,
                group_id,
                values,
                expected_revision=revision or None,
            )
            return self._ok(saved, message=str(saved.get("message") or "群聊策略已保存。"))
        except (OSError, ValueError, GroupPolicyError) as exc:
            return self._failed(exc, "15001")

    def preview_group_policy(
        self,
        account_id: str,
        group_id: str,
        values: dict[str, Any],
        revision: str = "",
    ) -> dict[str, object]:
        try:
            return self._ok(
                self._group_policy_service().preview(
                    account_id,
                    group_id,
                    values,
                    expected_revision=revision or None,
                )
            )
        except (OSError, ValueError, GroupPolicyError) as exc:
            return self._failed(exc, "15001")

    def get_contacts(self, refresh: bool = False) -> dict[str, object]:
        return self._ok(self._contacts.list(bool(refresh)))

    def get_contact_detail(self, contact_id: str) -> dict[str, object]:
        try:
            return self._ok(self._contacts.profile(contact_id))
        except GroupDirectoryError as exc:
            return {
                "ok": False,
                "error": {"title": "联系人详情未更新", "message": str(exc)},
            }

    def get_private_policy(
        self, account_id: str, contact_id: str
    ) -> dict[str, object]:
        try:
            return self._ok(self._private_policies.overview(account_id, contact_id))
        except (OSError, ValueError, PrivatePolicyError) as exc:
            return self._failed(exc, "15002")

    def save_private_policy(
        self,
        account_id: str,
        contact_id: str,
        values: dict[str, Any],
        revision: str = "",
    ) -> dict[str, object]:
        try:
            saved = self._private_policies.save(
                account_id,
                contact_id,
                values,
                expected_revision=revision or None,
            )
            return self._ok(
                saved,
                message=str(saved.get("message") or "联系人可用功能已保存。"),
            )
        except (OSError, ValueError, PrivatePolicyError) as exc:
            return self._failed(exc, "15002")

    def get_messages(
        self,
        conversation_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        try:
            values = filters or {}
            account_id, offline = self._history_data_identity()
            expected_account = str(values.get("account_id") or "").strip()
            paged = values.get("paged") is True
            if not account_id:
                if paged:
                    return self._ok({
                        "items": [],
                        "has_more": False,
                        "account_id": "",
                        "requires_login": True,
                    })
                return self._ok([])
            if expected_account and expected_account != account_id:
                if paged:
                    return self._ok({
                        "items": [],
                        "has_more": False,
                        "account_id": account_id,
                        "account_changed": True,
                    })
                return self._ok([])
            page_size = 20
            before_timestamp: float | None = None
            before_id: int | None = None
            if paged:
                page_size = max(1, min(int(values.get("page_size") or 20), 20))
                before = values.get("before")
                if isinstance(before, dict) and before.get("timestamp") is not None:
                    before_timestamp = float(before["timestamp"])
                    if before.get("id") is not None:
                        before_id = int(before["id"])
            query = {
                "account_id": account_id,
                "search": str(values.get("search") or "").strip(),
                "start_time": self._date_timestamp(values.get("start")),
                "end_time": self._date_timestamp(values.get("end"), end=True),
            }
            if paged:
                query.update(
                    before=before_timestamp,
                    before_id=before_id,
                    limit=page_size + 1,
                )
            messages = list(
                self._history.messages(
                    conversation_id,
                    **query,
                )
            )
            has_more = paged and len(messages) > page_size
            if has_more:
                messages = messages[-page_size:]
            if conversation_id.endswith("@chatroom"):
                member_cache = getattr(self, "_history_group_members", None)
                if not isinstance(member_cache, dict):
                    member_cache = {}
                    self._history_group_members = member_cache
                cache_key = (account_id, conversation_id)
                # Never reuse member details from a previously logged-in account.
                for old_key in tuple(member_cache):
                    if old_key[0] != account_id:
                        member_cache.pop(old_key, None)
                members = {} if offline else member_cache.get(cache_key)
                if members is None:
                    members = {}
                    try:
                        directory = self._groups.members(conversation_id)
                        trusted = bool(account_id) and directory.get("account_id") == account_id and not directory.get("stale")
                        for item in (directory.get("items") or []) if trusted else []:
                            if not isinstance(item, dict):
                                continue
                            user_id = str(item.get("user_id") or "").strip()
                            if user_id:
                                members[user_id] = {
                                    "wechat_id": str(item.get("wechat_id") or item.get("alias") or "").strip(),
                                    "wechat_name": str(item.get("name") or "").strip(),
                                    "group_nickname": str(item.get("group_nickname") or "").strip(),
                                    "avatar": str(item.get("avatar") or "").strip(),
                                }
                        if trusted:
                            member_cache[cache_key] = members
                    except (OSError, ValueError, GroupDirectoryError):
                        pass
                for message in messages:
                    if message.get("role") == "assistant":
                        continue
                    profile = members.get(str(message.get("sender_id") or ""), {})
                    for key in ("wechat_name", "group_nickname", "avatar"):
                        if not message.get(key) and profile.get(key):
                            message[key] = profile[key]
                    message["wechat_id"] = str(
                        message.get("wechat_id")
                        or profile.get("wechat_id")
                        or message.get("sender_id")
                        or ""
                    )
            elif not offline and any(
                message.get("role") != "assistant"
                and (
                    not message.get("avatar")
                    or not message.get("wechat_name")
                    or not message.get("wechat_id")
                )
                for message in messages
            ):
                contact = self._history_contacts([conversation_id]).get(
                    conversation_id, {}
                )
                avatar = str(contact.get("avatar") or "")
                wechat_name = str(contact.get("name") or "")
                for message in messages:
                    if message.get("role") == "assistant":
                        continue
                    if not message.get("avatar"):
                        message["avatar"] = avatar
                    if not message.get("wechat_name"):
                        message["wechat_name"] = wechat_name
                    if not message.get("wechat_id"):
                        message["wechat_id"] = str(
                            contact.get("alias") or message.get("sender_id") or ""
                        )
            replies = [message for message in messages if message.get("role") == "assistant"]
            if replies:
                robot = self._contacts.self_profile() if not offline and hasattr(getattr(self, "_contacts", None), "self_profile") else {}
                name = load_robot_settings(self._install_root).profile.display_name
                for message in replies:
                    message["avatar"] = str(robot.get("avatar") or (message.get("avatar") if offline else "") or "")
                    message["sender_id"] = account_id
                    message["sender_name"] = name
            if paged:
                return self._ok({
                    "items": messages,
                    "has_more": has_more,
                    "account_id": account_id,
                })
            return self._ok(messages)
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def delete_conversation(
        self, conversation_id: str, expected_account_id: str = ""
    ) -> dict[str, object]:
        try:
            account_id = self._history_account_id()
            if not account_id:
                return self._ok({"deleted": 0, "requires_login": True})
            if expected_account_id and expected_account_id != account_id:
                return self._ok({"deleted": 0, "account_changed": True})
            count = self._history.delete_conversation(
                conversation_id,
                account_id=account_id,
            )
            return self._ok({"deleted": count})
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def delete_history_before(
        self, value: str, expected_account_id: str = ""
    ) -> dict[str, object]:
        try:
            account_id = self._history_account_id()
            if not account_id:
                return self._ok({"deleted": 0, "requires_login": True})
            if expected_account_id and expected_account_id != account_id:
                return self._ok({"deleted": 0, "account_changed": True})
            timestamp = self._date_timestamp(value)
            if timestamp is None:
                raise ValueError("请选择清理日期。")
            count = self._history.delete_before(
                timestamp,
                account_id=account_id,
            )
            return self._ok({"deleted": count})
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def export_history(
        self, format: str = "csv", expected_account_id: str = ""
    ) -> dict[str, object]:
        try:
            account_id, _ = self._history_data_identity()
            if not account_id:
                return self._ok({"cancelled": True, "requires_login": True})
            if expected_account_id and expected_account_id != account_id:
                return self._ok({"cancelled": True, "account_changed": True})
            if self._window is None:
                raise ValueError("导出窗口尚未就绪。")
            import webview

            dialog_type = getattr(webview, "SAVE_DIALOG", None)
            if dialog_type is None:
                dialog_type = webview.FileDialog.SAVE
            suffix = "json" if format.casefold() == "json" else "csv"
            result = self._window.create_file_dialog(
                dialog_type,
                save_filename=f"聊天记录.{suffix}",
                file_types=(
                    "JSON 文件 (*.json)" if suffix == "json" else "CSV 文件 (*.csv)",
                ),
            )
            if not result:
                return self._ok({"cancelled": True})
            if self._history_data_identity()[0] != account_id:
                return self._ok({"cancelled": True, "account_changed": True})
            selected = result[0] if isinstance(result, (list, tuple)) else result
            target = self._history.export(
                selected,
                format=suffix,
                account_id=account_id,
            )
            return self._ok({"path": str(target)})
        except (OSError, ValueError) as exc:
            return self._failed(exc)
