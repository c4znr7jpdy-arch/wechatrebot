"""Damaged manager settings leave a usable UI without crossing installations."""

import json

import pytest

from wechatrebot_manager import environment, lifecycle, runtime_status
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
