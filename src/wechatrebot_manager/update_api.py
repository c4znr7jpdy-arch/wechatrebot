"""Background update checks, progress and installation lifecycle."""
from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sys
import threading

from . import __version__
from .json_io import write_json_atomic
from .payload import PayloadError
from .updates import (
    MandatoryUpdateUnavailable,
    PreparedUpdate,
    UpdateChannel,
    UpdateError,
    UpdateRelease,
    check_for_update,
    create_update_plan,
    launch_update_helper,
    load_update_channel,
    installed_release_identity,
    is_newer_update,
    prepare_update,
    assert_update_runtime_stopped,
)
from .update_errors import UpdateFailure


class UpdateApiMixin:
    @staticmethod
    def _release_id(release: UpdateRelease) -> str:
        identity = (release.version, release.build, release.package_url,
                    release.package_sha256, release.package_size,
                    release.package_kind, release.base_fingerprint)
        return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()

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
            "release_id": UpdateApiMixin._release_id(release),
            "version": release.version,
            "title": release.title,
            "notes": list(release.notes),
            "published_at": release.published_at,
            "package_size": release.package_size,
            "package_kind": release.package_kind,
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
        if getattr(self, "_update_installing", False):
            return self._ok({"busy": True}, message="更新正在安装，请稍候。")
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
            current_version, current_build = installed_release_identity(self._install_root, __version__)
            release = check_for_update(
                channel,
                current_version=current_version,
                current_build=current_build,
                install_root=self._install_root,
            )
            previous = self._available_update
            if (previous is not None and (release is None
                    or self._release_id(previous) != self._release_id(release))):
                self._update_confirmation_token = ""
                self._prepared_update = None
                self._set_update_status(
                    state="idle", phase="idle", confirmation_token="",
                    release_id=self._release_id(release) if release else "",
                    message="更新信息已变化，请重新下载。", error=None,
                )
            self._available_update = release
            self._mandatory_update = bool(release and release.mandatory)
            self._update_logger.write(
                "check_completed",
                state="available" if release else "current",
                version=release.version if release else current_version,
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
                self._prepared_update = None
                self._update_confirmation_token = ""
                self._set_update_status(
                    state="idle", phase="idle", release_id="", confirmation_token="",
                    message="当前没有可用的更新包。", error=None,
                )
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

    def _prepared_matches(self, release: UpdateRelease) -> bool:
        prepared = self._prepared_update
        return bool(
            prepared is not None
            and self._release_id(prepared.release) == self._release_id(release)
            and prepared.archive_path.is_file()
            and prepared.payload_root.is_dir()
        )

    def _mark_update_ready(self, release: UpdateRelease) -> None:
        self._update_confirmation_token = secrets.token_hex(32)
        self._set_update_status(
            state="ready", phase="ready_to_install", completed=1, total=1,
            percent=100, message="更新已下载并校验，等待确认关闭机器人和微信后安装",
            error=None, release_id=self._release_id(release),
            version=release.version,
            confirmation_token=self._update_confirmation_token,
        )

    def _run_update_download(self, release: UpdateRelease, channel: UpdateChannel) -> None:
        try:
            self._prepared_update = prepare_update(
                release, channel, self._install_root,
                progress_callback=self._update_progress,
            )
            self._mark_update_ready(release)
            self._update_logger.write(
                "download_ready", state="ready_to_install", version=release.version,
            )
        except Exception as exc:
            self._prepared_update = None
            self._update_confirmation_token = ""
            code = (
                "UPD-1003" if isinstance(exc, (PayloadError, UpdateError))
                else "UPD-1015" if isinstance(exc, PermissionError) else "UPD-1002"
            )
            failure = self._update_failed(
                exc if isinstance(exc, UpdateFailure) else UpdateFailure(code, str(exc))
            )
            self._set_update_status(
                state="failed", phase="failed", confirmation_token="",
                message="更新下载或校验未能完成。", error=failure["error"],
            )
        finally:
            self._update_lock.release()

    def _run_update_install(self, prepared: PreparedUpdate) -> None:
        succeeded = False
        try:
            current_version, current_build = installed_release_identity(self._install_root, __version__)
            if not is_newer_update(
                prepared.release.version, prepared.release.build,
                current_version=current_version, current_build=current_build,
            ):
                raise UpdateFailure("UPD-1003", "待安装更新不高于当前安装版本，请重新检查更新。")
            # Verify the staged files and installation baseline before closing anything.
            plan = create_update_plan(
                prepared, self._install_root, current_version=current_version,
            )
            self._set_update_status(
                state="running", phase="stopping_services", percent=100,
                message="正在强制关闭机器人程序和所有微信窗口", error=None,
            )
            try:
                self._lifecycle.force_stop_for_update()
                assert_update_runtime_stopped(prepared.payload_root, self._install_root)
            except UpdateFailure:
                raise
            except Exception as exc:
                name = str(getattr(exc, "service_name", ""))
                code = "UPD-1014" if name == "wechat" else (
                    "UPD-1006" if "bridge" in name.casefold() else "UPD-1011"
                )
                raise UpdateFailure(code, str(exc)) from exc
            self._set_update_status(
                state="running", phase="launching", percent=100,
                message="正在安装更新并启动新版客户端", error=None,
            )
            launch_update_helper(plan)
            self._set_update_status(
                state="restarting", phase="restarting", completed=1, total=1,
                percent=100, message="更新已准备完成，客户端正在重启", error=None,
            )
            succeeded = True
            if self._window is not None:
                threading.Timer(0.35, self._close_window_for_update).start()
        except Exception as exc:
            code = "UPD-1003" if isinstance(exc, (PayloadError, UpdateError)) else "UPD-1007"
            if code == "UPD-1003":
                self._prepared_update = None
            failure = self._update_failed(
                exc if isinstance(exc, UpdateFailure) else UpdateFailure(code, str(exc))
            )
            self._set_update_status(
                state="failed", phase="failed", confirmation_token="",
                message=failure["error"]["message"], error=failure["error"],
            )
        finally:
            if not succeeded:
                self._update_installing = False
            self._action_lock.release()
            self._update_lock.release()

    def install_update(self, version: str, confirmation_token: str = "") -> dict[str, object]:
        if not self._update_lock.acquire(blocking=False):
            return self._ok({"accepted": False, "busy": True}, message="正在处理更新操作。")
        action_locked = False
        handed_off = False
        try:
            if getattr(self, "_update_installing", False):
                return self._ok({"accepted": False, "busy": True}, message="更新正在安装，请稍候。")
            if not getattr(sys, "frozen", False):
                raise UpdateFailure("UPD-1013", "开发运行方式不执行自更新。")
            release = self._available_update
            token = getattr(self, "_update_confirmation_token", "")
            with self._update_status_lock:
                ready = self._update_status.get("state") == "ready"
            if (release is None or release.version != str(version).strip()
                    or not self._prepared_matches(release) or not ready
                    or not token or not isinstance(confirmation_token, str)
                    or not confirmation_token.isascii()
                    or not secrets.compare_digest(confirmation_token, token)):
                raise UpdateFailure(
                    "UPD-1003", "安装确认已失效或更新文件尚未准备完成。",
                    public_message="请重新打开更新，下载完成后确认安装。",
                )
            if not self._action_lock.acquire(blocking=False):
                return self._ok({"accepted": False, "busy": True}, message="机器人正在执行其他操作，请稍后确认安装。")
            action_locked = True
            self._update_installing = True
            self._update_confirmation_token = ""
            self._set_update_status(
                state="running", phase="prepare", percent=100,
                version=release.version,
                release_id=self._release_id(release), confirmation_token="",
                message="正在复核更新文件", error=None,
            )
            threading.Thread(
                target=self._run_update_install, args=(self._prepared_update,),
                daemon=True, name="wechatrebot-update",
            ).start()
            handed_off = True
            return self._ok({"accepted": True, "version": release.version}, message="已确认关闭机器人和微信，正在安装。")
        except (OSError, RuntimeError, ValueError, UpdateError, UpdateFailure) as exc:
            self._update_installing = False
            failure = self._update_failed(
                exc if isinstance(exc, UpdateFailure) else UpdateFailure("UPD-1013", str(exc))
            )
            if action_locked:
                self._set_update_status(
                    state="failed", phase="failed", confirmation_token="",
                    message="安装任务未能启动，请重试更新。", error=failure["error"],
                )
            return failure
        finally:
            if not handed_off:
                if action_locked:
                    self._action_lock.release()
                self._update_lock.release()

    def download_update(self, version: str, release_id: str = "") -> dict[str, object]:
        if not self._update_lock.acquire(blocking=False):
            return self._ok({"accepted": False, "busy": True})
        handed_off = False
        started = False
        try:
            if not getattr(sys, "frozen", False):
                raise UpdateFailure("UPD-1013", "开发运行方式不执行自更新。")
            if getattr(self, "_update_installing", False):
                return self._ok({"accepted": False, "busy": True})
            release = self._available_update
            if (release is None or release.version != str(version).strip()
                    or (release_id and release_id != self._release_id(release))):
                raise UpdateFailure("UPD-1001", "目标更新已变化。", public_message="更新信息已变化，请重新检查更新。")
            channel = self._load_update_channel()
            if channel is None or not channel.enabled:
                detail = getattr(self, "_update_channel_error", "") or "当前未启用在线更新。"
                raise UpdateFailure("UPD-1013", detail)
            if self._prepared_matches(release):
                self._mark_update_ready(release)
                return self._ok({"accepted": True, "version": release.version, "ready": True})
            self._update_confirmation_token = ""
            self._set_update_status(
                state="running", phase="starting", completed=0,
                version=release.version,
                total=release.package_size, percent=0,
                message="正在准备下载更新", error=None,
                release_id=self._release_id(release), confirmation_token="",
            )
            started = True
            threading.Thread(
                target=self._run_update_download, args=(release, channel),
                daemon=True, name="wechatrebot-update-download",
            ).start()
            handed_off = True
            return self._ok({"accepted": True, "version": release.version})
        except (OSError, RuntimeError, ValueError, UpdateError, UpdateFailure) as exc:
            failure = self._update_failed(
                exc if isinstance(exc, UpdateFailure) else UpdateFailure("UPD-1013", str(exc))
            )
            if started:
                self._set_update_status(
                    state="failed", phase="failed", confirmation_token="",
                    message="下载任务未能启动，请重试更新。", error=failure["error"],
                )
            return failure
        finally:
            if not handed_off:
                self._update_lock.release()
