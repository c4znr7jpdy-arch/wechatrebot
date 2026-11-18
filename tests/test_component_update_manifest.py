"""Signed component-only releases never fall back to another installation base."""
from copy import deepcopy
import json

import pytest

from test_updates import _channel_and_key, _incremental_manifest, _resign_manifest
from wechatrebot_manager import updates
from wechatrebot_manager.payload import installed_payload_fingerprint
from wechatrebot_manager.updates import UpdateError, manifest_package_releases, parse_update_manifest


def _component_manifest(channel, private, *, size=100):
    payload = _incremental_manifest(channel, private, size=size)
    payload.pop("package")
    payload["component_update_protocol"] = 1
    return _resign_manifest(payload, channel, private)


@pytest.mark.parametrize("size", [100, 1234, 2000])
def test_component_manifest_selects_exact_base_without_size_preference(size):
    channel, private = _channel_and_key()
    payload = _component_manifest(channel, private, size=size)
    other = deepcopy(payload["incremental_packages"][0])
    other["base_fingerprint"] = "A" * 64
    other["package"]["size"] = 1
    payload["incremental_packages"].insert(0, other)
    _resign_manifest(payload, channel, private)

    release = parse_update_manifest(payload, channel, current_version="0.7.0", current_fingerprint="b" * 64)
    assert release.package_kind == "incremental"
    assert release.base_fingerprint == "B" * 64
    assert release.package_size == size
    assert release.version == "0.8.0"
    all_packages = manifest_package_releases(payload, channel)
    assert len(all_packages) == 2
    assert all(item.package_kind == "incremental" for item in all_packages)


@pytest.mark.parametrize("fingerprint", ["", "invalid", "D" * 64])
def test_component_manifest_rejects_unknown_base_before_download(fingerprint, monkeypatch):
    channel, private = _channel_and_key()
    payload = _component_manifest(channel, private)
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_kw: pytest.fail("must not download"))
    with pytest.raises(UpdateError, match="安装基线没有匹配"):
        parse_update_manifest(payload, channel, current_version="0.7.0", current_fingerprint=fingerprint)


@pytest.mark.parametrize("inventory", ["valid", "missing", "invalid"])
def test_component_selection_uses_inventory_without_scanning_runtime(tmp_path, monkeypatch, inventory):
    channel, private = _channel_and_key()
    payload = _component_manifest(channel, private)
    if inventory != "missing":
        (tmp_path / "config").mkdir()
        path = tmp_path / "config/installed-payload.json"
        path.write_text(json.dumps({"schema_version": 1, "files": [
            {"path": "runtime/python/python.exe", "sha256": "A" * 64},
        ]}) if inventory == "valid" else "{invalid", encoding="utf-8")
    if inventory == "valid":
        fingerprint = installed_payload_fingerprint(tmp_path)
        payload["incremental_packages"][0]["base_fingerprint"] = fingerprint
        _resign_manifest(payload, channel, private)
    monkeypatch.setattr(updates, "sha256_file", lambda *_a: pytest.fail("must not scan files"))
    if inventory == "valid":
        result = parse_update_manifest(payload, channel, current_version="0.7.0", install_root=tmp_path)
        assert result.base_fingerprint == fingerprint
    else:
        # A caller-supplied fingerprint cannot mask a broken real inventory.
        with pytest.raises(UpdateError, match="安装基线没有匹配"):
            parse_update_manifest(payload, channel, current_version="0.7.0", install_root=tmp_path,
                                  current_fingerprint="B" * 64)


@pytest.mark.parametrize("version,build,available", [
    ("0.7.0", 0, True), ("0.8.0", 0, True), ("0.8.0", 2, False), ("0.9.0", 0, False),
])
def test_component_version_and_build_ordering(version, build, available):
    channel, private = _channel_and_key()
    payload = _component_manifest(channel, private)
    payload["build"] = 2
    payload["minimum_supported_version"] = "0.7.5"
    _resign_manifest(payload, channel, private)
    release = parse_update_manifest(payload, channel, current_version=version, current_build=build,
                                    current_fingerprint="B" * 64 if available else "")
    assert (release is not None) is available
    if available:
        assert release.build == 2
        assert release.mandatory is (version == "0.7.0")


@pytest.mark.parametrize("field,value", [
    ("component_update_protocol", None), ("component_update_protocol", True),
    ("component_update_protocol", 2), ("component_update_protocol", "1"),
    ("version", "invalid"), ("version", "v0.8.0"), ("version", 1),
    ("build", -1), ("build", True), ("build", 1.5), ("build", "2"),
    ("incremental_packages", []), ("incremental_packages", None),
    ("incremental_packages", {}), ("incremental_packages", [None]),
    ("package", None),
])
def test_invalid_component_manifest_is_rejected(field, value):
    channel, private = _channel_and_key()
    payload = _component_manifest(channel, private)
    payload[field] = value
    _resign_manifest(payload, channel, private)
    with pytest.raises(UpdateError):
        manifest_package_releases(payload, channel)
    with pytest.raises(UpdateError):
        parse_update_manifest(payload, channel, current_version="0.7.0", current_fingerprint="B" * 64)


@pytest.mark.parametrize("case", ["missing_protocol", "missing_packages", "duplicate", "too_many", "bad_unmatched_url"])
def test_component_manifest_validates_all_candidates(case):
    channel, private = _channel_and_key()
    payload = _component_manifest(channel, private)
    candidate = deepcopy(payload["incremental_packages"][0])
    if case == "missing_protocol":
        payload.pop("component_update_protocol")
    elif case == "missing_packages":
        payload.pop("incremental_packages")
    elif case == "duplicate":
        candidate["base_fingerprint"] = "b" * 64
        payload["incremental_packages"].append(candidate)
    elif case == "too_many":
        payload["incremental_packages"] = [{**candidate, "base_fingerprint": f"{i:064X}"} for i in range(65)]
    else:
        candidate["base_fingerprint"] = "D" * 64
        candidate["package"]["url"] = "http://updates.example.test/unsafe.zip"
        payload["incremental_packages"].append(candidate)
    _resign_manifest(payload, channel, private)
    with pytest.raises(UpdateError):
        manifest_package_releases(payload, channel)
    with pytest.raises(UpdateError):
        parse_update_manifest(payload, channel, current_version="0.7.0", current_fingerprint="B" * 64)


def test_component_signature_is_verified_before_inventory(tmp_path, monkeypatch):
    channel, private = _channel_and_key()
    payload = _component_manifest(channel, private)
    payload["incremental_packages"][0]["package"]["size"] += 1
    monkeypatch.setattr(updates, "installed_payload_fingerprint", lambda *_a: pytest.fail("untrusted manifest"))
    with pytest.raises(UpdateError, match="签名校验失败"):
        parse_update_manifest(payload, channel, current_version="0.7.0", install_root=tmp_path)
    with pytest.raises(UpdateError, match="签名校验失败"):
        manifest_package_releases(payload, channel)


def test_default_package_keeps_existing_selection_with_protocol_marker():
    channel, private = _channel_and_key()
    payload = _incremental_manifest(channel, private, size=2000)
    payload["component_update_protocol"] = 1
    _resign_manifest(payload, channel, private)
    release = parse_update_manifest(payload, channel, current_version="0.7.0", current_fingerprint="B" * 64)
    assert release.package_kind == "default"
    assert release.package_size == 1234
    assert len(manifest_package_releases(payload, channel)) == 2
