from __future__ import annotations

from pathlib import Path

from wechatrebot_manager import app_shell


def test_global_feature_switches_are_not_exposed() -> None:
    root = Path(app_shell.__file__).resolve().parent
    app = (root / "app_webui" / "app.js").read_text(encoding="utf-8")
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
    app = (root / "app_webui" / "app.js").read_text(encoding="utf-8")
    css = (root / "app_webui" / "app.css").read_text(encoding="utf-8")

    assert '<label class="toggle-line"' not in html + app
    assert 'type === "bool"' in app
    assert '<span class="field-label">' in app
    assert '<label class="toggle-track" for="${fieldId}"' in app
    assert ".toggle-track" in css and "cursor: pointer" in css
