from copy import deepcopy
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from wechatrebot_manager.licensing.config import load_channel
from wechatrebot_manager.licensing.protocol import (
    LEASE_DOMAIN, REQUEST_DOMAIN, LicenseError, canonical, decode, encode,
    extend_period, fingerprint, issue_lease, parse_json, public_key, sign_request,
    verify_lease, verify_request,
)


@pytest.fixture
def protocol():
    key, device_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    device = fingerprint(public_key(device_key))
    payload = {
        "schema_version": 1, "audience": "wechatrebot", "license_id": "license-one",
        "device": device, "account": "wxid_example", "plan": "month", "status": "active",
        "issued_at": 1800000000, "lease_until": 1800086400, "valid_until": 1802592000,
        "revision": 1, "request_nonce": "a" * 32,
    }
    return key, device_key, device, payload


def assert_invalid(code, callback):
    with pytest.raises(LicenseError) as caught:
        callback()
    assert caught.value.code == code


def test_signed_lease_and_request_domains_are_separate(protocol):
    key, _, device, payload = protocol
    keys = {"server": public_key(key)}
    envelope = issue_lease(key, "server", payload)
    assert verify_lease(envelope, keys, device) == payload
    envelope["signature"] = encode(key.sign(REQUEST_DOMAIN + canonical(payload)))
    assert_invalid("invalid_lease", lambda: verify_lease(envelope, keys, device))
    request = sign_request(key, "refresh", {"license_id": "license-one"}, payload["issued_at"], "a" * 32)
    request_payload = {name: value for name, value in request.items() if name != "signature"}
    request["signature"] = encode(key.sign(LEASE_DOMAIN + canonical(request_payload)))
    assert_invalid("invalid_request", lambda: verify_request(request, "refresh", payload["issued_at"]))


@pytest.mark.parametrize("field,value", [
    ("valid_until", 1900000000), ("plan", "year"), ("account", "wxid_other"),
    ("license_id", "license-other"), ("device", "1" * 64),
    ("status", "revoked"), ("request_nonce", "b" * 32), ("revision", 900),
])
def test_unsigned_lease_mutation_is_rejected(protocol, field, value):
    key, _, device, payload = protocol
    envelope = issue_lease(key, "server", deepcopy(payload))
    envelope["payload"][field] = value
    assert_invalid("invalid_lease", lambda: verify_lease(envelope, {"server": public_key(key)}, device))


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("audience", "other-product"),
    ("license_id", ""), ("account", None), ("account", "wxid invalid"),
    ("account", "x" * 129), ("plan", "forever"), ("status", "expired"),
    ("issued_at", True), ("valid_until", -1), ("issued_at", 253402214401),
    ("valid_until", 253402214401), ("lease_until", 1800086401),
    ("revision", -1), ("revision", 1.5), ("request_nonce", "short"),
    ("request_nonce", None), ("request_nonce", "A" * 32),
])
def test_even_signed_invalid_lease_fields_are_rejected(protocol, field, value):
    key, _, device, payload = protocol
    payload[field] = value
    assert_invalid("invalid_lease", lambda: verify_lease(issue_lease(key, "server", payload), {"server": public_key(key)}, device))


def test_lease_bounds_allow_expired_signed_status_but_never_extend_entitlement(protocol):
    key, _, device, payload = protocol
    payload.update(issued_at=1803000000, lease_until=1802592000, status="revoked", account="")
    assert verify_lease(issue_lease(key, "server", payload), {"server": public_key(key)}, device)["status"] == "revoked"
    payload["lease_until"] = payload["valid_until"] + 1
    assert_invalid("invalid_lease", lambda: verify_lease(issue_lease(key, "server", payload), {"server": public_key(key)}, device))


@pytest.mark.parametrize("case", ["unknown_key", "wrong_algorithm", "additional_field", "missing_field", "wrong_device"])
def test_lease_envelope_is_strict(protocol, case):
    key, _, device, payload = protocol
    envelope = issue_lease(key, "server", payload)
    if case == "unknown_key":
        envelope["key_id"] = "untrusted"
    elif case == "wrong_algorithm":
        envelope["algorithm"] = "none"
    elif case == "additional_field":
        envelope["accepted"] = True
    elif case == "missing_field":
        del envelope["signature"]
    else:
        device = "f" * 64
    assert_invalid("invalid_lease", lambda: verify_lease(envelope, {"server": public_key(key)}, device))


@pytest.mark.parametrize("offset,accepted", [(-301, False), (-300, True), (0, True), (300, True), (301, False)])
def test_request_time_skew_has_exact_bounds(protocol, offset, accepted):
    _, key, device, payload = protocol
    now = payload["issued_at"]
    envelope = sign_request(key, "refresh", {"license_id": "one"}, now + offset, "a" * 32)
    if accepted:
        assert verify_request(envelope, "refresh", now) == (device, {"license_id": "one"})
    else:
        assert_invalid("invalid_request", lambda: verify_request(envelope, "refresh", now))


@pytest.mark.parametrize("field,value", [("timestamp", True), ("body", []), ("nonce", "0" * 31), ("nonce", "Z" * 32), ("nonce", None)])
def test_request_schema_rejects_invalid_fields(protocol, field, value):
    _, key, _, payload = protocol
    envelope = sign_request(key, "refresh", {}, payload["issued_at"], "a" * 32)
    envelope[field] = value
    unsigned = {name: item for name, item in envelope.items() if name != "signature"}
    envelope["signature"] = encode(key.sign(REQUEST_DOMAIN + canonical(unsigned)))
    assert_invalid("invalid_request", lambda: verify_request(envelope, "refresh", payload["issued_at"]))


@pytest.mark.parametrize("raw", [
    '{"a":1,"a":2}', '{"body":{"code":"first","code":"second"}}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '[]', 'null', '"text"', '{',
])
def test_protocol_json_rejects_duplicates_nonfinite_literals_and_nonobjects(raw):
    assert_invalid("invalid_data", lambda: parse_json(raw))


@pytest.mark.parametrize("raw", [None, 0, "bad", "@@@", "x" * 44, encode(b"short")])
def test_public_keys_require_exact_base64_ed25519_size(raw):
    assert_invalid("invalid_signature", lambda: decode(raw, 32))


@pytest.mark.parametrize("start,plan,anchor,expected", [
    ("2026-01-31T10:20:30", "month", None, "2026-02-28T10:20:30"),
    ("2026-02-28T10:20:30", "month", 31, "2026-03-31T10:20:30"),
    ("2024-01-31T10:20:30", "month", None, "2024-02-29T10:20:30"),
    ("2024-02-29T10:20:30", "year", None, "2025-02-28T10:20:30"),
    ("2027-02-28T10:20:30", "year", 29, "2028-02-29T10:20:30"),
    ("2026-12-31T23:30:00", "trial", None, "2027-01-01T00:30:00"),
    ("2026-12-31T23:30:00", "month", None, "2027-01-31T23:30:00"),
])
def test_calendar_terms_in_china_timezone(start, plan, anchor, expected):
    convert = lambda value: int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    assert extend_period(convert(start), plan, anchor) == convert(expected)


def config(tmp_path, **overrides):
    data = {"schema_version": 1, "enabled": True, "service_url": "https://license.example.invalid",
            "public_keys": {"server": public_key(Ed25519PrivateKey.generate())},
            "qq_group": "12345678", "qq_url": "https://qm.qq.com/q/test"}
    data.update(overrides)
    path = tmp_path / "config/licensing.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def test_valid_channel_and_explicit_unconfigured_channel(tmp_path):
    assert load_channel(config(tmp_path)).ready
    pending = load_channel(config(tmp_path, enabled=False, service_url="", public_keys={}, qq_group="", qq_url=""))
    assert not pending.ready and not pending.enabled


@pytest.mark.parametrize("overrides", [
    {"schema_version": True}, {"schema_version": 2}, {"enabled": "false"},
    {"service_url": "http://license.example.invalid"},
    {"service_url": "https://admin:secret@license.example.invalid"},
    {"service_url": "https://license.example.invalid?token=secret"},
    {"service_url": "https://license.example.invalid#fragment"},
    {"service_url": "https://license.example.invalid:bad"},
    {"service_url": "https://license.example.invalid:65536"},
    {"public_keys": {"": "bad"}}, {"public_keys": {"server": "bad"}},
    {"qq_group": "１２３４５６"}, {"qq_group": "123"},
    {"qq_url": "https://qm.qq.com.evil.invalid/"},
    {"qq_url": "https://qm.qq.com:8080/"}, {"qq_url": "javascript:alert(1)"},
])
def test_channel_rejects_unsafe_or_malformed_configuration(tmp_path, overrides):
    assert_invalid("configuration_invalid", lambda: load_channel(config(tmp_path, **overrides)))
