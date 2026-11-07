"""Damaged manager settings leave a usable UI without crossing installations."""

import json

import pytest

from wechatrebot_manager import environment, launcher, lifecycle, runtime_config, runtime_status, uninstaller
from wechatrebot_manager.app_shell import ApplicationApi
from wechatrebot_manager.customer_config import (
    load_provider_services, save_provider_thinking_preference,
)
from wechatrebot_manager.wechat_path import WechatExecutable


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b"null", b'{"ports":\xff}'])
def test_damaged_manager_config_bootstraps_without_default_probes(tmp_path, monkeypatch, raw):
    manager = tmp_path / "config/manager.json"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(raw)
    engine = tmp_path / "app/astrbot/data/cmd_config.json"
    engine.parent.mkdir(parents=True)
    engine.write_text(json.dumps({
        "provider": [{"id": "custom/model", "provider_source_id": "custom", "model": "model"}],
        "provider_sources": [{"id": "custom", "key": ["placeholder-key"], "api_base": "https://example.invalid/v1"}],
        "provider_settings": {"default_provider_id": "custom/model"},
    }), encoding="utf-8")
    candidate = WechatExecutable(tmp_path / "Weixin.exe", "4.1.8.27", "auto", ())
    monkeypatch.setattr(lifecycle, "inspect_wechat_environment", lambda root: environment.inspect_wechat_environment(
        root, discoverer=lambda: (candidate,), process_probe=lambda: (),
    ))
    monkeypatch.setattr(runtime_status, "port_accepting", lambda *_a, **_k: pytest.fail("must not probe another installation"))
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda **_k: pytest.fail("must not probe another bridge"))
    monkeypatch.setattr(lifecycle, "ProcessSupervisor", lambda *_a, **_k: pytest.fail("must not adopt or start services"))

    api = ApplicationApi(tmp_path, development=True)
    bootstrapped = api.bootstrap()
    assert bootstrapped["ok"] is True
    assert bootstrapped["data"]["provider"]["configured"] is True
    assert bootstrapped["data"]["providers"][0]["id"] == "custom/model"
    assert "placeholder-key" not in json.dumps(bootstrapped)
    assert api.get_onboarding_readiness()["ok"] is True
    for response in (api.get_status(), api.check_environment()):
        assert response["ok"] is True
        status = response["data"]
        assert status["state"] == "ERROR" and not status["can_start"]
        assert status["history_account"] == "" and not status["online"]
        assert status["error"]["code"] == "13001" and status["error"]["solutions"]
    assert api.get_conversations()["ok"] is False
    assert api.get_license_status()["data"]["current_account"] == ""
    assert api.get_trial_status()["ok"] is False
    assert api.bind_license_account("wxid_other_instance")["ok"] is False
    assert api.get_diagnostics()["ok"] is True
    assert api.export_diagnostics()["ok"] is True
    assert api.start_robot()["data"]["accepted"] is True
    assert api._action_lock.acquire(timeout=3), "mock start did not finish"
    api._action_lock.release()
    assert api.get_status()["data"]["state"] == "ERROR"
    with pytest.raises((OSError, ValueError)):
        load_provider_services(tmp_path)
    with pytest.raises((OSError, ValueError)):
        save_provider_thinking_preference(tmp_path, "example", True)
    assert manager.read_bytes() == raw

    probes = []
    monkeypatch.setattr(runtime_status, "port_accepting", lambda _host, port: probes.append(port) or False)
    repaired_ports = {"dashboard": 16185, "astrbot_ws": 16200, "bridge_callback": 15000, "hook_api": 29088}
    manager.write_text(json.dumps({"ports": repaired_ports}), encoding="utf-8")
    repaired = api.get_status()["data"]
    assert repaired["state"] == "STOPPED" and repaired["error"] is None
    assert repaired["can_start"] is True
    assert probes == [16200, 15000]


@pytest.mark.parametrize("service_ready", [False, True])
@pytest.mark.parametrize("repair_configuration", [False, True])
@pytest.mark.parametrize("licensed", [False, True])
def test_readopted_services_keep_stop_control_across_configuration_recovery(
    tmp_path, monkeypatch, service_ready, repair_configuration, licensed,
):
    ports = runtime_config.RuntimePorts(astrbot_ws=16200, bridge_callback=15000)
    monkeypatch.setattr(launcher, "_hook_integrity_files", lambda _: ())
    specs = launcher.build_service_specs(tmp_path, ports=ports)
    identities = {
        pid: launcher.ProcessIdentity(launcher._normalized_executable(spec.argv[0]), pid * 10)
        for pid, spec in enumerate(specs, 101)
    }
    state = tmp_path / "data/manager/service-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "schema_version": 1,
        "complete": True,
        "services": [
            {"name": spec.name, "pid": pid, "executable": identities[pid].executable,
             "creation_time": identities[pid].creation_time}
            for pid, spec in enumerate(specs, 101)
        ],
    }), encoding="utf-8")
    manager = tmp_path / "config/manager.json"
    manager.parent.mkdir()
    valid_config = json.dumps({"ports": {"astrbot_ws": 16200, "bridge_callback": 15000}})
    manager.write_text("{broken" if repair_configuration else valid_config, encoding="utf-8")
    built_ports = []

    def supervisor(specs, **kwargs):
        built_ports.append([spec.ready_port for spec in specs])
        return launcher.ProcessSupervisor(
            specs, **kwargs, identity_probe=identities.get,
            readiness_probe=lambda _host, _port: service_ready,
        )

    monkeypatch.setattr(lifecycle, "ProcessSupervisor", supervisor)
    monkeypatch.setattr(runtime_status, "port_accepting", lambda _host, _port: service_ready)
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda **_: runtime_status.BridgeHealth(
        True, True, True, True, "wxid_owned", True, True,
    ))
    from test_license_lifecycle import License
    license_client = License() if licensed else None
    if license_client is not None:
        license_client.bound, license_client.granted = "wxid_owned", True
    controller = lifecycle.LifecycleController(
        tmp_path, environment_checker=lambda _: environment.WechatEnvironment(True, "10000", "", "", ()),
        license_client=license_client,
    )
    if repair_configuration:
        assert controller.view().state == lifecycle.LifecycleState.ERROR
        assert built_ports == [] and controller._supervisor is None
        manager.write_text(valid_config, encoding="utf-8")

    repaired = controller.refresh()
    assert repaired.state == (lifecycle.LifecycleState.RUNNING if service_ready else lifecycle.LifecycleState.ERROR)
    assert repaired.can_stop is True
    assert repaired.can_start is False
    assert built_ports == [[16200, 15000]]
    assert [item.process.pid for item in controller._supervisor.running] == [101, 102]
    assert controller.refresh().can_stop is True
    assert built_ports == [[16200, 15000]]
    if not service_ready:
        manager.write_text("{broken", encoding="utf-8")
        assert controller.refresh().error.code == "13001"
        manager.write_text(valid_config, encoding="utf-8")
        recovered = controller.refresh()
        assert recovered.error is not None and recovered.error.code == "13002"
        assert recovered.can_stop and not recovered.can_start
        assert built_ports == [[16200, 15000]]
        from unittest.mock import Mock
        start_missing = Mock(side_effect=AssertionError("must stop incomplete recovery before starting"))
        monkeypatch.setattr(controller._supervisor, "start_missing", start_missing)
        monkeypatch.setattr(controller, "_snapshotter", Mock(side_effect=AssertionError("must reject before probing")))
        rejected = controller._start_services(("astrbot",))
        assert rejected.error.code == "13002" and not rejected.can_start
        start_missing.assert_not_called()
        monkeypatch.setattr(controller, "_snapshotter", lambda: runtime_status.RuntimeSnapshot(False, False, ""))
    terminated = []

    def terminate(pid, *, expected_creation_time):
        assert expected_creation_time == identities[pid].creation_time
        terminated.append(pid)
        identities.pop(pid)
        return True

    monkeypatch.setattr(uninstaller, "_terminate_windows_process", terminate)
    controller.force_stop()
    assert terminated == [102, 101]
    assert not state.exists()
