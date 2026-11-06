"""Invalid display probes never prevent stopping already owned services."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_lifecycle import _Supervisor, _environment
from wechatrebot_manager import lifecycle, runtime_status
from wechatrebot_manager.lifecycle import LifecycleController, LifecycleState
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
