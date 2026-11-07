"""Invalid display probes never prevent stopping already owned services."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_lifecycle import _Supervisor, _environment
from wechatrebot_manager import lifecycle, runtime_status
from wechatrebot_manager.lifecycle import LifecycleController, LifecycleState
from wechatrebot_manager.launcher import ServiceStopError
from wechatrebot_manager.runtime_status import RuntimeSnapshot


def _running_controller(tmp_path, *, licensed=False):
    controller = LifecycleController(
        tmp_path,
        environment_checker=lambda root: _environment(root),
        snapshotter=lambda: RuntimeSnapshot(False, False, ""),
        supervisor_factory=lambda progress: _Supervisor(progress),
        license_client=SimpleNamespace(
            snapshot=lambda _account: {"enabled": licensed, "can_run": False, "message": "Paused"},
            pause=Mock(),
        ),
    )
    supervisor = _Supervisor(lambda *_args: None)
    supervisor.running = [object()]
    controller._supervisor = supervisor
    controller.state = LifecycleState.RUNNING
    controller._running_events_emitted = True
    controller._wechat_closed_confirmations = 1
    return controller, supervisor


def _assert_configuration_error(view, *, owned):
    assert view.state == LifecycleState.ERROR
    assert view.error is not None and view.error.code == "13001"
    assert view.error.solutions
    assert "配置" in view.message
    assert view.can_stop is owned
    assert not view.can_start and not view.online
    assert not view.runtime.account_status_available


@pytest.mark.parametrize("raw", [b"{broken", b'{"ports":{"bridge_callback":0}}'])
def test_invalid_ports_keep_normal_stop_safe_and_allow_explicit_force_stop(tmp_path, monkeypatch, raw):
    controller, supervisor = _running_controller(tmp_path)
    manager = tmp_path / "config/manager.json"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(raw)
    controller._snapshotter = lambda: runtime_status.runtime_snapshot(install_root=tmp_path)
    monkeypatch.setattr(runtime_status, "port_accepting", lambda *_a, **_k: pytest.fail("no default port probe"))
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda **_k: pytest.fail("no other bridge probe"))

    waiting = controller.request_stop()

    _assert_configuration_error(waiting, owned=True)
    assert supervisor.running and not supervisor.stopped
    assert controller._supervisor is supervisor

    stopped = controller.force_stop()

    _assert_configuration_error(stopped, owned=False)
    assert supervisor.forced and not supervisor.running
    assert controller._supervisor is None
    assert not controller._running_events_emitted
    assert controller._wechat_closed_confirmations == 0
    assert manager.read_bytes() == raw


@pytest.mark.parametrize("failure", [OSError, ValueError])
@pytest.mark.parametrize("licensed", [False, True])
def test_force_stop_reports_configuration_problem_after_owned_shutdown(tmp_path, failure, licensed):
    controller, supervisor = _running_controller(tmp_path, licensed=licensed)

    def unavailable():
        raise failure("synthetic unavailable ports")

    controller._snapshotter = unavailable
    stopped = controller.force_stop()

    _assert_configuration_error(stopped, owned=False)
    assert supervisor.forced and not supervisor.running
    assert controller._supervisor is None
    assert not controller._running_events_emitted
    assert controller._wechat_closed_confirmations == 0
    assert controller.licenses.pause.call_count == int(licensed)


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_normal_stop_finishes_when_only_the_post_stop_probe_fails(tmp_path, failure):
    controller, supervisor = _running_controller(tmp_path)
    snapshots = iter((RuntimeSnapshot(False, False, ""), failure("synthetic unavailable ports")))

    def snapshot():
        value = next(snapshots)
        if isinstance(value, Exception):
            raise value
        return value

    controller._snapshotter = snapshot
    stopped = controller.request_stop()

    _assert_configuration_error(stopped, owned=False)
    assert supervisor.stopped and not supervisor.running
    assert controller._supervisor is None
    assert not controller._running_events_emitted
    assert controller._wechat_closed_confirmations == 0

    controller._snapshotter = lambda: RuntimeSnapshot(False, False, "")
    repaired = controller.refresh()
    assert repaired.state == LifecycleState.STOPPED
    assert repaired.error is None and repaired.can_start and not repaired.can_stop


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_update_stop_requires_a_fresh_initial_probe(tmp_path, monkeypatch, failure):
    controller, supervisor = _running_controller(tmp_path)

    def unavailable():
        raise failure("synthetic unavailable ports")

    controller._snapshotter = unavailable
    monkeypatch.setattr(lifecycle, "capture_update_wechat_process", Mock(side_effect=AssertionError("no capture")))
    monkeypatch.setattr(lifecycle, "force_stop_update_processes", Mock(side_effect=AssertionError("no process stop")))

    with pytest.raises(failure, match="synthetic unavailable ports"):
        controller.force_stop_for_update()

    assert supervisor.running and not supervisor.stopped
    assert controller._supervisor is supervisor


def test_update_stop_does_not_accept_display_fallback_as_final_proof(tmp_path, monkeypatch):
    controller, supervisor = _running_controller(tmp_path)
    calls = []

    def snapshot():
        calls.append("probe")
        if len(calls) == 1:
            return RuntimeSnapshot(False, False, "")
        raise ValueError("synthetic unavailable ports")

    controller._snapshotter = snapshot
    owned_wechat = object()
    monkeypatch.setattr(lifecycle, "capture_update_wechat_process", lambda *_a, **_k: owned_wechat)
    stop_processes = Mock()
    monkeypatch.setattr(lifecycle, "force_stop_update_processes", stop_processes)

    with pytest.raises(ValueError, match="synthetic unavailable ports"):
        controller.force_stop_for_update()

    assert supervisor.forced and not supervisor.running
    assert calls == ["probe", "probe", "probe"]
    stop_processes.assert_called_once_with(tmp_path, wechat_process=owned_wechat)
    _assert_configuration_error(controller.view(), owned=False)
    assert not controller.maintenance_stopped()


@pytest.mark.parametrize("action", ["stop_robot", "force_stop_robot"])
@pytest.mark.parametrize("licensed", [False, True])
@pytest.mark.parametrize("failure", [ServiceStopError("astrbot", "core"), PermissionError("synthetic state write")])
def test_accepted_stop_failure_preserves_ownership_and_force_retry(tmp_path, monkeypatch, action, licensed, failure):
    from test_app_shell import _api
    from test_license_lifecycle import setup_controller

    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()
    supervisor = holders[0]
    if not licensed:
        monkeypatch.setattr(license, "snapshot", lambda _account="": {"enabled": False})
        world.update(account="", bridge=False)
    operation = ("force_stop" if action == "force_stop_robot"
                 else "stop_names" if licensed else "stop")
    stop = getattr(supervisor, operation)
    failed_stop = Mock(side_effect=failure)
    monkeypatch.setattr(supervisor, operation, failed_stop)
    api = _api(tmp_path)
    api._lifecycle = controller

    assert getattr(api, action)()["data"]["accepted"]
    assert api._action_lock.acquire(timeout=3)
    api._action_lock.release()

    for _ in range(3):
        status = api.get_status()
        assert status["ok"]
        view = status["data"]
        assert view["state"] == "ERROR" and view["error"]["code"] == "13008"
        assert view["title"] == "机器人停止未完成"
        assert view["can_stop"] and not view["can_start"]
        assert not view["busy"] and not view["online"] and not view["start_pending"]
    assert failed_stop.call_count == 1, "polling must not repeatedly terminate services"
    assert controller._supervisor is supervisor and len(supervisor.running) == 2
    assert not controller._start_services().can_start
    assert controller.check_environment().error.code == "13008"

    monkeypatch.setattr(supervisor, operation, stop)
    world.update(account="", bridge=False)
    assert api.force_stop_robot()["data"]["accepted"]
    assert api._action_lock.acquire(timeout=3)
    api._action_lock.release()
    status = api.get_status()["data"]
    assert status["state"] == "STOPPED" and status["error"] is None
    assert status["can_start"] and not status["can_stop"] and not status["busy"]
    assert not world["core"] and not world["bridge"] and not supervisor.running
    assert controller._supervisor is None


def test_stop_state_write_failure_can_retry_after_all_processes_exited(tmp_path, monkeypatch):
    controller, supervisor = _running_controller(tmp_path)
    attempts = []

    def stop():
        attempts.append(True)
        supervisor.running.clear()
        if len(attempts) == 1:
            raise PermissionError("synthetic state cleanup failure")

    monkeypatch.setattr(supervisor, "force_stop", stop)
    with pytest.raises(PermissionError):
        controller.force_stop()
    failed = controller.refresh()
    assert failed.error.code == "13008" and failed.can_stop and not failed.can_start
    assert not supervisor.running and len(attempts) == 1

    stopped = controller.force_stop()
    assert len(attempts) == 2, "retry must also finish failed process-state cleanup"
    assert stopped.state == LifecycleState.STOPPED and stopped.error is None
    assert stopped.can_start and not stopped.can_stop


def test_authorization_cleanup_failure_returns_recoverable_status_without_repeated_stops(tmp_path, monkeypatch):
    from test_license_lifecycle import setup_controller

    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()
    license.granted = False
    failed_stop = Mock(side_effect=ServiceStopError("astrbot", "core"))
    monkeypatch.setattr(holders[0], "stop_names", failed_stop)

    failed = controller.refresh()
    assert failed.error.code == "13008" and failed.can_stop and not failed.can_start
    assert failed.state == LifecycleState.ERROR and not failed.online
    assert controller.refresh().error == failed.error
    assert failed_stop.call_count == 1
    assert controller._supervisor is holders[0] and world["core"]


def test_update_stop_failure_remains_an_error_and_does_not_continue_to_wechat(tmp_path, monkeypatch):
    controller, supervisor = _running_controller(tmp_path)
    failure = ServiceStopError("astrbot", "core")
    monkeypatch.setattr(supervisor, "force_stop", Mock(side_effect=failure))
    monkeypatch.setattr(lifecycle, "capture_update_wechat_process", lambda *_a, **_k: None)
    stop_processes = Mock()
    monkeypatch.setattr(lifecycle, "force_stop_update_processes", stop_processes)

    with pytest.raises(ServiceStopError) as error:
        controller.force_stop_for_update()

    assert error.value is failure
    stop_processes.assert_not_called()
    assert controller.refresh().error.code == "13008"
    assert controller._supervisor is supervisor and supervisor.running
