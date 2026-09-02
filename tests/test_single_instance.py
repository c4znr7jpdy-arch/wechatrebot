from pathlib import Path

from wechatrebot_manager.single_instance import SingleInstanceLock, instance_name


def test_instance_name_is_stable_and_does_not_expose_install_path() -> None:
    name = instance_name(Path("D:/客户/机器人"))
    assert name.startswith("Local\\WeChatReBot-")
    assert "客户" not in name
    assert name == instance_name("d:/客户/机器人")


def test_only_one_lock_for_the_same_key_can_be_held(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path / "install")
    second = SingleInstanceLock(tmp_path / "install")
    third = SingleInstanceLock(tmp_path / "install")
    try:
        assert first.acquire()
        assert not second.acquire()
        first.release()
        assert third.acquire()
    finally:
        first.release()
        second.release()
        third.release()
