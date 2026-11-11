from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechatrebot_manager import installer, native_setup
from wechatrebot_manager.brand import BrandProfile, install_brand_profile
from wechatrebot_manager.install_paths import InstallPathValidation
from wechatrebot_manager.provider import OpenAIProviderProfile
from wechatrebot_manager.runtime_config import RuntimePorts, write_runtime_configs


def test_native_setup_uses_verified_backend_without_duplicate_entrypoints(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def install(request, **kwargs):
        calls.append((request, kwargs))
        manager = request.install_root / "manager" / "WeChatReBot.exe"
        manager.parent.mkdir(parents=True)
        manager.write_bytes(b"manager")
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
    assert options == {
        "create_shortcuts": False, "create_entrypoints": False,
        "preserve_existing_settings": False,
    }
    assert result["file_count"] == 2
    assert result["stopped_processes"] == 0
    assert (request.install_root / "启动机器人.exe").read_bytes() == b"manager"
    assert not (request.install_root / "卸载机器人.exe").exists()


def test_native_setup_stops_existing_install_before_replacing_files(
    monkeypatch, tmp_path: Path
) -> None:
    install = tmp_path / "install"
    install.mkdir()
    (install / native_setup.INSTALL_MARKER).write_text("{}", encoding="utf-8")
    events: list[str] = []
    (install / "manager").mkdir()
    (install / "manager" / "WeChatReBot.exe").write_bytes(b"manager")

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


@pytest.mark.parametrize("repair", [False, True, "incomplete"])
def test_native_install_preserves_customer_settings_only_for_existing_install(
    monkeypatch, tmp_path: Path, repair: bool | str,
) -> None:
    install = tmp_path / "install"
    payload = tmp_path / "payload"
    plugin_config = "app/astrbot/data/config/customer_feature_config.json"
    payload_files = {
        "manager/WeChatReBot.exe": b"synthetic-manager",
        "app/astrbot/data/cmd_config.json": json.dumps({
            "native_payload_default": True, "admins_id": [], "persona": [],
            "provider": [], "provider_sources": [],
        }).encode(),
        plugin_config: b'{"template": "default"}',
    }
    for name, content in payload_files.items():
        target = payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (payload / "release-manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                  for name, content in payload_files.items()],
    }), encoding="utf-8")
    monkeypatch.setattr(installer, "validate_install_path", lambda path, **_kwargs: InstallPathValidation(
        Path(path), (), (), 10 * 1024**3,
    ))
    monkeypatch.setattr(installer, "save_install_location", lambda _root: None)
    monkeypatch.setattr(native_setup, "stop_installation_processes", lambda _root: 0)
    engine_path = install / "app/astrbot/data/cmd_config.json"
    manager_path = install / "config/manager.json"
    brand_path = install / "config/brand.json"
    wechat = tmp_path / "Weixin.exe"
    if repair is True:
        wechat.write_bytes(b"synthetic-wechat")
        profile = BrandProfile("客户机器人", "客户", "客户人格", name_source="wechat")
        install_brand_profile(install, profile)
        write_runtime_configs(
            install, wechat, wechat_version="4.1.8.27", brand_profile=profile,
            ports=RuntimePorts(dashboard=16185, astrbot_ws=16200, bridge_callback=15000, hook_api=29088),
            provider_profile=OpenAIProviderProfile(
                api_key="synthetic-secret", api_base="https://example.test/v1", model="customer-model",
            ), admin_id="wxid_customer_admin",
        )
        (install / native_setup.INSTALL_MARKER).write_text(
            json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8",
        )
        engine = json.loads(engine_path.read_text("utf-8"))
        engine["admins_id"].append("wxid_second_admin")
        engine["persona"][0]["prompt"] = "客户独立编辑的人格提示词"
        engine_path.write_text(json.dumps(engine), encoding="utf-8")
        (install / plugin_config).write_bytes(b'{"customer": "saved"}')
        original_brand = brand_path.read_bytes()
    elif repair == "incomplete":
        install.mkdir()
        (install / native_setup.INSTALL_MARKER).write_text(
            json.dumps({"schema_version": 1, "state": "failed"}), encoding="utf-8",
        )

    result = native_setup.install_from_native_setup(payload, install)

    saved_engine = json.loads(engine_path.read_text("utf-8"))
    saved_manager = json.loads(manager_path.read_text("utf-8"))
    assert result["ok"] is True
    assert (install / "启动机器人.exe").read_bytes() == b"synthetic-manager"
    if repair is True:
        assert brand_path.read_bytes() == original_brand
        assert saved_engine["admins_id"] == engine["admins_id"]
        assert saved_engine["persona"] == engine["persona"]
        assert saved_engine["provider"] == engine["provider"]
        assert saved_engine["provider_sources"] == engine["provider_sources"]
        assert saved_engine["provider_settings"]["default_provider_id"] == engine["provider_settings"]["default_provider_id"]
        assert "native_payload_default" not in saved_engine
        assert (install / plugin_config).read_bytes() == b'{"customer": "saved"}'
        assert saved_manager["wechat_exe_path"] == str(wechat.resolve())
        assert saved_manager["wechat_version"] == "4.1.8.27"
        assert saved_manager["ports"]["dashboard"] == 16185
    else:
        assert json.loads(brand_path.read_text("utf-8")) == native_setup.DEFAULT_NATIVE_BRAND.to_dict()
        assert saved_engine["admins_id"] == []
        assert saved_engine["native_payload_default"] is True
        assert (install / plugin_config).read_bytes() == payload_files[plugin_config]
        assert saved_manager["wechat_exe_path"] == ""
        assert saved_manager["wechat_version"] is None


@pytest.mark.parametrize("config", ["config/brand.json", "config/manager.json", "app/astrbot/data/cmd_config.json"])
def test_native_repair_rejects_broken_settings_before_replacing_payload(
    monkeypatch, tmp_path: Path, config: str,
) -> None:
    install = tmp_path / "install"
    damaged = install / config
    damaged.parent.mkdir(parents=True)
    damaged.write_text("{broken", encoding="utf-8")
    marker = install / native_setup.INSTALL_MARKER
    marker.write_text('{"schema_version": 1, "state": "ready"}', encoding="utf-8")
    original_marker = marker.read_bytes()
    monkeypatch.setattr(native_setup, "stop_installation_processes", lambda _root: 0)
    monkeypatch.setattr(installer.InstallRequest, "validate", lambda _request: ())
    monkeypatch.setattr(installer, "install_payload", lambda *_args, **_kwargs: pytest.fail("replaced payload before validating settings"))

    with pytest.raises(ValueError, match="配置.*读取"):
        native_setup.install_from_native_setup(tmp_path / "payload", install)

    assert damaged.read_text("utf-8") == "{broken"
    assert marker.read_bytes() == original_marker


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
    monkeypatch.setattr(native_setup, "load_install_location", lambda: install)

    result = native_setup.prepare_native_uninstall(install)

    assert result == {"ok": True, "stopped_processes": 3}
    assert not residual.exists()


def test_native_uninstall_does_not_remove_another_install_locator(
    monkeypatch, tmp_path: Path
) -> None:
    install = tmp_path / "install"
    residual = tmp_path / "WeChatReBot"
    residual.mkdir()
    (residual / "install-location.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(native_setup, "validate_uninstall_root", lambda value: install)
    monkeypatch.setattr(native_setup, "stop_installation_processes", lambda value: 0)
    monkeypatch.setattr(native_setup, "locator_path", lambda: residual / "install-location.json")
    monkeypatch.setattr(native_setup, "load_install_location", lambda: tmp_path / "other")

    native_setup.prepare_native_uninstall(install)

    assert residual.exists()


def test_inno_script_uses_native_scope_download_and_downgrade_guards() -> None:
    script = (
        Path(__file__).parents[1] / "installer" / "WeChatReBot.iss"
    ).read_text(encoding="utf-8")
    assert "PrivilegesRequiredOverridesAllowed=dialog" in script
    assert "DefaultDirName={code:DefaultInstallDirectory}" in script
    assert "CreateDownloadPage" in script
    assert "ArchiveExtraction=full" in script
    assert "PayloadSHA256" in script
    assert "ComparePackedVersion(InstalledVersion, SetupVersion) > 0" in script
    assert "install-from-native-setup" in script
    assert "prepare-native-uninstall" in script
    assert "function InitializeUninstall: Boolean" in script
    icons = script.split("[Icons]", 1)[1].split("[Run]", 1)[0]
    assert icons.count('Filename: "{app}\\启动机器人.exe"') == 2
    assert "Parameters:" not in icons
