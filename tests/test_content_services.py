import json
from pathlib import Path

import pytest

from wechatrebot_manager.content_services import (
    CONTENT_SERVICE_CONFIG,
    CONTENT_SERVICE_TEMPLATE,
    apply_content_service_defaults,
    read_content_service_defaults,
)
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.runtime_config import write_runtime_configs


DEFAULTS = {
    "content_api_id": "test-publisher",
    "content_api_key": "publisher-credential-1234567890",
    "weather_detail_token": "publisher-weather-1234567890",
}


@pytest.mark.parametrize("existing", [None, {}, dict.fromkeys(DEFAULTS, "")])
def test_normal_runtime_setup_provisions_news_and_weather(tmp_path: Path, existing):
    write_json_atomic(tmp_path / CONTENT_SERVICE_TEMPLATE, DEFAULTS)
    config_path = tmp_path / CONTENT_SERVICE_CONFIG
    if existing is not None:
        write_json_atomic(config_path, {**existing, "setu_recall_enabled": False})

    write_runtime_configs(tmp_path)

    saved = json.loads(config_path.read_text("utf-8"))
    assert all(saved[key] == value for key, value in DEFAULTS.items())
    if existing is not None:
        assert saved["setu_recall_enabled"] is False
    before = config_path.stat().st_mtime_ns
    apply_content_service_defaults(tmp_path)
    assert config_path.stat().st_mtime_ns == before


@pytest.mark.parametrize("account,key,expected", [
    ("customer-account", "customer-key", {"content_api_id": "customer-account", "content_api_key": "customer-key"}),
    ("customer-account", "", {"content_api_id": "customer-account", "content_api_key": ""}),
    ("", "customer-key", {"content_api_id": "", "content_api_key": "customer-key"}),
    (DEFAULTS["content_api_id"], "", {key: DEFAULTS[key] for key in ("content_api_id", "content_api_key")}),
])
def test_defaults_preserve_custom_accounts_and_complete_matching_pair(tmp_path, account, key, expected):
    write_json_atomic(tmp_path / CONTENT_SERVICE_TEMPLATE, DEFAULTS)
    config = {"content_api_id": account, "content_api_key": key,
              "weather_detail_token": "customer-weather"}
    write_json_atomic(tmp_path / CONTENT_SERVICE_CONFIG, config)

    apply_content_service_defaults(tmp_path)

    assert json.loads((tmp_path / CONTENT_SERVICE_CONFIG).read_text("utf-8")) == {
        **expected, "weather_detail_token": "customer-weather",
    }


def test_existing_installs_without_a_template_keep_their_config(tmp_path):
    path = tmp_path / CONTENT_SERVICE_CONFIG
    write_json_atomic(path, DEFAULTS)
    before = path.read_bytes()
    apply_content_service_defaults(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("data", [
    {**DEFAULTS, "cookie": "private-unrelated-cookie"},
    {**DEFAULTS, "content_api_key": ""},
    {**DEFAULTS, "content_api_id": 123},
    [],
])
def test_invalid_default_configuration_is_rejected_without_values(tmp_path, data):
    path = tmp_path / CONTENT_SERVICE_TEMPLATE
    write_json_atomic(path, data)
    with pytest.raises(ValueError) as caught:
        read_content_service_defaults(path)
    assert "publisher" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_corrupt_customer_config_is_preserved(tmp_path):
    write_json_atomic(tmp_path / CONTENT_SERVICE_TEMPLATE, DEFAULTS)
    path = tmp_path / CONTENT_SERVICE_CONFIG
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        apply_content_service_defaults(tmp_path)
    assert path.read_text("utf-8") == "{"


@pytest.mark.parametrize("entry", ["manager", "cli"])
def test_reopening_fills_existing_empty_config_before_loading_runtime(tmp_path, monkeypatch, entry):
    from wechatrebot_manager import app_shell, lifecycle

    write_json_atomic(tmp_path / CONTENT_SERVICE_TEMPLATE, DEFAULTS)
    config = tmp_path / CONTENT_SERVICE_CONFIG
    write_json_atomic(config, dict.fromkeys(DEFAULTS, ""))

    class RuntimeInitializationReached(Exception):
        pass

    def licenses(_root):
        assert json.loads(config.read_text("utf-8")) == DEFAULTS
        raise RuntimeInitializationReached

    # Observe the configuration before runtime construction, without starting
    # services or reading a real installation.
    monkeypatch.setattr(lifecycle, "LicenseClient", licenses)
    with pytest.raises(RuntimeInitializationReached):
        if entry == "manager":
            app_shell.ApplicationApi(tmp_path, development=True)
        else:
            lifecycle.LifecycleController(tmp_path)


def test_bad_optional_content_config_does_not_block_manager_construction(tmp_path, monkeypatch, caplog):
    from wechatrebot_manager import app_shell, lifecycle

    write_json_atomic(tmp_path / CONTENT_SERVICE_TEMPLATE, DEFAULTS)
    config = tmp_path / CONTENT_SERVICE_CONFIG
    config.parent.mkdir(parents=True)
    config.write_text('{"content_api_key": "private-broken', encoding="utf-8")

    class RuntimeInitializationReached(Exception):
        pass

    def licenses(_root):
        raise RuntimeInitializationReached

    monkeypatch.setattr(lifecycle, "LicenseClient", licenses)
    with pytest.raises(RuntimeInitializationReached):
        app_shell.ApplicationApi(tmp_path, development=True)
    assert "默认配置未能写入" in caplog.text
    assert "private-broken" not in caplog.text
    assert config.read_text("utf-8") == '{"content_api_key": "private-broken'
