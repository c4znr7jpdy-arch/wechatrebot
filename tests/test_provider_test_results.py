"""Model history retains safe evidence only for the current configuration."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from wechatrebot_manager import model_api, provider_test_results as history
from wechatrebot_manager.app_shell import ApplicationApi
from wechatrebot_manager.customer_config import ProviderService
from wechatrebot_manager.error_catalog import load_error_catalog
from wechatrebot_manager.events import EventStore
from wechatrebot_manager.plugin_errors import ProviderTestError


@pytest.fixture
def service():
    return ProviderService("test/model", "test", "Model", "https://example.test/v1", "model",
                           "private-provider-key", True, False)


def finish(root, attempt, services, **changes):
    return history.finish_provider_test(root, attempt, services,
                                        **{"status": "success", "elapsed_ms": 120, **changes})


@pytest.mark.parametrize("status,code", [("success", ""), ("error", "16007")])
def test_history_survives_reload_without_secrets_or_response(tmp_path, service, status, code):
    result = finish(tmp_path, history.begin_provider_test(tmp_path, service), [service],
                    status=status, error_code=code)
    assert history.load_provider_test_results(tmp_path, [service]) == {service.provider_id: result}
    assert set(result) == {"status", "elapsed_ms", "tested_at", "error_code"}
    raw = (tmp_path / "data/manager/provider-test-results.json").read_text("utf-8")
    assert service.api_key not in raw and service.api_base not in raw
    assert "response_text" not in raw and "api_key" not in raw


@pytest.mark.parametrize("changes", [
    {"api_key": "changed-secret"}, {"api_base": "https://changed.test/v1"},
    {"model": "changed-model"}, {"service_kind": "embedding"},
    {"thinking_enabled": False}, {"modalities": ("text",)},
])
def test_changed_configuration_hides_prior_result(tmp_path, service, changes):
    finish(tmp_path, history.begin_provider_test(tmp_path, service), [service])
    assert history.load_provider_test_results(tmp_path, [replace(service, **changes)]) == {}


@pytest.mark.parametrize("deleted", [False, True])
def test_inflight_test_cannot_record_for_replaced_or_deleted_service(tmp_path, service, deleted):
    attempt = history.begin_provider_test(tmp_path, service)
    services = [] if deleted else [replace(service, api_key="new-key")]
    assert finish(tmp_path, attempt, services) is None
    assert history.load_provider_test_results(tmp_path, [service]) == {}


def test_newer_attempt_wins_even_when_older_finishes_last(tmp_path, service):
    older = history.begin_provider_test(tmp_path, service)
    newer = history.begin_provider_test(tmp_path, service)
    result = finish(tmp_path, newer, [service], status="error", error_code="16007")
    assert finish(tmp_path, older, [service]) is None
    assert history.load_provider_test_results(tmp_path, [service]) == {service.provider_id: result}


def test_older_attempt_cannot_clear_newer_pending_attempt(tmp_path, service):
    older = history.begin_provider_test(tmp_path, service)
    newer = history.begin_provider_test(tmp_path, service)
    assert finish(tmp_path, older, [service]) is None
    result = finish(tmp_path, newer, [service])
    assert history.load_provider_test_results(tmp_path, [service]) == {service.provider_id: result}


def test_concurrent_models_keep_both_records(tmp_path, service):
    services = [service, replace(service, provider_id="test/second", model="second")]
    attempts = [history.begin_provider_test(tmp_path, item) for item in services]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda attempt: finish(tmp_path, attempt, services), attempts))
    assert history.load_provider_test_results(tmp_path, services) == {
        item.provider_id: result for item, result in zip(services, results)
    }


@pytest.mark.parametrize("raw", ["{broken", "[]", '{"results":[]}'])
def test_optional_corrupt_history_does_not_block_catalog(tmp_path, service, raw, caplog):
    path = tmp_path / "data/manager/provider-test-results.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    assert history.load_provider_test_results(tmp_path, [service]) == {}
    assert "history" in caplog.text


def api_for(root):
    api = ApplicationApi.__new__(ApplicationApi)
    api._install_root = root
    api._catalog = load_error_catalog()
    api._lifecycle = SimpleNamespace(events=EventStore(root))
    return api


@pytest.mark.parametrize("failed", [False, True])
def test_history_write_failure_preserves_actual_probe_result(monkeypatch, tmp_path, service, failed):
    monkeypatch.setattr(model_api, "load_provider_services", lambda _root: (service,))

    def probe(_provider_id):
        if failed:
            raise ProviderTestError("Request timed out", code="16007")
        return {"id": service.provider_id, "status": "available", "elapsed_ms": 150,
                "response_text": "PONG", "test_kind": "chat", "model": service.model}

    monkeypatch.setattr(model_api, "EngineApiClient", lambda _root: SimpleNamespace(
        is_available=lambda: True, test_provider=probe,
    ))

    def write_failed(_path, _payload):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(history, "write_json_atomic", write_failed)
    result = api_for(tmp_path).test_provider(service.provider_id)
    assert result["ok"] is not failed
    payload = result["error"] if failed else result["data"]
    assert payload["last_test"]["status"] == ("error" if failed else "success")
    assert payload["history_warning"] == "本次测试已完成，但测试记录未能保存。"
    assert "private filesystem detail" not in json.dumps(result)


def test_removed_provider_is_rejected_without_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(model_api, "load_provider_services", lambda _root: ())
    monkeypatch.setattr(model_api, "EngineApiClient", lambda _root: pytest.fail("must not call engine"))
    result = api_for(tmp_path).test_provider("removed/model")
    assert result["ok"] is False
    assert result["error"]["code"] == "16001"
    assert result["error"]["last_test"] is None


def test_api_discards_result_after_configuration_changes_during_request(monkeypatch, tmp_path, service):
    services = [service]
    monkeypatch.setattr(model_api, "load_provider_services", lambda _root: tuple(services))

    def probe(_provider_id):
        services[0] = replace(service, api_key="new-key")
        return {"id": service.provider_id, "status": "available", "elapsed_ms": 150,
                "response_text": "PONG", "test_kind": "chat", "model": service.model}

    monkeypatch.setattr(model_api, "EngineApiClient", lambda _root: SimpleNamespace(
        is_available=lambda: True, test_provider=probe,
    ))
    api = api_for(tmp_path)
    result = api.test_provider(service.provider_id)
    assert result["ok"] is True
    assert result["data"]["last_test"] is None
    assert api.get_providers()["data"][0]["last_test"] is None
