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
                "source_manifest_status": "ready",
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
            api_key="customer-secret",
            api_base="https://api.example.com/v1",
            model="chat-model",
        ),
    )
    monkeypatch.setattr(
        "wechatrebot_manager.installer.validate_install_path",
        lambda path: InstallPathValidation(Path(path), (), (), 10 * 1024**3),
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
    assert phases == ["verify", "stage", "install", "configure", "configure"]
