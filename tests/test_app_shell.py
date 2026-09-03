from pathlib import Path
from types import SimpleNamespace
import threading

from wechatrebot_manager import app_shell
from wechatrebot_manager.error_catalog import load_error_catalog
from wechatrebot_manager.updates import UpdateChannel, UpdateRelease


class _Events:
    def __init__(self) -> None:
        self.items = []

    def emit(self, *args, **kwargs) -> None:
        self.items.append((args, kwargs))


def _api(tmp_path: Path) -> app_shell.ApplicationApi:
    value = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    value.install_root = tmp_path
    value.catalog = load_error_catalog()
    value._update_lock = threading.Lock()
    value._available_update = None
    value._update_channel = UpdateChannel(
        manifest_url="https://updates.example.test/stable.json",
        channel="stable",
        key_id="test",
        public_key=b"0" * 32,
    )
    value.lifecycle = SimpleNamespace(events=_Events())
    value.window = None
    return value


def test_check_update_returns_customer_safe_release(monkeypatch, tmp_path: Path) -> None:
    release = UpdateRelease(
        version="0.9.0",
        title="体验升级",
        notes=("聊天记录更易查找",),
        published_at="2026-09-03T00:00:00Z",
        package_url="https://updates.example.test/0.9.0.zip",
        package_size=1234,
        package_sha256="A" * 64,
        mandatory=False,
    )
    monkeypatch.setattr(app_shell, "check_for_update", lambda *_args, **_kwargs: release)
    api = _api(tmp_path)

    result = api.check_update()

    assert result["ok"] is True
    assert result["data"]["release"] == {
        "version": "0.9.0",
        "title": "体验升级",
        "notes": ["聊天记录更易查找"],
        "published_at": "2026-09-03T00:00:00Z",
        "package_size": 1234,
        "mandatory": False,
    }
    assert "package_url" not in result["data"]["release"]
    assert "package_sha256" not in result["data"]["release"]


def test_install_update_requires_stopped_robot(monkeypatch, tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.lifecycle.refresh = lambda: SimpleNamespace(
        state=SimpleNamespace(value="RUNNING"), online=True, can_stop=True
    )
    monkeypatch.setattr(app_shell.sys, "frozen", True, raising=False)

    result = api.install_update("0.9.0")

    assert result["ok"] is False
    assert result["error"]["code"] == "14003"
    assert api.lifecycle.events.items[-1][0][:2] == ("APP_ERROR", "failed")
