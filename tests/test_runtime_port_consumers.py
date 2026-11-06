"""Consumers use their own installation's ports and refuse invalid settings."""

from dataclasses import asdict
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from wechatrebot_manager import control_center, customer_maintenance, engine_api, group_directory, runtime_status
from wechatrebot_manager.customer_maintenance import CustomerMaintenanceService, MaintenanceBusyError
from wechatrebot_manager.licensing.bundle import install_runtime_bundle
from wechatrebot_manager.runtime_config import RuntimePorts
from wechatrebot_manager.runtime_status import BridgeHealth, RuntimeSnapshot
from wechatrebot_manager.schedules import ScheduleService


guard = importlib.import_module("wechatrebot_manager.licensing.runtime_guard")


def configured(root, ports):
    config = root / "config"
    config.mkdir(parents=True)
    (config / "manager.json").write_text(json.dumps({"ports": asdict(ports)}), encoding="utf-8")
    (root / ".wechatrebot-install.json").write_text(json.dumps({"state": "ready"}), encoding="utf-8")


@pytest.fixture
def installations(tmp_path, monkeypatch):
    active, stopped = tmp_path / "active", tmp_path / "stopped"
    ports = RuntimePorts(dashboard=16185, astrbot_ws=16200, bridge_callback=15000, hook_api=29088)
    configured(active, ports)
    configured(stopped, RuntimePorts(dashboard=26185, astrbot_ws=26200, bridge_callback=25000, hook_api=39088))
    queried = []
    monkeypatch.setattr(runtime_status, "port_accepting", lambda host, port: queried.append(port) or port in {16200, 15000})
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda **kw: BridgeHealth(
        service_ready=True, status_available=True, hook_ready=True, wechat_logged_in=True,
        wechat_identity="wxid_this_install", upstream_connected=True, heartbeat_fresh=True,
    ))
    return active, stopped, queried


def test_backup_of_stopped_installation_does_not_use_active_neighbor(installations):
    active, stopped, queried = installations
    with pytest.raises(MaintenanceBusyError):
        CustomerMaintenanceService(active).create_backup()
    assert not (active / "backups").exists()
    result = CustomerMaintenanceService(stopped).create_backup()
    assert Path(result["path"]).is_file()
    assert set(queried) == {16200, 15000, 26200, 25000}


def test_schedule_account_cannot_be_borrowed_from_another_installation(installations):
    active, stopped, _ = installations
    current = ScheduleService(active)
    unrelated = ScheduleService(stopped)
    assert current._runtime_account() == "wxid_this_install"
    assert unrelated._runtime_account() == ""
    row = {"payload": json.dumps({"account_id": "wxid_this_install"})}
    current._authorize_job(row)
    with pytest.raises(ValueError):
        unrelated._authorize_job(row)


def test_control_center_refreshes_its_own_root_for_maintenance(installations):
    active, stopped, _ = installations
    first = SimpleNamespace(install_root=active, last_snapshot=RuntimeSnapshot(False, False, ""))
    second = SimpleNamespace(install_root=stopped, last_snapshot=RuntimeSnapshot(True, True, "wxid_other"))
    assert control_center.ControlCenter._runtime_ports_used(first) is True
    assert control_center.ControlCenter._runtime_ports_used(second) is False


def test_invalid_port_config_blocks_backup_schedule_and_control_center(tmp_path, monkeypatch):
    configured(tmp_path, RuntimePorts())
    (tmp_path / "config/manager.json").write_text('{"ports":{"bridge_callback":false}}', encoding="utf-8")
    monkeypatch.setattr(runtime_status, "port_accepting", lambda *_: pytest.fail("invalid ports must not probe"))
    with pytest.raises(MaintenanceBusyError):
        CustomerMaintenanceService(tmp_path).create_backup()
    with pytest.raises(ValueError):
        ScheduleService(tmp_path)._runtime_account()
    center = SimpleNamespace(install_root=tmp_path)
    assert control_center.ControlCenter._runtime_ports_used(center) is True


def test_control_center_update_refuses_invalid_ports_before_handoff(tmp_path, monkeypatch):
    configured(tmp_path, RuntimePorts())
    (tmp_path / "config/manager.json").write_text('{"ports":null}', encoding="utf-8")
    errors = []
    dialog = SimpleNamespace(exists=lambda: True, set_error=errors.append)
    center = SimpleNamespace(install_root=tmp_path, update_dialog=dialog)
    monkeypatch.setattr(control_center.sys, "frozen", True, raising=False)
    control_center.ControlCenter._begin_update(center, SimpleNamespace())
    assert len(errors) == 1


def test_guard_default_source_keeps_runtime_account_bound_to_root(tmp_path, monkeypatch):
    configured(tmp_path, RuntimePorts(bridge_callback=15000))
    requests = []
    payload = {
        "service_ready": True, "status_available": True, "hook_ready": True,
        "wechat_logged_in": True, "wechat_identity": "wxid_this_install",
    }

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, size):
            return json.dumps(payload).encode()

    def open_request(request, **kwargs):
        requests.append(request.full_url)
        return Response()

    monkeypatch.setattr(guard, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    runtime = guard.RuntimeGuard(tmp_path, client=SimpleNamespace())
    assert runtime.account_source() == "wxid_this_install"
    assert requests == ["http://127.0.0.1:15000/api/health"]
    (tmp_path / "config/manager.json").write_text('{"ports":{"bridge_callback":false}}', encoding="utf-8")
    assert runtime.account_source() == ""
    assert len(requests) == 1


def test_existing_engine_and_directory_connections_respect_each_root(tmp_path, monkeypatch):
    roots = (tmp_path / "first", tmp_path / "second")
    observed = []
    for index, root in enumerate(roots, start=1):
        ports = RuntimePorts(dashboard=6185 + index * 10000, astrbot_ws=6200 + index * 10000,
                             bridge_callback=5000 + index * 10000, hook_api=19088 + index * 10000)
        configured(root, ports)
        engine = root / "app/astrbot/data/cmd_config.json"
        engine.parent.mkdir(parents=True)
        engine.write_text(json.dumps({"dashboard":{"jwt_secret":"synthetic-secret"}}), encoding="utf-8")
        bridge = root / "app/bridge/config.json"
        bridge.parent.mkdir(parents=True)
        bridge.write_text(json.dumps({"manager_query_token":"synthetic-token-" * 3}), encoding="utf-8")
        monkeypatch.setattr(group_directory, "probe_bridge_health", lambda **kw: observed.append(kw["port"]) or SimpleNamespace(
            status_available=True, wechat_logged_in=True, wechat_identity="wxid_this_install"))
        assert engine_api.EngineApiClient(root)._url("/api/test") == f"http://127.0.0.1:{ports.dashboard}/api/test"
        assert group_directory.GroupDirectoryService(root)._connection()[0] == ports.bridge_callback
    assert observed == [15000, 25000]


@pytest.mark.parametrize("invalid", [False, 0, "6185", None])
def test_invalid_engine_port_does_not_fall_back_to_another_instance(tmp_path, invalid):
    engine = tmp_path / "app/astrbot/data/cmd_config.json"
    engine.parent.mkdir(parents=True)
    engine.write_text(json.dumps({"dashboard":{"jwt_secret":"synthetic-secret", "port":invalid}}), encoding="utf-8")
    with pytest.raises(engine_api.EngineUnavailableError):
        engine_api.EngineApiClient(tmp_path)._url("/api/test")


def test_minimal_runtime_bundle_imports_and_loads_ports_without_manager_dependencies(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    site_packages = tmp_path / "runtime/python/Lib/site-packages"
    install_runtime_bundle(repository, site_packages)
    configured(tmp_path, RuntimePorts(bridge_callback=15000))
    code = """import importlib,sys
from pathlib import Path
site,root=sys.argv[1:]
sys.path.insert(0,site)
guard=importlib.import_module('wechatrebot_manager.licensing.runtime_guard')
assert guard.load_runtime_ports(root).bridge_callback == 15000
assert 'wechatrebot_manager.runtime_config' not in sys.modules
assert not (Path(site)/'wechatrebot_manager/runtime_config.py').exists()
print('minimal authorization bundle import: passed')
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(site_packages), str(tmp_path)],
                            capture_output=True, text=True, check=True)
    assert "bundle import: passed" in result.stdout
