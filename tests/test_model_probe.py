"""Model tests must prove a reply instead of accepting HTTP transport success."""
import json
from types import SimpleNamespace

import pytest

from wechatrebot_manager import model_api
from wechatrebot_manager.app_shell import ApplicationApi
from wechatrebot_manager.engine_api import EngineApiClient
from wechatrebot_manager.error_catalog import load_error_catalog, provider_error_code
from wechatrebot_manager.events import EventStore
from wechatrebot_manager.plugin_errors import ProviderTestError


PROVIDER = "test/model"


def reply(**changes):
    return {"id": PROVIDER, "model": "model", "status": "available", "error": None,
            "test_kind": "chat", "response_text": "PONG", "elapsed_ms": 120, **changes}


def probe(monkeypatch, tmp_path, result):
    client = EngineApiClient(tmp_path)
    monkeypatch.setattr(client, "request", lambda method, path, payload: result)
    return client


@pytest.mark.parametrize("error_type,code", [
    ("APITimeoutError", "16007"), ("openai.APITimeoutError", "16007"),
    ("TimeoutError", "16007"), ("httpx.ReadTimeout", "16007"),
    ("AuthenticationError", "16002"), ("PermissionDeniedError", "16003"),
    ("RateLimitError", "16004"), ("APIConnectionError", "16006"),
    ("InternalServerError", "16008"), ("NotFoundError", "16009"),
    ("BadRequestError", "16010"), ("EmptyModelResponseError", "16012"),
    ("ValueError", "19000"),
])
def test_http_success_cannot_hide_provider_failure(monkeypatch, tmp_path, error_type, code):
    client = probe(monkeypatch, tmp_path, reply(
        status="unavailable", error_type=error_type, error="Request failed"
    ))
    with pytest.raises(ProviderTestError) as error:
        client.test_provider(PROVIDER)
    assert error.value.error_code == code
    assert PROVIDER in str(error.value)


@pytest.mark.parametrize("result", [
    None, [], "available", {}, reply(id="other/model"), reply(status="unknown"),
    reply(status=["available"]), reply(test_kind=[]), reply(error="upstream rejected"),
    reply(error=[]), reply(error={}),
    reply(response_text=""), reply(response_text="  \n"), reply(response_text=None),
    reply(response_text={"content": "not a string"}), reply(model=None),
    reply(elapsed_ms=True), reply(elapsed_ms=-1), reply(elapsed_ms=float("inf")),
    reply(elapsed_ms=float("nan")), reply(elapsed_ms="120"),
])
def test_model_probe_rejects_unverified_or_malformed_success(monkeypatch, tmp_path, result):
    with pytest.raises(ProviderTestError):
        probe(monkeypatch, tmp_path, result).test_provider(PROVIDER)


@pytest.mark.parametrize("result", [
    {"id": PROVIDER, "status": "available", "error": None},
    {key: value for key, value in reply().items() if key != "response_text"},
])
def test_old_runtime_requires_upgrade_instead_of_claiming_generation(monkeypatch, tmp_path, result):
    with pytest.raises(ProviderTestError, match="更新机器人"):
        probe(monkeypatch, tmp_path, result).test_provider(PROVIDER)


def test_real_response_uses_requested_provider_and_exposes_only_safe_evidence(monkeypatch, tmp_path):
    client = EngineApiClient(tmp_path)
    requests = []

    def request(method, path, payload):
        requests.append((method, path, payload))
        return reply(response_text=" PONG\nAuthorization: Bearer private-token ",
                     api_key="private-secret", unexpected={"secret": "private-hidden"})

    monkeypatch.setattr(client, "request", request)
    result = client.test_provider(PROVIDER)
    assert requests == [("POST", "/api/v1/providers/test", {"provider_id": PROVIDER})]
    assert result["response_text"].startswith("PONG")
    assert result["elapsed_ms"] == 120
    assert set(result) == {"id", "model", "status", "test_kind", "response_text", "elapsed_ms"}
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["embedding", "stt", "tts", "rerank"])
def test_other_provider_tests_preserve_their_capability_kind(monkeypatch, tmp_path, kind):
    result = reply(test_kind=kind)
    del result["response_text"]
    assert probe(monkeypatch, tmp_path, result).test_provider(PROVIDER)["test_kind"] == kind


def test_provider_error_classification_uses_metadata_not_prose():
    assert provider_error_code("upstream said HTTP 401") == ""
    assert provider_error_code("APIStatusError", 503) == "16008"
    assert provider_error_code("APIStatusError", 401) == "16002"
    assert provider_error_code("APIStatusError", "401") == ""
    assert provider_error_code("APIStatusError", True) == ""
    assert provider_error_code("APIStatusError", 200) == ""


def test_failed_probe_is_visible_and_recorded_with_timeout_code(monkeypatch, tmp_path):
    api = ApplicationApi.__new__(ApplicationApi)
    api._install_root = tmp_path
    api._catalog = load_error_catalog()
    api._lifecycle = SimpleNamespace(events=EventStore(tmp_path))
    client = probe(monkeypatch, tmp_path, reply(
        status="unavailable", error_type="APITimeoutError",
        error='Request timed out. Authorization: Bearer private-secret',
    ))
    monkeypatch.setattr(client, "is_available", lambda: True)
    monkeypatch.setattr(model_api, "EngineApiClient", lambda _root: client)
    result = api.test_provider(PROVIDER)
    assert result["ok"] is False
    assert result["error"]["code"] == "16007"
    assert result["error"]["recorded"] is True
    assert "Request timed out" in result["error"]["detail"]
    assert "private-secret" not in json.dumps(result)
    event = json.loads(api._lifecycle.events.path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["code"] == "16007"
    assert event["details"]["operation"] == "test_provider"
    assert "private-secret" not in json.dumps(event)


def test_confirmed_probe_has_a_bounded_success_claim(monkeypatch, tmp_path):
    api = ApplicationApi.__new__(ApplicationApi)
    api._install_root = tmp_path
    client = probe(monkeypatch, tmp_path, reply())
    monkeypatch.setattr(client, "is_available", lambda: True)
    monkeypatch.setattr(model_api, "EngineApiClient", lambda _root: client)
    result = api.test_provider(PROVIDER)
    assert result["ok"] is True
    assert result["data"]["response_text"] == "PONG"
    assert result["message"] == ""
