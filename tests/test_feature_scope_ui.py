from __future__ import annotations
from webui_source import manager_script

from pathlib import Path

from wechatrebot_manager import app_shell


def test_global_feature_switches_are_not_exposed() -> None:
    root = Path(app_shell.__file__).resolve().parent
    app = manager_script()
    core = (root / "app_webui" / "core-settings.js").read_text(encoding="utf-8")
    plugin_html = (root / "plugin_webui" / "index.html").read_text(encoding="utf-8")
    plugin_app = (root / "plugin_webui" / "app.js").read_text(encoding="utf-8")

    assert "set_feature_enabled" not in app
    assert "set_feature_enabled" not in app_shell.PUBLIC_API_METHODS
    assert 'id="featureEnabled"' not in app
    assert 'id="coreEnabled"' not in core
    assert 'id="toggle-plugin"' not in plugin_html
    assert "togglePlugin" not in plugin_app


def test_switch_text_and_rows_do_not_toggle_hidden_checkbox() -> None:
    root = Path(app_shell.__file__).resolve().parent
    html = (root / "app_webui" / "index.html").read_text(encoding="utf-8")
    app = manager_script()
    css = (root / "app_webui" / "app.css").read_text(encoding="utf-8")

    assert '<label class="toggle-line"' not in html + app
    assert 'type === "bool"' in app
    assert '<span class="field-label">' in app
    assert '<label class="toggle-track" for="${fieldId}"' in app
    assert ".toggle-track" in css and "cursor: pointer" in css


def test_dedicated_config_iframe_fills_remaining_detail_height() -> None:
    plugin_webui = Path(app_shell.__file__).with_name("plugin_webui")
    styles = (plugin_webui / "app.css").read_text(encoding="utf-8")

    assert "#detail-view:not([hidden]) { min-height: 100%; display: flex; flex-direction: column; }" in styles
    assert "#pages-panel:not([hidden]) { min-height: 0; display: flex; flex: 1 1 auto; flex-direction: column; }" in styles
    assert "#plugin-frame { width: 100%; height: 100%; min-height: 180px; flex: 1 1 auto;" in styles
    assert ".detail-pane.showing-page { overflow: hidden; }" in styles
    assert ".detail-pane.showing-page #detail-view { height: 100%; min-height: 0; }" in styles
