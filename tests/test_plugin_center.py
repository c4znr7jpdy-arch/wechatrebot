from __future__ import annotations

import base64
import hashlib
import hmac
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wechatrebot_manager.brand import BrandProfile, save_brand_profile
from wechatrebot_manager.plugin_center import (
    ConfigConflictError,
    EngineApiClient,
    PluginCenterError,
    PluginCenterService,
    _asset_root,
    _handler,
)
from wechatrebot_manager.runtime_config import write_runtime_configs


def _install(tmp_path: Path) -> tuple[Path, str]:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    save_brand_profile(
        install / "config" / "brand.json",
        BrandProfile("群聊管家", "管家", "小管家").normalized(),
    )
    write_runtime_configs(install, wechat)
    engine_config_path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    engine_config = json.loads(engine_config_path.read_text(encoding="utf-8"))
    engine_config["provider"] = [
        {"id": "vendor/reasoning-model", "model": "推理模型", "enable": True},
        {"id": "vector_private", "type": "openai_embedding", "enable": True},
        {"id": "disabled/model", "model": "停用模型", "enable": False},
    ]
    engine_config["persona"] = [{"name": "客服人格"}]
    engine_config_path.write_text(
        json.dumps(engine_config, ensure_ascii=False), encoding="utf-8"
    )

    module_name = "astrbot_plugin_self_learning"
    plugin = install / "app" / "astrbot" / "data" / "plugins" / module_name
    (plugin / "pages" / "dashboard").mkdir(parents=True)
    schema = {
        "model_group": {
            "description": "AstrBot 模型配置",
            "hint": "在 WebUI Dashboard 中选择，不使用 NapCat 或 QQ 号。",
            "type": "object",
            "items": {
                "provider_id": {
                    "description": "思考模型",
                    "type": "string",
                    "default": None,
                    "_special": "select_provider",
                },
                "api_secret": {
                    "description": "服务密钥",
                    "type": "string",
                    "default": "",
                    "_secret": True,
                },
            },
        },
        "AstrBot_internal_key": {
            "description": "内部兼容值",
            "type": "string",
            "default": "keep",
        },
    }
    (plugin / "_conf_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    config_path = install / "app" / "astrbot" / "data" / "config"
    config_path.mkdir(parents=True)
    (config_path / f"{module_name}_config.json").write_text(
        json.dumps({"unknown": {"keep": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    public_id = service.catalog()["plugins"][0]["id"]
    return install, public_id


def test_catalog_uses_opaque_ids_and_detail_white_labels_schema(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]

    catalog = service.catalog()
    assert catalog["product_name"] == "群聊管家"
    assert catalog["plugins"] == [
        {
            "id": public_id,
            "display_name": "自我学习",
            "enabled": True,
            "configurable": True,
            "field_count": 3,
            "pages": ("dashboard",),
        }
    ]
    assert public_id.startswith("feature-")
    assert "module_name" not in catalog["plugins"][0]

    detail = service.detail(public_id)
    assert "AstrBot_internal_key" in detail["schema"]
    customer_copy = json.dumps(
        {
            "description": detail["schema"]["model_group"]["description"],
            "hint": detail["schema"]["model_group"]["hint"],
        },
        ensure_ascii=False,
    ).casefold()
    assert "astrbot" not in customer_copy
    assert "napcat" not in customer_copy
    assert "webui" not in customer_copy
    assert "qq" not in customer_copy
    assert detail["config"]["unknown"] == {"keep": True}
    assert detail["config"]["model_group"]["provider_id"] is None
    assert detail["providers"] == [
        {"value": "vendor/reasoning-model", "label": "推理模型"}
    ]
    assert detail["embedding_providers"] == [
        {"value": "vector_private", "label": "vector_private"}
    ]
    assert detail["personas"] == ["客服人格"]


def test_offline_save_preserves_unknown_values_and_detects_conflict(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    updated = detail["config"]
    updated["model_group"]["provider_id"] = "vendor/reasoning-model"

    result = service.save(public_id, updated, detail["revision"])
    assert result["saved"] is True
    assert result["reloaded"] is False
    saved = json.loads(
        (
            install
            / "app"
            / "astrbot"
            / "data"
            / "config"
            / "astrbot_plugin_self_learning_config.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["unknown"] == {"keep": True}
    assert saved["model_group"]["provider_id"] == "vendor/reasoning-model"

    with pytest.raises(ConfigConflictError, match="其他窗口"):
        service.save(public_id, updated, detail["revision"])


def test_offline_save_validates_field_types(tmp_path: Path) -> None:
    _, public_id = _install(tmp_path)
    service = PluginCenterService(tmp_path / "install")
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["model_group"]["api_secret"] = 42
    with pytest.raises(PluginCenterError, match="类型不正确"):
        service.save(public_id, detail["config"], detail["revision"])


def test_dashboard_token_is_short_lived_and_signed(tmp_path: Path) -> None:
    install = tmp_path / "install"
    path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "dashboard": {
                    "jwt_secret": "local-signing-secret",
                    "username": "local-console",
                    "port": 6185,
                }
            }
        ),
        encoding="utf-8",
    )
    token = EngineApiClient(install)._token()
    header, body, signature = token.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    )
    expected = base64.urlsafe_b64encode(
        hmac.new(
            b"local-signing-secret",
            f"{header}.{body}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode("ascii")
    assert signature == expected
    assert payload["username"] == "local-console"
    assert 0 < payload["exp"] - int(time.time()) <= 300


def test_plugin_web_assets_are_packaged_and_customer_facing() -> None:
    root = _asset_root()
    html = (root / "index.html").read_text(encoding="utf-8")
    javascript = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "app.css").read_text(encoding="utf-8")
    visible = f"{html}\n{javascript}\n{css}".casefold()
    assert "热重载" in visible
    assert "保存并热重载" in visible
    assert "astrbot" not in visible
    assert "window.alert(" not in visible
    assert "window.confirm(" not in visible
    assert "window.prompt(" not in visible


def test_original_engine_home_is_replaced_with_neutral_local_page() -> None:
    project = Path(__file__).parents[1]
    page = (project / "assets" / "engine-webui" / "index.html").read_text(
        encoding="utf-8"
    )
    version = (
        project / "assets" / "engine-webui" / "assets" / "version"
    ).read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (project / "release" / "source-manifest.json").read_text(encoding="utf-8")
    )
    assert "astrbot" not in page.casefold()
    assert version == "v4.26.6"
    assert any(
        item.get("target") == "app/astrbot/data/dist"
        and item.get("source") == "assets/engine-webui"
        for item in manifest["entries"]
    )


def test_local_web_handler_serves_assets_and_rejects_foreign_mutation() -> None:
    class FakeService:
        def catalog(self):
            return {"plugins": [], "engine_online": False, "product_name": "测试机器人"}

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(FakeService()))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/", timeout=2) as response:
            assert response.status == 200
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
            assert "功能配置中心" in response.read().decode("utf-8")
        with urlopen(f"{base}/control/catalog", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["data"]["product_name"] == "测试机器人"
        request = Request(
            f"{base}/control/plugins/feature-0000000000000000/enabled",
            data=b'{"enabled":true}',
            method="PATCH",
            headers={"Content-Type": "application/json", "Origin": "https://example.invalid"},
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(request, timeout=2)
        assert rejected.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
