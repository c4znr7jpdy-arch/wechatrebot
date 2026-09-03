from pathlib import Path

from wechatrebot_manager.environment import inspect_wechat_environment
from wechatrebot_manager.runtime_config import write_runtime_configs
from wechatrebot_manager.wechat_path import WechatExecutable


def test_environment_reports_missing_wechat(tmp_path: Path) -> None:
    write_runtime_configs(tmp_path)

    report = inspect_wechat_environment(
        tmp_path,
        discoverer=lambda: (),
        process_probe=lambda: (),
    )

    assert report.ready is False
    assert report.code == "11001"
    assert report.to_dict()["download_url"].startswith("https://")


def test_environment_uses_automatically_discovered_supported_wechat(
    tmp_path: Path,
) -> None:
    write_runtime_configs(tmp_path)
    executable = tmp_path / "Weixin.exe"
    item = WechatExecutable(executable, "4.1.8.27", "auto", ())

    report = inspect_wechat_environment(
        tmp_path,
        discoverer=lambda: (item,),
        process_probe=lambda: ("weixin.exe",),
    )

    assert report.ready is True
    assert report.path == str(executable)
    assert report.process_running is True


def test_environment_reports_unsupported_configured_version(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "Weixin.exe"
    write_runtime_configs(tmp_path, selected, wechat_version="4.2.0.0")
    unsupported = WechatExecutable(
        selected,
        "4.2.0.0",
        "configured",
        ("版本不支持。",),
    )

    report = inspect_wechat_environment(
        tmp_path,
        validator=lambda value, source: unsupported,
        discoverer=lambda: (),
        process_probe=lambda: (),
    )

    assert report.ready is False
    assert report.code == "11002"
    assert report.version == "4.2.0.0"
