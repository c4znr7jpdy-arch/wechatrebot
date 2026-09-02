from pathlib import Path
import struct

from wechatrebot_manager.wechat_path import validate_wechat_executable


def test_wrong_filename_is_rejected(tmp_path: Path) -> None:
    executable = tmp_path / "Other.exe"
    executable.write_bytes(b"not-a-real-exe")
    result = validate_wechat_executable(executable)
    assert not result.supported
    assert "Weixin.exe" in " ".join(result.errors)


def test_missing_wechat_is_rejected(tmp_path: Path) -> None:
    result = validate_wechat_executable(tmp_path / "Weixin.exe")
    assert not result.supported
    assert "不存在" in " ".join(result.errors)


def test_relative_wechat_path_is_rejected() -> None:
    result = validate_wechat_executable("Weixin.exe")
    assert not result.supported
    assert "绝对路径" in " ".join(result.errors)


def _wechat_pe(path: Path, machine: int) -> None:
    payload = bytearray(256)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", payload, 0x84, machine)
    path.write_bytes(payload)


def test_wechat_must_be_x64(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "Weixin.exe"
    _wechat_pe(executable, 0x014C)
    monkeypatch.setattr("wechatrebot_manager.wechat_path.read_file_version", lambda path: "4.1.8.27")

    result = validate_wechat_executable(executable)

    assert not result.supported
    assert "x64" in " ".join(result.errors)


def test_supported_x64_wechat_is_accepted(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "Weixin.exe"
    _wechat_pe(executable, 0x8664)
    monkeypatch.setattr("wechatrebot_manager.wechat_path.read_file_version", lambda path: "4.1.8.27")

    result = validate_wechat_executable(executable)

    assert result.supported
