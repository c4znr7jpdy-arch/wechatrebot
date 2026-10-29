from pathlib import Path
import threading

import pytest

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

    def force_stop(self) -> None:
        self.forced = True
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


def test_refresh_reuses_environment_but_reads_each_runtime_snapshot(tmp_path: Path) -> None:
    environment_calls = []
    environments = iter((_environment(tmp_path), _environment(tmp_path, False)))
    snapshots = iter((
        RuntimeSnapshot(False, False, ""),
        RuntimeSnapshot(True, True, "wxid_first", True, True, True),
        RuntimeSnapshot(True, True, "wxid_second", True, True, True),
    ))

    def check_environment(root):
        environment_calls.append(root)
        return next(environments)

    controller = LifecycleController(
        tmp_path,
        environment_checker=check_environment,
        snapshotter=lambda: next(snapshots),
        supervisor_factory=lambda progress: _Supervisor(progress),
    )

    first = controller.refresh()
    second = controller.refresh()

    assert first.runtime.wechat_identity == "wxid_first"
    assert second.runtime.wechat_identity == "wxid_second"
    assert first.environment.ready and second.environment.ready
    assert environment_calls == [tmp_path]

    checked = controller.check_environment()

    assert checked.environment.ready is False
    assert checked.error is not None and checked.error.code == "11001"
    assert environment_calls == [tmp_path, tmp_path]


def test_fresh_runtime_snapshot_bypasses_lifecycle_lock_without_state_changes(tmp_path: Path) -> None:
    initial = RuntimeSnapshot(False, False, "")
    first = RuntimeSnapshot(True, True, "wxid_first", True, True, True)
    unknown = RuntimeSnapshot(True, True, "", account_status_available=False)
    snapshots = iter((initial, first, unknown))
    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        snapshotter=lambda: next(snapshots),
        supervisor_factory=lambda progress: _Supervisor(progress),
    )
    finished = threading.Event()
    results = []

    def probe():
        results.append(controller.fresh_runtime_snapshot())
        results.append(controller.fresh_runtime_snapshot())
        finished.set()

    worker = threading.Thread(target=probe)
    with controller._lock:
        worker.start()
        completed_while_locked = finished.wait(1)
    worker.join(1)

    assert completed_while_locked
    assert not worker.is_alive()
    assert results == [first, unknown]
    assert controller.view().runtime == initial
    assert controller.state == LifecycleState.STOPPED


def test_start_rechecks_environment_before_launch(tmp_path: Path) -> None:
    environments = iter((_environment(tmp_path), _environment(tmp_path, False)))
    launches = []
    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: next(environments),
        snapshotter=lambda: RuntimeSnapshot(False, False, ""),
        supervisor_factory=lambda progress: launches.append(progress),
    )
    assert controller.view().environment.ready

    view = controller.start()

    assert view.state == LifecycleState.STOPPED
    assert not view.can_start
    assert view.error is not None and view.error.code == "11001"
    assert launches == []


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
    assert started.error is None
    assert started.title == "正在确认连接"
    assert running.state == LifecycleState.RUNNING
    assert running.online is True


def test_start_waits_for_wechat_login_without_reporting_an_error(tmp_path: Path) -> None:
    snapshots = iter(
        (
            RuntimeSnapshot(False, False, ""),
            RuntimeSnapshot(True, True, "", True, False, False),
        )
    )

    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        environment_saver=lambda root, environment: None,
        snapshotter=lambda: next(snapshots),
        process_probe=lambda: (),
        preflight=lambda root: (),
        supervisor_factory=lambda progress: _Supervisor(progress),
    )

    waiting = controller.start()

    assert waiting.state == LifecycleState.WAITING_WECHAT
    assert waiting.title == "等待登录微信"
    assert waiting.message == "请在微信窗口完成登录。"
    assert waiting.error is None


def test_status_remains_readable_while_services_are_starting(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingSupervisor(_Supervisor):
        def start(self):
            self.progress("starting", _Spec("astrbot"))
            self.progress("starting", _Spec("new-hook-bridge"))
            entered.set()
            assert release.wait(2)
            self.running = [object()]
            return tuple(self.running)

    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        environment_saver=lambda root, environment: None,
        snapshotter=lambda: RuntimeSnapshot(False, False, ""),
        process_probe=lambda: (),
        preflight=lambda root: (),
        supervisor_factory=lambda progress: BlockingSupervisor(progress),
    )
    worker = threading.Thread(target=controller.start)
    worker.start()
    assert entered.wait(1)

    status = controller.refresh()

    assert status.state == LifecycleState.CONNECTION_STARTING
    assert status.title == "正在打开机器人微信"
    assert status.busy is True
    release.set()
    worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize("unknown", [
    RuntimeSnapshot(True, True, "", False, True, True),
    RuntimeSnapshot(True, True, "", account_status_available=False),
    RuntimeSnapshot(True, False, "", False, False, False, False),
])
def test_unavailable_connection_never_requests_login_and_recovers(tmp_path: Path, unknown) -> None:
    ready = RuntimeSnapshot(True, True, "wxid_bot", True, True, True)
    logged_out = RuntimeSnapshot(True, True, "", True, True, True)
    snapshots = iter((RuntimeSnapshot(False, False, ""), unknown, ready, unknown, ready, logged_out))
    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        environment_saver=lambda root, environment: None,
        snapshotter=lambda: next(snapshots),
        process_probe=lambda: (),
        preflight=lambda root: (),
        supervisor_factory=lambda progress: _Supervisor(progress),
    )

    starting = controller.start()
    assert starting.state == LifecycleState.VERIFYING_CONNECTION
    assert starting.title == "正在确认连接"
    assert controller.refresh().state == LifecycleState.RUNNING

    unavailable = controller.refresh()
    assert unavailable.state == LifecycleState.VERIFYING_CONNECTION
    assert unavailable.title == "正在确认连接"
    assert unavailable.error is None
    assert not unavailable.online
    assert controller.refresh().state == LifecycleState.RUNNING

    logout = controller.refresh()
    assert logout.state == LifecycleState.WAITING_WECHAT
    assert logout.title == "等待登录微信"


def test_adopted_service_with_unknown_account_does_not_request_login(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from wechatrebot_manager import lifecycle

    supervisor = SimpleNamespace(
        running=[SimpleNamespace(spec=_Spec("astrbot")), SimpleNamespace(spec=_Spec("new-hook-bridge"))],
        adoption_complete=True,
        adopt=lambda: True,
    )
    monkeypatch.setattr(lifecycle, "build_service_specs", lambda root: ())
    monkeypatch.setattr(lifecycle, "ProcessSupervisor", lambda *args, **kwargs: supervisor)
    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        snapshotter=lambda: RuntimeSnapshot(True, True, "", False, True, True),
    )

    assert controller.view().state == LifecycleState.VERIFYING_CONNECTION
    assert controller.view().title == "正在确认连接"


def test_logged_in_account_is_acknowledged_while_features_are_still_starting(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    logged_in = RuntimeSnapshot(False, True, "wxid_bot", True, True, True)

    class BlockingSupervisor(_Supervisor):
        def start(self):
            self.progress("starting", _Spec("astrbot"))
            self.progress("starting", _Spec("new-hook-bridge"))
            entered.set()
            assert release.wait(2)
            self.running = [object()]
            return tuple(self.running)

    snapshots = iter((RuntimeSnapshot(False, False, ""), logged_in, logged_in))
    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(tmp_path),
        environment_saver=lambda root, environment: None,
        snapshotter=lambda: next(snapshots),
        process_probe=lambda: (),
        preflight=lambda root: (),
        supervisor_factory=lambda progress: BlockingSupervisor(progress),
    )
    worker = threading.Thread(target=controller.start)
    worker.start()
    assert entered.wait(1)

    status = controller.refresh()

    assert status.state == LifecycleState.CONNECTION_STARTING
    assert status.title == "微信已登录，正在启动机器人功能"
    assert status.message == "账号连接成功，剩余功能正在后台加载，完成后会自动变为可用。"
    assert status.busy is True
    assert status.online is False
    release.set()
    worker.join(2)
    assert not worker.is_alive()


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


def test_force_stop_bypasses_wechat_wait_and_returns_to_stopped(tmp_path: Path) -> None:
    snapshots = iter(
        (
            RuntimeSnapshot(False, False, ""),
            RuntimeSnapshot(True, True, "wxid_bot", True, True, True),
            RuntimeSnapshot(False, False, ""),
        )
    )
    holder = {}

    def factory(progress):
        holder["supervisor"] = _Supervisor(progress)
        holder["supervisor"].forced = False
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

    stopped = controller.force_stop()

    assert stopped.state == LifecycleState.STOPPED
    assert stopped.can_stop is False
    assert stopped.can_start is True
    assert holder["supervisor"].forced is True


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
