from pathlib import Path
import os
from typing import Any

from wechatrebot_manager.launcher import ProcessSupervisor, build_service_specs


class _FakeProcess:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
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
    (root / "app" / "bridge" / "src").mkdir(parents=True)


def test_specs_share_one_python_and_start_astrbot_first(tmp_path: Path) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    assert [item.name for item in specs] == ["astrbot", "new-hook-bridge"]
    assert specs[0].argv[0] == specs[1].argv[0]
    assert specs[0].ready_port == 6200
    assert specs[1].ready_port == 5000
    assert specs[1].environment["PYTHONPATH"].endswith("app\\bridge\\src")
    assert specs[0].environment["PATH"].split(os.pathsep)[0].endswith(
        "runtime\\ffmpeg\\bin"
    )
    assert specs[1].environment["PATH"] == specs[0].environment["PATH"]
    assert specs[0].environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert specs[1].environment["PYTHONDONTWRITEBYTECODE"] == "1"


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
