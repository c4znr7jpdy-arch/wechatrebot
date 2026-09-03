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
from .customer_config import (
    RobotSettings,
    load_provider_profile,
    load_robot_settings,
    save_provider_profile,
    save_robot_settings,
)
from .environment import WECHAT_DOWNLOAD_URL
from .error_catalog import load_error_catalog
from .lifecycle import LifecycleController
from .plugin_center import (
    PluginCenterError,
    PluginCenterService,
    launch_plugin_center,
)
from .provider import OpenAIProviderProfile
from .schedules import ScheduleService
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
from .wechat_path import validate_wechat_executable


PUBLIC_API_METHODS = (
    "bootstrap",
    "get_status",
    "check_environment",
    "start_robot",
    "stop_robot",
    "select_wechat",
    "open_wechat_download",
    "get_features",
    "get_feature",
    "set_feature_enabled",
    "save_feature",
    "reload_feature",
    "open_feature_center",
    "get_conversations",
    "get_messages",
    "delete_conversation",
    "delete_history_before",
    "export_history",
    "get_schedules",
    "set_schedule_enabled",
    "update_schedule_time",
    "delete_schedule",
    "get_activity",
    "save_brand",
    "save_provider",
    "open_install_folder",
    "check_update",
    "install_update",
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
        self._schedules = ScheduleService(self._install_root)
        self._catalog = load_error_catalog(self._install_root)
        self._window: Any | None = None
        self._action_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._update_channel: UpdateChannel | None = None
        self._available_update: UpdateRelease | None = None
        self._update_result = consume_update_result(self._install_root)
        threading.Thread(
            target=cleanup_stale_update_workdirs,
            args=(self._install_root,),
            daemon=True,
        ).start()

    def _attach_window(self, window: Any) -> None:
        self._window = window

    @staticmethod
    def _ok(data: Any = None, *, message: str = "") -> dict[str, object]:
        return {"ok": True, "data": data, "message": message}

    def _failed(self, exc: BaseException, code: str = "19000") -> dict[str, object]:
        definition = self._catalog.get(code)
        self._lifecycle.events.emit(
            "APP_ERROR",
            "failed",
            code=definition.code,
            message=str(exc),
        )
        return {
            "ok": False,
            "error": definition.to_dict(),
        }

    def bootstrap(self) -> dict[str, object]:
        try:
            settings = load_robot_settings(self._install_root)
            provider = load_provider_profile(self._install_root)
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
                        "api_base": provider.api_base if provider else "",
                        "model": provider.model if provider else "",
                        "api_key_configured": bool(provider and provider.api_key),
                    },
                    "update": {
                        "enabled": bool(channel and channel.enabled),
                        "channel": channel.channel if channel else "stable",
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

    def get_feature(self, feature_id: str) -> dict[str, object]:
        try:
            return self._ok(self._features.detail(feature_id))
        except (OSError, ValueError, PluginCenterError) as exc:
            return self._failed(exc, "13003")

    def set_feature_enabled(
        self, feature_id: str, enabled: bool
    ) -> dict[str, object]:
        try:
            return self._ok(self._features.set_enabled(feature_id, bool(enabled)))
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

    def open_feature_center(self) -> dict[str, object]:
        try:
            launch_plugin_center(self._install_root)
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

    def get_conversations(self, filters: dict[str, Any] | None = None) -> dict[str, object]:
        try:
            values = filters or {}
            return self._ok(
                self._history.conversations(
                    kind=str(values.get("kind") or "all"),
                    search=str(values.get("search") or ""),
                    start_time=self._date_timestamp(values.get("start")),
                    end_time=self._date_timestamp(values.get("end"), end=True),
                )
            )
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def get_messages(self, conversation_id: str) -> dict[str, object]:
        try:
            return self._ok(list(self._history.messages(conversation_id)))
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def delete_conversation(self, conversation_id: str) -> dict[str, object]:
        try:
            count = self._history.delete_conversation(conversation_id)
            return self._ok({"deleted": count})
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def delete_history_before(self, value: str) -> dict[str, object]:
        try:
            timestamp = self._date_timestamp(value)
            if timestamp is None:
                raise ValueError("请选择清理日期。")
            count = self._history.delete_before(timestamp)
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
            target = self._history.export(selected, format=suffix)
            return self._ok({"path": str(target)})
        except (OSError, ValueError) as exc:
            return self._failed(exc)

    def get_schedules(self) -> dict[str, object]:
        try:
            return self._ok(self._schedules.list())
        except (OSError, ValueError, sqlite3.Error) as exc:
            return self._failed(exc)

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

    def save_brand(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            current = load_robot_settings(self._install_root)
            from .brand import BrandProfile

            profile = BrandProfile(
                display_name=str(values.get("display_name") or ""),
                short_name=str(values.get("short_name") or ""),
                persona_name=str(values.get("persona_name") or ""),
                avatar_path=current.profile.avatar_path,
                welcome_text=str(values.get("welcome_text") or ""),
            )
            saved = save_robot_settings(
                self._install_root,
                RobotSettings(
                    profile=profile,
                    admin_id=str(values.get("admin_id") or ""),
                ),
            )
            return self._ok(
                {**saved.profile.to_dict(), "admin_id": saved.admin_id},
                message="机器人名称和回复身份已统一更新。",
            )
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def save_provider(self, values: dict[str, Any]) -> dict[str, object]:
        try:
            current = load_provider_profile(self._install_root)
            api_key = str(values.get("api_key") or "").strip()
            if not api_key and current is not None:
                api_key = current.api_key
            saved = save_provider_profile(
                self._install_root,
                OpenAIProviderProfile(
                    api_key=api_key,
                    api_base=str(values.get("api_base") or ""),
                    model=str(values.get("model") or ""),
                ),
            )
            return self._ok(
                {
                    "configured": True,
                    "api_base": saved.api_base,
                    "model": saved.model,
                    "api_key_configured": bool(saved.api_key),
                },
                message="回复模型已保存，下次启动后应用。",
            )
        except (OSError, ValueError) as exc:
            return self._failed(exc, "15001")

    def open_install_folder(self) -> dict[str, object]:
        try:
            if os.name == "nt":
                os.startfile(self._install_root)  # type: ignore[attr-defined]
            return self._ok({"opened": True})
        except OSError as exc:
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

    def install_update(self, version: str) -> dict[str, object]:
        if not self._update_lock.acquire(blocking=False):
            return self._ok({"accepted": False}, message="正在处理上一个更新操作。")
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
            try:
                prepared = prepare_update(release, channel, self._install_root)
            except (OSError, ValueError, UpdateError) as exc:
                return self._failed(exc, "14002")
            plan = create_update_plan(prepared, self._install_root)
            launch_update_helper(plan)
            if self._window is not None:
                threading.Timer(0.35, self._window.destroy).start()
            return self._ok(
                {"accepted": True, "version": release.version},
                message="更新已准备完成，软件将自动重启。",
            )
        except (OSError, ValueError, UpdateError) as exc:
            return self._failed(exc, "14003")
        finally:
            self._update_lock.release()


def run_application(install_root: str | Path) -> int:
    if os.name != "nt":
        return 2
    try:
        import webview

        api = ApplicationApi(install_root)
        brand = load_robot_settings(install_root).profile
        window = webview.create_window(
            f"{brand.display_name} · 机器人管理",
            (_asset_root() / "index.html").resolve().as_uri(),
            width=1380,
            height=860,
            min_size=(1080, 680),
            background_color="#F3F6FA",
            text_select=True,
        )
        api._attach_window(window)
        window.expose(*(getattr(api, name) for name in PUBLIC_API_METHODS))
        webview.start(gui="edgechromium", debug=False, private_mode=True)
        return 0
    except ImportError as exc:
        raise RuntimeError("主界面组件缺失，请重新安装软件。") from exc


__all__ = ["ApplicationApi", "PUBLIC_API_METHODS", "run_application"]
