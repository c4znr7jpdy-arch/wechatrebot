"""Firmware-based trial identity is independent of installation credentials."""
import ctypes
import hashlib
from types import SimpleNamespace

import pytest

from wechatrebot_manager.licensing import device
from wechatrebot_manager.licensing.protocol import LicenseError


def firmware(identifier=bytes(range(16)), *, version=(3, 2), prefix=b"", suffix=b""):
    record = bytes([1, 27, 0, 0, 1, 2, 3, 4]) + identifier + bytes(3)
    table = prefix + record + b"manufacturer\0model\0version\0serial\0\0" + suffix
    return bytes([0, *version, 0]) + len(table).to_bytes(4, "little") + table


def test_reinstall_and_user_changes_preserve_identity_without_storing_raw_data(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "_read_smbios", lambda: firmware())
    first = device.stable_machine_id(tmp_path / "old")
    assert first == device.stable_machine_id(tmp_path / "new-user" / "new-install")
    assert first == hashlib.sha256(b"wechatrebot:trial-machine:smbios-v1:" + bytes(range(16))).hexdigest()
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(device, "_read_smbios", lambda: firmware(bytes(reversed(range(16)))))
    assert device.stable_machine_id(tmp_path) != first


def test_unrelated_firmware_fields_do_not_change_trial_identity(monkeypatch):
    monkeypatch.setattr(device, "_read_smbios", lambda: firmware())
    first = device.stable_machine_id()
    monkeypatch.setattr(device, "_read_smbios", lambda: firmware(
        prefix=b"\x00\x04\x00\x00\x00\x00", suffix=b"\x7f\x04\x00\x00\x00\x00",
        version=(3, 9),
    ))
    assert device.stable_machine_id() == first


@pytest.mark.parametrize("raw", [
    b"", bytes(8), firmware(bytes(16)), firmware(b"\xff" * 16),
    firmware(version=(2, 5)), firmware()[:-1],
    firmware(prefix=b"\x00\x03\x00\x00\x00\x00"),
    firmware(suffix=firmware()[8:]),
    bytes([0, 3, 2, 0]) + (6).to_bytes(4, "little") + b"\x7f\x04\x00\x00\x00\x00",
])
def test_unreliable_firmware_never_falls_back_to_new_identity(monkeypatch, raw):
    monkeypatch.setattr(device, "_read_smbios", lambda: raw)
    with pytest.raises(LicenseError) as failure:
        device.stable_machine_id()
    assert failure.value.code == "device_identity_unavailable"


def test_firmware_read_failure_is_actionable_and_does_not_expose_raw_error(monkeypatch):
    def fail():
        raise OSError("private-hardware-serial")
    monkeypatch.setattr(device, "_read_smbios", fail)
    with pytest.raises(LicenseError) as failure:
        device.stable_machine_id()
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("reported_size,read_delta", [(0, 0), (16 * 1024 * 1024 + 1, 0), (64, -1)])
def test_native_table_size_is_bounded_and_checked(monkeypatch, reported_size, read_delta):
    class Read:
        def __call__(self, provider, table, buffer, size):
            assert provider == int.from_bytes(b"RSMB", "big") and table == 0
            return reported_size if buffer is None else size + read_delta
    monkeypatch.setattr(device, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_kw: SimpleNamespace(GetSystemFirmwareTable=Read()), raising=False)
    with pytest.raises(OSError):
        device._read_smbios()
