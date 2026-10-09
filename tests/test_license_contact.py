from __future__ import annotations

import base64
import json

import pytest

from wechatrebot_manager.licensing.contact import (
    MAX_CONTACT_RESPONSE_BYTES, MAX_QR_BYTES, contact_snapshot,
    create_contact, load_contact, parse_contact,
)
from wechatrebot_manager.licensing.protocol import LicenseError


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


def test_public_contact_round_trip_and_exact_expiry(tmp_path):
    record = create_contact(PNG, " WXRebot ", 1000)
    assert record["group_name"] == "WXRebot"
    path = tmp_path / "contact.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert load_contact(path) == record
    ready = contact_snapshot(record, 999, "remote")
    assert ready["state"] == "ready"
    assert ready["remaining_seconds"] == 1
    assert ready["image_data_url"].startswith("data:image/png;base64,")
    for now in (1000, 1001):
        expired = contact_snapshot(record, now, "remote")
        assert expired["state"] == "expired" and expired["image_data_url"] == ""
        assert expired["remaining_seconds"] == 0
        assert expired["expires_at"] == 1000 and expired["group_name"] == "WXRebot"


@pytest.mark.parametrize("overrides", [
    {"schema_version": True}, {"schema_version": 2}, {"unexpected": "field"},
    {"group_name": ""}, {"group_name": "x" * 81}, {"group_name": "group\nname"},
    {"group_name": "group\x7fname"}, {"group_name": 3},
    {"expires_at": True}, {"expires_at": 0}, {"expires_at": -1},
    {"expires_at": 1.5}, {"expires_at": 253402214401},
    {"image_base64": "bad!!"}, {"image_base64": ""}, {"image_base64": 12},
    {"mime_type": "image/jpeg"}, {"mime_type": "image/svg+xml"},
])
def test_rejects_malformed_or_unsafe_contact(overrides):
    record = create_contact(PNG, "WXRebot", 1000)
    record.update(overrides)
    with pytest.raises(LicenseError) as error:
        parse_contact(record)
    assert error.value.code == "contact_invalid"


def test_image_size_and_type_are_bounded():
    for image in (b"<svg></svg>", PNG + b"0" * MAX_QR_BYTES):
        with pytest.raises(LicenseError, match="二维码资料无效"):
            create_contact(image, "WXRebot", 1000)


@pytest.mark.parametrize("raw", [
    b"not-json", b"null", b'{"schema_version":1,"schema_version":1}',
    b" " * (MAX_CONTACT_RESPONSE_BYTES + 1),
], ids=["malformed-json", "null", "duplicate-fields", "oversized"])
def test_bad_contact_file_never_returns_ready(tmp_path, raw):
    path = tmp_path / "contact.json"
    path.write_bytes(raw)
    with pytest.raises(LicenseError) as error:
        load_contact(path)
    assert error.value.code == "contact_invalid"


def test_missing_contact_is_explicitly_unavailable(tmp_path):
    assert load_contact(tmp_path / "missing.json") is None
    snapshot = contact_snapshot(None, 1000, "remote")
    assert snapshot["state"] == "unavailable" and snapshot["image_data_url"] == ""
    assert snapshot["expires_at"] == 0
