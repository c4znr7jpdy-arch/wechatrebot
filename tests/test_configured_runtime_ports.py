"""Runtime and account probes follow this installation's configured ports."""

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechatrebot_manager import launcher, lifecycle, runtime_config, runtime_status
from wechatrebot_manager.app_shell import ApplicationApi
from wechatrebot_manager.chat_api import ChatApiMixin
from wechatrebot_manager.environment import WechatEnvironment
from wechatrebot_manager.error_catalog import AccountStatusUnavailableError
from wechatrebot_manager.licensing.api import LicensingApiMixin


CUSTOM = runtime_config.RuntimePorts(dashboard=16185, astrbot_ws=16200, bridge_callback=15000, hook_api=29088)


def save_ports(root: Path, value) -> Path:
    manager = root / "config/manager.json"
    manager.parent.mkdir(parents=True, exist_ok=True)
    manager.write_text(json.dumps({"ports": value}), encoding="utf-8")
    return manager


def online_health(account: str) -> runtime_status.BridgeHealth:
    return runtime_status.BridgeHealth(True, True, True, True, account, True, True)


def test_loading_ports_uses_saved_configuration_and_defaults_only_for_missing_fields(tmp_path):
    assert runtime_config.load_runtime_ports(tmp_path) == runtime_config.RuntimePorts()
    save_ports(tmp_path, {"bridge_callback": CUSTOM.bridge_callback})
    assert runtime_config.load_runtime_ports(tmp_path) == runtime_config.RuntimePorts(bridge_callback=CUSTOM.bridge_callback)
    save_ports(tmp_path, asdict(CUSTOM))
    assert runtime_config.load_runtime_ports(tmp_path) == CUSTOM


@pytest.mark.parametrize("saved", [None, [], {"other": 1234}, {"bridge_callback": True},
    {"bridge_callback": "15000"}, {"bridge_callback": 0}, {"bridge_callback": 65536},
    {"astrbot_ws": 5000}])
def test_invalid_saved_ports_fail_before_any_instance_is_probed(tmp_path, monkeypatch, saved):
    save_ports(tmp_path, saved)
    calls = []
    monkeypatch.setattr(runtime_status, "port_accepting", lambda *_a, **_k: calls.append("port") or True)
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda **_k: calls.append("health") or online_health("wxid_wrong_instance"))
    with pytest.raises(ValueError):
        runtime_config.load_runtime_ports(tmp_path)
    with pytest.raises(ValueError):
        runtime_status.runtime_snapshot(install_root=tmp_path)
    with pytest.raises(ValueError):
        launcher.build_service_specs(tmp_path)
    assert calls == []


@pytest.mark.parametrize("raw", ["{broken", "[]", "null"])
def test_unreadable_manager_config_never_falls_back_to_default_ports(tmp_path, monkeypatch, raw):
    manager = save_ports(tmp_path, asdict(CUSTOM))
    manager.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(runtime_status, "port_accepting", lambda *_a, **_k: pytest.fail("must not probe another instance"))
    with pytest.raises(ValueError):
        runtime_status.runtime_snapshot(install_root=tmp_path)


def test_runtime_probe_and_service_readiness_use_the_same_saved_ports(tmp_path, monkeypatch):
    runtime_config.write_runtime_configs(tmp_path, ports=CUSTOM)
    ports = []
    health_ports = []
    monkeypatch.setattr(runtime_status, "port_accepting", lambda _host, port: ports.append(port) or port in {CUSTOM.astrbot_ws, CUSTOM.bridge_callback})
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda *, port: health_ports.append(port) or online_health("wxid_custom"))
    monkeypatch.setattr(launcher, "_hook_integrity_files", lambda _: ())

    snapshot = runtime_status.runtime_snapshot(install_root=tmp_path)
    specs = launcher.build_service_specs(tmp_path)

    assert snapshot.online and snapshot.wechat_identity == "wxid_custom"
    assert ports == [CUSTOM.astrbot_ws, CUSTOM.bridge_callback]
    assert health_ports == [CUSTOM.bridge_callback]
    assert {spec.name: spec.ready_port for spec in specs} == {"astrbot": CUSTOM.astrbot_ws, "new-hook-bridge": CUSTOM.bridge_callback}


def test_lifecycle_history_and_license_account_never_probe_other_installation(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    second_ports = runtime_config.RuntimePorts(dashboard=26185, astrbot_ws=26200, bridge_callback=25000, hook_api=39088)
    runtime_config.write_runtime_configs(first, ports=CUSTOM)
    runtime_config.write_runtime_configs(second, ports=second_ports)
    port_calls = []
    accounts = {CUSTOM.bridge_callback: "wxid_first", second_ports.bridge_callback: "wxid_second", 5000: "wxid_unrelated_default"}
    monkeypatch.setattr(runtime_status, "port_accepting", lambda _host, port: port_calls.append(port) or True)
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda *, port: online_health(accounts[port]))
    monkeypatch.setattr(launcher, "_hook_integrity_files", lambda _: ())
    built = []

    class Supervisor:
        def __init__(self, specs, **kwargs):
            built.append(specs)

        def adopt(self):
            return False

    monkeypatch.setattr(lifecycle, "ProcessSupervisor", Supervisor)

    class Facade(ChatApiMixin, LicensingApiMixin):
        pass

    for root, expected in ((first, "wxid_first"), (second, "wxid_second")):
        controller = lifecycle.LifecycleController(
            root, environment_checker=lambda _: WechatEnvironment(False, "11001", "", "", (), "", "", "test"),
            license_client=SimpleNamespace(), catalog=SimpleNamespace(),
            event_store=SimpleNamespace(emit=lambda *_a, **_k: None),
        )
        facade = Facade()
        facade._lifecycle = controller
        assert controller.fresh_runtime_snapshot().wechat_identity == expected
        assert facade._history_account_id() == expected
        assert facade._license_account(required=True) == expected
    assert {spec.ready_port for specs in built for spec in specs} == {CUSTOM.astrbot_ws, CUSTOM.bridge_callback, second_ports.astrbot_ws, second_ports.bridge_callback}
    assert 6200 not in port_calls and 5000 not in port_calls


def test_runtime_snapshot_reloads_saved_ports_and_preserves_explicit_override(tmp_path, monkeypatch):
    save_ports(tmp_path, asdict(CUSTOM))
    ports = []
    monkeypatch.setattr(runtime_status, "port_accepting", lambda _host, port: ports.append(port) or False)
    runtime_status.runtime_snapshot(install_root=tmp_path)
    save_ports(tmp_path, {"astrbot_ws": 26200, "bridge_callback": 25000})
    runtime_status.runtime_snapshot(install_root=tmp_path)
    runtime_status.runtime_snapshot(runtime_config.RuntimePorts(), install_root=tmp_path)
    assert ports == [16200, 15000, 26200, 25000, 6200, 5000]


def test_explicit_service_ports_and_injected_snapshotter_remain_compatible(tmp_path, monkeypatch):
    save_ports(tmp_path, {"bridge_callback": "invalid"})
    monkeypatch.setattr(launcher, "_hook_integrity_files", lambda _: ())
    specs = launcher.build_service_specs(tmp_path, ports=CUSTOM)
    assert [spec.ready_port for spec in specs] == [16200, 15000]
    snapshot = runtime_status.RuntimeSnapshot(False, False, "")
    controller = lifecycle.LifecycleController(
        tmp_path, environment_checker=lambda _: SimpleNamespace(), snapshotter=lambda: snapshot,
        supervisor_factory=lambda _: None, license_client=SimpleNamespace(),
        catalog=SimpleNamespace(), event_store=SimpleNamespace(emit=lambda *_a, **_k: None),
    )
    assert controller.fresh_runtime_snapshot() is snapshot


def test_invalid_ports_keep_manager_open_with_repairable_error_and_no_network_probe(tmp_path, monkeypatch):
    save_ports(tmp_path, {"bridge_callback": False})
    monkeypatch.setattr(lifecycle, "inspect_wechat_environment", lambda _: WechatEnvironment(True, "10000", "", "", ()))
    monkeypatch.setattr(runtime_status, "port_accepting", lambda *_a, **_k: pytest.fail("invalid ports must not probe another installation"))
    monkeypatch.setattr(lifecycle, "ProcessSupervisor", lambda *_a, **_k: pytest.fail("invalid ports must not adopt or start services"))

    api = ApplicationApi(tmp_path, development=True)
    bootstrapped = api.bootstrap()
    assert bootstrapped["ok"] is True
    for response in (api.get_status(), api.get_status(), api.check_environment()):
        assert response["ok"] is True
        view = response["data"]
        assert view["state"] == "ERROR"
        assert view["online"] is False and view["can_start"] is False
        assert view["runtime"]["account_status_available"] is False
        assert view["error"]["code"] == "13001"
        assert "配置" in view["error"]["message"]
        assert view["error"]["solutions"]
    with pytest.raises(ValueError):
        api._lifecycle.fresh_runtime_snapshot()
    with pytest.raises(AccountStatusUnavailableError):
        api._history_account_id()
    errors = [item for item in api._lifecycle.events.recent() if item["event"] == "APP_ERROR"]
    assert len(errors) == 1


def test_running_manager_reports_new_invalid_ports_then_recovers_after_repair(tmp_path, monkeypatch):
    save_ports(tmp_path, asdict(CUSTOM))
    monkeypatch.setattr(lifecycle, "inspect_wechat_environment", lambda _: WechatEnvironment(True, "10000", "", "", ()))
    port_calls = []
    monkeypatch.setattr(runtime_status, "port_accepting", lambda _host, port: port_calls.append(port) or True)
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda **_k: online_health("wxid_this_install"))
    monkeypatch.setattr(launcher, "_hook_integrity_files", lambda _: ())
    monkeypatch.setattr(lifecycle, "ProcessSupervisor", lambda *_a, **_k: SimpleNamespace(adopt=lambda: False))
    api = ApplicationApi(tmp_path, development=True)
    assert api.get_status()["data"]["online"] is True
    port_calls.clear()
    save_ports(tmp_path, {"astrbot_ws": "invalid"})

    assert api.get_status()["data"]["state"] == "ERROR"
    assert api.get_status()["data"]["history_account"] == ""
    assert port_calls == []
    with pytest.raises(ValueError):
        api._lifecycle.fresh_runtime_snapshot()
    save_ports(tmp_path, asdict(CUSTOM))
    repaired = api.get_status()["data"]
    assert repaired["online"] is True and repaired["state"] == "RUNNING"
    assert repaired["error"] is None
    assert repaired["history_account"] == "wxid_this_install"
    assert port_calls == [CUSTOM.astrbot_ws, CUSTOM.bridge_callback]
