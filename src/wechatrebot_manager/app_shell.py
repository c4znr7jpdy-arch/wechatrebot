from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sqlite3
import sys
import threading
from typing import Any, Callable
import webbrowser

from . import __version__
from .chat_history import ChatHistoryService
from .contact_directory import ContactDirectoryService
from .group_directory import GroupDirectoryService, GroupDirectoryError
from .customer_config import (
    delete_provider_profile,
    delete_provider_thinking_preference,
    load_provider_profile,
    load_provider_services,
    load_robot_settings,
    provider_reference_modules,
    replace_provider_references,
    replace_provider_value,
    save_provider_profile,
    save_provider_thinking_preference,
    set_default_provider,
    set_provider_thinking,
)
from .customer_maintenance import (
    CustomerMaintenanceService,
    MaintenanceError,
)
from .environment import WECHAT_DOWNLOAD_URL
from .error_catalog import load_error_catalog
from .experience import ExperienceError, ExperienceService
from .core_settings import RobotCoreSettingsService
from .group_policy import GroupPolicyError, GroupPolicyService
from .lifecycle import LifecycleController
from .payload import PayloadError
from .private_policy import PrivateChatPolicyService, PrivatePolicyError
from .plugin_center import (
    EngineApiClient,
    EngineUnavailableError,
    PluginCenterError,
    PluginCenterService,
    launch_plugin_center,
)
from .provider import (
    DEEPSEEK_PROVIDER_API_BASE,
    DEEPSEEK_PROVIDER_MODEL,
    DEFAULT_PROVIDER_API_BASE,
    DEFAULT_PROVIDER_MODEL,
    DEFAULT_PROVIDER_SOURCE_ID,
    OpenAIProviderProfile,
    discover_openai_models,
    provider_source_id,
    supports_thinking_toggle,
)
from .response_trace import ResponseTraceService
from .schedules import ScheduleService
from .scheduled_task_service import ScheduledTaskService
from .task_store import next_occurrences, parse_command, schedule_text
from .updates import (
    UpdateChannel,
    UpdateError,
    UpdateRelease,
    check_for_update,
    cleanup_stale_update_workdirs,
    consume_update_result,
    create_update_plan,
    launch_update_helper,
    load_update_channel,
    prepare_update,
)
from .shortcuts import refresh_windows_shortcuts
from .uninstaller import launch_installed_uninstaller, provision_uninstaller
from .installed_entries import ensure_launcher_executable
from .wechat_path import validate_wechat_executable
from .windows_tray import WindowsTrayController


PUBLIC_API_METHODS = (
    "bootstrap",
    "get_status",
    "check_environment",
    "start_robot",
    "stop_robot",
    "select_wechat",
    "open_wechat_download",
    "get_features",
    "get_robot_core",
    "save_robot_core",
    "request_robot_core",
    "get_feature",
    "save_feature",
    "reload_feature",
    "open_feature_center",
    "get_conversations",
    "get_groups",
    "get_group_members",
    "get_group_policy",
    "save_group_policy",
    "get_contacts",
    "get_contact_detail",
    "get_private_policy",
    "save_private_policy",
    "get_messages",
    "delete_conversation",
    "delete_history_before",
    "export_history",
    "get_schedules",
    "set_schedule_enabled",
    "update_schedule_time",
    "delete_schedule",
    "get_scheduled_task_context",
    "save_scheduled_task",
    "set_scheduled_task_enabled",
    "delete_scheduled_task",
    "preview_scheduled_task",
    "test_scheduled_task",
    "get_scheduled_task_runs",
    "parse_scheduled_command",
    "preview_scheduled_times",
    "get_activity",
    "get_response_traces",
    "save_memory_settings",
    "save_emotion_settings",
    "complete_onboarding",
    "get_providers",
    "save_provider",
    "set_default_provider",
    "set_provider_thinking",
    "test_provider",
    "discover_provider_models",
    "delete_provider",
    "get_maintenance",
    "create_backup",
    "validate_restore",
    "restore_backup",
    "get_diagnostics",
    "export_diagnostics",
    "open_install_folder",
    "open_uninstaller",
    "check_update",
    "install_update",
    "get_update_status",
)


def _asset_root() -> Path:
    candidates = [Path(__file__).with_name("app_webui")]
    import sys

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.insert(0, Path(bundle_root) / "wechatrebot_manager" / "app_webui")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    raise FileNotFoundError("主界面资源缺失，请重新安装软件。")


class ApplicationApi:
    def __init__(self, install_root: str | Path) -> None:
        self._install_root = Path(install_root).expanduser().resolve()
        self._lifecycle = LifecycleController(self._install_root)
        self._features = PluginCenterService(self._install_root)
        self._history = ChatHistoryService(self._install_root)
        self._contacts = ContactDirectoryService(self._install_root)
        self._groups = GroupDirectoryService(self._install_root)
        self._schedules = ScheduleService(self._install_root)
        self._response_traces = ResponseTraceService(self._install_root)
        self._group_policies = GroupPolicyService(
            self._install_root, schedules=self._schedules
        )
        self._private_policies = PrivateChatPolicyService(self._install_root)
        self._experience = ExperienceService(self._install_root)
        self._maintenance = CustomerMaintenanceService(
            self._install_root,
            is_running=self._runtime_active,
        )
        self._catalog = load_error_catalog(self._install_root)
        self._window: Any | None = None
        self._tray: WindowsTrayController | None = None
        self._history_group_members: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
        self._action_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._update_status_lock = threading.Lock()
        self._update_status: dict[str, object] = {
            "state": "idle",
            "phase": "idle",
            "completed": 0,
            "total": 0,
            "percent": 0,
            "message": "",
            "error": None,
        }
        self._update_channel: UpdateChannel | None = None
        self._available_update: UpdateRelease | None = None
        self._update_result = consume_update_result(self._install_root)
        threading.Thread(
            target=cleanup_stale_update_workdirs,
            args=(self._install_root,),
            daemon=True,
        ).start()

    def _runtime_active(self) -> bool:
        status = self._lifecycle.refresh()
        state = getattr(getattr(status, "state", ""), "value", getattr(status, "state", ""))
        return str(state) != "STOPPED"

    def _experience_service(self) -> ExperienceService:
        service = getattr(self, "_experience", None)
        return service if service is not None else ExperienceService(self._install_root)

    def _group_policy_service(self) -> GroupPolicyService:
        service = getattr(self, "_group_policies", None)
        if service is not None:
            return service
        schedules = getattr(self, "_schedules", None)
        return GroupPolicyService(self._install_root, schedules=schedules)

    def _attach_window(
        self,
        window: Any,
        tray: WindowsTrayController | None = None,
    ) -> None:
        self._window = window
        self._tray = tray

    def _close_window_for_update(self) -> None:
        if self._tray is not None:
            self._tray.permit_programmatic_close()
        if self._window is not None:
            self._window.destroy()

    @staticmethod
    def _ok(data: Any = None, *, message: str = "") -> dict[str, object]:
        return {"ok": True, "data": data, "message": message}

    def _failed(self, exc: BaseException, code: str = "19000") -> dict[str, object]:
        definition = self._catalog.get(code)
        error = definition.to_dict()
        # Policy validation errors are written for users. Never expose raw
        # filesystem/transport errors, which can contain paths or credentials.
        if isinstance(exc, (GroupPolicyError, PrivatePolicyError)):
            error["detail"] = str(exc)
        self._lifecycle.events.emit(
            "APP_ERROR",
            "failed",
            code=definition.code,
            message=str(exc),
        )
        return {
            "ok": False,
            "error": error,
        }

    def bootstrap(self) -> dict[str, object]:
        try:
            settings = load_robot_settings(self._install_root)
            provider = load_provider_profile(self._install_root)
            providers = load_provider_services(self._install_root)
            channel = self._load_update_channel()
            return self._ok(
                {
                    "version": __version__,
                    "install_root": str(self._install_root),
                    "status": self._lifecycle.refresh().to_dict(),
                    "brand": {
                        **settings.profile.to_dict(),
                        "admin_id": settings.admin_id,
                    },
                    "provider": {
                        "configured": provider is not None,
                        "api_base": (
                            provider.api_base
                            if provider
                            else DEFAULT_PROVIDER_API_BASE
                        ),
                        "model": (
                            provider.model if provider else DEFAULT_PROVIDER_MODEL
                        ),
                        "api_key_configured": bool(provider and provider.api_key),
                    },
                    "providers": [item.to_public_dict() for item in providers],
                    "experience": self._experience_service().snapshot(),
                    "provider_presets": [
                        {
                            "id": "free_grok",
                            "name": "免费 Grok 4.6（推荐）",
                            "api_base": DEFAULT_PROVIDER_API_BASE,
                            "model": DEFAULT_PROVIDER_MODEL,
                            "description": "面向普通用户的默认回复服务。",
                        },
                        {
                            "id": "deepseek",
                            "name": "DeepSeek（自用）",
                            "api_base": DEEPSEEK_PROVIDER_API_BASE,
                            "model": DEEPSEEK_PROVIDER_MODEL,
                            "description": "适合在你自己的设备上使用。",
                        },
                        {
                            "id": "custom",
                            "name": "其他兼容服务",
                            "api_base": "",
                            "model": "",
                            "description": "填写兼容接口地址、模型名称和密钥。",
                        },
                    ],
                    "update": {
                        "enabled": bool(channel and channel.enabled),
                        "channel": channel.channel if channel else "stable",
                        "check_interval_seconds": (
                            channel.check_interval_seconds if channel else None
                        ),
                        "result": self._update_result,
                    },
                }
            )
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def get_status(self) -> dict[str, object]:
        try:
            return self._ok(self._lifecycle.refresh().to_dict())
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def check_environment(self) -> dict[str, object]:
        try:
            return self._ok(self._lifecycle.check_environment().to_dict())
        except (OSError, ValueError) as exc:
            return self._failed(exc, "11004")

    def start_robot(self) -> dict[str, object]:
        if not self._action_lock.acquire(blocking=False):
            return self._ok({"accepted": False}, message="正在处理上一个操作。")

        def worker() -> None:
            try:
                self._lifecycle.start()
            except Exception as exc:  # pragma: no cover - final UI safety net
                self._failed(exc, "13001")
            finally:
                self._action_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return self._ok({"accepted": True}, message="正在启动机器人。")

    def stop_robot(self) -> dict[str, object]:
        if not self._action_lock.acquire(blocking=False):
            return self._ok({"accepted": False}, message="正在处理上一个操作。")

        def worker() -> None:
            try:
                self._lifecycle.request_stop()
            except Exception as exc:  # pragma: no cover - final UI safety net
                self._failed(exc)
            finally:
                self._action_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return self._ok({"accepted": True}, message="正在停止机器人。")

    def select_wechat(self, value: str = "") -> dict[str, object]:
        try:
            selected = value.strip()
            if not selected:
                if self._window is None:
                    raise ValueError("选择窗口尚未就绪。")
                import webview

                dialog_type = getattr(webview, "OPEN_DIALOG", None)
                if dialog_type is None:
                    dialog_type = webview.FileDialog.OPEN
                result = self._window.create_file_dialog(
                    dialog_type,
                    allow_multiple=False,
                    file_types=("微信主程序 (Weixin.exe)", "可执行文件 (*.exe)"),
                )
                if not result:
                    return self._ok({"cancelled": True})
                selected = str(result[0])
            item = validate_wechat_executable(selected)
            if not item.supported:
                code = "11002" if item.path.is_file() else "11001"
                return {
                    "ok": False,
                    "error": {
                        **self._catalog.get(code).to_dict(),
                        "message": "；".join(item.errors),
                    },
                }
            from .runtime_config import write_runtime_configs

            write_runtime_configs(
                self._install_root,
                item.path,
                wechat_version=item.version,
            )
            return self._ok(self._lifecycle.check_environment().to_dict())
        except (OSError, ValueError) as exc:
            return self._failed(exc, "11002")

    def open_wechat_download(self) -> dict[str, object]:
        opened = webbrowser.open(WECHAT_DOWNLOAD_URL)
        return self._ok({"opened": bool(opened)})

    def get_features(self) -> dict[str, object]:
        try:
            return self._ok(self._features.catalog())
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "13003")

    def get_robot_core(self) -> dict[str, object]:
        try:
            return self._ok(RobotCoreSettingsService(self._features).detail())
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "13003")

    def save_robot_core(
        self,
        config: dict[str, Any],
        revision: str = "",
        identity: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        try:
            saved = RobotCoreSettingsService(self._features).save_global(
                config, revision, identity
            )
            return self._ok(saved)
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def request_robot_core(
        self, method: str, endpoint: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        try:
            return self._ok(RobotCoreSettingsService(self._features).request(method, endpoint, payload))
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def get_feature(self, feature_id: str) -> dict[str, object]:
        try:
            return self._ok(self._features.detail(feature_id))
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "13003")

    def save_feature(
        self,
        feature_id: str,
        config: dict[str, Any],
        revision: str = "",
    ) -> dict[str, object]:
        try:
            return self._ok(
                self._features.save(feature_id, config, revision or None)
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def reload_feature(self, feature_id: str) -> dict[str, object]:
        try:
            return self._ok(self._features.reload(feature_id))
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "13003")

    def open_feature_center(self, feature_id: str = "") -> dict[str, object]:
        try:
            launch_plugin_center(self._install_root, feature_id or None)
            return self._ok(
                {"opened": True},
                message="高级配置窗口正在打开。",
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "13003")

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
        """Return the last verified bot account without starting a new probe."""
        try:
            view = self._lifecycle.view()
            runtime = getattr(view, "runtime", None)
            return str(getattr(runtime, "wechat_identity", "") or "").strip()
        except (AttributeError, OSError, ValueError):
            return ""

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
            start_time = self._date_timestamp(values.get("start"))
            end_time = self._date_timestamp(values.get("end"), end=True)
            directory = {"items": []}
            if kind in {"all", "group"}:
                directory = self._groups.list(values.get("refresh_groups") is True)
            account_id = str(
                directory.get("account_id") or self._history_account_id()
            ).strip()
            history = self._history.conversations(
                kind=kind,
                search=search,
                start_time=start_time,
                end_time=end_time,
                account_id=account_id,
            )
            items = list(history.get("items") or [])
            private_contacts = self._history_contacts(
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
                        known[group_id]["avatar"] = str(group.get("avatar") or "")
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
            return self._ok({"items": items, "summary": summary})
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def get_groups(self, refresh: bool = False) -> dict[str, object]:
        if refresh:
            getattr(self, "_history_group_members", {}).clear()
        return self._ok(self._groups.list(bool(refresh)))

    def get_group_members(self, group_id: str) -> dict[str, object]:
        try:
            return self._ok(self._groups.members(group_id))
        except GroupDirectoryError as exc:
            return {"ok": False, "error": {"title": "成员信息未更新", "message": str(exc)}}

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
            account_id = self._history_account_id()
            paged = values.get("paged") is True
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
                members = member_cache.get(cache_key)
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
            elif any(
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
            if paged:
                return self._ok({"items": messages, "has_more": has_more})
            return self._ok(messages)
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def delete_conversation(self, conversation_id: str) -> dict[str, object]:
        try:
            count = self._history.delete_conversation(
                conversation_id,
                account_id=self._history_account_id(),
            )
            return self._ok({"deleted": count})
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def delete_history_before(self, value: str) -> dict[str, object]:
        try:
            timestamp = self._date_timestamp(value)
            if timestamp is None:
                raise ValueError("请选择清理日期。")
            count = self._history.delete_before(
                timestamp,
                account_id=self._history_account_id(),
            )
            return self._ok({"deleted": count})
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def export_history(self, format: str = "csv") -> dict[str, object]:
        try:
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
            selected = result[0] if isinstance(result, (list, tuple)) else result
            target = self._history.export(
                selected,
                format=suffix,
                account_id=self._history_account_id(),
            )
            return self._ok({"path": str(target)})
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def get_schedules(self) -> dict[str, object]:
        try:
            return self._ok(self._schedules.list())
        except (OSError, ValueError, sqlite3.Error) as exc:
            return self._failed(exc)

    def _scheduled_task_service(self) -> ScheduledTaskService:
        service = getattr(self, "_scheduled_tasks", None)
        if service is None:
            service = ScheduledTaskService(
                self._install_root, groups=getattr(self, "_groups", None),
                contacts=getattr(self, "_contacts", None), account_hint=self._history_account_id,
            )
            self._scheduled_tasks = service
        return service

    def _scheduled_call(self, operation: Callable, *args: Any) -> dict[str, object]:
        try:
            return self._ok(operation(*args))
        except ValueError as exc:
            return {"ok": False, "error": {"title": "任务尚未完成配置", "message": str(exc)}}
        except (OSError, sqlite3.Error, PluginCenterError, GroupDirectoryError):
            # Transport exceptions may include upstream addresses or credentials.
            return {"ok": False, "error": {"title": "定时任务操作未完成", "message": "连接或保存未完成，请检查机器人状态后刷新核对；你的编辑内容仍保留。"}}

    def get_scheduled_task_context(self) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().context)

    def save_scheduled_task(self, payload: dict[str, Any]) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().save, payload)

    def set_scheduled_task_enabled(self, account_id: str, task_id: str, enabled: bool, revision: int) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().set_enabled, account_id, task_id, enabled, revision)

    def delete_scheduled_task(self, account_id: str, task_id: str, revision: int) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().delete, account_id, task_id, revision)

    def preview_scheduled_task(self, payload: dict[str, Any]) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().preview, payload)

    def test_scheduled_task(self, payload: dict[str, Any]) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().test, payload)

    def get_scheduled_task_runs(self, account_id: str, task_id: str) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().runs, account_id, task_id)

    def parse_scheduled_command(self, command: str) -> dict[str, object]:
        return self._scheduled_call(parse_command, command)

    def preview_scheduled_times(self, schedule: dict[str, Any]) -> dict[str, object]:
        return self._scheduled_call(lambda: {"items": next_occurrences(schedule), "text": schedule_text(schedule)})

    def set_schedule_enabled(self, job_id: str, enabled: bool) -> dict[str, object]:
        try:
            return self._ok(self._schedules.set_enabled(job_id, bool(enabled)))
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def update_schedule_time(self, job_id: str, value: str) -> dict[str, object]:
        try:
            return self._ok(self._schedules.update_time(job_id, value))
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def delete_schedule(self, job_id: str) -> dict[str, object]:
        try:
            return self._ok(self._schedules.delete(job_id))
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def get_activity(self) -> dict[str, object]:
        names = {
            "APP_START": "软件已打开",
            "CHECK_WECHAT": "正在检查微信环境",
            "WECHAT_READY": "微信环境检查通过",
            "CORE_START": "开始启动机器人功能",
            "CORE_READY": "机器人功能已就绪",
            "CONNECTION_START": "开始连接微信",
            "CONNECTION_CONNECTED": "微信连接成功",
            "FUNCTION_LOADED": "全部功能加载完成",
            "WECHAT_CLOSED": "检测到微信已关闭",
            "CONNECTION_STOP": "微信连接已停止",
            "CORE_STOP": "机器人已停止",
            "APP_ERROR": "操作未完成",
        }
        items = []
        for event in reversed(self._lifecycle.events.recent(300)):
            items.append(
                {
                    "time": event.get("time", ""),
                    "event": event.get("event", ""),
                    "title": names.get(str(event.get("event")), "运行记录"),
                    "status": event.get("status", ""),
                    "code": event.get("code", ""),
                }
            )
        return self._ok(items)

    def get_response_traces(self) -> dict[str, object]:
        try:
            service = getattr(self, "_response_traces", None)
            if service is None:
                service = ResponseTraceService(self._install_root)
            return self._ok(service.recent())
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def get_experience(self) -> dict[str, object]:
        try:
            return self._ok(self._experience_service().snapshot())
        except (OSError, ExperienceError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def save_persona_settings(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            result = self._experience_service().save_persona(values)
            return self._ok(
                result,
                message=(
                    "人格设置已热更新，下一条消息立即使用。"
                    if result.get("apply_state") == "applied"
                    else "人格设置已保存，机器人下次启动时使用。"
                ),
            )
        except (OSError, ExperienceError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def save_memory_settings(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            result = self._experience_service().save_memory(values)
            return self._ok(
                result,
                message=(
                    "长期记忆设置已热更新，下一条消息立即使用。"
                    if result.get("apply_state") == "applied"
                    else "长期记忆设置已保存，机器人下次启动时使用。"
                ),
            )
        except (OSError, ExperienceError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def save_emotion_settings(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            result = self._experience_service().save_emotion(values)
            return self._ok(
                result,
                message=(
                    "情感设置已热更新，下一条消息立即使用。"
                    if result.get("apply_state") == "applied"
                    else "情感设置已保存，机器人下次启动时使用。"
                ),
            )
        except (OSError, ExperienceError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def complete_onboarding(self) -> dict[str, object]:
        try:
            return self._ok(self._experience_service().finish_onboarding(), message="基础设置已完成。")
        except (OSError, ExperienceError) as exc:
            return self._failed(exc, "15001")

    def save_provider(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            services = load_provider_services(self._install_root)
            original_id = str(values.get("provider_id") or "").strip()
            selected = next(
                (item for item in services if item.provider_id == original_id),
                None,
            )
            make_default = not services or bool(
                selected is not None and selected.is_default
            )
            current = load_provider_profile(self._install_root)
            api_key = str(values.get("api_key") or "").strip()
            if not api_key and selected is not None:
                api_key = selected.api_key
            elif not api_key and not original_id and current is not None:
                api_key = current.api_key
            api_base = str(values.get("api_base") or "").strip()
            model = str(values.get("model") or "").strip()
            profile = OpenAIProviderProfile(
                api_key=api_key,
                api_base=api_base,
                model=model,
                source_id=(
                    selected.source_id
                    if (
                        selected is not None
                        and selected.api_base.rstrip("/") == api_base.rstrip("/")
                    )
                    else provider_source_id(api_base, model)
                ),
                thinking_enabled=(
                    self._requested_thinking(values["thinking_enabled"])
                    if "thinking_enabled" in values else None
                ),
            )
            errors = profile.validate()
            if errors:
                raise ValueError("；".join(errors))
            client = EngineApiClient(self._install_root)
            hot_applied = client.is_available()
            if hot_applied:
                client.save_provider(
                    profile,
                    original_provider_id=(
                        original_id
                        if selected is not None and selected.model == model
                        else None
                    ),
                    make_default=make_default,
                )
                saved = profile.normalized()
                if saved.thinking_enabled is not None:
                    save_provider_thinking_preference(
                        self._install_root, saved.model_id, saved.thinking_enabled
                    )
            else:
                saved = save_provider_profile(
                    self._install_root,
                    profile,
                    make_default=make_default,
                )
            return self._ok(
                {
                    "id": saved.model_id,
                    "name": next(
                        (
                            item.display_name
                            for item in load_provider_services(self._install_root)
                            if item.provider_id == saved.model_id
                        ),
                        saved.model,
                    ),
                    "configured": True,
                    "api_base": saved.api_base,
                    "model": saved.model,
                    "api_key_configured": bool(saved.api_key),
                    "is_default": make_default,
                    "is_recommended": (
                        saved.source_id == DEFAULT_PROVIDER_SOURCE_ID
                        and saved.model == DEFAULT_PROVIDER_MODEL
                    ),
                    "thinking_supported": supports_thinking_toggle(saved.api_base, saved.model),
                    "thinking_enabled": profile.thinking_enabled,
                    "apply_state": "applied" if hot_applied else "saved_for_start",
                },
                message=(
                    (
                        "回复模型已热更新并立即生效。"
                        if hot_applied
                        else "回复模型已保存，机器人启动时会直接使用。"
                    )
                    if make_default
                    else "模型服务已保存，当前回复模型保持不变。"
                ),
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    @staticmethod
    def _requested_thinking(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.casefold() in {"true", "false"}:
            return value.casefold() == "true"
        raise ValueError("思考模式只能选择开启或关闭。")

    def get_providers(self) -> dict[str, object]:
        try:
            return self._ok(
                [
                    item.to_public_dict()
                    for item in load_provider_services(self._install_root)
                ]
            )
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def set_default_provider(self, provider_id: str) -> dict[str, object]:
        try:
            services = load_provider_services(self._install_root)
            selected = next(
                (item for item in services if item.provider_id == provider_id),
                None,
            )
            if selected is None or not selected.configured:
                raise ValueError("所选模型服务不存在或尚未完成配置。")
            client = EngineApiClient(self._install_root)
            hot_applied = client.is_available()
            if hot_applied:
                client.set_default_provider(provider_id)
            else:
                set_default_provider(self._install_root, provider_id)
            return self._ok(
                {
                    "id": provider_id,
                    "apply_state": "applied" if hot_applied else "saved_for_start",
                },
                message=(
                    "默认回复模型已切换并立即生效。"
                    if hot_applied
                    else "默认回复模型已保存，机器人启动时会直接使用。"
                ),
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def set_provider_thinking(self, provider_id: str, enabled: bool) -> dict[str, object]:
        try:
            requested = self._requested_thinking(enabled)
            client = EngineApiClient(self._install_root)
            hot_applied = client.is_available()
            if hot_applied:
                protocol_applied = client.set_provider_thinking(provider_id, requested)
                save_provider_thinking_preference(
                    self._install_root, provider_id, requested
                )
            else:
                protocol_applied = set_provider_thinking(
                    self._install_root, provider_id, requested
                )
            saved = next(
                (
                    item
                    for item in load_provider_services(self._install_root)
                    if item.provider_id == provider_id
                ),
                None,
            )
            if saved is None or saved.thinking_enabled is not requested:
                raise PluginCenterError("思考模式未能确认保存，请刷新后重试。")
            return self._ok(
                {
                    "id": provider_id,
                    "thinking_enabled": requested,
                    "thinking_supported": protocol_applied,
                    "apply_state": (
                        "applied" if hot_applied and protocol_applied
                        else "saved_for_start" if protocol_applied
                        else "saved_preference"
                    ),
                },
                message=(
                    "思考模式已热更新，下一条消息立即生效。"
                    if hot_applied and protocol_applied
                    else "思考模式已保存，机器人下次启动时生效。"
                    if protocol_applied
                    else "思考模式偏好已保存；当前模型不支持时会自动忽略。"
                ),
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def test_provider(self, provider_id: str) -> dict[str, object]:
        try:
            client = EngineApiClient(self._install_root)
            if not client.is_available():
                raise EngineUnavailableError("请先启动机器人，再测试模型服务。")
            return self._ok(
                client.test_provider(provider_id),
                message="模型服务连接正常。",
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def discover_provider_models(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            provider_id = str(values.get("provider_id") or "").strip()
            selected = next(
                (
                    item
                    for item in load_provider_services(self._install_root)
                    if item.provider_id == provider_id
                ),
                None,
            )
            api_key = str(values.get("api_key") or "").strip()
            if not api_key and selected is not None:
                api_key = selected.api_key
            models = discover_openai_models(
                api_key=api_key,
                api_base=str(values.get("api_base") or "").strip(),
            )
            return self._ok(
                {"items": list(models), "total": len(models)},
                message=f"已读取 {len(models)} 个可用模型。",
            )
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def delete_provider(
        self, provider_id: str, replacement_provider_id: str = ""
    ) -> dict[str, object]:
        try:
            services = load_provider_services(self._install_root)
            selected = next(
                (item for item in services if item.provider_id == provider_id),
                None,
            )
            if selected is None:
                raise ValueError("所选模型服务不存在，请刷新后重试。")
            reference_modules = provider_reference_modules(
                self._install_root, provider_id, strict=True
            )
            needs_replacement = selected.is_default or bool(reference_modules)
            if needs_replacement:
                replacement = next(
                    (
                        item
                        for item in services
                        if item.provider_id == replacement_provider_id
                        and item.provider_id != provider_id
                        and item.configured
                    ),
                    None,
                )
                if replacement is None:
                    raise ValueError("请先选择一个已配置的模型接替当前服务及相关功能。")
            client = EngineApiClient(self._install_root)
            if client.is_available():
                snapshots: dict[str, dict[str, Any]] = {}
                prepared: dict[str, dict[str, Any]] = {}
                for module_name in reference_modules:
                    snapshot = client.get_config(module_name)
                    updated, count = replace_provider_value(
                        snapshot["config"], provider_id, replacement_provider_id
                    )
                    if count:
                        snapshots[module_name] = snapshot
                        prepared[module_name] = updated

                applied: list[tuple[str, str]] = []
                try:
                    for module_name, updated in prepared.items():
                        receipt = client.save_config(
                            module_name,
                            updated,
                            base_revision=str(snapshots[module_name]["revision"]),
                        )
                        applied.append(
                            (module_name, str(receipt.get("revision") or ""))
                        )
                except PluginCenterError:
                    for module_name, revision in reversed(applied):
                        try:
                            client.save_config(
                                module_name,
                                snapshots[module_name]["config"],
                                base_revision=revision or None,
                            )
                        except PluginCenterError:
                            pass
                    raise
                if selected.is_default:
                    client.set_default_provider(replacement_provider_id)
                client.delete_provider(provider_id, selected.source_id)
                delete_provider_thinking_preference(
                    self._install_root, provider_id
                )
                hot_applied = True
            else:
                if reference_modules:
                    replace_provider_references(
                        self._install_root,
                        provider_id,
                        replacement_provider_id,
                    )
                if selected.is_default:
                    set_default_provider(self._install_root, replacement_provider_id)
                delete_provider_profile(self._install_root, provider_id)
                hot_applied = False
            if hot_applied and reference_modules:
                message = "模型服务已删除，相关功能已切换并立即生效。"
            elif hot_applied:
                message = "模型服务已删除并立即生效。"
            elif reference_modules:
                message = "模型服务已删除，相关功能已切换。"
            else:
                message = "模型服务已删除。"
            return self._ok(
                {
                    "id": provider_id,
                    "updated_features": list(reference_modules),
                },
                message=message,
            )
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def _maintenance_service(self) -> CustomerMaintenanceService:
        return getattr(
            self,
            "_maintenance",
            CustomerMaintenanceService(self._install_root, is_running=self._runtime_active),
        )

    def get_maintenance(self) -> dict[str, object]:
        try:
            service = self._maintenance_service()
            return self._ok(
                {
                    "preview": service.preview_backup(),
                    "backups": service.list_backups(),
                    "diagnostics": service.diagnostic_summary(),
                }
            )
        except (OSError, ValueError, MaintenanceError) as exc:
            return self._failed(exc, "15001")

    def create_backup(self) -> dict[str, object]:
        try:
            return self._ok(
                self._maintenance_service().create_backup(),
                message="设置、人格和长期记忆已备份。",
            )
        except (OSError, ValueError, MaintenanceError) as exc:
            return self._failed(exc, "15001")

    def validate_restore(self, backup_id: str) -> dict[str, object]:
        try:
            return self._ok(self._maintenance_service().validate_restore(backup_id))
        except (OSError, ValueError, MaintenanceError) as exc:
            return self._failed(exc, "15001")

    def restore_backup(self, backup_id: str) -> dict[str, object]:
        try:
            return self._ok(
                self._maintenance_service().restore_backup(backup_id),
                message="备份已经恢复，并已保留恢复前副本。",
            )
        except (OSError, ValueError, MaintenanceError) as exc:
            return self._failed(exc, "15001")

    def get_diagnostics(self) -> dict[str, object]:
        try:
            return self._ok(self._maintenance_service().diagnostic_summary())
        except (OSError, ValueError, MaintenanceError) as exc:
            return self._failed(exc, "15001")

    def export_diagnostics(self) -> dict[str, object]:
        try:
            return self._ok(
                self._maintenance_service().export_diagnostics(),
                message="安全诊断摘要已导出。",
            )
        except (OSError, ValueError, MaintenanceError) as exc:
            return self._failed(exc, "15001")

    def open_install_folder(self) -> dict[str, object]:
        try:
            ensure_launcher_executable(self._install_root)
            if os.name == "nt":
                os.startfile(self._install_root)  # type: ignore[attr-defined]
            return self._ok({"opened": True})
        except OSError as exc:
            return self._failed(exc)

    def open_uninstaller(self) -> dict[str, object]:
        try:
            display_name = load_robot_settings(
                self._install_root
            ).profile.display_name
            provision_uninstaller(
                self._install_root,
                display_name=display_name,
            )
            launch_installed_uninstaller(self._install_root)
            return self._ok(
                {"opened": True},
                message="卸载程序已经打开。",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._failed(exc)

    def _load_update_channel(self) -> UpdateChannel | None:
        if self._update_channel is not None:
            return self._update_channel
        try:
            self._update_channel = load_update_channel(self._install_root)
        except (OSError, ValueError, UpdateError):
            return None
        return self._update_channel

    @staticmethod
    def _release_payload(release: UpdateRelease) -> dict[str, object]:
        return {
            "version": release.version,
            "title": release.title,
            "notes": list(release.notes),
            "published_at": release.published_at,
            "package_size": release.package_size,
            "mandatory": release.mandatory,
        }

    def check_update(self) -> dict[str, object]:
        if not self._update_lock.acquire(blocking=False):
            return self._ok({"busy": True}, message="正在检查新版本。")
        try:
            channel = self._load_update_channel()
            if channel is None or not channel.enabled:
                return self._ok(
                    {"enabled": False, "current": True, "release": None},
                    message="当前未启用在线更新。",
                )
            release = check_for_update(channel, current_version=__version__)
            self._available_update = release
            return self._ok(
                {
                    "enabled": True,
                    "current": release is None,
                    "release": self._release_payload(release) if release else None,
                }
            )
        except (OSError, ValueError, UpdateError) as exc:
            return self._failed(exc, "14001")
        finally:
            self._update_lock.release()

    def _set_update_status(self, **values: object) -> None:
        with self._update_status_lock:
            self._update_status = {**self._update_status, **values}

    def _update_progress(self, phase: str, completed: int, total: int) -> None:
        labels = {
            "download": "正在下载新版本",
            "extract": "正在解压更新包",
            "verify": "正在校验更新文件",
        }
        percent = min(100, max(0, round(completed * 100 / total))) if total else 0
        self._set_update_status(
            state="running",
            phase=phase,
            completed=completed,
            total=total,
            percent=percent,
            message=labels.get(phase, "正在准备更新"),
            error=None,
        )

    def get_update_status(self) -> dict[str, object]:
        with self._update_status_lock:
            return self._ok(dict(self._update_status))

    def _run_update_install(
        self, release: UpdateRelease, channel: UpdateChannel
    ) -> None:
        try:
            try:
                prepared = prepare_update(
                    release,
                    channel,
                    self._install_root,
                    progress_callback=self._update_progress,
                )
            except (OSError, PayloadError, ValueError, UpdateError) as exc:
                failure = self._failed(exc, "14002")
                self._set_update_status(
                    state="failed",
                    phase="failed",
                    message="更新下载或校验未能完成。",
                    error=failure["error"],
                )
                return

            self._set_update_status(
                state="running",
                phase="launching",
                completed=1,
                total=1,
                percent=100,
                message="正在启动安装程序",
                error=None,
            )
            try:
                plan = create_update_plan(prepared, self._install_root)
                launch_update_helper(plan)
            except (OSError, ValueError, UpdateError) as exc:
                failure = self._failed(exc, "14003")
                self._set_update_status(
                    state="failed",
                    phase="failed",
                    message="更新安装程序未能启动。",
                    error=failure["error"],
                )
                return

            self._set_update_status(
                state="restarting",
                phase="restarting",
                completed=1,
                total=1,
                percent=100,
                message="更新已准备完成，软件正在重启",
                error=None,
            )
            if self._window is not None:
                threading.Timer(0.35, self._close_window_for_update).start()
        except Exception as exc:
            failure = self._failed(exc, "14003")
            self._set_update_status(
                state="failed",
                phase="failed",
                message="更新过程中发生意外错误。",
                error=failure["error"],
            )
        finally:
            self._update_lock.release()

    def install_update(self, version: str) -> dict[str, object]:
        if not self._update_lock.acquire(blocking=False):
            return self._ok(
                {"accepted": False}, message="正在处理上一个更新操作。"
            )
        try:
            view = self._lifecycle.refresh()
            if view.state.value != "STOPPED" or view.online or view.can_stop:
                raise ValueError("请先在运行中心停止机器人，再安装新版本。")
            if not getattr(sys, "frozen", False):
                raise ValueError("开发运行方式不执行自更新，请使用正式安装版验证。")
            channel = self._load_update_channel()
            if channel is None or not channel.enabled:
                raise ValueError("当前未启用在线更新。")
            release = self._available_update
            if release is None or release.version != str(version).strip():
                release = check_for_update(channel, current_version=__version__)
            if release is None or release.version != str(version).strip():
                raise ValueError("目标版本已不可用，请重新检查更新。")
            self._set_update_status(
                state="running",
                phase="starting",
                completed=0,
                total=release.package_size,
                percent=0,
                message="正在连接更新服务",
                error=None,
            )
            threading.Thread(
                target=self._run_update_install,
                args=(release, channel),
                daemon=True,
                name="wechatrebot-update",
            ).start()
            return self._ok(
                {"accepted": True, "version": release.version},
                message="已经开始下载新版本。",
            )
        except (OSError, RuntimeError, ValueError, UpdateError) as exc:
            self._update_lock.release()
            return self._failed(exc, "14003")


def run_application(install_root: str | Path) -> int:
    if os.name != "nt":
        return 2
    try:
        import webview

        brand = load_robot_settings(install_root).profile
        try:
            ensure_launcher_executable(install_root)
            provision_uninstaller(
                install_root,
                display_name=brand.display_name,
            )
            refresh_windows_shortcuts(
                install_root,
                description=f"启动{brand.display_name}",
            )
        except (OSError, RuntimeError, ValueError):
            pass
        api = ApplicationApi(install_root)
        title = f"{brand.display_name} · 机器人管理"
        window = webview.create_window(
            title,
            (_asset_root() / "index.html").resolve().as_uri(),
            width=1380,
            height=860,
            min_size=(1080, 680),
            background_color="#F3F6FA",
            text_select=True,
        )
        tray = WindowsTrayController(window, brand.display_name)
        window.events.shown += tray.start
        window.events.closing += tray.handle_window_closing
        window.events.closed += tray.stop
        api._attach_window(window, tray)
        window.expose(*(getattr(api, name) for name in PUBLIC_API_METHODS))
        webview.start(gui="edgechromium", debug=False, private_mode=True)
        return 0
    except ImportError as exc:
        raise RuntimeError("主界面组件缺失，请重新安装软件。") from exc


__all__ = ["ApplicationApi", "PUBLIC_API_METHODS", "run_application"]
