import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from wechatrebot_manager import ui_skins
from wechatrebot_manager.customer_config import save_ui_theme
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.plugin_center_web import _handler
from wechatrebot_manager.self_test import _app_assets_ready


def _assets(tmp_path: Path) -> tuple[Path, list[dict[str, str]]]:
    assets = tmp_path / "app_webui"
    assets.mkdir()
    (assets / "index.html").write_text(
        '<html lang="zh-CN"><!-- manager-skins --><script src="soft-theme.js"></script></html>',
        encoding="utf-8",
    )
    (assets / "soft-theme.js").write_text("window.skinReady = true;", encoding="utf-8")
    catalog = ui_skins.load_skin_catalog()
    for item in catalog:
        if item["stylesheet"]:
            (assets / item["stylesheet"]).write_text("html[data-skin=soft]{}", encoding="utf-8")
    write_json_atomic(assets / "skins.json", catalog)
    return assets, catalog


@pytest.mark.parametrize("saved", [None, "", "unknown", [], {}])
def test_unknown_saved_skin_has_no_effect(tmp_path, saved):
    write_json_atomic(tmp_path / "data/manager/ui-preferences.json", {"skin": saved})
    assert ui_skins.load_ui_skin(tmp_path) == ""


@pytest.mark.parametrize("stylesheet", [
    "../outside.css", "/outside.css", "https://example.test/theme.css", "C:/theme.css",
    "skins\\theme.css", "skins/../theme.css", "theme.css?x=1", "theme.css#fragment",
    "theme%2ecss", "theme.js", "theme.css\" onload=\"alert(1)", "skins//theme.css", "",
])
def test_catalog_rejects_nonlocal_or_nonstylesheet_paths(tmp_path, stylesheet):
    assets, catalog = _assets(tmp_path)
    catalog[1]["stylesheet"] = stylesheet
    write_json_atomic(assets / "skins.json", catalog)
    with pytest.raises(ValueError, match="样式路径"):
        ui_skins.load_skin_catalog(assets)


@pytest.mark.parametrize("invalid", ["duplicate-id", "duplicate-css", "missing-default", "default-css", "invalid-id", "invalid-name", "not-list"])
def test_catalog_rejects_invalid_definitions(tmp_path, invalid):
    assets, catalog = _assets(tmp_path)
    if invalid == "duplicate-id":
        catalog.append(copy.deepcopy(catalog[1]))
    elif invalid == "duplicate-css":
        catalog.append({**catalog[1], "id": "another"})
    elif invalid == "missing-default":
        catalog.pop(0)
    elif invalid == "default-css":
        catalog[0]["stylesheet"] = "default.css"
    elif invalid == "invalid-id":
        catalog[1]["id"] = 'bad" onload="alert(1)'
    elif invalid == "invalid-name":
        catalog[1]["name"] = None
    else:
        catalog = {}
    write_json_atomic(assets / "skins.json", catalog)
    with pytest.raises(ValueError):
        ui_skins.load_skin_catalog(assets)


def test_skin_catalog_uses_frozen_bundle_assets(monkeypatch, tmp_path):
    assets, catalog = _assets(tmp_path)
    bundle_assets = tmp_path / "wechatrebot_manager" / "app_webui"
    bundle_assets.parent.mkdir()
    assets.rename(bundle_assets)
    catalog[1]["name"] = "打包皮肤"
    write_json_atomic(bundle_assets / "skins.json", catalog)
    monkeypatch.setattr(ui_skins.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert ui_skins.load_skin_catalog()[1]["name"] == "打包皮肤"


def test_new_catalog_skin_is_persisted_and_served_across_ports(tmp_path):
    assets, catalog = _assets(tmp_path)
    description = '</script><script>alert("unsafe")</script>&'
    catalog.append({"id": "forest", "name": "森林", "description": description, "stylesheet": "skins/forest.css"})
    (assets / "skins").mkdir()
    (assets / "skins/forest.css").write_text("html[data-skin=forest]{}", encoding="utf-8")
    (assets / "unregistered.css").write_text("private{}", encoding="utf-8")
    write_json_atomic(assets / "skins.json", catalog)
    save_ui_theme(tmp_path, "dark")
    ui_skins.save_ui_skin(tmp_path, "forest", asset_root=assets)
    assert ui_skins.load_ui_skin(tmp_path, asset_root=assets) == "forest"
    servers = [
        ThreadingHTTPServer(("127.0.0.1", 0), _handler(SimpleNamespace(install_root=tmp_path), manager_assets=assets))
        for _ in range(2)
    ]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        assert servers[0].server_port != servers[1].server_port
        for server in servers:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base, timeout=2) as response:
                html = response.read().decode("utf-8")
            assert 'data-saved-skin="forest"' in html
            assert 'data-saved-theme="dark" data-theme="dark"' in html
            assert 'data-manager-skin="forest" href="skins/forest.css"' in html
            assert html.index('id="managerSkinCatalog"') < html.index('src="soft-theme.js"')
            assert description not in html
            encoded = re.search(r'<script type="application/json" id="managerSkinCatalog">(.*?)</script>', html).group(1)
            assert json.loads(encoded) == catalog
            with urlopen(f"{base}/skins/forest.css", timeout=2) as response:
                assert response.read() == b"html[data-skin=forest]{}"
                assert response.headers.get_content_type() == "text/css"
            with pytest.raises(HTTPError) as rejected:
                urlopen(f"{base}/unregistered.css", timeout=2)
            assert rejected.value.code == 404
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@pytest.mark.parametrize("missing", ["skins.json", "soft-theme.css"])
def test_asset_self_test_requires_catalog_and_registered_stylesheets(tmp_path, missing):
    assets, _ = _assets(tmp_path)
    assert _app_assets_ready(assets) is True
    (assets / missing).unlink()
    assert _app_assets_ready(assets) is False
