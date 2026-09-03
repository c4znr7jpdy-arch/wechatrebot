import json

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
    assert json.loads(target.read_text(encoding="utf-8")) == result
