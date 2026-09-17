from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from wechatrebot_manager import native_setup


def test_native_setup_uses_verified_backend_without_duplicate_entrypoints(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def install(request, **kwargs):
        calls.append((request, kwargs))
        return SimpleNamespace(files=(object(), object()))

    monkeypatch.setattr(native_setup, "perform_install", install)
    monkeypatch.setattr(
        native_setup,
        "stop_installation_processes",
        lambda value: (_ for _ in ()).throw(AssertionError("fresh install stopped processes")),
    )
    result = native_setup.install_from_native_setup(
        tmp_path / "payload", tmp_path / "install"
    )

    request, options = calls[0]
    assert request.payload_root == tmp_path / "payload"
    assert request.install_root == tmp_path / "install"
    assert options == {"create_shortcuts": False, "create_entrypoints": False}
    assert result["file_count"] == 2
    assert result["stopped_processes"] == 0


def test_native_setup_stops_existing_install_before_replacing_files(
    monkeypatch, tmp_path: Path
) -> None:
    install = tmp_path / "install"
    install.mkdir()
    (install / native_setup.INSTALL_MARKER).write_text("{}", encoding="utf-8")
    events: list[str] = []

    monkeypatch.setattr(
        native_setup,
        "stop_installation_processes",
        lambda value: events.append(f"stop:{value}") or 2,
    )
    monkeypatch.setattr(
        native_setup,
        "perform_install",
        lambda request, **kwargs: events.append(f"install:{request.install_root}")
        or SimpleNamespace(files=(object(),)),
    )

    result = native_setup.install_from_native_setup(tmp_path / "payload", install)

    assert events == [f"stop:{install.resolve()}", f"install:{install.resolve()}"]
    assert result["stopped_processes"] == 2


def test_native_uninstall_stops_owned_processes_and_removes_locator(
    monkeypatch, tmp_path: Path
) -> None:
    install = tmp_path / "install"
    residual = tmp_path / "WeChatReBot"
    residual.mkdir()
    (residual / "install-location.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(native_setup, "validate_uninstall_root", lambda value: install)
    monkeypatch.setattr(native_setup, "stop_installation_processes", lambda value: 3)
    monkeypatch.setattr(native_setup, "locator_path", lambda: residual / "install-location.json")

    result = native_setup.prepare_native_uninstall(install)

    assert result == {"ok": True, "stopped_processes": 3}
    assert not residual.exists()


def test_inno_script_uses_native_scope_download_and_downgrade_guards() -> None:
    script = (
        Path(__file__).parents[1] / "installer" / "WeChatReBot.iss"
    ).read_text(encoding="utf-8")
    assert "PrivilegesRequiredOverridesAllowed=dialog" in script
    assert "DefaultDirName={code:DefaultInstallDirectory}" in script
    assert "CreateDownloadPage" in script
    assert "PayloadSHA256" in script
    assert "ComparePackedVersion(InstalledVersion, SetupVersion) > 0" in script
    assert "install-from-native-setup" in script
    assert "prepare-native-uninstall" in script
