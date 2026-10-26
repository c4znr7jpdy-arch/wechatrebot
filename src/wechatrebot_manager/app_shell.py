from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any
import webbrowser
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

from . import __version__
from .chat_api import ChatApiMixin
from .activity_api import ActivityApiMixin
from .schedule_api import ScheduleApiMixin
from .model_api import AnalysisModelBindingError, ModelApiMixin
from .update_api import UpdateApiMixin
from .chat_history import ChatHistoryService
from .contact_directory import ContactDirectoryService
from .group_directory import GroupDirectoryService, GroupDirectoryError
from .customer_config import (
    ProviderService,
    load_provider_profile,
    load_provider_services,
    load_robot_settings,
    load_ui_theme,
    save_ui_theme,
)
from .customer_maintenance import CustomerMaintenanceService, MaintenanceError
from .environment import WECHAT_DOWNLOAD_URL
from .error_catalog import load_error_catalog, classify_error
from .experience import ExperienceError, ExperienceService
from .core_settings import RobotCoreSettingsService
from .group_policy import GroupPolicyError, GroupPolicyService
from .lifecycle import LifecycleController
from .licensing.api import LicensingApiMixin
from .licensing.protocol import LicenseError
from .payload import PayloadError
from .private_policy import PrivateChatPolicyService, PrivatePolicyError
from .plugin_center import (
    ConfigConflictError,
    PluginCenterError,
    PluginCenterService,
    _handler as plugin_center_handler,
    launch_plugin_center,
)
from .provider import DEFAULT_PROVIDER_API_BASE, DEFAULT_PROVIDER_MODEL, MODEL_PROVIDER_PRESETS
from .response_trace import ResponseTraceService
from .retired_plugins import purge_retired_plugins
from .schedules import ScheduleService
from .legacy_task_migration import has_pending_migrations
from .group_analysis_task_migration import has_pending_group_analysis_migration
from .updates import (
    UpdateChannel,
    UpdateRelease,
    cleanup_stale_update_workdirs,
    consume_update_result,
    recover_interrupted_update,
)
from .update_errors import UpdateFailure
from .update_logger import UpdateLogger
from .shortcuts import refresh_windows_shortcuts
from .uninstaller import launch_installed_uninstaller, provision_uninstaller
from .installed_entries import ensure_launcher_executable
from .wechat_path import validate_wechat_executable
from .windows_tray import WindowsTrayController
from .windows_titlebar import apply_window_theme
from .ui_skins import save_ui_skin


PUBLIC_API_METHODS = (
    "bootstrap",
    "save_theme",
    "sync_window_theme",
    "save_skin",
    "get_status",
    "get_license_status",
    "get_trial_status",
    "activate_free_trial",
    "prepare_license_device",
    "preview_activation_code",
    "redeem_activation_code",
    "refresh_license",
    "connect_license_wechat",
    "bind_license_account",
    "get_license_contact",
    "check_environment",
    "start_robot",
    "stop_robot",
    "force_stop_robot",
    "select_wechat",
    "open_wechat_download",
    "get_features",
    "get_robot_core",
    "save_robot_core",
    "request_robot_core",
    "get_feature",
    "save_feature",
    "reload_feature",
    "get_feature_page",
    "request_feature_page",
    "open_feature_center",
    "get_conversations",
    "get_groups",
    "get_group_members",
    "get_group_qrcode",
    "get_group_policy",
    "preview_group_policy",
    "save_group_policy",
    "get_contacts",
    "get_contact_detail",
    "get_contact_network_profile",
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
    "get_legacy_schedule_migrations",
    "preview_legacy_schedule_migration",
    "migrate_legacy_schedule",
    "cancel_legacy_schedule_migration",
    "preview_group_analysis_migration",
    "migrate_group_analysis",
    "cancel_group_analysis_migration",
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
    "record_ui_error",
    "export_activity_diagnostics",
    "get_onboarding_readiness",
    "preview_experience",
    "save_memory_settings",
    "save_emotion_settings",
    "complete_onboarding",
    "get_providers",
    "get_web_search_settings",
    "save_web_search_settings",
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
    "download_update",
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


class ApplicationApi(ActivityApiMixin, LicensingApiMixin, ChatApiMixin, ScheduleApiMixin, ModelApiMixin, UpdateApiMixin):
    def __init__(self, install_root: str | Path, *, development: bool = False) -> None:
        self._development = development
        self._install_root = Path(install_root).expanduser().resolve()
        self._build_number = self._installed_build_number(self._install_root)
        self._update_logger = UpdateLogger(self._install_root)
        self._startup_update_id = os.environ.get("WECHATREBOT_UPDATE_ID", "").strip()
        self._startup_update_from = os.environ.get("WECHATREBOT_UPDATE_FROM", "").strip()
        self._startup_update_to = os.environ.get("WECHATREBOT_UPDATE_TO", "").strip()
        purge_retired_plugins(self._install_root)
        self._lifecycle = LifecycleController(self._install_root)
        self._features = PluginCenterService(self._install_root)
        self._history = ChatHistoryService(
            self._install_root,
            local_app_data=os.environ.get("LOCALAPPDATA"),
        )
        self._contacts = ContactDirectoryService(self._install_root)
        self._groups = GroupDirectoryService(self._install_root)
        self._schedules = ScheduleService(self._install_root, account_hint=self._history_account_id)
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
        self._feature_page_server: ThreadingHTTPServer | None = None
        self._feature_page_thread: threading.Thread | None = None
        self._history_group_members: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
        self._action_lock = threading.Lock()
        self._ui_error_lock = threading.Lock()
        self._ui_error_times: list[float] = []
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
        self._update_channel_error = ""
        self._available_update: UpdateRelease | None = None
        self._prepared_update = None
        self._install_requested = ""
        self._mandatory_update = False
        self._update_result = None
        if not development:
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

    @staticmethod
    def _installed_build_number(install_root: Path) -> int:
        manifest_path = install_root / "config" / "installed-payload.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            value = payload.get("release_build") if isinstance(payload, dict) else None
            return max(0, int(value)) if value is not None and not isinstance(value, bool) else 0
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return 0

    def _runtime_online(self) -> bool:
        return bool(getattr(self._lifecycle.refresh(), "online", False))

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

    def _ensure_feature_page_server(self) -> ThreadingHTTPServer:
        server = getattr(self, "_feature_page_server", None)
        if server is not None:
            return server
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            plugin_center_handler(self._features, manager_assets=_asset_root()),
        )
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name="wechatrebot-feature-page",
        )
        thread.start()
        self._feature_page_server = server
        self._feature_page_thread = thread
        return server

    def _close_feature_page_server(self) -> None:
        server = getattr(self, "_feature_page_server", None)
        self._feature_page_server = None
        self._feature_page_thread = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def get_feature_page(self, feature_id: str) -> dict[str, object]:
        try:
            detail = self._features.detail(str(feature_id or ""))
            # The engine probe used by the feature catalog can briefly lag
            # behind the lifecycle probe while services are settling. The run
            # center's online state is authoritative for this gate; the page
            # request itself still reports a concrete API failure if the
            # management endpoint cannot be reached.
            if not detail.get("engine_online") and not self._runtime_online():
                raise PluginCenterError("专属配置页需要机器人在线，请先在运行中心启动机器人。")
            if not detail.get("enabled"):
                raise PluginCenterError("这项功能尚未加载，请重启机器人后再打开专属配置页。")
            pages = detail.get("pages") or ()
            if not pages:
                raise PluginCenterError("当前功能没有专属配置页。")
            content_path = "/plugin-center?" + urlencode({"feature": feature_id, "embedded": "1"})
            server = self._ensure_feature_page_server()
            base = f"http://127.0.0.1:{server.server_port}"
            return self._ok({"url": f"{base}{content_path}", "origin": base})
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "13003")

    def request_feature_page(
        self,
        feature_id: str,
        method: str,
        endpoint: str,
        payload: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> object:
        try:
            normalized_method = str(method or "").upper()
            if normalized_method not in {"GET", "POST"}:
                raise PluginCenterError("专属配置页请求方式无效。")
            upstream_path = self._features.extension_path(
                str(feature_id or ""), str(endpoint or "")
            )
            if normalized_method == "GET" and params:
                query = urlencode(
                    {str(key): str(value) for key, value in params.items() if value is not None}
                )
                if query:
                    upstream_path = f"{upstream_path}?{query}"
            body = None
            if normalized_method == "POST":
                body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            status, _headers, raw = self._features.engine.raw_request(
                normalized_method,
                upstream_path,
                body=body,
                content_type="application/json",
                accept_language="zh-CN",
            )
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PluginCenterError("专属配置页返回了无法识别的数据。") from exc
            if isinstance(result, dict) and result.get("status") == "error":
                raise PluginCenterError(str(result.get("message") or "专属配置页请求失败。"))
            if (
                isinstance(result, dict)
                and result.get("status") == "ok"
                and "data" in result
            ):
                result = result["data"]
            if status >= 400:
                message = result.get("message") if isinstance(result, dict) else ""
                raise PluginCenterError(str(message or "专属配置页请求失败。"))
            return self._ok(result)
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "13003")

    def _close_window_for_update(self) -> None:
        self._close_feature_page_server()
        if self._tray is not None:
            self._tray.permit_programmatic_close()
        if self._window is not None:
            self._window.destroy()

    @staticmethod
    def _ok(data: Any = None, *, message: str = "") -> dict[str, object]:
        return {"ok": True, "data": data, "message": message}

    def _failed(self, exc: BaseException, code: str = "19000") -> dict[str, object]:
        operation = sys._getframe(1).f_code.co_name
        code = classify_error(exc, code, operation)
        definition = self._catalog.get(code)
        error = definition.to_dict()
        # Policy validation errors are written for users. Never expose raw
        # filesystem/transport errors, which can contain paths or credentials.
        if isinstance(exc, (GroupPolicyError, PrivatePolicyError, ConfigConflictError)):
            error["detail"] = str(exc)
        if isinstance(exc, AnalysisModelBindingError):
            error["message"] = str(exc)
        event_id, saved = self._record_error(exc, definition.code, operation=operation)
        error.update(event_id=event_id, recorded=saved)
        return {
            "ok": False,
            "error": error,
        }

    def save_theme(self, theme: str) -> dict[str, object]:
        try:
            save_ui_theme(self._install_root, theme)
            return self._ok({"theme": theme})
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def sync_window_theme(self, theme: str, background: str, foreground: str) -> dict[str, object]:
        try:
            applied = apply_window_theme(self._window, theme, background, foreground)
            return self._ok({"applied": applied})
        except ValueError as exc:
            return self._failed(exc, "15001")

    def save_skin(self, skin: str) -> dict[str, object]:
        try:
            save_ui_skin(self._install_root, skin)
            return self._ok({"skin": skin})
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def bootstrap(self) -> dict[str, object]:
        try:
            started = time.perf_counter()
            settings = load_robot_settings(self._install_root)
            providers = load_provider_services(self._install_root)
            provider = load_provider_profile(self._install_root, services=providers)
            channel = self._load_update_channel()
            configured = time.perf_counter()
            status = self._status_data()
            checked = time.perf_counter()
            experience = self._experience_service().snapshot()
            experienced = time.perf_counter()
            readiness = self._onboarding_readiness(view=status, provider_services=providers)
            ready = time.perf_counter()
            return self._ok(
                {
                    "version": __version__,
                    "install_root": str(self._install_root),
                    "status": status,
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
                    "experience": experience,
                    "readiness": readiness,
                    "startup_timing_ms": {
                        "configuration": round((configured - started) * 1000, 1),
                        "status": round((checked - configured) * 1000, 1),
                        "experience": round((experienced - checked) * 1000, 1),
                        "readiness": round((ready - experienced) * 1000, 1),
                        "total": round((ready - started) * 1000, 1),
                    },
                    "provider_presets": [dict(item) for item in MODEL_PROVIDER_PRESETS],
                    "update": {
                        "enabled": bool(channel and channel.enabled),
                        "channel": channel.channel if channel else "stable",
                        "build": getattr(self, "_build_number", 0),
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
            return self._ok(self._status_data())
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def _status_data(self) -> dict:
        view = self._lifecycle.refresh()
        result = view.to_dict()
        account = self._offline_history_account(view)
        if account:
            result.update(history_account=account, history_offline=True)
        return result

    def check_environment(self) -> dict[str, object]:
        try:
            return self._ok(self._lifecycle.check_environment().to_dict())
        except (OSError, ValueError) as exc:
            return self._failed(exc, "11004")

    def start_robot(self) -> dict[str, object]:
        if self._mandatory_update:
            return self._update_failed(
                UpdateFailure("UPD-1016", "当前版本已低于最低支持版本。")
            )
        try:
            self._check_license_start()
        except LicenseError as exc:
            return self._license_failed(exc)
        return self._queue_robot_action("start", "正在启动机器人。", "13001")

    def stop_robot(self) -> dict[str, object]:
        return self._queue_robot_action("request_stop", "正在停止机器人。")

    def force_stop_robot(self) -> dict[str, object]:
        return self._queue_robot_action("force_stop", "正在强制停止机器人。")

    def _queue_robot_action(self, action: str, message: str, error_code: str = "19000") -> dict[str, object]:
        if not self._action_lock.acquire(blocking=False):
            return self._ok({"accepted": False}, message="正在处理上一个操作。")
        if action in {"start", "connect_wechat"}:
            try:
                if (has_pending_migrations(self._install_root)
                        or has_pending_group_analysis_migration(self._install_root)):
                    self._action_lock.release()
                    return self._ok({"accepted": False}, message="请先在定时任务页完成中断的旧任务迁移，再启动机器人。")
            except (OSError, ValueError, sqlite3.Error, PayloadError):
                self._action_lock.release()
                return self._ok({"accepted": False}, message="旧任务迁移记录暂时无法确认，请先检查后再启动。")

        def worker() -> None:
            try:
                getattr(self._lifecycle, action)()
            except LicenseError as exc:
                self._lifecycle.events.emit("APP_ERROR", "failed", code=f"LIC-{exc.code}", message=str(exc))
            except Exception as exc:  # pragma: no cover - final UI safety net
                self._failed(exc, error_code)
            finally:
                self._action_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return self._ok({"accepted": True}, message=message)

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
        baseline: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        try:
            return self._ok(
                self._features.save(
                    feature_id, config, revision or None, baseline
                )
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


    def get_response_traces(self) -> dict[str, object]:
        try:
            service = getattr(self, "_response_traces", None)
            if service is None:
                service = ResponseTraceService(self._install_root)
            return self._ok(service.recent())
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def _onboarding_readiness(
        self, *, view: dict[str, object] | None = None,
        provider_services: tuple[ProviderService, ...] | None = None,
    ) -> dict[str, object]:
        """Build a read-only first-use checklist from current local state."""
        if view is None:
            view = self._lifecycle.refresh().to_dict()
        environment = view.get("environment") if isinstance(view, dict) else {}
        runtime = view.get("runtime") if isinstance(view, dict) else {}
        environment = environment if isinstance(environment, dict) else {}
        runtime = runtime if isinstance(runtime, dict) else {}
        provider = load_provider_profile(self._install_root, services=provider_services)
        account_id = str(runtime.get("wechat_identity") or "").strip()
        if runtime.get("account_status_available") is False:
            account_id = ""
        directory_service = getattr(self, "_groups", None)
        groups = {"items": [], "stale": True, "account_id": ""}
        if account_id and directory_service is not None:
            try:
                candidate = directory_service.list(False)
                if isinstance(candidate, dict) and candidate.get("account_id") == account_id:
                    groups = candidate
            except (OSError, GroupDirectoryError):
                # Directory availability is a checklist result, not a prerequisite
                # for opening the manager and starting its stopped bridge.
                groups = {"items": [], "stale": True, "account_id": ""}
        group_items = groups.get("items") if isinstance(groups, dict) else []
        group_items = group_items if isinstance(group_items, list) else []
        group_stale = bool(groups.get("stale")) if isinstance(groups, dict) else True
        try:
            catalog = self._features.catalog() if getattr(self, "_features", None) is not None else {}
            feature_items = catalog.get("plugins") if isinstance(catalog, dict) else []
        except (OSError, ValueError, PluginCenterError):
            feature_items = []
        feature_items = feature_items if isinstance(feature_items, list) else []
        online = bool(view.get("online"))
        items = [
            {
                "id": "wechat",
                "label": "微信环境",
                "status": "ready" if environment.get("ready") else "needs_config",
                "detail": "已找到兼容的微信客户端。" if environment.get("ready") else str(environment.get("message") or "请选择兼容的微信客户端。"),
                "next_step": "继续检查" if environment.get("ready") else "选择微信客户端",
            },
            {
                "id": "account",
                "label": "机器人账号",
                "status": "ready" if account_id else "offline" if not online else "needs_config",
                "detail": account_id or "启动机器人并登录微信后才能确认账号。",
                "next_step": "已确认" if account_id else "启动机器人并登录微信",
            },
            {
                "id": "provider",
                "label": "回复模型",
                "status": "ready" if provider is not None else "needs_config",
                "detail": provider.model if provider is not None else "还没有配置可用的聊天模型。",
                "next_step": "继续检查" if provider is not None else "配置聊天模型",
            },
            {
                "id": "groups",
                "label": "目标群聊",
                "status": "offline" if group_stale and not group_items else "ready" if group_items else "needs_config",
                "detail": "群目录暂时未更新，请连接微信后刷新。" if group_stale and not group_items else f"已读取 {len(group_items)} 个群聊。" if group_items else "读取当前账号群目录后选择至少一个目标群。",
                "next_step": "刷新群聊目录" if group_stale else "进入群聊管理选择目标群",
            },
            {
                "id": "core",
                "label": "核心功能",
                "status": "ready" if online and feature_items else "offline" if not online else "needs_config",
                "detail": "机器人核心和功能目录已可检查。" if online and feature_items else "启动机器人后检查核心功能是否可用。",
                "next_step": "继续检查" if online and feature_items else "启动机器人并检查功能",
            },
        ]
        complete = all(item["status"] == "ready" for item in items)
        return {
            "schema_version": 1,
            "readonly": True,
            "complete": complete,
            "items": items,
            "test_hint": "准备完成后，可在选定群里点名机器人测试。",
            "message": "检查只读取当前环境、账号、模型和目录，不会启动机器人、发送消息或修改群策略。",
        }

    def get_onboarding_readiness(self) -> dict[str, object]:
        try:
            return self._ok(self._onboarding_readiness())
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def get_experience(self) -> dict[str, object]:
        try:
            return self._ok(self._experience_service().snapshot())
        except (OSError, ExperienceError, PluginCenterError) as exc:
            return self._failed(exc, "15001")

    def preview_experience(self, values: dict[str, Any] | None = None) -> dict[str, object]:
        try:
            return self._ok(self._experience_service().preview(values))
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
            if not getattr(self, "_development", False):
                ensure_launcher_executable(self._install_root)
            if os.name == "nt":
                os.startfile(self._install_root)  # type: ignore[attr-defined]
            return self._ok({"opened": True})
        except OSError as exc:
            return self._failed(exc)

    def open_uninstaller(self) -> dict[str, object]:
        try:
            if getattr(self, "_development", False):
                raise ValueError("源码验证模式不执行卸载，请使用正式安装版。")
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


def _fit_window_to_work_area(window: Any) -> None:
    """Fit the default window on small/high-DPI displays before it becomes visible."""
    try:
        from System.Drawing import Point, Size  # type: ignore[import-not-found]
        from System.Windows.Forms import Screen  # type: ignore[import-not-found]

        native = window.native
        area = Screen.FromControl(native).WorkingArea
        if area.Width <= 0 or area.Height <= 0:
            return
        # WinForms has already applied DPI scaling; all values here are physical pixels.
        native.MinimumSize = Size(min(native.MinimumSize.Width, area.Width),
                                  min(native.MinimumSize.Height, area.Height))
        native.Size = Size(min(native.Width, area.Width), min(native.Height, area.Height))
        native.Location = Point(max(area.Left, min(native.Left, area.Right - native.Width)),
                                max(area.Top, min(native.Top, area.Bottom - native.Height)))
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not fit manager window to working area: %s", type(exc).__name__)


def run_application(install_root: str | Path, *, development: bool = False) -> int:
    if os.name != "nt":
        return 2
    try:
        if not development and recover_interrupted_update(install_root):
            return 0

        import webview

        brand = load_robot_settings(install_root).profile
        if not development:
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
        api = (
            ApplicationApi(install_root, development=True)
            if development else ApplicationApi(install_root)
        )
        title = "WxRebot · 机器人管理"
        if development:
            title += " [源码验证]"
        server = api._ensure_feature_page_server()
        try:
            window = webview.create_window(
                title,
                f"http://127.0.0.1:{server.server_port}/",
                width=1520,
                height=900,
                min_size=(1080, 680),
                background_color="#111827" if load_ui_theme(install_root) == "dark" else "#F3F6FA",
                text_select=True,
            )
            tray = None
            before_show = getattr(window.events, "before_show", None)
            if before_show is not None:
                before_show += lambda: _fit_window_to_work_area(window)
            window.events.shown += api.confirm_update_startup
            if not development:
                tray = WindowsTrayController(window, "WxRebot")
                window.events.shown += tray.start
                window.events.closing += tray.handle_window_closing
                window.events.closed += tray.stop
            api._attach_window(window, tray)
            window.expose(*(getattr(api, name) for name in PUBLIC_API_METHODS))
            webview.start(gui="edgechromium", debug=False, private_mode=True)
            return 0
        finally:
            close_feature_page_server = getattr(api, "_close_feature_page_server", None)
            if close_feature_page_server is not None:
                close_feature_page_server()
    except ImportError as exc:
        raise RuntimeError("主界面组件缺失，请重新安装软件。") from exc


__all__ = ["ApplicationApi", "PUBLIC_API_METHODS", "run_application"]
