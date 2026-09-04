from pathlib import Path

from wechatrebot_manager.install_paths import validate_install_path


def test_relative_install_path_is_rejected() -> None:
    result = validate_install_path("relative/path", required_bytes=0)
    assert not result.ok
    assert "绝对路径" in " ".join(result.errors)


def test_nonempty_unmanaged_directory_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    (target / "unrelated.txt").write_text("x", encoding="utf-8")
    result = validate_install_path(target, required_bytes=0)
    assert not result.ok
    assert "非空" in " ".join(result.errors)


def test_unicode_and_spaces_are_supported() -> None:
    target = Path.cwd() / "artifacts" / "机器人 安装目录"
    result = validate_install_path(target, required_bytes=0)
    assert result.ok, result.errors


def test_payload_depth_reduces_allowed_install_root_length() -> None:
    base = Path.cwd() / "artifacts"
    target = base / ("x" * 90)
    relative = Path("runtime") / ("y" * 143)

    result = validate_install_path(
        target,
        required_bytes=0,
        payload_relative_paths=(relative,),
    )

    assert not result.ok
    assert "当前载荷最多支持" in " ".join(result.errors)
