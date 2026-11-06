"""Damaged appearance preferences leave the manager usable without losing data."""

import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_app_shell import _api
from wechatrebot_manager import app_shell, customer_config, ui_skins


@pytest.fixture(params=[b"{broken", b"[]", b"null", b"\xff\xfe\x80"],
                ids=["invalid-json", "array", "null", "invalid-utf8"])
def damaged_preferences(tmp_path, request):
    path = tmp_path / "data/manager/ui-preferences.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(request.param)
    return path, request.param


def test_optional_appearance_reads_use_defaults_and_preserve_bad_bytes(damaged_preferences, tmp_path):
    path, original = damaged_preferences
    assert customer_config.load_ui_theme(tmp_path) == ""
    assert ui_skins.load_ui_skin(tmp_path) == ""
    assert path.read_bytes() == original
    with pytest.raises(ValueError):
        customer_config._object(path)


def test_saving_damaged_preferences_reports_failure_and_preserves_bad_bytes(damaged_preferences, tmp_path):
    path, original = damaged_preferences
    api = _api(tmp_path)
    for save, choice in ((api.save_theme, "dark"), (api.save_skin, "soft")):
        result = save(choice)
        assert result["ok"] is False
        assert result["error"]["code"] == "15001"
        assert result["error"]["solutions"]
        assert path.read_bytes() == original


def test_unreadable_preferences_use_defaults_but_save_stays_strict(tmp_path, monkeypatch):
    path = tmp_path / "data/manager/ui-preferences.json"
    path.parent.mkdir(parents=True)
    original = b'{"theme":"dark","skin":"soft","other":"keep"}'
    path.write_bytes(original)
    read_text = Path.read_text

    def unreadable(target, *args, **kwargs):
        if target == path:
            raise PermissionError("synthetic read denied")
        return read_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert customer_config.load_ui_theme(tmp_path) == ""
    assert ui_skins.load_ui_skin(tmp_path) == ""
    with pytest.raises(ValueError):
        customer_config._object(path)
    api = _api(tmp_path)
    assert api.save_theme("light")["ok"] is False
    assert api.save_skin("default")["ok"] is False
    assert path.read_bytes() == original


def test_unwritable_preferences_do_not_report_success_or_replace_existing_data(tmp_path, monkeypatch):
    path = tmp_path / "data/manager/ui-preferences.json"
    path.parent.mkdir(parents=True)
    original = b'{"theme":"dark","skin":"soft","other":"keep"}'
    path.write_bytes(original)

    def denied(*_args, **_kwargs):
        raise PermissionError("synthetic write denied")

    monkeypatch.setattr(customer_config, "write_json_atomic", denied)
    api = _api(tmp_path)
    assert api.save_theme("light")["ok"] is False
    assert api.save_skin("default")["ok"] is False
    assert path.read_bytes() == original


def test_broken_trusted_skin_catalog_is_still_rejected(damaged_preferences, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "skins.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        ui_skins.load_ui_skin(tmp_path, asset_root=assets)


@pytest.mark.skipif(os.name != "nt", reason="native manager entry point requires Windows")
def test_damaged_preferences_still_construct_native_manager_window(damaged_preferences, tmp_path, monkeypatch):
    path, original = damaged_preferences
    api = _api(tmp_path)
    api._ensure_feature_page_server = lambda: SimpleNamespace(server_port=4321)
    api._close_feature_page_server = Mock()

    class Hook:
        def __iadd__(self, callback):
            return self

    window = SimpleNamespace(events=SimpleNamespace(before_show=None, shown=Hook()), expose=Mock())
    webview = SimpleNamespace(create_window=Mock(return_value=window), start=Mock())
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr(app_shell, "ApplicationApi", lambda *_args, **_kwargs: api)

    assert app_shell.run_application(tmp_path, development=True) == 0
    webview.create_window.assert_called_once()
    assert webview.create_window.call_args.kwargs["background_color"] == "#F3F6FA"
    assert window.expose.call_count == 1
    webview.start.assert_called_once()
    api._close_feature_page_server.assert_called_once()
    assert path.read_bytes() == original
