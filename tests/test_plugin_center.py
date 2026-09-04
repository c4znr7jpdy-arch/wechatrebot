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
    _normalize_config,
    _normalize_plugin_extension_endpoint,
    plugin_center_command,
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
        {"id": "rank_private", "type": "vllm_rerank", "enable": True},
        {"id": "disabled/model", "model": "停用模型", "enable": False},
    ]
    engine_config["persona"] = [{"name": "客服人格"}]
    engine_config_path.write_text(
        json.dumps(engine_config, ensure_ascii=False), encoding="utf-8"
    )

    module_name = "sample_configurable_feature"
    plugin = install / "app" / "astrbot" / "data" / "plugins" / module_name
    (plugin / "pages" / "dashboard").mkdir(parents=True)
    (plugin / "pages" / "dashboard" / "index.html").write_text(
        "<!doctype html><title>dashboard</title>", encoding="utf-8"
    )
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
                "embedding_provider_id": {
                    "description": "记忆检索模型",
                    "type": "string",
                    "default": None,
                },
                "api_secret": {
                    "description": "服务密钥",
                    "type": "string",
                    "default": "",
                    "_secret": True,
                },
                "enc_params": {
                    "description": "内部加密参数",
                    "type": "string",
                    "default": "",
                    "invisible": True,
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
    config_path.mkdir(parents=True, exist_ok=True)
    (config_path / f"{module_name}_config.json").write_text(
        json.dumps(
            {
                "unknown": {"keep": True},
                "model_group": {
                    "api_secret": "stored-secret",
                    "enc_params": "stored-encrypted-value",
                },
            },
            ensure_ascii=False,
        ),
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
            "display_name": "Sample Configurable Feature",
            "enabled": True,
            "configurable": True,
            "field_count": 4,
            "model_field_count": 2,
            "pages": ("dashboard",),
            "configuration": "schema-and-page",
            "description": "",
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
    assert "unknown" not in detail["config"]
    assert detail["config"]["model_group"]["provider_id"] is None
    assert detail["config"]["model_group"]["api_secret"] == ""
    assert "enc_params" not in detail["schema"]["model_group"]["items"]
    assert detail["schema"]["model_group"]["items"]["api_secret"]["secret"] is True
    assert detail["schema"]["model_group"]["items"]["api_secret"]["_secret_configured"] is True
    assert detail["providers"] == [
        {
            "value": "vendor/reasoning-model",
            "label": "推理模型 · vendor/reasoning-model",
        }
    ]
    assert detail["embedding_providers"] == [
        {"value": "vector_private", "label": "vector_private"}
    ]
    assert detail["rerank_providers"] == [
        {"value": "rank_private", "label": "rank_private"}
    ]
    assert detail["personas"] == ["客服人格"]
    assert "provider_id" in detail["model_schema"]["model_group"]["items"]
    assert "embedding_provider_id" in detail["model_schema"]["model_group"]["items"]
    assert "provider_id" not in detail["basic_schema"]["model_group"]["items"]
    assert "api_secret" in detail["basic_schema"]["model_group"]["items"]
    assert detail["model_schema"]["model_group"]["items"]["provider_id"]["options"] == [
        "",
        "vendor/reasoning-model",
    ]
    assert detail["model_schema"]["model_group"]["items"]["embedding_provider_id"]["options"] == [
        "",
        "vector_private",
    ]


def test_delivery_policy_hides_legacy_and_technical_configuration(tmp_path: Path) -> None:
    install = tmp_path / "install"
    plugin_root = install / "app" / "astrbot" / "data" / "plugins"
    legal = install / "legal"
    legal.mkdir(parents=True)
    modules = {
        "astrbot_plugin_angel_heart": ("chat-config",),
        "astrbot_plugin_music": (),
        "jiang_short_drama": ("recommendation-schedules",),
        "astrbot_plugin_livingmemory": (),
        "astrbot_plugin_self_learning": (),
    }
    for module, pages in modules.items():
        directory = plugin_root / module
        directory.mkdir(parents=True)
        (directory / "_conf_schema.json").write_text(
            json.dumps({"technical_value": {"type": "string", "default": "hidden"}}),
            encoding="utf-8",
        )
        for page in pages:
            page_root = directory / "pages" / page
            page_root.mkdir(parents=True)
            (page_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (legal / "feature-set.json").write_text(
        json.dumps(
            {
                "features": [
                    {
                        "module": "astrbot_plugin_angel_heart",
                        "display_name": "长期记忆",
                        "configuration": "page",
                        "description": "原生上下文与天使之心",
                    },
                    {
                        "module": "astrbot_plugin_music",
                        "display_name": "点歌",
                        "configuration": "none",
                    },
                    {
                        "module": "jiang_short_drama",
                        "display_name": "短剧",
                        "configuration": "page-and-global-schedule-center",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]

    catalog = service.catalog()["plugins"]

    assert [item["display_name"] for item in catalog] == ["长期记忆", "点歌", "短剧"]
    assert all(item["configurable"] is False for item in catalog)
    by_name = {item["display_name"]: item for item in catalog}
    assert by_name["点歌"]["pages"] == ()
    assert by_name["短剧"]["pages"] == ("recommendation-schedules",)
    assert by_name["长期记忆"]["pages"] == ("chat-config",)
    music = service.detail(by_name["点歌"]["id"])
    drama = service.detail(by_name["短剧"]["id"])
    assert music["schema"] == music["config"] == {}
    assert drama["schema"] == drama["config"] == {}
    with pytest.raises(PluginCenterError, match="没有可编辑"):
        service.save(by_name["点歌"]["id"], {}, music["revision"])


def test_plugin_center_command_can_open_selected_feature(tmp_path: Path) -> None:
    command = plugin_center_command(tmp_path, "feature-0123456789abcdef")

    assert command[-1] == "feature-0123456789abcdef"
    with pytest.raises(PluginCenterError, match="标识无效"):
        plugin_center_command(tmp_path, "../not-a-feature")


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
            / "sample_configurable_feature_config.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["unknown"] == {"keep": True}
    assert saved["model_group"]["provider_id"] == "vendor/reasoning-model"
    assert saved["model_group"]["api_secret"] == "stored-secret"
    assert saved["model_group"]["enc_params"] == "stored-encrypted-value"

    with pytest.raises(ConfigConflictError, match="其他窗口"):
        service.save(public_id, updated, detail["revision"])


def test_offline_save_validates_field_types(tmp_path: Path) -> None:
    _, public_id = _install(tmp_path)
    service = PluginCenterService(tmp_path / "install")
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["model_group"]["provider_id"] = ["not-a-string"]
    with pytest.raises(PluginCenterError, match="类型不正确"):
        service.save(public_id, detail["config"], detail["revision"])


def test_online_save_enable_and_reload_use_live_engine_contract(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    config_path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "sample_configurable_feature_config.json"
    )
    calls: list[tuple[object, ...]] = []
    service.engine.is_available = lambda: True  # type: ignore[method-assign]

    def save_config(module_name: str, config: dict) -> None:
        calls.append(("save", module_name, config))
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    service.engine.save_config = save_config  # type: ignore[method-assign]
    service.engine.set_enabled = lambda module, enabled: calls.append(  # type: ignore[method-assign]
        ("enabled", module, enabled)
    )
    service.engine.reload = lambda module: calls.append(("reload", module))  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["model_group"]["provider_id"] = "vendor/reasoning-model"

    saved = service.save(public_id, detail["config"], detail["revision"])
    enabled = service.set_enabled(public_id, True)
    reloaded = service.reload(public_id)

    assert saved["saved"] is True
    assert saved["reloaded"] is True
    assert enabled == {
        "enabled": True,
        "applied": True,
        "message": "功能状态已立即更新。",
    }
    assert reloaded["reloaded"] is True
    assert [call[:2] for call in calls] == [
        ("save", "sample_configurable_feature"),
        ("enabled", "sample_configurable_feature"),
        ("reload", "sample_configurable_feature"),
    ]


def test_engine_api_routes_match_installed_runtime_contract(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    client = EngineApiClient(install)
    calls: list[tuple[str, str, object]] = []

    def request(method: str, path: str, body=None):
        calls.append((method, path, body))
        if path.endswith("/pages/settings"):
            return {"content_path": "/api/v1/plugins/example/pages/settings/content/index.html"}
        return {}

    client.request = request  # type: ignore[method-assign]
    client.save_config("example", {"enabled": True})
    client.set_enabled("example", False)
    client.reload("example")
    page = client.page_entry("example", "settings")

    assert calls == [
        ("PUT", "/api/v1/plugins/example/config", {"config": {"enabled": True}}),
        ("PATCH", "/api/v1/plugins/example/enabled", {"enabled": False}),
        ("POST", "/api/v1/plugins/example/reload", None),
        ("GET", "/api/v1/plugins/example/pages/settings", None),
    ]
    assert page.endswith("/content/index.html")


def test_plugin_extension_path_is_scoped_to_selected_feature(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)

    assert service.extension_path(public_id, "page/schedules") == (
        "/api/plug/sample_configurable_feature/page/schedules"
    )
    assert _normalize_plugin_extension_endpoint("profiles/update") == "profiles/update"
    for endpoint in ("", "../config", "page//config", "https://example.invalid"):
        with pytest.raises(PluginCenterError, match="请求无效"):
            service.extension_path(public_id, endpoint)


def test_offline_save_can_replace_and_explicitly_clear_secret(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["model_group"]["api_secret"] = "replacement-secret"
    first = service.save(public_id, detail["config"], detail["revision"])
    path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "sample_configurable_feature_config.json"
    )
    assert json.loads(path.read_text(encoding="utf-8"))["model_group"]["api_secret"] == "replacement-secret"

    refreshed = service.detail(public_id)
    refreshed["config"]["model_group"]["api_secret"] = None
    service.save(public_id, refreshed["config"], first["revision"])
    assert json.loads(path.read_text(encoding="utf-8"))["model_group"]["api_secret"] == ""


def test_offline_save_validates_template_list_items(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    plugin = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "plugins"
        / "sample_configurable_feature"
    )
    schema = json.loads((plugin / "_conf_schema.json").read_text(encoding="utf-8"))
    schema["workers"] = {
        "description": "任务列表",
        "type": "template_list",
        "default": [],
        "templates": {
            "worker": {
                "name": "任务",
                "items": {
                    "retries": {"description": "重试次数", "type": "int", "default": 2}
                },
            }
        },
    }
    (plugin / "_conf_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]
    detail = service.detail(public_id)
    detail["config"]["workers"] = [
        {"__template_key": "worker", "retries": "two"}
    ]

    with pytest.raises(PluginCenterError, match="类型不正确"):
        service.save(public_id, detail["config"], detail["revision"])


def test_template_list_credentials_are_redacted_and_preserved(tmp_path: Path) -> None:
    install, public_id = _install(tmp_path)
    plugin = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "plugins"
        / "sample_configurable_feature"
    )
    schema = json.loads((plugin / "_conf_schema.json").read_text(encoding="utf-8"))
    schema["workers"] = {
        "description": "任务列表",
        "type": "template_list",
        "default": [],
        "templates": {
            "worker": {
                "name": "任务",
                "items": {
                    "name": {"description": "名称", "type": "string", "default": ""},
                    "api_token": {"description": "令牌", "type": "string", "default": ""},
                },
            }
        },
    }
    (plugin / "_conf_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    config_path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "sample_configurable_feature_config.json"
    )
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    stored["workers"] = [
        {"__template_key": "worker", "name": "日报", "api_token": "row-secret"}
    ]
    config_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    service = PluginCenterService(install)
    service.engine.is_available = lambda: False  # type: ignore[method-assign]

    detail = service.detail(public_id)

    row = detail["config"]["workers"][0]
    assert row["api_token"] == ""
    assert row["__manager_index"] == 0
    token_schema = detail["schema"]["workers"]["templates"]["worker"]["items"]["api_token"]
    assert token_schema["secret"] is True
    assert token_schema["_secret_configured"] is True
    service.save(public_id, detail["config"], detail["revision"])
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["workers"][0]["api_token"] == "row-secret"
    assert "__manager_index" not in saved["workers"][0]


def test_legacy_scalar_values_are_normalized_for_schema_forms() -> None:
    normalized = _normalize_config(
        {
            "probability": {"type": "string"},
            "retries": {"type": "int"},
            "enabled": {"type": "bool"},
        },
        {"probability": 0.3, "retries": "2", "enabled": "true"},
    )

    assert normalized == {"probability": "0.3", "retries": 2, "enabled": True}


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
    assert "重新载入设置" in visible
    assert "保存并立即应用" in visible
    assert "热重载" not in visible
    assert "模型绑定" in visible
    assert "template-card" in visible
    assert "astrbot" not in visible.replace("astrbot-plugin-page", "")
    assert "window.alert(" not in visible
    assert "window.confirm(" not in visible
    assert "window.prompt(" not in visible
    assert "astrbot-plugin-page" in javascript
    assert 'message.action === "api:get"' in javascript
    assert 'message.action === "api:post"' in javascript


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


def test_local_web_handler_hosts_plugin_page_bridge_and_preserves_content_type() -> None:
    calls: list[tuple[str, str, bytes | None]] = []

    class FakeEngine:
        def raw_request(self, method, path, *, body=None, **_kwargs):
            calls.append((method, path, body))
            if path.startswith("/api/plugin/page/"):
                return 200, {"content-type": "text/html; charset=utf-8"}, b"<h1>page</h1>"
            return 200, {"content-type": "application/json; charset=utf-8"}, b'{"status":"ok","data":{"saved":true}}'

    class FakeService:
        engine = FakeEngine()

        def extension_path(self, public_id: str, endpoint: str) -> str:
            assert public_id == "feature-0000000000000000"
            assert endpoint == "page/schedules"
            return "/api/plug/example/page/schedules"

    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(FakeService()))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/plugin/page/content/example/dashboard/", timeout=2) as response:
            assert response.headers.get_content_type() == "text/html"
            assert response.read() == b"<h1>page</h1>"

        with urlopen(
            f"{base}/control/plugins/feature-0000000000000000/extension/page/schedules?kind=short",
            timeout=2,
        ) as response:
            assert response.headers.get_content_type() == "application/json"
            assert json.loads(response.read())["data"]["saved"] is True

        request = Request(
            f"{base}/control/plugins/feature-0000000000000000/extension/page/schedules",
            data=b'{"enabled":true}',
            method="POST",
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with urlopen(request, timeout=2) as response:
            assert response.status == 200
        assert calls == [
            ("GET", "/api/plugin/page/content/example/dashboard/", None),
            ("GET", "/api/plug/example/page/schedules?kind=short", None),
            ("POST", "/api/plug/example/page/schedules", b'{"enabled":true}'),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
