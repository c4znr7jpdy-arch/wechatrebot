from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from scripts import dev_manager as dev
from wechatrebot_manager import app_shell


def test_preview_serves_current_files_and_changes_revision(tmp_path: Path):
    page = tmp_path / "index.html"
    page.write_text("<body>first</body>", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), dev.preview_handler(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/?preview") as response:
            html = response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
        assert "first" in html and "location.reload()" in html
        assert "示例数据" in html
        with urlopen(base + "/__dev_revision") as response:
            before = response.read()
        page.write_text("<body>second version</body>", encoding="utf-8")
        with urlopen(base + "/__dev_revision") as response:
            assert response.read() != before
        with urlopen(base + "/?preview") as response:
            assert b"second version" in response.read()
        (tmp_path / "folder").mkdir()
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/folder/")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_preflight_rejects_unfinished_install_without_writing(tmp_path: Path):
    marker = tmp_path / ".wechatrebot-install.json"
    marker.write_text(json.dumps({"state": "installing"}), encoding="utf-8")
    with pytest.raises(ValueError, match="安装尚未完成"):
        dev.resolve_install_root(tmp_path)
    marker.write_text(json.dumps({"state": "ready"}), encoding="utf-8")
    assert dev.resolve_install_root(tmp_path) == tmp_path.resolve()
    assert list(tmp_path.iterdir()) == [marker]


def test_supervisor_restarts_only_its_child_on_source_save(monkeypatch, tmp_path: Path):
    events = []

    class Child:
        def __init__(self, code):
            self.code = code

        def poll(self):
            return self.code

        def terminate(self):
            events.append("terminate")
            self.code = -1

        def wait(self, **_kwargs):
            return self.code

    first, second = Child(None), Child(0)
    children = iter([first, second])

    def launch(command, **kwargs):
        events.append(command)
        assert kwargs["env"]["PYTHONPATH"].split(dev.os.pathsep)[0] == str(dev.SOURCE)
        return next(children)

    snapshots = iter([(), (("app.py", 1, 1),), (("app.py", 2, 2),)])
    monkeypatch.setattr(dev, "snapshot", lambda _root: next(snapshots))
    monkeypatch.setattr(dev.subprocess, "Popen", launch)
    monkeypatch.setattr(dev.time, "sleep", lambda _seconds: None)
    assert dev.supervise(["python", "child.py"], tmp_path) == 0
    assert events == [["python", "child.py"], "terminate", ["python", "child.py"]]


def test_source_window_skips_install_integration_and_tray(monkeypatch, tmp_path: Path):
    calls = {}
    api = SimpleNamespace(_attach_window=lambda window, tray: calls.update(tray=tray))
    for name in app_shell.PUBLIC_API_METHODS:
        setattr(api, name, lambda: None)

    def make_api(root, *, development):
        assert root == tmp_path and development is True
        return api

    def create_window(title, url, **_kwargs):
        calls.update(title=title, url=url)
        return SimpleNamespace(expose=lambda *methods: calls.update(methods=methods))

    def forbidden(*_args, **_kwargs):
        pytest.fail("source verification must not provision installed entries or tray")

    monkeypatch.setattr(app_shell, "ApplicationApi", make_api)
    monkeypatch.setattr(app_shell, "load_robot_settings", lambda _root: SimpleNamespace(profile=SimpleNamespace(display_name="测试")))
    for name in ("ensure_launcher_executable", "provision_uninstaller", "refresh_windows_shortcuts", "WindowsTrayController"):
        monkeypatch.setattr(app_shell, name, forbidden)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=lambda **kwargs: None))
    assert app_shell.run_application(tmp_path, development=True) == 0
    assert "源码验证" in calls["title"]
    assert calls["url"].startswith("file:")
    assert calls["tray"] is None
    assert len(calls["methods"]) == len(app_shell.PUBLIC_API_METHODS)


def test_development_disables_uninstall_before_provision(monkeypatch):
    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    api._development = True
    monkeypatch.setattr(api, "_failed", lambda exc: {"ok": False, "message": str(exc)})
    result = api.open_uninstaller()
    assert not result["ok"]
    assert "源码验证" in result["message"]


def test_existing_manager_lock_is_respected(monkeypatch, tmp_path: Path):
    from wechatrebot_manager import single_instance
    monkeypatch.setattr(single_instance, "SingleInstanceLock", lambda root: SimpleNamespace(acquire=lambda: False))
    assert dev.run_child(tmp_path) == 3
