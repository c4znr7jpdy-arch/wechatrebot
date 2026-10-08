from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from typing import Callable, IO, Mapping, Protocol

from .json_io import write_json_atomic
from .runtime_config import RuntimePorts
from .payload import sha256_file


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
    required_files: tuple[Path, ...] = ()
    required_free_ports: tuple[tuple[str, int], ...] = ()
    required_hashes: tuple[tuple[Path, str], ...] = ()
    display_name: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.name

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        executable = Path(self.argv[0])
        if not executable.is_file():
            errors.append(f"{self.label}运行时不存在，请重新安装或联系技术支持。")
        if not self.cwd.is_dir():
            errors.append(f"{self.label}工作目录不存在，请重新安装。")
        for required in self.required_files:
            if not required.is_file():
                errors.append(f"{self.label}必需文件不存在：{required.name}")
        for required, expected in self.required_hashes:
            if required.is_file() and sha256_file(required) != expected:
                errors.append(f"{self.label}文件完整性校验失败：{required.name}")
        if not 1 <= self.ready_port <= 65535:
            errors.append(f"{self.label}就绪端口无效。")
        return tuple(errors)


@dataclass(slots=True)
class RunningService:
    spec: ServiceSpec
    process: ProcessLike
    log_handle: IO[bytes]


class ServiceLaunchError(RuntimeError):
    """Raised when the managed startup sequence cannot complete."""


class ServiceStopError(ServiceLaunchError):
    """Raised when a managed service remains alive after graceful stop retries."""

    def __init__(self, service_name: str, label: str) -> None:
        self.service_name = service_name
        self.label = label
        super().__init__(f"{label}未能安全停止，请稍后重试。")


_RUN_MODULE_FROM_PATH = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module(sys.argv.pop(1),run_name='__main__')"
)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    executable: str
    creation_time: int


def _normalized_executable(value: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(value)))


def process_identity(pid: int) -> ProcessIdentity | None:
    """Return a PID-reuse-safe Windows process identity without extra packages."""
    if os.name != "nt" or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information, False, pid
    )
    if not handle:
        return None
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(capacity)
        ):
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        creation_time = (int(created.dwHighDateTime) << 32) | int(
            created.dwLowDateTime
        )
        return ProcessIdentity(_normalized_executable(buffer.value), creation_time)
    finally:
        kernel32.CloseHandle(handle)


class AdoptedProcess:
    """Minimal process facade for a service started by an earlier UI session."""

    def __init__(
        self,
        pid: int,
        identity: ProcessIdentity,
        identity_probe: Callable[[int], ProcessIdentity | None] = process_identity,
    ) -> None:
        self.pid = pid
        self._identity = identity
        self._identity_probe = identity_probe

    def poll(self) -> int | None:
        current = self._identity_probe(self.pid)
        if current is None or current != self._identity:
            return 1
        return None

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.1)
        return 0

    def terminate(self) -> None:
        os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        os.kill(self.pid, signal.SIGTERM)

    def send_signal(self, sig: int) -> None:
        os.kill(self.pid, sig)


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


def _hook_integrity_files(root: Path) -> tuple[tuple[Path, str], ...]:
    manifest_path = root / "legal" / "hook-binaries.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError("缺少微信连接组件完整性清单，请重新安装。") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"微信连接组件完整性清单无法读取：{exc}") from exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(files, list)
    ):
        raise ValueError("微信连接组件完整性清单格式错误。")
    hook_root = root / "app" / "bridge" / "vendor" / "hook"
    expected_names = {
        "x64 inject.exe",
        "libglesv1-c2c-video-v2.dll",
    }
    results: dict[str, tuple[Path, str]] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = Path(str(item.get("path", ""))).name
        digest = str(item.get("sha256", "")).upper()
        key = filename.casefold()
        if key in expected_names and len(digest) == 64:
            results[key] = (hook_root / filename, digest)
    if set(results) != expected_names:
        raise ValueError("微信连接组件清单缺少必要文件。")
    return tuple(results[name] for name in sorted(results))


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
    node_bin = python_exe.parent / "Lib/site-packages/playwright/driver"
    inherited_path = os.environ.get("PATH", "")
    common_environment = {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": (
            f"{ffmpeg_bin}{os.pathsep}{node_bin}{os.pathsep}{inherited_path}"
            if inherited_path
            else f"{ffmpeg_bin}{os.pathsep}{node_bin}"
        ),
    }
    hook_integrity_files = _hook_integrity_files(root)
    astrbot = ServiceSpec(
        name="astrbot",
        argv=(str(python_exe), "-m", "astrbot.cli.__main__", "run"),
        cwd=astrbot_root,
        log_file=logs_root / "astrbot-service.log",
        ready_host="127.0.0.1",
        ready_port=ports.astrbot_ws,
        ready_timeout_seconds=120,
        environment=common_environment,
        required_files=(
            astrbot_root / ".astrbot",
            astrbot_root / "data" / "cmd_config.json",
            ffmpeg_bin / "ffmpeg.exe",
            ffmpeg_bin / "ffprobe.exe",
        ),
        display_name="回复引擎",
    )
    # The default HOOK API port may belong to an already-open WeChat.  The
    # managed bridge disables reuse, reserves another port, and launches the
    # robot's own WeChat instance, so this port must not be a supervisor-level
    # free-port requirement.
    bridge = ServiceSpec(
        name="new-hook-bridge",
        argv=(
            str(python_exe),
            "-c",
            _RUN_MODULE_FROM_PATH,
            str(bridge_root / "src"),
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
        required_files=(
            root / "legal" / "hook-binaries.json",
            bridge_root / "config.json",
            bridge_root / "src" / "hook_bridge" / "__init__.py",
            bridge_root / "src" / "hook_bridge" / "__main__.py",
            bridge_root / "vendor" / "hook" / "x64 inject.exe",
            bridge_root
            / "vendor"
            / "hook"
            / "libGLESv1-c2c-video-v2.dll",
        ),
        required_hashes=hook_integrity_files,
        display_name="微信连接",
    )
    return astrbot, bridge


class ProcessSupervisor:
    def __init__(
        self,
        specs: tuple[ServiceSpec, ...],
        *,
        popen_factory: Callable[..., ProcessLike] = subprocess.Popen,
        readiness_waiter: Callable[[ServiceSpec, ProcessLike], bool] = wait_until_ready,
        progress_callback: Callable[[str, ServiceSpec], None] | None = None,
        state_path: str | Path | None = None,
        identity_probe: Callable[[int], ProcessIdentity | None] = process_identity,
        readiness_probe: Callable[[str, int], bool] = port_accepting,
    ) -> None:
        self.specs = specs
        self._popen_factory = popen_factory
        self._readiness_waiter = readiness_waiter
        self._progress_callback = progress_callback
        self._state_path = Path(state_path).resolve() if state_path is not None else None
        self._identity_probe = identity_probe
        self._readiness_probe = readiness_probe
        self.running: list[RunningService] = []
        self.adoption_complete = False

    def _clear_state(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.unlink()
        except FileNotFoundError:
            return

    def _persist_state(self) -> None:
        if self._state_path is None:
            return
        services: list[dict[str, object]] = []
        for running in self.running:
            pid = int(getattr(running.process, "pid", 0) or 0)
            identity = self._identity_probe(pid)
            if identity is None:
                raise ServiceLaunchError(
                    f"无法记录{running.spec.label}的运行状态，请重试。"
                )
            services.append(
                {
                    "name": running.spec.name,
                    "pid": pid,
                    "executable": identity.executable,
                    "creation_time": identity.creation_time,
                }
            )
        write_json_atomic(
            self._state_path,
            {
                "schema_version": 1,
                "complete": len(self.running) == len(self.specs),
                "services": services,
            },
        )

    def adopt(self) -> bool:
        """Reattach to services previously started by this installation."""
        if self.running or self._state_path is None:
            return bool(self.running)
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return False
        except (OSError, json.JSONDecodeError):
            self._clear_state()
            return False
        entries = payload.get("services") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(entries, list)
        ):
            self._clear_state()
            return False
        by_name = {spec.name: spec for spec in self.specs}
        adopted: list[RunningService] = []
        adopted_names: set[str] = set()
        try:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                spec = by_name.get(str(entry.get("name") or ""))
                try:
                    pid = int(entry.get("pid") or 0)
                    creation_time = int(entry.get("creation_time") or 0)
                except (TypeError, ValueError):
                    continue
                identity = self._identity_probe(pid)
                expected_executable = _normalized_executable(spec.argv[0]) if spec else ""
                if (
                    spec is None
                    or spec.name in adopted_names
                    or identity is None
                    or identity.creation_time != creation_time
                    or identity.executable != _normalized_executable(
                        str(entry.get("executable") or "")
                    )
                    or identity.executable != expected_executable
                    or not self._readiness_probe(spec.ready_host, spec.ready_port)
                ):
                    continue
                spec.log_file.parent.mkdir(parents=True, exist_ok=True)
                log_handle = spec.log_file.open("ab", buffering=0)
                adopted.append(
                    RunningService(
                        spec,
                        AdoptedProcess(pid, identity, self._identity_probe),
                        log_handle,
                    )
                )
                adopted_names.add(spec.name)
        except OSError:
            for running in adopted:
                running.log_handle.close()
            return False
        self.running = adopted
        if not self.running:
            self._clear_state()
            return False
        self.adoption_complete = len(self.running) == len(self.specs)
        self._persist_state()
        return True

    def start(self) -> tuple[RunningService, ...]:
        if self.running:
            raise ServiceLaunchError("服务监督器已经启动。")
        validation_errors = [
            error
            for spec in self.specs
            for error in spec.validate()
        ]
        if validation_errors:
            raise ServiceLaunchError("；".join(validation_errors))
        occupied: list[str] = []
        checked_ports: set[tuple[str, int]] = set()
        for spec in self.specs:
            ports = ((spec.label, spec.ready_port), *spec.required_free_ports)
            for label, port in ports:
                address = (spec.ready_host, port)
                if address in checked_ports:
                    continue
                checked_ports.add(address)
                if port_accepting(*address):
                    occupied.append(
                        f"{label} 端口已被占用：{spec.ready_host}:{port}"
                    )
        if occupied:
            raise ServiceLaunchError("；".join(occupied))
        # Launch every service before waiting for the slower reply engine.  The
        # bridge is designed to reconnect to that engine, so this opens WeChat
        # immediately instead of making a first-time user wait through the full
        # plugin startup sequence before a login window can appear.
        for spec in self.specs:
            if self._progress_callback is not None:
                self._progress_callback("starting", spec)
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
                raise ServiceLaunchError(f"无法启动{spec.label}：{exc}") from exc
            running = RunningService(spec, process, log_handle)
            self.running.append(running)
            try:
                self._persist_state()
            except (OSError, ServiceLaunchError) as exc:
                self.stop()
                raise ServiceLaunchError(str(exc)) from exc
        for running in tuple(self.running):
            spec = running.spec
            process = running.process
            if not self._readiness_waiter(spec, process):
                exit_code = process.poll()
                self.stop()
                raise ServiceLaunchError(
                    f"{spec.label}未在规定时间内就绪，退出码：{exit_code!r}。"
                )
            if self._progress_callback is not None:
                self._progress_callback("ready", spec)
        self.adoption_complete = True
        try:
            self._persist_state()
        except (OSError, ServiceLaunchError) as exc:
            self.stop()
            raise ServiceLaunchError(str(exc)) from exc
        return tuple(self.running)

    def stop(self, timeout_seconds: float = 15) -> None:
        remaining: list[RunningService] = []
        failures: list[RunningService] = []
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
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=5)
                    except Exception:
                        try:
                            if process.poll() is None:
                                process.kill()
                                process.wait(timeout=5)
                        except Exception:
                            pass
            if process.poll() is None:
                remaining.append(running)
                failures.append(running)
            else:
                running.log_handle.close()
        self.running = list(reversed(remaining))
        self.adoption_complete = False
        if failures:
            try:
                self._persist_state()
            except (OSError, ServiceLaunchError):
                pass
            first = failures[0]
            raise ServiceStopError(first.spec.name, first.spec.label)
        self._clear_state()

    def force_stop(self) -> None:
        """Terminate every managed service immediately, without a grace period."""
        for running in reversed(self.running):
            process = running.process
            if process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    process.wait(timeout=5)
                except Exception:
                    pass
            running.log_handle.close()
        self.running.clear()
        self.adoption_complete = False
        self._clear_state()

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
