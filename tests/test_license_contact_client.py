"""Public QR fetching is bounded, anonymous, and independent from licensing."""
from http.client import IncompleteRead
import io
import json
from types import SimpleNamespace

import pytest

from wechatrebot_manager.licensing import config as channel_config
from wechatrebot_manager.licensing import contact_client as module
from wechatrebot_manager.licensing.client import _NoRedirect
from wechatrebot_manager.licensing.contact import create_contact
from wechatrebot_manager.licensing.contact_client import ContactClient, http_contact_transport
from wechatrebot_manager.licensing.protocol import LicenseError

from test_license_client import Clock


@pytest.fixture
def contact_setup(tmp_path, monkeypatch):
    clock = Clock()
    channel = tmp_path / "channel.json"
    bundle = tmp_path / "contact.json"
    bundled = create_contact(b"\x89PNG\r\n\x1a\nbundled", "WXRebot", clock.server + 3600)
    bundle.write_text(json.dumps(bundled), encoding="utf-8")
    monkeypatch.setattr(channel_config, "PACKAGED_CHANNEL", channel)
    requests = []
    response = {"ok": True, "data": {**bundled, "group_name": "Updated Group"}, "server_time": clock.server}

    def configure(url=""):
        channel.write_text(json.dumps({
            "schema_version": 1, "enabled": False, "service_url": url, "public_keys": {},
        }), encoding="utf-8")

    def fetch(url):
        requests.append(url)
        if isinstance(response, Exception):
            raise response
        return response

    def respond(value):
        nonlocal response
        response = value

    configure()
    client = ContactClient(tmp_path, bundled_path=bundle, transport=fetch, clock=clock)
    return SimpleNamespace(client=client, clock=clock, channel=channel, bundle=bundle, bundled=bundled,
                           configure=configure, respond=respond, requests=requests, response=response)


def test_no_service_uses_bundle_without_device_files_or_entitlement_changes(contact_setup):
    case = contact_setup
    before = case.channel.read_bytes()
    result = case.client.snapshot()
    assert result["state"] == "ready" and result["source"] == "bundled"
    assert result["image_data_url"].startswith("data:image/png;base64,")
    assert not case.requests and not (case.client.root / "data").exists()
    assert case.channel.read_bytes() == before


def test_remote_contact_does_not_require_enabled_licensing_or_signing_keys(contact_setup):
    case = contact_setup
    case.configure("https://example.test/base")
    result = case.client.snapshot()
    assert result["source"] == "remote" and result["group_name"] == "Updated Group"
    assert case.requests == ["https://example.test/base/v1/contact"]
    assert json.loads(case.channel.read_text())["enabled"] is False
    assert not (case.client.root / "data").exists()


def test_cache_uses_sixty_seconds_but_force_and_endpoint_changes_refresh(contact_setup):
    case = contact_setup
    case.configure("https://example.test/base")
    case.client.snapshot()
    case.clock.advance(59)
    case.client.snapshot()
    assert len(case.requests) == 1
    case.clock.advance(1)
    case.client.snapshot()
    assert len(case.requests) == 2
    case.client.snapshot(force=True)
    assert len(case.requests) == 3
    case.configure("https://another.test/new-base")
    case.client.snapshot()
    assert case.requests[-1] == "https://another.test/new-base/v1/contact" and len(case.requests) == 4


@pytest.mark.parametrize("remote", [False, True])
def test_cached_image_disappears_at_exact_expiry_without_another_fetch(contact_setup, remote):
    case = contact_setup
    record = {**case.bundled, "expires_at": case.clock.server + 10}
    if remote:
        case.configure("https://example.test")
        case.respond({"ok": True, "data": record, "server_time": case.clock.server})
    else:
        case.bundle.write_text(json.dumps(record), encoding="utf-8")
    assert case.client.snapshot()["state"] == "ready"
    requests = len(case.requests)
    case.clock.advance(10)
    result = case.client.snapshot()
    assert result["state"] == "expired" and result["image_data_url"] == ""
    assert len(case.requests) == requests


def test_server_time_and_monotonic_progress_prevent_cached_clock_rollback(contact_setup):
    case = contact_setup
    case.configure("https://example.test")
    case.respond({"ok": True, "server_time": case.clock.server + 3600,
                  "data": {**case.bundled, "expires_at": case.clock.server + 3610}})
    assert case.client.snapshot()["state"] == "ready"
    case.clock.wall -= 3600
    case.clock.mono += 10
    result = case.client.snapshot()
    assert result["state"] == "expired" and result["image_data_url"] == ""
    assert len(case.requests) == 1


def test_successful_refresh_replaces_an_incorrect_future_local_estimate(contact_setup):
    case = contact_setup
    case.clock.wall += 7200
    assert case.client.snapshot()["state"] == "expired"
    case.configure("https://example.test")
    case.clock.wall = case.clock.server
    result = case.client.snapshot(force=True)
    assert result["state"] == "ready" and result["source"] == "remote"
    assert result["image_data_url"]


def test_verified_server_time_ignores_fast_and_backward_local_wall_clock(contact_setup):
    case = contact_setup
    case.configure("https://example.test")
    case.respond({"ok": True, "server_time": case.clock.server,
                  "data": {**case.bundled, "expires_at": case.clock.server + 10}})
    case.clock.wall += 7200
    assert case.client.snapshot()["state"] == "ready"
    assert case.client.snapshot()["state"] == "ready"
    case.clock.wall -= 14400
    case.clock.mono += 10
    expired = case.client.snapshot()
    assert expired["state"] == "expired" and expired["image_data_url"] == ""
    # A later HTTP response with an older timestamp must not rewind the
    # previously verified server clock or revive this same expired record.
    assert case.client.snapshot(force=True)["state"] == "expired"


def test_request_latency_does_not_extend_qr_validity(contact_setup):
    case = contact_setup
    case.configure("https://example.test")
    response = {"ok": True, "server_time": case.clock.server,
                "data": {**case.bundled, "expires_at": case.clock.server + 1}}

    def delayed(_):
        case.clock.advance(2)
        return response

    case.client.transport = delayed
    assert case.client.snapshot()["state"] == "expired"


def test_remote_null_remains_authoritative_during_later_network_failure(contact_setup):
    case = contact_setup
    case.configure("https://example.test")
    case.respond({"ok": True, "data": None, "server_time": case.clock.server})
    result = case.client.snapshot()
    assert result["state"] == "unavailable" and result["source"] == "remote"
    case.respond(OSError("private server details"))
    result = case.client.snapshot(force=True)
    assert result["state"] == "unavailable" and result["source"] == "remote"
    assert not result["image_data_url"] and "private" not in result["message"]
    case.respond(case.response)
    assert case.client.snapshot(force=True)["state"] == "ready"


def test_expired_remote_contact_does_not_fall_back_to_valid_bundle(contact_setup):
    case = contact_setup
    case.configure("https://example.test")
    case.respond({"ok": True, "data": {**case.bundled, "expires_at": case.clock.server},
                  "server_time": case.clock.server})
    result = case.client.snapshot()
    assert result["state"] == "expired" and result["source"] == "remote"
    assert not result["image_data_url"]


@pytest.mark.parametrize("bundle_state", ["ready", "expired", "missing", "invalid"])
def test_failed_refresh_uses_only_usable_bundle_and_reports_failure(contact_setup, bundle_state):
    case = contact_setup
    case.configure("https://example.test")
    case.respond(OSError("secret-path-and-token"))
    if bundle_state == "expired":
        case.bundle.write_text(json.dumps({**case.bundled, "expires_at": case.clock.server}), encoding="utf-8")
    elif bundle_state == "missing":
        case.bundle.unlink()
    elif bundle_state == "invalid":
        case.bundle.write_text('{"image_base64":"tampered"}', encoding="utf-8")
    result = case.client.snapshot()
    assert result["state"] == (bundle_state if bundle_state in {"ready", "expired"} else "unavailable")
    assert result["source"] == "bundled" and "刷新失败" in result["message"]
    assert bool(result["image_data_url"]) is (bundle_state == "ready")
    assert "secret" not in result["message"]


@pytest.mark.parametrize("response", [
    None,
    {"ok": False, "error": {"code": "contact_invalid", "message": "private path"}},
    {"ok": True, "data": None},
    {"ok": True, "data": None, "server_time": True},
    {"ok": True, "data": None, "server_time": 10 ** 99},
    {"ok": True, "data": {"image_base64": "invalid"}, "server_time": 1_800_000_000},
])
def test_malformed_response_does_not_act_as_remote_null(contact_setup, response):
    case = contact_setup
    case.configure("https://example.test")
    case.respond(response)
    result = case.client.snapshot()
    assert result["state"] == "ready" and result["source"] == "bundled"
    assert "刷新失败" in result["message"] and "private" not in result["message"]


@pytest.mark.parametrize("url", ["http://example.test", "file:///tmp/qr", "https://user:pass@example.test"])
def test_invalid_service_configuration_never_sends_a_request(contact_setup, url):
    case = contact_setup
    case.configure(url)
    result = case.client.snapshot()
    assert result["source"] == "bundled" and "刷新失败" in result["message"]
    assert not case.requests


def test_http_request_is_bounded_anonymous_get_with_shared_redirect_guard(monkeypatch):
    raw = b'{"ok":true,"data":null,"server_time":1800000000}'
    response = io.BytesIO(raw)
    observed = []

    def opener(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], _NoRedirect)

        def open_request(request, *, timeout):
            observed.append((request.full_url, request.get_method(), request.data, request.headers, timeout))
            return response

        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(module, "build_opener", opener)
    assert http_contact_transport("https://example.test/base/v1/contact")["data"] is None
    url, method, body, headers, timeout = observed[0]
    assert url == "https://example.test/base/v1/contact" and method == "GET" and body is None
    assert not {"Authorization", "Cookie"} & headers.keys() and timeout == 5
    assert response.closed


def test_http_redirect_is_refused_without_opening_another_origin(monkeypatch):
    def opener(*handlers):
        def redirected(request, *, timeout):
            return handlers[0].redirect_request(request, None, 302, "Found", {}, "https://other.test/qr")
        return SimpleNamespace(open=redirected)

    monkeypatch.setattr(module, "build_opener", opener)
    with pytest.raises(LicenseError) as error:
        http_contact_transport("https://example.test/v1/contact")
    assert error.value.code == "contact_unavailable"


@pytest.mark.parametrize("failure", ["oversized", "truncated", "timeout"])
def test_http_response_failures_are_bounded_and_sanitized(monkeypatch, failure):
    class Response(io.BytesIO):
        def read(self, size):
            assert size == module.MAX_CONTACT_RESPONSE_BYTES + 1
            if failure == "truncated":
                raise IncompleteRead(b"private content")
            return b"x" * size

    def open_request(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError("private service address")
        return Response()

    monkeypatch.setattr(module, "build_opener", lambda *_: SimpleNamespace(open=open_request))
    with pytest.raises(LicenseError) as error:
        http_contact_transport("https://example.test/v1/contact")
    assert error.value.code == "contact_unavailable" and "private" not in str(error.value)
