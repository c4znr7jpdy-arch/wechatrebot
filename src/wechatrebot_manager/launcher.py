from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from typing import Callable, IO, Mapping, Protocol

from .runtime_config import RuntimePorts


class ProcessLike(Protocol):
    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def send_signal(self, sig: int) -> None: ...


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    log_file: Path
    ready_host: str
    ready_port: int
    ready_timeout_seconds: float
    environment: Mapping[str, str]

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        executable = Path(self.argv[0])
        if not executable.is_file():
            errors.append(f"{self.name} 运行时不存在：{executable}")
        if not self.cwd.is_dir():
            errors.append(f"{self.name} 工作目录不存在：{self.cwd}")
        if not 1 <= self.ready_port <= 65535:
            errors.append(f"{self.name} 就绪端口无效。")
        return tuple(errors)


@dataclass(slots=True)
class RunningService:
    spec: ServiceSpec
    process: ProcessLike
    log_handle: IO[bytes]


class ServiceLaunchError(RuntimeError):
    """Raised when the managed startup sequence cannot complete."""


def port_accepting(host: str, port: int, timeout_seconds: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def wait_until_ready(spec: ServiceSpec, process: ProcessLike) -> bool:
    deadline = time.monotonic() + spec.ready_timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if port_accepting(spec.ready_host, spec.ready_port):
            return True
        time.sleep(0.2)
    return False


def build_service_specs(
    install_root: str | Path,
    *,
    ports: RuntimePorts = RuntimePorts(),
) -> tuple[ServiceSpec, ServiceSpec]:
    errors = ports.validate()
    if errors:
        raise ValueError("；".join(errors))
    root = Path(install_root).expanduser().resolve()
    python_exe = root / "runtime" / "python" / "python.exe"
    astrbot_root = root / "app" / "astrbot"
    bridge_root = root / "app" / "bridge"
    logs_root = root / "logs"
    ffmpeg_bin = root / "runtime" / "ffmpeg" / "bin"
    inherited_path = os.environ.get("PATH", "")
    common_environment = {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": (
            f"{ffmpeg_bin}{os.pathsep}{inherited_path}"
            if inherited_path
            else str(ffmpeg_bin)
        ),
    }
    astrbot = ServiceSpec(
        name="astrbot",
        argv=(str(python_exe), "-m", "astrbot.cli.__main__", "run"),
        cwd=astrbot_root,
        log_file=logs_root / "astrbot-service.log",
        ready_host="127.0.0.1",
        ready_port=ports.astrbot_ws,
        ready_timeout_seconds=120,
        environment=common_environment,
    )
    bridge = ServiceSpec(
        name="new-hook-bridge",
        argv=(
            str(python_exe),
            "-m",
            "hook_bridge",
            "--config",
            str(bridge_root / "config.json"),
        ),
        cwd=bridge_root,
        log_file=logs_root / "new-hook-bridge.log",
        ready_host="127.0.0.1",
        ready_port=ports.bridge_callback,
        ready_timeout_seconds=45,
        environment={
            **common_environment,
            "PYTHONPATH": str(bridge_root / "src"),
        },
    )
    return astrbot, bridge


class ProcessSupervisor:
    def __init__(
        self,
        specs: tuple[ServiceSpec, ...],
        *,
        popen_factory: Callable[..., ProcessLike] = subprocess.Popen,
        readiness_waiter: Callable[[ServiceSpec, ProcessLike], bool] = wait_until_ready,
    ) -> None:
        self.specs = specs
        self._popen_factory = popen_factory
        self._readiness_waiter = readiness_waiter
        self.running: list[RunningService] = []

    def start(self) -> tuple[RunningService, ...]:
        if self.running:
            raise ServiceLaunchError("服务监督器已经启动。")
        for spec in self.specs:
            errors = spec.validate()
            if errors:
                self.stop()
                raise ServiceLaunchError("；".join(errors))
            if port_accepting(spec.ready_host, spec.ready_port):
                self.stop()
                raise ServiceLaunchError(
                    f"{spec.name} 端口已被占用：{spec.ready_host}:{spec.ready_port}"
                )
            spec.log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = spec.log_file.open("ab", buffering=0)
            environment = os.environ.copy()
            environment.update(spec.environment)
            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
                creation_flags |= subprocess.CREATE_NO_WINDOW
            try:
                process = self._popen_factory(
                    list(spec.argv),
                    cwd=str(spec.cwd),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
            except Exception as exc:
                log_handle.close()
                self.stop()
                raise ServiceLaunchError(f"无法启动 {spec.name}：{exc}") from exc
            running = RunningService(spec, process, log_handle)
            self.running.append(running)
            if not self._readiness_waiter(spec, process):
                exit_code = process.poll()
                self.stop()
                raise ServiceLaunchError(
                    f"{spec.name} 未在规定时间内就绪，退出码：{exit_code!r}。"
                )
        return tuple(self.running)

    def stop(self, timeout_seconds: float = 15) -> None:
        for running in reversed(self.running):
            process = running.process
            if process.poll() is None:
                try:
                    if os.name == "nt":
                        process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        process.terminate()
                    process.wait(timeout=timeout_seconds)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception:
                        pass
            running.log_handle.close()
        self.running.clear()

    def wait(self) -> int:
        if not self.running:
            raise ServiceLaunchError("没有正在监督的服务。")
        while True:
            for running in self.running:
                code = running.process.poll()
                if code is not None:
                    self.stop()
                    return code
            time.sleep(0.5)


def run_services_foreground(install_root: str | Path) -> int:
    supervisor = ProcessSupervisor(build_service_specs(install_root))
    try:
        supervisor.start()
        return supervisor.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        supervisor.stop()
