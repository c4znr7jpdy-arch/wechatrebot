"""Unexpected exits retain process facts before the supervisor clears them."""

import json
from types import SimpleNamespace

import pytest

from wechatrebot_manager.activity_service import ActivityService
from wechatrebot_manager.lifecycle import LifecycleController, LifecycleState
from wechatrebot_manager.runtime_status import RuntimeSnapshot

from test_lifecycle import _environment
from test_license_lifecycle import setup_controller


def service(name, code):
    process = SimpleNamespace(code=code, argv=["token=private-argument"])
    process.poll = lambda: process.code
    label = "微信连接" if name == "new-hook-bridge" else "回复引擎"
    return SimpleNamespace(spec=SimpleNamespace(name=name, label=label, environment={"TOKEN": "private-env"}), process=process)


def controller_with_services(tmp_path, services, *, stop_fails=False):
    def stop(**kwargs):
        if stop_fails:
            raise OSError("cannot stop live service")
        for item in tuple(supervisor.running):
            item.process.code = -15
        supervisor.running.clear()

    supervisor = SimpleNamespace(running=list(services), stop=stop)
    controller = LifecycleController(
        tmp_path, environment_checker=lambda _: _environment(tmp_path),
        snapshotter=lambda: RuntimeSnapshot(False, False, ""),
        supervisor_factory=lambda _: supervisor,
        license_client=SimpleNamespace(snapshot=lambda _: {"enabled": False}),
    )
    controller._supervisor = supervisor
    return controller, supervisor


@pytest.mark.parametrize("exit_code", [0, 1, -9, 0xC0000005])
def test_bridge_exit_records_original_code_before_cleanup(tmp_path, exit_code):
    bridge = service("new-hook-bridge", exit_code)
    survivor = service("astrbot", None)
    controller, supervisor = controller_with_services(tmp_path, [bridge, survivor])

    failed = controller.refresh()

    assert failed.error.code == "13002" and failed.state == LifecycleState.ERROR
    assert not supervisor.running and survivor.process.code == -15
    controller.refresh()
    errors = [record for record in controller.events.recent() if record.get("event") == "APP_ERROR"]
    assert len(errors) == 1
    assert errors[0]["message"] == "托管服务意外退出。"
    assert errors[0]["details"] == {
        "operation": "runtime_supervision",
        "technical": {"exited_services": [{"service": "微信连接", "exit_code": exit_code}]},
    }
    report = next(item["report"] for item in ActivityService(tmp_path).recent() if item["id"] == errors[0]["event_id"])
    assert '"exit_code": ' + str(exit_code) in report
    assert "runtime_supervision" in report
    assert "private-argument" not in report and "private-env" not in report


def test_simultaneous_exits_record_each_service_once(tmp_path):
    controller, _ = controller_with_services(tmp_path, [service("astrbot", 2), service("new-hook-bridge", 3)])
    controller.refresh()
    error = next(record for record in controller.events.recent() if record.get("code") == "13002")
    assert error["details"]["technical"]["exited_services"] == [
        {"service": "回复引擎", "exit_code": 2}, {"service": "微信连接", "exit_code": 3},
    ]


def test_failed_cleanup_keeps_stop_error_and_process_ownership(tmp_path):
    controller, supervisor = controller_with_services(
        tmp_path, [service("new-hook-bridge", 1), service("astrbot", None)], stop_fails=True,
    )
    failed = controller.refresh()
    assert failed.error.code == "13008" and failed.can_stop and not failed.can_start
    assert controller._supervisor is supervisor and len(supervisor.running) == 2
    assert not any(record.get("code") == "13002" for record in controller.events.recent())


def test_licensed_core_exit_preserves_bridge_and_exit_evidence(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_private_account"
    controller.start()
    core = next(item for item in holders[0].running if item.spec.name == "astrbot")
    core.process.code = 7
    failed = controller.refresh()
    assert failed.error.code == "13004" and failed.can_start and world["bridge"]
    error = next(record for record in controller.events.recent() if record.get("code") == "13004")
    assert error["details"]["technical"]["exited_services"] == [{"service": "回复引擎", "exit_code": 7}]
    assert "wxid_private_account" not in json.dumps(error)


def test_live_services_do_not_emit_exit_errors(tmp_path):
    controller, supervisor = controller_with_services(tmp_path, [service("new-hook-bridge", None)])
    controller.refresh()
    assert controller._supervisor is supervisor and supervisor.running
    assert not any(record.get("event") == "APP_ERROR" for record in controller.events.recent())
