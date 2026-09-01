from pathlib import Path

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
