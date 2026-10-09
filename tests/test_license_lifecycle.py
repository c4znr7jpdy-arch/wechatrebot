from pathlib import Path
from types import SimpleNamespace

import pytest

from wechatrebot_manager.environment import WechatEnvironment
from wechatrebot_manager.lifecycle import LifecycleController, LifecycleState
from wechatrebot_manager.licensing.protocol import LicenseError
from wechatrebot_manager.runtime_status import RuntimeSnapshot


class License:
    def __init__(self):
        self.valid = True
        self.bound = ""
        self.granted = False
        self.grants = []
        self.pause_error = False

    def snapshot(self, account=""):
        return {"enabled": True, "authorized": self.valid, "can_connect": self.valid,
                "can_run": self.valid and self.granted and bool(account) and account == self.bound,
                "state": "ready" if self.valid else "expired", "message": "测试授权状态"}

    def authorize_run(self, account):
        if not self.valid or not self.bound or account != self.bound:
            raise LicenseError("denied", "不能运行")
        self.grants.append(account)
        self.granted = True

    def pause(self):
        if self.pause_error:
            raise LicenseError("storage_error", "授权存储暂不可用")
        self.granted = False


class Supervisor:
    def __init__(self, world, progress):
        self.world = world
        self.progress = progress
        self.running = []
        self.started = []
        self.stopped = []
        self.adoption_complete = False

    def start_missing(self, names):
        for name in names:
            if any(item.spec.name == name for item in self.running):
                continue
            self.started.append(name)
            spec = SimpleNamespace(name=name)
            process = SimpleNamespace(code=None)
            process.poll = lambda p=process: p.code
            self.running.append(SimpleNamespace(spec=spec, process=process))
            self.world["core" if name == "astrbot" else "bridge"] = True
            self.progress("starting", spec)
            self.progress("ready", spec)
        return tuple(self.running)

    def stop_names(self, names, timeout_seconds=15):
        for item in tuple(self.running):
            if item.spec.name in names:
                self.stopped.append(item.spec.name)
                self.running.remove(item)
                self.world["core" if item.spec.name == "astrbot" else "bridge"] = False

    def stop(self, timeout_seconds=15):
        self.stop_names(tuple(item.spec.name for item in self.running))

    def force_stop(self):
        self.stop()

    def adopt(self):
        return bool(self.running)


def setup_controller(tmp_path: Path):
    world = {"account": "", "core": False, "bridge": False}
    license = License()
    holders = []
    def factory(progress):
        supervisor = Supervisor(world, progress)
        holders.append(supervisor)
        return supervisor
    def snapshot():
        return RuntimeSnapshot(world["core"], world["bridge"], world["account"],
                               world["bridge"], world["core"], world["bridge"])
    environment = WechatEnvironment(True, "10000", "就绪", "就绪", (),
                                    str(tmp_path / "Weixin.exe"), "4.1.8.27", "test")
    controller = LifecycleController(tmp_path, license_client=license,
        environment_checker=lambda _: environment, environment_saver=lambda *_: None,
        snapshotter=snapshot, preflight=lambda _: (), supervisor_factory=factory)
    return controller, license, world, holders


def test_redemption_connects_only_bridge_then_explicit_start_opens_core(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    connected = controller.connect_wechat()
    supervisor = holders[0]
    assert supervisor.started == ["new-hook-bridge"]
    assert connected.can_start and connected.can_stop
    assert not license.grants and not connected.online
    world["account"] = "wxid_a"
    with pytest.raises(LicenseError):
        controller.start()
    license.bound = "wxid_a"
    running = controller.start()
    assert license.grants == ["wxid_a"]
    assert supervisor.started == ["new-hook-bridge", "astrbot"]
    assert running.online and running.state == LifecycleState.RUNNING


def test_expiry_preserves_wechat_and_renewal_requires_explicit_resume(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()
    license.valid = False
    expired = controller.refresh()
    assert expired.state == LifecycleState.LICENSE_PAUSED
    assert not expired.online and world["bridge"] and not world["core"]
    assert holders[0].stopped == ["astrbot"]
    license.valid = True
    ready = controller.refresh()
    assert ready.can_start and not ready.online and not license.granted
    assert holders[0].started.count("astrbot") == 1
    controller.start()
    assert holders[0].started.count("astrbot") == 2
    assert holders[0].started.count("new-hook-bridge") == 1


def test_account_switch_pauses_and_does_not_bind_or_resume_automatically(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()
    world["account"] = "wxid_b"
    assert not controller.refresh().online
    assert not world["core"] and world["bridge"]
    assert license.bound == "wxid_a"
    with pytest.raises(LicenseError):
        controller.start()
    assert holders[0].started.count("astrbot") == 1


def test_no_entitlement_cannot_connect_or_launch(tmp_path):
    controller, license, _, holders = setup_controller(tmp_path)
    license.valid = False
    with pytest.raises(LicenseError):
        controller.connect_wechat()
    with pytest.raises(LicenseError):
        controller.start()
    assert holders == []


def test_core_crash_is_reaped_without_closing_wechat(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()
    core = next(item for item in holders[0].running if item.spec.name == "astrbot")
    core.process.code = 1
    controller.refresh()
    assert not license.granted
    assert world["bridge"] and not world["core"]
    assert [item.spec.name for item in holders[0].running] == ["new-hook-bridge"]


def test_stop_still_works_when_authorization_storage_is_broken(tmp_path):
    controller, license, world, _ = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()
    license.pause_error = True
    stopped = controller.request_stop()
    assert not world["core"] and not world["bridge"]
    assert "暂停未能写入" in stopped.message
    controller.force_stop()
    assert not world["bridge"]


def test_pause_writer_busy_stops_bridge_and_reconnect_clears_old_grant(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()
    original_pause = license.pause
    license.pause = lambda: (_ for _ in ()).throw(LicenseError("busy", "授权正在刷新"))
    controller.request_stop()
    assert not holders[0].running  # No delayed bridge sender remains alive.
    assert license.granted  # Simulates the cache still held by the refresh writer.
    with pytest.raises(LicenseError, match="刷新"):
        controller.connect_wechat()
    assert not world["bridge"]
    license.pause = original_pause
    controller.connect_wechat()
    assert not license.granted and world["bridge"] and not world["core"]


def test_reopen_accepts_existing_connection_only_processes(tmp_path, monkeypatch):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    candidate = holders[0]
    monkeypatch.setattr("wechatrebot_manager.lifecycle.build_service_specs", lambda _: ())
    monkeypatch.setattr("wechatrebot_manager.lifecycle.ProcessSupervisor", lambda *a, **kw: candidate)
    reopened = LifecycleController(tmp_path, license_client=license,
        environment_checker=controller._environment_checker,
        snapshotter=controller._snapshotter, preflight=lambda _: ())
    view = reopened.refresh()
    assert view.state == LifecycleState.LICENSE_READY
    assert view.error is None and view.can_start and view.can_stop
    assert world["bridge"] and not world["core"]
