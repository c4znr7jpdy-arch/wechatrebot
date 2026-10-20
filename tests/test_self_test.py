import json
import shutil

import pytest

from wechatrebot_manager import self_test
from wechatrebot_manager.self_test import manager_self_test


def test_manager_self_test_includes_unified_ui_assets(monkeypatch, tmp_path) -> None:
    target = tmp_path / "self-test.json"
    monkeypatch.setattr(
        "wechatrebot_manager.self_test.importlib.util.find_spec",
        lambda _name: object(),
    )

    result = manager_self_test(target)

    assert result["ok"] is True
    assert result["app_assets"] is True
    assert result["webview2_backend"] is True
    assert result["error_codes"] is True
    assert result["timezone_data"] is True
    assert json.loads(target.read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("missing", ["scheduled-tasks.js", "features.js", "preview.js", "model-services.js"])
def test_manager_self_test_rejects_missing_page_module(monkeypatch, tmp_path, missing):
    source = self_test._asset_root()
    assets = tmp_path / "webui"
    assets.mkdir()
    for path in source.iterdir():
        if path.is_file() and path.suffix in {".html", ".js", ".css", ".json"}:
            shutil.copy2(path, assets / path.name)
    monkeypatch.setattr(self_test, "_asset_root", lambda: assets)
    monkeypatch.setattr(self_test.importlib.util, "find_spec", lambda _name: object())
    assert manager_self_test(tmp_path / "complete.json")["ok"] is True
    (assets / missing).unlink()
    result = manager_self_test(tmp_path / "missing.json")
    assert result["ok"] is False and result["app_assets"] is False


@pytest.mark.parametrize('failure', ['package', 'resource', 'lookup'])
def test_manager_self_test_rejects_missing_timezone_data(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(self_test.importlib.util, 'find_spec', lambda _name: object())
    if failure == 'package':
        def missing(_package):
            raise ModuleNotFoundError('tzdata')
        monkeypatch.setattr(self_test.importlib.resources, 'files', missing)
    elif failure == 'resource':
        monkeypatch.setattr(self_test.importlib.resources, 'files', lambda _package: tmp_path)
    else:
        class MissingZone:
            @staticmethod
            def no_cache(_name):
                raise self_test.ZoneInfoNotFoundError('Asia/Shanghai')
        monkeypatch.setattr(self_test, 'ZoneInfo', MissingZone)
    result = manager_self_test(tmp_path / 'self-test.json')
    assert result['ok'] is False and result['timezone_data'] is False
    assert result['app_assets'] is True and result['error_codes'] is True
