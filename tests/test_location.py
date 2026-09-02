from pathlib import Path

from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.location import load_install_location, save_install_location


def test_install_location_round_trip_requires_ready_marker(tmp_path: Path) -> None:
    install = tmp_path / "install"
    install.mkdir()
    write_json_atomic(
        install / ".wechatrebot-install.json",
        {"schema_version": 1, "state": "ready"},
    )
    local = tmp_path / "local"
    save_install_location(install, local_app_data=local)
    assert load_install_location(local_app_data=local) == install.resolve()


def test_install_location_ignores_failed_install(tmp_path: Path) -> None:
    install = tmp_path / "install"
    install.mkdir()
    write_json_atomic(
        install / ".wechatrebot-install.json",
        {"schema_version": 1, "state": "failed"},
    )
    local = tmp_path / "local"
    save_install_location(install, local_app_data=local)
    assert load_install_location(local_app_data=local) is None
