"""Background update checks, progress and installation lifecycle."""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time

from . import __version__
from .json_io import write_json_atomic
from .payload import PayloadError, payload_requires_runtime_stop
from .updates import (
    MandatoryUpdateUnavailable,
    UpdateChannel,
    UpdateError,
    UpdateRelease,
    check_for_update,
    create_update_plan,
    launch_update_helper,
    load_update_channel,
    prepare_update,
)
from .update_errors import UpdateFailure


class UpdateApiMixin:
    def _load_update_channel(self) -> UpdateChannel | None:
        if getattr(self, "_development", False):
            return None
        if self._update_channel is not None:
            return self._update_channel
        try:
            self._update_channel = load_update_channel(self._install_root)
            self._update_channel_error = ""
        except (OSError, ValueError, UpdateError) as exc:
            self._update_channel_error = str(exc)
            logger = getattr(self, "_update_logger", None)
            if logger is not None:
                logger.write(
                    "configuration_failed",
                    state="failed",
                    error_code="UPD-1013",
                    message=str(exc),
                )
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
            "build": release.build,
            "minimum_supported_version": release.minimum_supported_version,
        }

    def _update_failed(self, failure: UpdateFailure) -> dict[str, object]:
        error = failure.to_public_dict()
        self._update_logger.write(
            "operation_failed",
            state="failed",
            error_code=error["code"],
            message=failure.detail,
        )
        return {"ok": False, "error": error}

    def check_update(self) -> dict[str, object]:
        if not self._update_lock.acquire(blocking=False):
            return self._ok({"busy": True}, message="正在检查新版本。")
        try:
            channel = self._load_update_channel()
            if channel is None and getattr(self, "_update_channel_error", ""):
                return self._update_failed(
                    UpdateFailure("UPD-1013", self._update_channel_error)
                )
            if channel is None or not channel.enabled:
                return self._ok(
                    {"enabled": False, "current": True, "release": None},
                    message="当前未启用在线更新。",
                )
            release = check_for_update(
                channel,
                current_version=__version__,
                current_build=getattr(self, "_build_number", 0),
            )
            self._available_update = release
            self._mandatory_update = bool(release and release.mandatory)
            self._update_logger.write(
                "check_completed",
                state="available" if release else "current",
                version=release.version if release else __version__,
            )
            return self._ok(
                {
                    "enabled": True,
                    "current": release is None,
                    "release": self._release_payload(release) if release else None,
                }
            )
        except (OSError, ValueError, UpdateError, UpdateFailure) as exc:
            if isinstance(exc, MandatoryUpdateUnavailable):
                self._mandatory_update = True
                self._available_update = None
                self._update_logger.write(
                    "mandatory_update_unavailable",
                    state="blocked",
                    error_code="UPD-1016",
                    message=str(exc),
                )
                return self._update_failed(UpdateFailure("UPD-1016", str(exc)))
            code = (
                "UPD-1013"
                if any(term in str(exc) for term in ("签名", "公钥", "配置", "通道", "格式"))
                else "UPD-1001"
            )
            self._update_logger.write(
                "check_failed", state="failed", error_code=code, message=str(exc)
            )
            return self._update_failed(
                UpdateFailure(code, str(exc))
            )
        finally:
            self._update_lock.release()

    def _set_update_status(self, **values: object) -> None:
        with self._update_status_lock:
            previous_phase = self._update_status.get("phase")
            self._update_status = {**self._update_status, **values}
            next_phase = self._update_status.get("phase")
        if next_phase != previous_phase and next_phase not in {None, "idle"}:
            logger = getattr(self, "_update_logger", None)
            if logger is not None:
                release = getattr(self, "_available_update", None)
                logger.write(
                    "phase_changed",
                    state=str(next_phase),
                    version=release.version if release else "",
                    message=str(self._update_status.get("message") or ""),
                )

    def _update_progress(self, phase: str, completed: int, total: int) -> None:
        labels = {
            "download": "正在下载新版本",
            "extract": "正在解压更新包",
            "verify": "正在校验更新文件",
            "prepare": "正在准备安装文件",
            "install": "正在替换软件文件",
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
        if completed == 1 or completed == total or percent in {10, 20, 30, 40, 50, 60, 70, 80, 90}:
            logger = getattr(self, "_update_logger", None)
            release = getattr(self, "_available_update", None)
            if logger is not None:
                logger.write(
                    "progress",
                    state=phase,
                    version=release.version if release else "",
                    details={"percent": percent, "completed": completed, "total": total},
                )

    def confirm_update_startup(self) -> None:
        update_id = self._startup_update_id
        if len(update_id) != 32 or not self._startup_update_to:
            return
        flag_path = (
            self._install_root
            / "updates"
            / f"startup-success-{update_id}.json"
        )
        try:
            write_json_atomic(
                flag_path,
                {
                    "schema_version": 1,
                    "update_id": update_id,
                    "ok": True,
                    "version": self._startup_update_to,
                },
            )
            self._update_result = {
                "schema_version": 1,
                "ok": True,
                "status": "success",
                "from_version": self._startup_update_from,
                "version": self._startup_update_to,
                "message": "更新已完成。",
            }
            self._update_logger.write(
                "startup_confirmed",
                state="success",
                version=self._startup_update_to,
            )
            journal_path = self._install_root / "updates" / "update-journal.json"
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8-sig"))
                if (
                    isinstance(journal, dict)
                    and journal.get("update_id") == update_id
                    and journal.get("state") == "awaiting_startup"
                ):
                    rollback = (
                        self._install_root
                        / str(journal.get("rollback_root") or "")
                    ).resolve()
                    rollback.relative_to((self._install_root / "updates").resolve())
                    shutil.rmtree(rollback, ignore_errors=True)
                    journal_path.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError):
                # The detached updater also performs this cleanup after seeing
                # the handshake; leave its recovery journal alone on mismatch.
                pass
        except OSError as exc:
            self._update_logger.write(
                "startup_confirmation_failed",
                state="failed",
                version=self._startup_update_to,
                error_code="UPD-1009",
                message=str(exc),
            )

    def get_update_status(self) -> dict[str, object]:
        with self._update_status_lock:
            return self._ok(dict(self._update_status))

    def _run_update_install(
        self,
        requested_version: str,
        release: UpdateRelease | None,
        channel: UpdateChannel,
        download_only: bool = False,
    ) -> None:
        try:
            if release is None:
                release = check_for_update(
                    channel,
                    current_version=__version__,
                    current_build=getattr(self, "_build_number", 0),
                )
            if release is None or release.version != requested_version:
                raise UpdateFailure("UPD-1001", "目标版本已不可用，请重新检查更新。")
            prepared = self._prepared_update
            if prepared is None or prepared.release.version != release.version:
                try:
                    prepared = prepare_update(
                        release,
                        channel,
                        self._install_root,
                        progress_callback=self._update_progress,
                    )
                except (OSError, PayloadError, ValueError, UpdateError, UpdateFailure) as exc:
                    if isinstance(exc, UpdateFailure):
                        update_failure = exc
                    elif isinstance(exc, PayloadError):
                        update_failure = UpdateFailure("UPD-1003", str(exc))
                    elif isinstance(exc, PermissionError):
                        update_failure = UpdateFailure("UPD-1015", str(exc))
                    else:
                        update_failure = UpdateFailure("UPD-1002", str(exc))
                    failure = self._update_failed(
                        update_failure
                    )
                    self._set_update_status(
                        state="failed",
                        phase="failed",
                        message="更新下载或校验未能完成。",
                        error=failure["error"],
                    )
                    return
            self._prepared_update = prepared
            install_requested = self._install_requested == release.version
            if download_only and not install_requested:
                self._set_update_status(
                    state="ready",
                    phase="ready_to_install",
                    completed=1,
                    total=1,
                    percent=100,
                    message="更新已下载并验证，可在方便时安装",
                    error=None,
                )
                self._update_logger.write(
                    "download_ready", state="ready_to_install", version=release.version
                )
                return
            self._install_requested = ""

            payload_root = getattr(prepared, "payload_root", None)
            if payload_root is None or payload_requires_runtime_stop(payload_root):
                self._set_update_status(
                    state="running",
                    phase="stopping_services",
                    completed=1,
                    total=1,
                    percent=100,
                    message="更新文件已校验，正在安全停止机器人",
                    error=None,
                )
                try:
                    self._lifecycle.request_stop()
                    deadline = time.monotonic() + 900
                    while time.monotonic() < deadline:
                        view = self._lifecycle.refresh()
                        state = getattr(view.state, "value", str(view.state))
                        if state == "STOPPED" and not view.online and not view.can_stop:
                            break
                        if state == "STOP_WAIT_WECHAT":
                            self._set_update_status(
                                state="running",
                                phase="waiting_wechat",
                                completed=1,
                                total=1,
                                percent=100,
                                message="请先退出机器人微信，检测关闭后会自动继续",
                                error=None,
                            )
                        else:
                            self._set_update_status(
                                state="running",
                                phase="stopping_services",
                                completed=1,
                                total=1,
                                percent=100,
                                message="正在安全停止微信连接和回复引擎",
                                error=None,
                            )
                        time.sleep(1)
                    else:
                        code = "UPD-1014"
                        raise UpdateFailure(code, "等待机器人微信退出超时。")
                    view = self._lifecycle.refresh()
                    state = getattr(view.state, "value", str(view.state))
                    if state != "STOPPED" or view.online or view.can_stop:
                        raise UpdateFailure("UPD-1006", "托管的微信连接未能停止。")
                except UpdateFailure:
                    raise
                except Exception as exc:
                    service_name = str(getattr(exc, "service_name", ""))
                    code = "UPD-1006" if "bridge" in service_name.casefold() else "UPD-1011"
                    raise UpdateFailure(code, str(exc)) from exc

            self._set_update_status(
                state="running",
                phase="launching",
                completed=1,
                total=1,
                percent=100,
                message="正在安装更新并启动新版管理器",
                error=None,
            )
            try:
                plan = create_update_plan(
                    prepared,
                    self._install_root,
                    current_version=__version__,
                )
                launch_update_helper(plan)
            except (OSError, ValueError, UpdateError, UpdateFailure) as exc:
                failure = self._update_failed(
                    exc if isinstance(exc, UpdateFailure) else UpdateFailure("UPD-1007", str(exc))
                )
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
            update_failure = (
                exc
                if isinstance(exc, UpdateFailure)
                else UpdateFailure("UPD-1007", str(exc))
            )
            failure = self._update_failed(update_failure)
            self._set_update_status(
                state="failed",
                phase="failed",
                message=update_failure.definition.message,
                error=failure["error"],
            )
        finally:
            self._update_lock.release()

    def install_update(self, version: str) -> dict[str, object]:
        normalized_version = str(version).strip()
        if not self._update_lock.acquire(blocking=False):
            with self._update_status_lock:
                current_status = dict(self._update_status)
                if (
                    current_status.get("state") == "running"
                    and current_status.get("phase") in {
                        "starting", "download", "extract", "verify", "prepare"
                    }
                    and self._available_update is not None
                    and self._available_update.version == normalized_version
                ):
                    self._install_requested = normalized_version
                    return self._ok(
                        {"accepted": True, "version": normalized_version},
                        message="更新准备完成后将立即安装。",
                    )
            return self._ok(
                {"accepted": False}, message="正在处理上一个更新操作。"
            )
        try:
            if not getattr(sys, "frozen", False):
                raise UpdateFailure("UPD-1013", "开发运行方式不执行自更新。")
            channel = self._load_update_channel()
            if channel is None or not channel.enabled:
                detail = getattr(self, "_update_channel_error", "") or "当前未启用在线更新。"
                raise UpdateFailure("UPD-1013", detail)
            release = self._available_update
            if release is not None and release.version != normalized_version:
                release = None
            self._set_update_status(
                state="running",
                phase="starting",
                completed=0,
                total=release.package_size if release else 0,
                percent=0,
                message="正在连接更新服务",
                error=None,
            )
            threading.Thread(
                target=self._run_update_install,
                args=(normalized_version, release, channel, False),
                daemon=True,
                name="wechatrebot-update",
            ).start()
            return self._ok(
                {"accepted": True, "version": normalized_version},
                message="正在准备更新；机器人会在更新文件校验完成后安全停止。",
            )
        except (OSError, RuntimeError, ValueError, UpdateError, UpdateFailure) as exc:
            self._update_lock.release()
            return self._update_failed(
                exc if isinstance(exc, UpdateFailure) else UpdateFailure("UPD-1013", str(exc))
            )

    def download_update(self, version: str) -> dict[str, object]:
        normalized_version = str(version).strip()
        if not getattr(sys, "frozen", False):
            return self._update_failed(UpdateFailure("UPD-1013", "开发运行方式不执行自更新。"))
        prepared = self._prepared_update
        if (
            prepared is not None
            and prepared.release.version == normalized_version
            and prepared.archive_path.is_file()
            and prepared.payload_root.is_dir()
        ):
            return self._ok({"accepted": True, "version": normalized_version, "ready": True})
        if not self._update_lock.acquire(blocking=False):
            return self._ok({"accepted": False, "busy": True})
        try:
            channel = self._load_update_channel()
            if channel is None and getattr(self, "_update_channel_error", ""):
                return self._update_failed(
                    UpdateFailure("UPD-1013", self._update_channel_error)
                )
            if channel is None or not channel.enabled:
                raise UpdateFailure("UPD-1013", "当前未启用在线更新。")
            release = self._available_update
            if release is not None and release.version != normalized_version:
                release = None
            self._install_requested = ""
            self._set_update_status(
                state="running",
                phase="starting",
                completed=0,
                total=release.package_size if release else 0,
                percent=0,
                message="正在后台准备更新",
                error=None,
            )
            self._update_logger.write(
                "background_download_started",
                state="starting",
                version=normalized_version,
            )
            threading.Thread(
                target=self._run_update_install,
                args=(normalized_version, release, channel, True),
                daemon=True,
                name="wechatrebot-update-download",
            ).start()
            return self._ok({"accepted": True, "version": normalized_version})
        except (OSError, RuntimeError, ValueError, UpdateError, UpdateFailure) as exc:
            self._update_lock.release()
            return self._update_failed(
                exc if isinstance(exc, UpdateFailure) else UpdateFailure("UPD-1013", str(exc))
            )
