from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from typing import Any

import pytest

from wechatrebot_manager.launcher import (
    ProcessIdentity,
    ProcessSupervisor,
    ServiceLaunchError,
    _normalized_executable,
    build_service_specs,
)


class _FakeProcess:
    def __init__(self, name: str, events: list[str], pid: int = 0) -> None:
        self.name = name
        self.events = events
        self.pid = pid
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        self.code = 0
        return 0

    def terminate(self) -> None:
        self.events.append(f"stop:{self.name}")
        self.code = 0

    def kill(self) -> None:
        self.events.append(f"kill:{self.name}")
        self.code = 1

    def send_signal(self, sig: int) -> None:
        self.terminate()


def _layout(root: Path) -> None:
    (root / "runtime" / "python").mkdir(parents=True)
    (root / "runtime" / "python" / "python.exe").write_bytes(b"runtime")
    (root / "app" / "astrbot").mkdir(parents=True)
    (root / "app" / "astrbot" / ".astrbot").touch()
    (root / "app" / "astrbot" / "data").mkdir()
    (root / "app" / "astrbot" / "data" / "cmd_config.json").write_text("{}")
    bridge_package = root / "app" / "bridge" / "src" / "hook_bridge"
    bridge_package.mkdir(parents=True)
    (bridge_package / "__init__.py").write_text("")
    (bridge_package / "__main__.py").write_text(
        "print('bridge-bootstrap-ok')\n", encoding="utf-8"
    )
    (root / "app" / "bridge" / "config.json").write_text("{}")
    hook = root / "app" / "bridge" / "vendor" / "hook"
    hook.mkdir(parents=True)
    (hook / "x64 inject.exe").write_bytes(b"injector")
    (hook / "libGLESv1-c2c-video-v2.dll").write_bytes(b"hook")
    legal = root / "legal"
    legal.mkdir()
    (legal / "hook-binaries.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [
                    {
                        "path": "hook/x64 inject.exe",
                        "sha256": hashlib.sha256(b"injector").hexdigest().upper(),
                    },
                    {
                        "path": "hook/libGLESv1-c2c-video-v2.dll",
                        "sha256": hashlib.sha256(b"hook").hexdigest().upper(),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_specs_share_one_python_and_start_astrbot_first(tmp_path: Path) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    assert [item.name for item in specs] == ["astrbot", "new-hook-bridge"]
    assert specs[0].argv[0] == specs[1].argv[0]
    assert specs[0].ready_port == 6200
    assert specs[1].ready_port == 5000
    assert specs[1].environment["PYTHONPATH"].endswith("app\\bridge\\src")
    assert specs[1].argv[1] == "-c"
    assert specs[1].argv[3].endswith("app\\bridge\\src")
    assert specs[1].argv[4] == "hook_bridge"
    assert specs[0].environment["PATH"].split(os.pathsep)[0].endswith(
        "runtime\\ffmpeg\\bin"
    )
    assert specs[1].environment["PATH"] == specs[0].environment["PATH"]
    assert specs[0].environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert specs[1].environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_bridge_bootstrap_imports_source_with_isolated_python(tmp_path: Path) -> None:
    _layout(tmp_path)
    bridge = build_service_specs(tmp_path)[1]

    result = subprocess.run(
        [sys.executable, "-I", *bridge.argv[1:]],
        cwd=bridge.cwd,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bridge-bootstrap-ok"


def test_supervisor_uses_ordered_readiness_and_reverse_shutdown(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    events: list[str] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        name = "astrbot" if "astrbot.cli.__main__" in argv else "new-hook-bridge"
        events.append(f"start:{name}")
        return _FakeProcess(name, events)

    def ready(spec, process) -> bool:
        events.append(f"ready:{spec.name}")
        return True

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(specs, popen_factory=popen, readiness_waiter=ready)
    supervisor.start()
    supervisor.stop()

    assert events[:4] == [
        "start:astrbot",
        "ready:astrbot",
        "start:new-hook-bridge",
        "ready:new-hook-bridge",
    ]
    assert events[-2:] == ["stop:new-hook-bridge", "stop:astrbot"]


def test_preflight_rejects_missing_hook_before_any_process_starts(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    (tmp_path / "app" / "bridge" / "vendor" / "hook" / "x64 inject.exe").unlink()
    starts: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        starts.append(argv)
        return _FakeProcess("unexpected", [])

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(build_service_specs(tmp_path), popen_factory=popen)

    with pytest.raises(ServiceLaunchError, match="x64 inject.exe"):
        supervisor.start()
    assert starts == []


def test_preflight_checks_wechat_connection_port_before_start(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    starts: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        starts.append(argv)
        return _FakeProcess("unexpected", [])

    monkeypatch.setattr(
        "wechatrebot_manager.launcher.port_accepting",
        lambda host, port: port == 19088,
    )
    supervisor = ProcessSupervisor(build_service_specs(tmp_path), popen_factory=popen)

    with pytest.raises(ServiceLaunchError, match="19088"):
        supervisor.start()
    assert starts == []


def test_preflight_rejects_tampered_hook_before_any_process_starts(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    (tmp_path / "app" / "bridge" / "vendor" / "hook" / "x64 inject.exe").write_bytes(
        b"tampered"
    )
    starts: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        starts.append(argv)
        return _FakeProcess("unexpected", [])

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(specs, popen_factory=popen)

    with pytest.raises(ServiceLaunchError, match="完整性校验失败"):
        supervisor.start()
    assert starts == []


def test_supervisor_persists_and_readopts_owned_processes(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    state_path = tmp_path / "data" / "manager" / "service-state.json"
    events: list[str] = []
    identities = {
        101: ProcessIdentity(_normalized_executable(specs[0].argv[0]), 1001),
        102: ProcessIdentity(_normalized_executable(specs[1].argv[0]), 1002),
    }
    pids = iter((101, 102))

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        name = "astrbot" if "astrbot.cli.__main__" in argv else "new-hook-bridge"
        return _FakeProcess(name, events, next(pids))

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(
        specs,
        popen_factory=popen,
        readiness_waiter=lambda spec, process: True,
        state_path=state_path,
        identity_probe=identities.get,
    )
    supervisor.start()
    assert json.loads(state_path.read_text(encoding="utf-8"))["complete"] is True

    for running in supervisor.running:
        running.log_handle.close()
    supervisor.running.clear()
    adopted = ProcessSupervisor(
        specs,
        state_path=state_path,
        identity_probe=identities.get,
        readiness_probe=lambda host, port: True,
    )

    assert adopted.adopt() is True
    assert adopted.adoption_complete is True
    assert [running.process.pid for running in adopted.running] == [101, 102]

    monkeypatch.setattr(os, "kill", lambda pid, sig: identities.pop(pid, None))
    adopted.stop()
    assert not state_path.exists()
