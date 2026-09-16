import hashlib
import json
from pathlib import Path

from wechatrebot_manager.brand import BrandProfile
from wechatrebot_manager.installer import InstallRequest, perform_install
from wechatrebot_manager.install_paths import InstallPathValidation
from wechatrebot_manager.provider import OpenAIProviderProfile
from wechatrebot_manager.wechat_path import WechatExecutable


def _payload(root: Path) -> None:
    source = root / "app" / "bridge" / "src" / "hook_bridge" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text("# bridge\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest().upper()
    (root / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [
                    {
                        "path": "app/bridge/src/hook_bridge/__init__.py",
                        "sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_perform_install_writes_ready_brand_and_provider(
    tmp_path: Path, monkeypatch
) -> None:
    payload = tmp_path / "payload"
    _payload(payload)
    wechat_path = tmp_path / "Weixin.exe"
    wechat_path.write_bytes(b"wechat")
    request = InstallRequest(
        install_root=tmp_path / "install",
        payload_root=payload,
        wechat=WechatExecutable(wechat_path, "4.1.8.27", "test", ()),
        brand=BrandProfile("群聊管家", "管家", "小管家"),
        provider=OpenAIProviderProfile(
            api_key="example-secret",
            api_base="https://api.example.com/v1",
            model="chat-model",
        ),
        admin_id="wxid_customer_admin",
    )
    monkeypatch.setattr(
        "wechatrebot_manager.installer.validate_install_path",
        lambda path, **_kwargs: InstallPathValidation(
            Path(path), (), (), 10 * 1024**3
        ),
    )

    phases: list[str] = []
    result = perform_install(
        request,
        progress_callback=lambda phase, current, total: phases.append(phase),
        save_locator=False,
        create_shortcuts=False,
    )

    assert result is not None
    marker = json.loads(
        (request.install_root / ".wechatrebot-install.json").read_text(
            encoding="utf-8"
        )
    )
    astrbot = json.loads(
        (request.install_root / "app" / "astrbot" / "data" / "cmd_config.json")
        .read_text(encoding="utf-8")
    )
    assert marker["state"] == "ready"
    assert astrbot["provider_settings"]["default_personality"] == "commercial_brand"
    assert astrbot["provider_settings"]["default_provider_id"].endswith("/chat-model")
    assert astrbot["admins_id"] == ["wxid_customer_admin"]
    assert phases == ["prepare", "install", "configure", "configure"]


def test_perform_install_does_not_require_wechat(
    tmp_path: Path, monkeypatch
) -> None:
    payload = tmp_path / "payload"
    _payload(payload)
    request = InstallRequest(
        install_root=tmp_path / "install",
        payload_root=payload,
        brand=BrandProfile("群聊管家", "管家", "小管家"),
    )
    monkeypatch.setattr(
        "wechatrebot_manager.installer.validate_install_path",
        lambda path, **_kwargs: InstallPathValidation(
            Path(path), (), (), 10 * 1024**3
        ),
    )

    perform_install(
        request,
        save_locator=False,
        create_shortcuts=False,
    )

    manager = json.loads(
        (request.install_root / "config" / "manager.json").read_text(
            encoding="utf-8"
        )
    )
    bridge = json.loads(
        (request.install_root / "app" / "bridge" / "config.json").read_text(
            encoding="utf-8"
        )
    )
    assert manager["wechat_exe_path"] == ""
    assert bridge["wechat_exe_path"] == ""


def test_install_creates_both_root_executables(tmp_path: Path, monkeypatch) -> None:
    from wechatrebot_manager.installed_entries import START_EXECUTABLE_NAME
    from wechatrebot_manager.uninstaller import UNINSTALL_EXECUTABLE_NAME

    payload = tmp_path / "payload"
    _payload(payload)
    executable = payload / "manager" / "WeChatReBot.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"test-manager")
    manifest_path = payload / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({
        "path": "manager/WeChatReBot.exe",
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest().upper(),
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        "wechatrebot_manager.installer.validate_install_path",
        lambda path, **_kwargs: InstallPathValidation(Path(path), (), (), 10 * 1024**3),
    )
    # Integration checks must not change this developer machine's Apps entry.
    monkeypatch.setattr("wechatrebot_manager.uninstaller.register_windows_uninstaller", lambda *a, **k: True)
    root = tmp_path / "install"
    perform_install(
        InstallRequest(root, payload, BrandProfile("群聊管家", "管家", "小管家")),
        save_locator=False, create_shortcuts=False,
    )
    assert (root / START_EXECUTABLE_NAME).read_bytes() == b"test-manager"
    assert (root / UNINSTALL_EXECUTABLE_NAME).read_bytes() == b"test-manager"
