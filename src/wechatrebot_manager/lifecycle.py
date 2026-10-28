from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import threading
from typing import Callable, Protocol

from .diagnostics import collect_diagnostics, diagnostic_error_messages
from .environment import (
    WechatEnvironment,
    configured_wechat_path,
    inspect_wechat_environment,
    running_wechat_processes,
    save_wechat_environment,
)
from .error_catalog import ErrorCatalog, ErrorDefinition, load_error_catalog
from .events import EventStore
from .launcher import (
    ProcessLike, ProcessSupervisor, ServiceLaunchError, ServiceSpec,
    build_service_specs, installation_services_stopped,
)
from .licensing.client import LicenseClient
from .licensing.protocol import LicenseError
from .runtime_status import RuntimeSnapshot, runtime_snapshot


class LifecycleState(StrEnum):
    STOPPED = "STOPPED"
    CHECKING = "CHECKING"
    CORE_STARTING = "CORE_STARTING"
    CORE_READY = "CORE_READY"
    CONNECTION_STARTING = "CONNECTION_STARTING"
    WAITING_WECHAT = "WAITING_WECHAT"
    VERIFYING_CONNECTION = "VERIFYING_CONNECTION"
    RUNNING = "RUNNING"
    LICENSE_READY = "LICENSE_READY"
    LICENSE_PAUSED = "LICENSE_PAUSED"
    STOP_WAIT_WECHAT = "STOP_WAIT_WECHAT"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class SupervisorLike(Protocol):
    running: list[object]

    def start(self) -> tuple[object, ...]: ...

    def stop(self, timeout_seconds: float = 15) -> None: ...

    def force_stop(self) -> None: ...

    def start_missing(self, names: tuple[str, ...]) -> tuple[object, ...]: ...

    def stop_names(self, names: tuple[str, ...], timeout_seconds: float = 15) -> None: ...


@dataclass(frozen=True, slots=True)
class LifecycleView:
    state: LifecycleState
    title: str
    message: str
    busy: bool
    online: bool
    can_start: bool
    can_stop: bool
    error: ErrorDefinition | None
    environment: WechatEnvironment
    runtime: RuntimeSnapshot

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "title": self.title,
            "message": self.message,
            "busy": self.busy,
            "online": self.online,
            "can_start": self.can_start,
            "can_stop": self.can_stop,
            "error": self.error.to_dict() if self.error is not None else None,
            "environment": self.environment.to_dict(),
            "runtime": {
                "engine_ready": self.runtime.engine_ready,
                "connection_ready": self.runtime.connection_ready,
                "wechat_identity": self.runtime.wechat_identity,
                "account_status_available": self.runtime.account_status_available,
                "hook_ready": self.runtime.hook_ready,
                "upstream_connected": self.runtime.upstream_connected,
                "heartbeat_fresh": self.runtime.heartbeat_fresh,
            },
        }


def _default_preflight(root: Path) -> tuple[str, ...]:
    return diagnostic_error_messages(collect_diagnostics(root))


class LifecycleController:
    """Owns the service lifecycle; UI code only requests transitions."""

    def __init__(
        self,
        install_root: str | Path,
        *,
        environment_checker: Callable[[Path], WechatEnvironment] | None = None,
        environment_saver: Callable[[Path, WechatEnvironment], None] | None = None,
        snapshotter: Callable[[], RuntimeSnapshot] = runtime_snapshot,
        process_probe: Callable[[], tuple[str, ...]] = running_wechat_processes,
        preflight: Callable[[Path], tuple[str, ...]] = _default_preflight,
        supervisor_factory: Callable[
            [Callable[[str, ServiceSpec], None]], SupervisorLike
        ]
        | None = None,
        catalog: ErrorCatalog | None = None,
        event_store: EventStore | None = None,
        license_client: LicenseClient | None = None,
    ) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.licenses = license_client or LicenseClient(self.install_root)
        self._environment_checker = environment_checker or (
            lambda root: inspect_wechat_environment(root)
        )
        self._environment_saver = environment_saver or save_wechat_environment
        self._snapshotter = snapshotter
        self._process_probe = process_probe
        self._preflight = preflight
        self._uses_default_supervisor = supervisor_factory is None
        self._supervisor_factory = supervisor_factory or (
            lambda progress: ProcessSupervisor(
                build_service_specs(self.install_root),
                progress_callback=progress,
                state_path=(
                    self.install_root
                    / "data"
                    / "manager"
                    / "service-state.json"
                ),
            )
        )
        self.catalog = catalog or load_error_catalog(self.install_root)
        self.events = event_store or EventStore(self.install_root)
        self._lock = threading.RLock()
        self.state = LifecycleState.STOPPED
        self._error: ErrorDefinition | None = None
        self._supervisor: SupervisorLike | None = None
        self._environment = self._environment_checker(self.install_root)
        self._runtime = self._snapshotter()
        self._running_events_emitted = False
        self._wechat_closed_confirmations = 0
        self._start_in_progress = False
        self._maintenance_processes: list[ProcessLike] = []
        self._license_pause_failed = False
        self.events.emit("APP_START", "success")
        if self._uses_default_supervisor:
            try:
                candidate = self._supervisor_factory(self._on_progress)
                adopter = getattr(candidate, "adopt", None)
                if callable(adopter) and adopter():
                    self._supervisor = candidate
                    connection_only = {item.spec.name for item in candidate.running} == {"new-hook-bridge"}
                    if connection_only and self._license_snapshot()["enabled"]:
                        self.state = LifecycleState.LICENSE_READY
                    elif not bool(getattr(candidate, "adoption_complete", False)):
                        self.state = LifecycleState.ERROR
                        self._error = self.catalog.get("13002")
                        self.events.emit(
                            "APP_ERROR",
                            "failed",
                            code=self._error.code,
                            message="检测到上次未完整启动的后台服务，请点击停止后重试。",
                        )
                    elif self._runtime.online:
                        self._mark_running()
                    elif self._runtime.login_required:
                        self.state = LifecycleState.WAITING_WECHAT
                        self._error = None
                    else:
                        self.state = LifecycleState.VERIFYING_CONNECTION
                        self._error = None
            except (OSError, ServiceLaunchError, ValueError):
                self._supervisor = None

    def _set_error(self, code: str, *, detail: str = "") -> LifecycleView:
        self.state = LifecycleState.ERROR
        self._error = self.catalog.get(code)
        self.events.emit(
            "APP_ERROR",
            "failed",
            code=self._error.code,
            message=detail or self._error.message,
        )
        return self.view()

    def _on_progress(self, event: str, spec: ServiceSpec) -> None:
        with self._lock:
            if spec.name == "astrbot":
                if event == "starting":
                    self.state = LifecycleState.CORE_STARTING
                    self.events.emit("CORE_START", "started")
                else:
                    self.state = LifecycleState.CORE_READY
                    self.events.emit("CORE_READY", "success")
            elif event == "starting":
                self.state = LifecycleState.CONNECTION_STARTING
                self.events.emit("CONNECTION_START", "started")

    def check_environment(self) -> LifecycleView:
        with self._lock:
            self.state = LifecycleState.CHECKING
            self._error = None
            self.events.emit("CHECK_WECHAT", "started")
            self._environment = self._environment_checker(self.install_root)
            if self._environment.ready:
                self.events.emit(
                    "WECHAT_READY",
                    "success",
                    details={"version": self._environment.version},
                )
                self.state = (
                    LifecycleState.RUNNING
                    if self._runtime.online
                    else LifecycleState.STOPPED
                )
            else:
                self.state = LifecycleState.STOPPED
                self._error = self.catalog.get(self._environment.code)
            return self.view()

    def _license_snapshot(self) -> dict:
        account = self._runtime.wechat_identity if self._runtime.account_status_available else ""
        return self.licenses.snapshot(account)

    def _pause_license(self, *, stop_on_error: bool = True) -> None:
        try:
            self.licenses.pause()
            self._license_pause_failed = False
        except LicenseError as exc:
            # A refresh may hold the writer lock. Until the run grant is revoked,
            # bridge tasks could still send; stop both owned services in this
            # exceptional path instead of leaving a live sender with the old grant.
            self._license_pause_failed = True
            self.events.emit("APP_ERROR", "failed", code=f"LIC-{exc.code}", message=str(exc))
            if stop_on_error and self._supervisor is not None:
                self._supervisor.stop(timeout_seconds=0)
                self._runtime = self._snapshotter()

    def connect_wechat(self) -> LifecycleView:
        """Connect WeChat for identity checks or binding, without reply plugins."""
        status = self._license_snapshot()
        if not status["enabled"]:
            return self.start()
        if not status["can_connect"]:
            raise LicenseError(status["state"], status["message"])
        owned_core = self._supervisor and any(
            item.spec.name == "astrbot" and item.process.poll() is None
            for item in self._supervisor.running
        )
        if not owned_core:
            # Also clears a grant left behind if the previous UI could not save
            # its pause. Reconnecting must never resume bridge automation.
            self.licenses.pause()
            self._license_pause_failed = False
        return self._start_services(("new-hook-bridge",))

    def start(self) -> LifecycleView:
        if not self._license_snapshot()["enabled"]:
            return self._start_services()
        with self._lock:
            if self._start_in_progress:
                return self.view()
            self._runtime = self._snapshotter()
            account = self._runtime.wechat_identity if self._runtime.account_status_available else ""
            if not self._runtime.connection_ready or not account:
                raise LicenseError("account_required", "请先连接微信并确认绑定实际账号。")
            self.licenses.authorize_run(account)
        try:
            result = self._start_services(("astrbot", "new-hook-bridge"))
            if result.state == LifecycleState.ERROR or not result.environment.ready:
                self._pause_license()
            return result
        except Exception:
            self._pause_license()
            raise

    def _start_services(self, names: tuple[str, ...] | None = None) -> LifecycleView:
        with self._lock:
            if self._start_in_progress:
                return self.view()
            existing = self._supervisor
            if existing is not None and existing.running:
                owned = {getattr(getattr(item, "spec", None), "name", "") for item in existing.running}
                if names is None or set(names) <= owned:
                    return self.refresh()
            environment = self.check_environment()
            if not environment.environment.ready:
                return environment
            try:
                if configured_wechat_path(self.install_root) != self._environment.path:
                    self._environment_saver(self.install_root, self._environment)
                preflight_errors = self._preflight(self.install_root)
                if preflight_errors:
                    return self._set_error("13001", detail="；".join(preflight_errors))
                supervisor = existing or self._supervisor_factory(self._on_progress)
                self._supervisor = supervisor
                self._start_in_progress = True
            except (OSError, ServiceLaunchError, ValueError) as exc:
                self._supervisor = existing
                self._start_in_progress = False
                return self._set_error("13001", detail=str(exc))

        # Service readiness can take over a minute on first run.  It must not
        # hold the lifecycle lock, otherwise status polling cannot show the
        # bridge launch or tell the user to log in to WeChat.
        try:
            if names is None:
                supervisor.start()
            else:
                supervisor.start_missing(names)
        except (OSError, ServiceLaunchError, ValueError) as exc:
            with self._lock:
                code = (
                    "12001"
                    if self.state == LifecycleState.CONNECTION_STARTING
                    else "13001"
                )
                if self._supervisor is supervisor and not supervisor.running:
                    self._supervisor = None
                self._start_in_progress = False
                return self._set_error(code, detail=str(exc))

        with self._lock:
            self._start_in_progress = False
            self._runtime = self._snapshotter()
            self._error = None
            if names is not None:
                return self.refresh()
            if self._runtime.online:
                self._mark_running()
            elif self._runtime.login_required:
                self.state = LifecycleState.WAITING_WECHAT
                self._error = None
            else:
                self.state = LifecycleState.VERIFYING_CONNECTION
                self._error = None
            return self.view()

    def _mark_running(self) -> None:
        self.state = LifecycleState.RUNNING
        self._error = None
        if not self._running_events_emitted:
            self.events.emit("CONNECTION_CONNECTED", "success")
            self.events.emit("FUNCTION_LOADED", "success")
            self._running_events_emitted = True

    def fresh_runtime_snapshot(self) -> RuntimeSnapshot:
        """Probe account state without waiting for lifecycle actions or changing them."""
        return self._snapshotter()

    def refresh(self) -> LifecycleView:
        with self._lock:
            # Environment discovery is expensive and belongs to the explicit
            # check/start paths. Polling still probes runtime and account state.
            self._runtime = self._snapshotter()
            if self._start_in_progress:
                if self._runtime.online:
                    self._mark_running()
                return self.view()
            if self.state == LifecycleState.STOP_WAIT_WECHAT:
                wechat_active = self._bot_wechat_active()
                if wechat_active:
                    self._wechat_closed_confirmations = 0
                    return self.view()
                self._wechat_closed_confirmations += 1
                if self._wechat_closed_confirmations >= 2:
                    return self._finish_stop()
                return self.view()
            licensed_view = self._refresh_licensed()
            if licensed_view is not None:
                return licensed_view
            if self._supervisor is not None and self._supervisor.running:
                exited = any(
                    getattr(item, "process", None) is not None
                    and item.process.poll() is not None
                    for item in self._supervisor.running
                )
                if exited:
                    self._supervisor.stop()
                    self._supervisor = None
                    return self._set_error("13002", detail="托管服务意外退出。")
                if self._runtime.online:
                    self._mark_running()
                elif self._runtime.login_required:
                    self.state = LifecycleState.WAITING_WECHAT
                    self._error = None
                else:
                    self.state = LifecycleState.VERIFYING_CONNECTION
                    self._error = None
            elif self._runtime.online:
                self.state = LifecycleState.RUNNING
                self._error = None
            elif self.state not in {LifecycleState.ERROR, LifecycleState.CHECKING}:
                self.state = LifecycleState.STOPPED
            return self.view()

    def _refresh_licensed(self) -> LifecycleView | None:
        status = self._license_snapshot()
        if not status["enabled"]:
            return None
        supervisor = self._supervisor
        if not supervisor or not supervisor.running:
            if self.state != LifecycleState.ERROR:
                self.state = LifecycleState.LICENSE_PAUSED
            return self.view()
        by_name = {item.spec.name: item for item in supervisor.running}
        bridge = by_name.get("new-hook-bridge")
        if bridge is not None and bridge.process.poll() is not None:
            self._pause_license()
            return None  # The existing lifecycle handles an unexpected bridge crash.
        core = by_name.get("astrbot")
        if core is not None and (not status["can_run"] or core.process.poll() is not None):
            # The runtime also enforces this independently when the manager is closed.
            self._pause_license()
            supervisor.stop_names(("astrbot",), timeout_seconds=3)
            self._running_events_emitted = False
            core = None
        if core is None:
            if self.state == LifecycleState.ERROR:
                return self.view()
            if status["can_run"]:
                self._pause_license()
            self.state = LifecycleState.LICENSE_READY if status["authorized"] else LifecycleState.LICENSE_PAUSED
            self._error = None
        elif self._runtime.online:
            self._mark_running()
        else:
            self.state = LifecycleState.VERIFYING_CONNECTION
        return self.view()

    def request_stop(self) -> LifecycleView:
        with self._lock:
            if self._license_snapshot()["enabled"]:
                self._pause_license()
                if self._supervisor is not None:
                    self._supervisor.stop_names(("astrbot",))
                self.state = LifecycleState.LICENSE_PAUSED
                self._running_events_emitted = False
                return self.view()
            return self._request_full_stop()

    def _request_full_stop(self) -> LifecycleView:
        if self._supervisor is None or not self._supervisor.running:
            self.state = LifecycleState.STOPPED
            self._error = None
            return self.view()
        self._runtime = self._snapshotter()
        if self._bot_wechat_active():
            self.state = LifecycleState.STOP_WAIT_WECHAT
            self._error = self.catalog.get("12003")
            self._wechat_closed_confirmations = 0
            return self.view()
        return self._finish_stop()

    def stop_for_maintenance(self) -> bool:
        """Request normal shutdown and only confirm a provably stopped runtime."""
        with self._lock:
            if self._start_in_progress:
                return False
            try:
                if self._supervisor is not None:
                    for item in tuple(self._supervisor.running):
                        process = item.process
                        if not any(process is known for known in self._maintenance_processes):
                            self._maintenance_processes.append(process)
                self.request_stop()
                if self.state == LifecycleState.LICENSE_PAUSED:
                    # A licensed user stop preserves the bridge. Maintenance
                    # must also stop it, using the normal WeChat-close boundary.
                    self._request_full_stop()
                return self.maintenance_stopped()
            except Exception:
                # Failure leaves the caller's maintenance transaction pending.
                # In particular, never clear surviving processes via force_stop.
                return False

    def maintenance_stopped(self) -> bool:
        """Read-only maintenance gate; False includes unavailable probe results."""
        with self._lock:
            # License polling also labels an entirely stopped installation as
            # paused. Neither UI state is proof: all probes below must pass.
            if self._start_in_progress or self.state not in {
                LifecycleState.STOPPED, LifecycleState.LICENSE_PAUSED,
            }:
                return False
            try:
                if self._supervisor is not None and self._supervisor.running:
                    return False
                if any(process.poll() is None for process in self._maintenance_processes):
                    return False
                runtime = self._snapshotter()
                if (
                    runtime.engine_ready or runtime.connection_ready
                    or runtime.wechat_identity or runtime.hook_ready
                    or runtime.upstream_connected or runtime.heartbeat_fresh
                ):
                    return False
                if not installation_services_stopped(self.install_root):
                    return False
            except Exception:
                return False
            self._maintenance_processes.clear()
            return True

    def force_stop(self) -> LifecycleView:
        """Stop owned services immediately, without waiting for WeChat."""
        with self._lock:
            if self._license_snapshot()["enabled"]:
                self._pause_license(stop_on_error=False)
            supervisor = self._supervisor
            if supervisor is None or not supervisor.running:
                self.state = LifecycleState.STOPPED
                self._error = None
                return self.view()
            self.state = LifecycleState.STOPPING
            self._error = None
            force_stop = getattr(supervisor, "force_stop", None)
            if callable(force_stop):
                force_stop()
            else:
                # Keep test/custom supervisors compatible while still forcing
                # the production supervisor down without a grace period.
                supervisor.stop(timeout_seconds=0)
            self._supervisor = None
            self._runtime = self._snapshotter()
            self.state = LifecycleState.STOPPED
            self._running_events_emitted = False
            self._wechat_closed_confirmations = 0
            return self.view()

    def _bot_wechat_active(self) -> bool:
        """Track only the WeChat instance owned by the active HOOK session.

        A customer may already have a personal WeChat open before starting the
        robot.  Process-name probing cannot distinguish that window from the
        dedicated instance launched by the bridge, while the HOOK and heartbeat
        signals are scoped to the latter.
        """
        return bool(
            self._runtime.hook_ready
            or self._runtime.heartbeat_fresh
            or self._runtime.wechat_identity
        )

    def _finish_stop(self) -> LifecycleView:
        self.state = LifecycleState.STOPPING
        self._error = None
        self.events.emit("WECHAT_CLOSED", "success")
        if self._supervisor is not None:
            self._supervisor.stop()
            self._supervisor = None
        self.events.emit("CONNECTION_STOP", "success")
        self.events.emit("CORE_STOP", "success")
        self._runtime = self._snapshotter()
        self.state = LifecycleState.STOPPED
        self._running_events_emitted = False
        self._wechat_closed_confirmations = 0
        return self.view()

    def view(self) -> LifecycleView:
        titles = {
            LifecycleState.STOPPED: ("机器人未启动", "环境就绪后即可启动机器人。"),
            LifecycleState.CHECKING: ("正在检查环境", "正在确认微信和运行文件。"),
            LifecycleState.CORE_STARTING: ("正在启动机器人", "正在准备核心功能。"),
            LifecycleState.CORE_READY: ("核心功能已就绪", "正在准备微信连接。"),
            LifecycleState.CONNECTION_STARTING: (
                "正在打开机器人微信",
                "微信窗口打开后请完成登录，机器人功能会在后台继续准备。",
            ),
            LifecycleState.WAITING_WECHAT: ("等待登录微信", "请在微信窗口完成登录。"),
            LifecycleState.VERIFYING_CONNECTION: ("正在确认连接", "正在检查消息通信是否正常。"),
            LifecycleState.RUNNING: ("机器人可用", "微信连接和全部功能运行正常。"),
            LifecycleState.LICENSE_READY: ("微信连接已保留", "完成账号绑定后，点击启动机器人。"),
            LifecycleState.LICENSE_PAUSED: ("自动化已暂停", "客户数据已保留，可前往订阅与授权继续处理。"),
            LifecycleState.STOP_WAIT_WECHAT: ("请先关闭微信", "检测到微信关闭后会自动继续停止。"),
            LifecycleState.STOPPING: ("正在停止机器人", "正在安全结束后台服务。"),
            LifecycleState.ERROR: ("机器人启动失败", "请根据错误提示处理后重试。"),
        }
        title, message = titles[self.state]
        if (
            self._start_in_progress
            and self._runtime.wechat_identity
            and not self._runtime.online
        ):
            title = "微信已登录，正在启动机器人功能"
            message = "账号连接成功，剩余功能正在后台加载，完成后会自动变为可用。"
        busy = self._start_in_progress or self.state in {
            LifecycleState.CHECKING,
            LifecycleState.CORE_STARTING,
            LifecycleState.CORE_READY,
            LifecycleState.CONNECTION_STARTING,
            LifecycleState.VERIFYING_CONNECTION,
            LifecycleState.STOPPING,
        }
        owned = self._supervisor is not None and bool(self._supervisor.running)
        licensing = self._license_snapshot()
        licensed = licensing["enabled"]
        online = self._runtime.online and (not licensed or licensing["can_run"])
        if licensed and self.state in {LifecycleState.LICENSE_READY, LifecycleState.LICENSE_PAUSED}:
            message = ("授权暂停未能写入，已停止后台服务；请稍后重新连接微信。"
                       if self._license_pause_failed else licensing["message"])
        return LifecycleView(
            state=self.state,
            title=title,
            message=message,
            busy=busy,
            online=online,
            can_start=(
                self._environment.ready
                and not busy
                and (not owned or licensed)
                and not online
            ),
            can_stop=owned and not busy,
            error=self._error,
            environment=self._environment,
            runtime=self._runtime,
        )


__all__ = ["LifecycleController", "LifecycleState", "LifecycleView"]
