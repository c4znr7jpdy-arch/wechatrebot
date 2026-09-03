from pathlib import Path

from wechatrebot_manager.environment import WechatEnvironment
from wechatrebot_manager.lifecycle import LifecycleController, LifecycleState
from wechatrebot_manager.runtime_status import RuntimeSnapshot


def _environment(tmp_path: Path, ready: bool = True) -> WechatEnvironment:
    return WechatEnvironment(
        ready=ready,
        code="10000" if ready else "11001",
        title="微信环境正常" if ready else "未检测到微信",
        message="正常" if ready else "未安装",
        solutions=(),
        path=str(tmp_path / "Weixin.exe") if ready else "",
        version="4.1.8.27" if ready else "",
        source="test",
    )


class _Spec:
    def __init__(self, name: str) -> None:
        self.name = name


class _Supervisor:
    def __init__(self, progress) -> None:
        self.progress = progress
        self.running = []
        self.stopped = False

    def start(self):
        for name in ("astrbot", "new-hook-bridge"):
            spec = _Spec(name)
            self.progress("starting", spec)
            self.progress("ready", spec)
        self.running = [object()]
        return tuple(self.running)

    def stop(self, timeout_seconds: float = 15) -> None:
        self.stopped = True
        self.running.clear()


def test_start_is_gated_when_wechat_is_missing(tmp_path: Path) -> None:
    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path, False),
        snapshotter=lambda: RuntimeSnapshot(False, False, ""),
        preflight=lambda root: (),
    )

    view = controller.start()

    assert view.state == LifecycleState.STOPPED
    assert view.can_start is False
    assert view.error is not None and view.error.code == "11001"


def test_only_full_health_marks_robot_running(tmp_path: Path) -> None:
    snapshots = iter(
        (
            RuntimeSnapshot(False, False, ""),
            RuntimeSnapshot(True, True, "wxid_bot", True, True, False),
            RuntimeSnapshot(True, True, "wxid_bot", True, True, True),
        )
    )
    holder = {}

    def factory(progress):
        holder["supervisor"] = _Supervisor(progress)
        return holder["supervisor"]

    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        environment_saver=lambda root, environment: None,
        snapshotter=lambda: next(snapshots),
        process_probe=lambda: (),
        preflight=lambda root: (),
        supervisor_factory=factory,
    )

    started = controller.start()
    running = controller.refresh()

    assert started.state == LifecycleState.VERIFYING_CONNECTION
    assert started.online is False
    assert running.state == LifecycleState.RUNNING
    assert running.online is True


def test_stop_ignores_personal_wechat_after_bot_hook_closes(tmp_path: Path) -> None:
    snapshots = iter(
        (
            RuntimeSnapshot(False, False, ""),
            RuntimeSnapshot(True, True, "wxid_bot", True, True, True),
            RuntimeSnapshot(True, True, "wxid_bot", True, True, True),
            RuntimeSnapshot(False, False, ""),
            RuntimeSnapshot(False, False, ""),
            RuntimeSnapshot(False, False, ""),
        )
    )
    holder = {}

    def factory(progress):
        holder["supervisor"] = _Supervisor(progress)
        return holder["supervisor"]

    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        environment_saver=lambda root, environment: None,
        snapshotter=lambda: next(snapshots),
        process_probe=lambda: ("weixin.exe",),
        preflight=lambda root: (),
        supervisor_factory=factory,
    )
    controller.start()

    waiting = controller.request_stop()
    confirming = controller.refresh()
    was_running_during_confirmation = not holder["supervisor"].stopped
    stopped = controller.refresh()

    assert waiting.state == LifecycleState.STOP_WAIT_WECHAT
    assert waiting.error is not None and waiting.error.code == "12003"
    assert confirming.state == LifecycleState.STOP_WAIT_WECHAT
    assert was_running_during_confirmation is True
    assert stopped.state == LifecycleState.STOPPED
    assert holder["supervisor"].stopped is True


def test_closing_personal_wechat_does_not_stop_bot_while_hook_is_alive(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            RuntimeSnapshot(False, False, ""),
            RuntimeSnapshot(True, True, "wxid_bot", True, True, True),
            RuntimeSnapshot(True, True, "wxid_bot", True, True, True),
            RuntimeSnapshot(True, True, "wxid_bot", True, True, True),
        )
    )
    holder = {}

    def factory(progress):
        holder["supervisor"] = _Supervisor(progress)
        return holder["supervisor"]

    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        environment_saver=lambda root, environment: None,
        snapshotter=lambda: next(snapshots),
        process_probe=lambda: (),
        preflight=lambda root: (),
        supervisor_factory=factory,
    )
    controller.start()

    waiting = controller.request_stop()
    still_waiting = controller.refresh()

    assert waiting.state == LifecycleState.STOP_WAIT_WECHAT
    assert still_waiting.state == LifecycleState.STOP_WAIT_WECHAT
    assert holder["supervisor"].stopped is False
