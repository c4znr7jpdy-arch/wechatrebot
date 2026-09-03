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
from .launcher import ProcessSupervisor, ServiceLaunchError, ServiceSpec, build_service_specs
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
    STOP_WAIT_WECHAT = "STOP_WAIT_WECHAT"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class SupervisorLike(Protocol):
    running: list[object]

    def start(self) -> tuple[object, ...]: ...

    def stop(self, timeout_seconds: float = 15) -> None: ...


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
    ) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
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
        self.events.emit("APP_START", "success")
        if self._uses_default_supervisor:
            try:
                candidate = self._supervisor_factory(self._on_progress)
                adopter = getattr(candidate, "adopt", None)
                if callable(adopter) and adopter():
                    self._supervisor = candidate
                    if not bool(getattr(candidate, "adoption_complete", False)):
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
                    elif self._runtime.connection_ready and not self._runtime.wechat_identity:
                        self.state = LifecycleState.WAITING_WECHAT
                        self._error = self.catalog.get("11003")
                    else:
                        self.state = LifecycleState.VERIFYING_CONNECTION
                        self._error = self.catalog.get("13002")
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

    def start(self) -> LifecycleView:
        with self._lock:
            if self._supervisor is not None and self._supervisor.running:
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
                self._supervisor = self._supervisor_factory(self._on_progress)
                self._supervisor.start()
            except (OSError, ServiceLaunchError, ValueError) as exc:
                code = (
                    "12001"
                    if self.state == LifecycleState.CONNECTION_STARTING
                    else "13001"
                )
                self._supervisor = None
                return self._set_error(code, detail=str(exc))
            self._runtime = self._snapshotter()
            self._error = None
            if self._runtime.online:
                self._mark_running()
            elif not self._runtime.wechat_identity:
                self.state = LifecycleState.WAITING_WECHAT
                self._error = self.catalog.get("11003")
            else:
                self.state = LifecycleState.VERIFYING_CONNECTION
                self._error = self.catalog.get("13002")
            return self.view()

    def _mark_running(self) -> None:
        self.state = LifecycleState.RUNNING
        self._error = None
        if not self._running_events_emitted:
            self.events.emit("CONNECTION_CONNECTED", "success")
            self.events.emit("FUNCTION_LOADED", "success")
            self._running_events_emitted = True

    def refresh(self) -> LifecycleView:
        with self._lock:
            self._environment = self._environment_checker(self.install_root)
            self._runtime = self._snapshotter()
            if self.state == LifecycleState.STOP_WAIT_WECHAT:
                wechat_active = bool(self._process_probe()) or bool(
                    self._runtime.hook_ready
                    or self._runtime.heartbeat_fresh
                    or self._runtime.wechat_identity
                )
                if wechat_active:
                    self._wechat_closed_confirmations = 0
                    return self.view()
                self._wechat_closed_confirmations += 1
                if self._wechat_closed_confirmations >= 2:
                    return self._finish_stop()
                return self.view()
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
                elif self._runtime.connection_ready and not self._runtime.wechat_identity:
                    self.state = LifecycleState.WAITING_WECHAT
                    self._error = self.catalog.get("11003")
                elif self._runtime.connection_ready:
                    self.state = LifecycleState.VERIFYING_CONNECTION
                    self._error = self.catalog.get("13002")
            elif self._runtime.online:
                self.state = LifecycleState.RUNNING
                self._error = None
            elif self.state not in {LifecycleState.ERROR, LifecycleState.CHECKING}:
                self.state = LifecycleState.STOPPED
            return self.view()

    def request_stop(self) -> LifecycleView:
        with self._lock:
            if self._supervisor is None or not self._supervisor.running:
                self.state = LifecycleState.STOPPED
                self._error = None
                return self.view()
            self._runtime = self._snapshotter()
            wechat_active = bool(self._process_probe()) or bool(
                self._runtime.hook_ready
                or self._runtime.heartbeat_fresh
                or self._runtime.wechat_identity
            )
            if wechat_active:
                self.state = LifecycleState.STOP_WAIT_WECHAT
                self._error = self.catalog.get("12003")
                self._wechat_closed_confirmations = 0
                return self.view()
            return self._finish_stop()

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
            LifecycleState.CONNECTION_STARTING: ("正在连接微信", "微信窗口可能会自动打开。"),
            LifecycleState.WAITING_WECHAT: ("等待登录微信", "请在微信窗口完成登录。"),
            LifecycleState.VERIFYING_CONNECTION: ("正在确认连接", "正在检查消息通信是否正常。"),
            LifecycleState.RUNNING: ("机器人可用", "微信连接和全部功能运行正常。"),
            LifecycleState.STOP_WAIT_WECHAT: ("请先关闭微信", "检测到微信关闭后会自动继续停止。"),
            LifecycleState.STOPPING: ("正在停止机器人", "正在安全结束后台服务。"),
            LifecycleState.ERROR: ("机器人启动失败", "请根据错误提示处理后重试。"),
        }
        title, message = titles[self.state]
        busy = self.state in {
            LifecycleState.CHECKING,
            LifecycleState.CORE_STARTING,
            LifecycleState.CORE_READY,
            LifecycleState.CONNECTION_STARTING,
            LifecycleState.VERIFYING_CONNECTION,
            LifecycleState.STOPPING,
        }
        owned = self._supervisor is not None and bool(self._supervisor.running)
        return LifecycleView(
            state=self.state,
            title=title,
            message=message,
            busy=busy,
            online=self._runtime.online,
            can_start=(
                self._environment.ready
                and not busy
                and not owned
                and not self._runtime.online
            ),
            can_stop=owned and not busy,
            error=self._error,
            environment=self._environment,
            runtime=self._runtime,
        )


__all__ = ["LifecycleController", "LifecycleState", "LifecycleView"]
