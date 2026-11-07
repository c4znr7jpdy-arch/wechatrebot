from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from wechatrebot_manager.environment import WechatEnvironment
from wechatrebot_manager.lifecycle import LifecycleController, LifecycleState
from wechatrebot_manager.licensing.protocol import LicenseError
from wechatrebot_manager.runtime_status import RuntimeSnapshot

from test_license_client import activate, setup as license_setup


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
                "state": "ready" if self.valid else "expired", "message": "测试授权状态",
                "account": self.bound}

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
        exited = tuple(item.spec.name for item in self.running if item.process.poll() is not None)
        if exited:
            self.stop_names(exited)
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


def setup_controller(tmp_path: Path, *, license_client=None):
    world = {"account": "", "core": False, "bridge": False}
    license = license_client or License()
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


def test_unactivated_identity_connection_keeps_only_bridge_without_consuming_trial(license_setup):
    client, service, _, _ = license_setup
    controller, _, world, holders = setup_controller(client.root, license_client=client)

    connected = controller.connect_wechat()
    assert holders[0].started == ["new-hook-bridge"]
    assert connected.can_stop and not connected.online
    assert world["bridge"] and not world["core"]

    world["account"] = "wxid_new_trial"
    for _ in range(3):
        status = controller.refresh()
        assert status.state == LifecycleState.LICENSE_PAUSED and not status.online
    assert holders[0].stopped == []
    assert client.snapshot(world["account"])["state"] == "unactivated"
    assert not client.snapshot(world["account"])["can_run"]
    with pytest.raises(LicenseError):
        controller.start()
    assert holders[0].started == ["new-hook-bridge"]
    assert not service.calls and not client.directory.exists()


def test_identity_connection_requires_a_configured_license_service(license_setup):
    client, service, _, _ = license_setup
    (client.root / "config/licensing.json").write_text(
        '{"schema_version":1,"enabled":true}', encoding="utf-8",
    )
    controller, _, _, holders = setup_controller(client.root, license_client=client)
    with pytest.raises(LicenseError) as error:
        controller.connect_wechat()
    assert error.value.code == "configuration_pending"
    assert not holders and not service.calls and not client.directory.exists()


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


def test_valid_license_without_services_is_stopped_and_never_starts_itself(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"

    for _ in range(2):
        ready = controller.refresh()
        assert ready.state == LifecycleState.STOPPED
        assert ready.title == "机器人未启动"
        assert ready.message == "授权有效，点击启动机器人即可运行。"
        assert ready.can_start and not ready.can_stop and not ready.online
    assert not holders and not license.granted
    assert not world["core"] and not world["bridge"]


def test_manual_stop_retains_connection_with_neutral_authorized_feedback(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()

    stopped = controller.request_stop()
    assert stopped.state == LifecycleState.LICENSE_READY
    assert stopped.title == "机器人未启动"
    assert stopped.message == "授权有效，点击启动机器人即可运行。"
    assert stopped.can_start and stopped.can_stop and not stopped.online
    assert not license.granted and not world["core"] and world["bridge"]
    assert controller.refresh().state == LifecycleState.LICENSE_READY
    assert holders[0].started.count("astrbot") == 1


def test_signed_paused_run_grant_does_not_turn_valid_entitlement_into_a_warning(license_setup):
    client, _, _, _ = license_setup
    activate(client)
    client.bind("wxid_a")
    controller, _, world, _ = setup_controller(client.root, license_client=client)
    controller.connect_wechat()
    world["account"] = "wxid_a"
    controller.start()

    stopped = controller.request_stop()
    assert client.snapshot("wxid_a")["state"] == "paused"
    assert stopped.state == LifecycleState.LICENSE_READY
    assert stopped.message == "授权有效，点击启动机器人即可运行。"
    assert not stopped.online and not world["core"] and world["bridge"]


def test_start_reconnects_then_retains_one_request_until_real_login(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"

    waiting = controller.start()
    assert holders[0].started == ["new-hook-bridge"]
    assert world["bridge"] and not world["core"] and not license.granted
    assert waiting.state == LifecycleState.WAITING_WECHAT
    assert waiting.title == "等待登录微信"
    assert waiting.start_pending and not waiting.busy
    assert not waiting.can_start and waiting.can_stop
    assert not license.grants and not waiting.online

    world["account"] = "wxid_a"
    ready = controller.refresh()
    assert ready.state == LifecycleState.CORE_STARTING
    assert ready.start_pending and ready.busy and not ready.can_start
    assert not world["core"] and not license.grants
    # Duplicate starts cannot consume or create another continuation.
    assert controller.start().start_pending and not license.grants
    assert controller.resume_pending_start().state == LifecycleState.RUNNING
    assert not controller.view().start_pending
    assert holders[0].started == ["new-hook-bridge", "astrbot"]
    assert license.grants == ["wxid_a"]


@pytest.mark.parametrize("action", ["request_stop", "force_stop"])
def test_stop_cancels_explicit_start_waiting_for_login(tmp_path, action):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    assert controller.start().start_pending

    stopped = getattr(controller, action)()
    assert not stopped.start_pending
    world["account"] = "wxid_a"
    controller.refresh()
    controller.resume_pending_start()

    assert not license.grants and not world["core"]
    assert holders[0].started == ["new-hook-bridge"]


@pytest.mark.parametrize("change", ["expired", "account", "binding", "bridge_exit"])
def test_pending_start_cannot_survive_changed_authorization_or_owned_bridge(tmp_path, change):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    assert controller.start().start_pending
    if change == "expired":
        license.valid = False
    elif change == "account":
        world["account"] = "wxid_other"
    elif change == "binding":
        license.bound = "wxid_other"
    else:
        holders[0].running[0].process.code = 1
        world["bridge"] = False

    assert not controller.refresh().start_pending
    license.valid = True
    license.bound = world["account"] = "wxid_a"
    controller.resume_pending_start()
    assert not license.grants and not world["core"]
    assert holders[0].started == ["new-hook-bridge"]


def test_unknown_login_state_retains_request_without_grant_or_core(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    controller.start()
    snapshot = controller._snapshotter
    controller._snapshotter = lambda: RuntimeSnapshot(
        False, True, "wxid_a", hook_ready=True, account_status_available=False,
    )
    assert controller.refresh().start_pending
    assert not controller.pending_start_ready()
    assert controller.resume_pending_start().start_pending
    assert not license.grants and not world["core"]
    assert holders[0].started == ["new-hook-bridge"]
    controller._snapshotter = snapshot
    world["account"] = "wxid_a"
    controller.refresh()
    assert controller.resume_pending_start().online


def test_status_poll_cannot_revoke_grant_before_guarded_core_launch(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    license.bound = world["account"] = "wxid_a"
    before_services, continue_start = threading.Event(), threading.Event()
    start_services = controller._start_services
    results = []

    def delayed_services(*args, **kwargs):
        before_services.set()
        assert continue_start.wait(3)
        return start_services(*args, **kwargs)

    controller._start_services = delayed_services
    worker = threading.Thread(target=lambda: results.append(controller.start()))
    worker.start()
    try:
        assert before_services.wait(2)
        # The grant is issued only inside the lifecycle start lock, so a poll
        # during this gap cannot see and revoke an unprotected fresh grant.
        assert not license.granted
        controller.refresh()
    finally:
        continue_start.set()
        worker.join(3)
    assert not worker.is_alive()
    assert results[0].online and license.granted
    assert holders[0].started == ["new-hook-bridge", "astrbot"]


def test_status_poll_during_connect_preparation_keeps_explicit_start_request(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    connect_ready, finish_connect = threading.Event(), threading.Event()
    connect = controller.connect_wechat
    results = []

    def delayed_connect():
        connect_ready.set()
        assert finish_connect.wait(3)
        return connect()

    controller.connect_wechat = delayed_connect
    worker = threading.Thread(target=lambda: results.append(controller.start()))
    worker.start()
    try:
        assert connect_ready.wait(2)
        assert controller.refresh().start_pending
    finally:
        finish_connect.set()
        worker.join(3)
    assert not worker.is_alive() and results[0].start_pending
    world["account"] = "wxid_a"
    controller.refresh()
    assert controller.resume_pending_start().online
    assert holders[0].started == ["new-hook-bridge", "astrbot"]


def test_api_queue_acceptance_is_busy_before_start_worker_runs(tmp_path, monkeypatch):
    from test_app_shell import _api
    from wechatrebot_manager import app_shell

    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    api = _api(tmp_path)
    api._lifecycle = controller
    workers = []
    monkeypatch.setattr(app_shell.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=lambda: workers.append(kwargs["target"]),
    ))

    assert api.start_robot()["data"]["accepted"]
    for _ in range(2):
        status = api.get_status()["data"]
        assert status["start_pending"] and status["busy"]
        assert status["state"] == "CORE_STARTING" and not status["can_start"]
        assert not api.start_robot()["data"]["accepted"]
    assert len(workers) == 1 and not license.grants
    workers.pop()()
    status = api.get_status()["data"]
    assert status["online"] and not status["start_pending"]
    assert holders[0].started == ["new-hook-bridge", "astrbot"]
    assert license.grants == ["wxid_a"]


def test_pending_login_resumes_signed_paused_grant_only_after_bound_account(license_setup):
    client, service, _, _ = license_setup
    activate(client)
    client.bind("wxid_a")
    client.pause()
    controller, _, world, holders = setup_controller(client.root, license_client=client)
    calls = len(service.calls)

    assert controller.start().start_pending
    assert client.snapshot()["state"] == "paused"
    assert not client.snapshot()["can_run"] and not world["core"]
    world["account"] = "wxid_a"
    controller.refresh()
    assert controller.resume_pending_start().online
    status = client.snapshot("wxid_a")
    assert status["state"] == "running" and status["can_run"] and status["run_id"]
    assert holders[0].started == ["new-hook-bridge", "astrbot"]
    assert len(service.calls) == calls


def test_api_status_continues_start_once_and_leaves_waiting_cancellable(tmp_path):
    from test_app_shell import _api

    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    api = _api(tmp_path)
    api._lifecycle = controller

    def wait_action():
        assert api._action_lock.acquire(timeout=3)
        api._action_lock.release()

    assert api.start_robot()["data"]["accepted"]
    wait_action()
    waiting = api.get_status()["data"]
    assert waiting["start_pending"] and not waiting["busy"]
    assert not waiting["can_start"] and waiting["can_stop"]
    assert api.stop_robot()["data"]["accepted"]
    wait_action()
    world["account"] = "wxid_a"
    assert not api.get_status()["data"]["start_pending"]
    assert not license.grants

    world["account"] = ""
    assert api.start_robot()["data"]["accepted"]
    wait_action()
    launch_started, finish_launch = threading.Event(), threading.Event()
    supervisor = holders[0]
    start_missing = supervisor.start_missing

    def delayed_core(names):
        assert license.granted
        launch_started.set()
        assert finish_launch.wait(3)
        return start_missing(names)

    supervisor.start_missing = delayed_core
    world["account"] = "wxid_a"
    assert api.get_status()["data"]["start_pending"]
    try:
        assert launch_started.wait(2)
        for _ in range(3):
            status = api.get_status()["data"]
            assert status["start_pending"] and status["busy"]
            assert not status["can_start"]
            assert not api.start_robot()["data"]["accepted"]
        assert license.grants == ["wxid_a"]
    finally:
        finish_launch.set()
        wait_action()
    assert api.get_status()["data"]["online"]
    assert not api.get_status()["data"]["start_pending"]
    assert supervisor.started == ["new-hook-bridge", "astrbot"]
    assert license.grants == ["wxid_a"]


def test_pending_core_failure_clears_request_and_requires_a_new_start(tmp_path):
    from wechatrebot_manager.launcher import ServiceLaunchError

    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    controller.start()
    world["account"] = "wxid_a"
    controller.refresh()
    holders[0].start_missing = lambda _names: (_ for _ in ()).throw(ServiceLaunchError("core failed"))

    failed = controller.resume_pending_start()
    assert failed.state == LifecycleState.ERROR and not failed.start_pending
    assert not license.granted and not world["core"]
    assert not controller.refresh().start_pending
    controller.resume_pending_start()
    assert license.grants == ["wxid_a"]
    assert holders[0].started == ["new-hook-bridge"]


def test_pending_resume_rechecks_account_and_cancellation_after_it_is_queued(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    controller.start()
    world["account"] = "wxid_a"
    assert controller.refresh().start_pending and controller.pending_start_ready()

    # The action queue can deliver a previously accepted resume after a stop.
    controller.request_stop()
    assert not controller.resume_pending_start().start_pending
    assert not license.grants

    world["account"] = ""
    controller.start()
    world["account"] = "wxid_a"
    controller.refresh()
    assert controller.pending_start_ready()
    world["account"] = "wxid_other"
    assert not controller.resume_pending_start().start_pending
    assert not license.grants and not world["core"]
    assert holders[0].started == ["new-hook-bridge"]


def test_api_pending_resume_thread_failure_unlocks_and_cancels_request(tmp_path, monkeypatch):
    from test_app_shell import _api
    from wechatrebot_manager import app_shell

    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    controller.start()
    api = _api(tmp_path)
    api._lifecycle = controller
    world["account"] = "wxid_a"

    def cannot_start(_thread):
        raise RuntimeError("cannot create worker")

    monkeypatch.setattr(app_shell.threading.Thread, "start", cannot_start)
    api.get_status()
    assert not controller.view().start_pending
    assert api._action_lock.acquire(blocking=False)
    api._action_lock.release()
    assert not license.grants and not world["core"]
    assert holders[0].started == ["new-hook-bridge"]


@pytest.mark.parametrize("action", ["stop_robot", "force_stop_robot"])
def test_api_stop_wins_over_resume_worker_that_has_not_started(tmp_path, monkeypatch, action):
    from test_app_shell import _api
    from wechatrebot_manager import app_shell

    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    controller.start()
    api = _api(tmp_path)
    api._lifecycle = controller
    workers = []
    monkeypatch.setattr(app_shell.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=lambda: workers.append(kwargs["target"]),
    ))
    world["account"] = "wxid_a"
    api.get_status()
    assert len(workers) == 1
    assert getattr(api, action)()["data"]["accepted"]
    assert api.get_status()["data"]["state"] == "STOPPING"
    workers.pop()()
    assert not api.get_status()["data"]["start_pending"]
    assert not license.grants and not world["core"]
    assert holders[0].started == ["new-hook-bridge"]


@pytest.mark.parametrize("change", ["stop", "expired", "account"])
def test_cancellation_during_authorized_handoff_cannot_revive_old_request(tmp_path, change):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    controller.start()
    world["account"] = "wxid_a"
    controller.refresh()
    entered, finish = threading.Event(), threading.Event()
    authorize = controller._start_authorized
    results = []

    def delayed_authorize(*args):
        entered.set()
        assert finish.wait(3)
        return authorize(*args)

    controller._start_authorized = delayed_authorize
    worker = threading.Thread(target=lambda: results.append(controller.resume_pending_start()))
    worker.start()
    try:
        assert entered.wait(2)
        if change == "stop":
            controller.request_stop()
        elif change == "expired":
            license.valid = False
            controller.refresh()
            license.valid = True
        else:
            world["account"] = "wxid_other"
            controller.refresh()
            world["account"] = "wxid_a"
    finally:
        finish.set()
        worker.join(3)
    assert not worker.is_alive() and not results[0].start_pending
    assert not license.grants and not world["core"]
    assert holders[0].started == ["new-hook-bridge"]


def test_api_stop_during_core_launch_runs_before_releasing_action_queue(tmp_path):
    from test_app_shell import _api

    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    api = _api(tmp_path)
    api._lifecycle = controller
    started, finish = threading.Event(), threading.Event()
    start_missing = holders[0].start_missing

    def delayed_start(names):
        started.set()
        assert finish.wait(3)
        return start_missing(names)

    holders[0].start_missing = delayed_start
    assert api.start_robot()["data"]["accepted"]
    try:
        assert started.wait(2)
        assert api.stop_robot()["data"]["accepted"]
        assert not license.granted
        assert not api.start_robot()["data"]["accepted"]
        assert api.get_status()["data"]["state"] == "STOPPING"
    finally:
        finish.set()
        assert api._action_lock.acquire(timeout=3)
        api._action_lock.release()
    assert not api.get_status()["data"]["online"]
    assert not license.granted and not world["core"] and world["bridge"]
    assert holders[0].started == ["new-hook-bridge", "astrbot"]
    assert holders[0].stopped == ["astrbot"]


@pytest.mark.parametrize("actual", ["wxid_a", "wxid_wrong"])
def test_start_rechecks_actual_account_after_reconnecting(tmp_path, actual):
    controller, license, world, holders = setup_controller(tmp_path)
    license.bound = "wxid_a"
    original_connect = controller.connect_wechat

    def connect():
        result = original_connect()
        world["account"] = actual
        return result

    controller.connect_wechat = connect
    if actual != license.bound:
        with pytest.raises(LicenseError):
            controller.start()
        assert holders[0].started == ["new-hook-bridge"]
        assert not world["core"] and not license.grants
    else:
        assert controller.start().state == LifecycleState.RUNNING
        assert license.grants == [actual]


def test_logged_in_account_without_owned_bridge_still_connects_before_start(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    world["account"] = license.bound = "wxid_a"
    world["bridge"] = True  # A previous or external connection exposes the same account.
    original_connect = controller.connect_wechat
    connections = []

    def connect():
        assert not license.grants and not world["core"]
        connections.append(True)
        world["account"] = ""  # The newly opened bot WeChat must confirm its own login.
        return original_connect()

    controller.connect_wechat = connect
    waiting = controller.start()

    assert connections == [True]
    assert holders[0].started == ["new-hook-bridge"]
    assert waiting.state == LifecycleState.WAITING_WECHAT
    assert not license.grants and not world["core"]
    world["account"] = "wxid_a"
    assert controller.resume_pending_start().state == LifecycleState.RUNNING
    assert holders[0].started == ["new-hook-bridge", "astrbot"]


def test_start_reaps_exited_bridge_and_reconnects_in_one_attempt(tmp_path):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    bridge = holders[0].running[0]
    bridge.process.code = 1
    world["bridge"] = False
    world["account"] = ""

    waiting = controller.start()

    assert waiting.state == LifecycleState.WAITING_WECHAT and waiting.error is None
    assert holders[0].started == ["new-hook-bridge", "new-hook-bridge"]
    assert len(holders) == 1 and holders[0].running[0] is not bridge
    assert world["bridge"] and not world["core"] and not license.grants
    world["account"] = "wxid_a"
    assert controller.resume_pending_start().state == LifecycleState.RUNNING


def test_account_signal_without_ready_hook_never_grants_or_starts_core(tmp_path):
    controller, license, _, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    license.bound = "wxid_a"
    controller._snapshotter = lambda: RuntimeSnapshot(
        False, True, "wxid_a", hook_ready=False, account_status_available=True,
    )

    result = controller.start()

    assert not result.online and not license.grants
    assert holders[0].started == ["new-hook-bridge"]


def test_invalid_or_mismatched_license_keeps_warning_without_services(tmp_path):
    controller, license, _, _ = setup_controller(tmp_path)
    license.valid = False
    assert controller.refresh().state == LifecycleState.LICENSE_PAUSED

    license.snapshot = lambda _account: {
        "enabled": True, "authorized": True, "can_run": False,
        "state": "account_mismatch", "message": "微信账号与授权不一致。",
    }
    mismatched = controller.refresh()
    assert mismatched.state == LifecycleState.LICENSE_PAUSED
    assert mismatched.message == "微信账号与授权不一致。"


def test_update_after_user_stop_closes_retained_connection_without_waiting_for_wechat(tmp_path, monkeypatch):
    controller, license, world, holders = setup_controller(tmp_path)
    controller.connect_wechat()
    world["account"] = license.bound = "wxid_a"
    controller.start()
    assert controller.request_stop().state == LifecycleState.LICENSE_READY
    assert world["bridge"] and not world["core"]
    shutdown_checks = []

    target = object()

    def capture(root, *, required):
        assert required is True
        assert world["bridge"] and world["account"] == "wxid_a"
        return target

    def verify_shutdown(root, *, wechat_process):
        assert root == tmp_path
        assert wechat_process is target
        assert not world["bridge"] and not world["core"]
        assert not holders[0].running
        shutdown_checks.append(root)
        world["account"] = ""

    monkeypatch.setattr("wechatrebot_manager.lifecycle.capture_update_wechat_process", capture)
    monkeypatch.setattr("wechatrebot_manager.lifecycle.force_stop_update_processes", verify_shutdown)
    stopped = controller.force_stop_for_update()
    assert stopped.state == LifecycleState.STOPPED
    assert shutdown_checks == [tmp_path]
    assert not license.granted and not stopped.can_stop and not stopped.online
    assert controller.refresh().state == LifecycleState.STOPPED


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
    assert view.state == LifecycleState.WAITING_WECHAT
    assert view.error is None and view.can_start and view.can_stop
    assert world["bridge"] and not world["core"]
