from pathlib import Path
from types import SimpleNamespace
import sys
import threading

from wechatrebot_manager import app_shell
from wechatrebot_manager.error_catalog import load_error_catalog
from wechatrebot_manager.payload import PayloadError
from wechatrebot_manager.updates import UpdateChannel, UpdateRelease


class _Events:
    def __init__(self) -> None:
        self.items = []

    def emit(self, *args, **kwargs) -> None:
        self.items.append((args, kwargs))


def _api(tmp_path: Path) -> app_shell.ApplicationApi:
    value = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    value._install_root = tmp_path
    value._catalog = load_error_catalog()
    value._update_lock = threading.Lock()
    value._update_status_lock = threading.Lock()
    value._update_status = {
        "state": "idle",
        "phase": "idle",
        "completed": 0,
        "total": 0,
        "percent": 0,
        "message": "",
        "error": None,
    }
    value._available_update = None
    value._update_result = None
    value._update_channel = UpdateChannel(
        manifest_url="https://updates.example.test/stable.json",
        channel="stable",
        key_id="test",
        public_key=b"0" * 32,
    )
    value._lifecycle = SimpleNamespace(events=_Events())
    value._window = None
    value._tray = None
    return value


def test_bootstrap_exposes_update_check_interval(monkeypatch, tmp_path: Path) -> None:
    api = _api(tmp_path)
    api._lifecycle.refresh = lambda: SimpleNamespace(to_dict=lambda: {"state": "STOPPED"})
    monkeypatch.setattr(
        app_shell,
        "load_robot_settings",
        lambda _root: SimpleNamespace(
            profile=SimpleNamespace(to_dict=lambda: {"display_name": "测试机器人"}),
            admin_id="wxid_admin",
        ),
    )
    monkeypatch.setattr(app_shell, "load_provider_profile", lambda _root: None)

    result = api.bootstrap()

    assert result["ok"] is True
    assert result["data"]["update"]["enabled"] is True
    assert result["data"]["update"]["check_interval_seconds"] == 21_600


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
    api._lifecycle.refresh = lambda: SimpleNamespace(
        state=SimpleNamespace(value="RUNNING"), online=True, can_stop=True
    )
    monkeypatch.setattr(app_shell.sys, "frozen", True, raising=False)

    result = api.install_update("0.9.0")

    assert result["ok"] is False
    assert result["error"]["code"] == "14003"
    assert api._lifecycle.events.items[-1][0][:2] == ("APP_ERROR", "failed")


def test_install_update_runs_in_background_and_reports_progress(
    monkeypatch, tmp_path: Path
) -> None:
    release = UpdateRelease(
        version="0.9.0",
        title="体验升级",
        notes=(),
        published_at="2026-09-03T00:00:00Z",
        package_url="https://updates.example.test/0.9.0.zip",
        package_size=100,
        package_sha256="A" * 64,
        mandatory=False,
    )
    api = _api(tmp_path)
    api._available_update = release
    api._lifecycle.refresh = lambda: SimpleNamespace(
        state=SimpleNamespace(value="STOPPED"), online=False, can_stop=False
    )
    monkeypatch.setattr(app_shell.sys, "frozen", True, raising=False)
    launched = threading.Event()
    prepared = SimpleNamespace()

    def fake_prepare(*_args, progress_callback, **_kwargs):
        progress_callback("download", 50, 100)
        progress_callback("verify", 4, 4)
        return prepared

    monkeypatch.setattr(app_shell, "prepare_update", fake_prepare)
    monkeypatch.setattr(app_shell, "create_update_plan", lambda *_args: "plan")
    monkeypatch.setattr(app_shell, "launch_update_helper", lambda _plan: launched.set())

    result = api.install_update("0.9.0")

    assert result["ok"] is True
    assert result["data"]["accepted"] is True
    assert launched.wait(2)
    assert api._update_lock.acquire(timeout=2)
    api._update_lock.release()
    status = api.get_update_status()
    assert status["ok"] is True
    assert status["data"]["state"] == "restarting"
    assert status["data"]["percent"] == 100


def test_install_update_surfaces_background_verification_failure(
    monkeypatch, tmp_path: Path
) -> None:
    release = UpdateRelease(
        version="0.9.0",
        title="体验升级",
        notes=(),
        published_at="2026-09-03T00:00:00Z",
        package_url="https://updates.example.test/0.9.0.zip",
        package_size=100,
        package_sha256="A" * 64,
        mandatory=False,
    )
    api = _api(tmp_path)
    api._available_update = release
    api._lifecycle.refresh = lambda: SimpleNamespace(
        state=SimpleNamespace(value="STOPPED"), online=False, can_stop=False
    )
    monkeypatch.setattr(app_shell.sys, "frozen", True, raising=False)
    failed = threading.Event()

    def fake_prepare(*_args, **_kwargs):
        try:
            raise PayloadError("载荷校验失败")
        finally:
            failed.set()

    monkeypatch.setattr(app_shell, "prepare_update", fake_prepare)

    result = api.install_update("0.9.0")

    assert result["ok"] is True
    assert failed.wait(2)
    assert api._update_lock.acquire(timeout=2)
    api._update_lock.release()
    status = api.get_update_status()["data"]
    assert status["state"] == "failed"
    assert status["error"]["code"] == "14002"


def test_public_api_allowlist_matches_frontend_calls() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")
    called = {
        match.split('"', 2)[1]
        for match in __import__("re").findall(r'api\("[a-z_]+"', script)
    }

    assert called | {"start_robot", "stop_robot"} == set(
        app_shell.PUBLIC_API_METHODS
    )
    assert "_attach_window" not in app_shell.PUBLIC_API_METHODS


def test_start_action_shows_pending_state_before_bridge_returns() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")
    handler = script[
        script.index("async function runPrimaryAction") : script.index(
            "async function checkEnvironment"
        )
    ]

    assert handler.index("state.startPending = true") < handler.index("await api(")
    assert 'current === "STOPPED" && status?.environment?.ready' in script
    assert '"启动中…"' in script
    assert 'role="status" aria-live="polite" aria-atomic="true"' in markup
    assert '.status-orbit[data-state="starting"] .power-button' in styles


def test_webui_schedules_startup_and_recurring_update_checks() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")

    assert "function automaticUpdateIntervalMs()" in script
    assert "state.bootstrap?.update?.check_interval_seconds" in script
    assert "scheduleAutomaticUpdateCheck(automaticUpdateIntervalMs())" in script
    assert "scheduleAutomaticUpdateCheck(1800)" in script
    assert "if (state.updateChecking) return" in script


def test_webui_keeps_update_progress_and_errors_visible() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")

    assert 'api("get_update_status")' in script
    assert 'pendingText: "正在下载…"' in script
    assert 'state.status?.state !== "STOPPED"' not in script
    assert 'id="modalProgress"' in markup
    assert 'id="modalError" role="alert"' in markup
    assert 'update.result.ok === true || update.result.status === "success"' in script
    assert "failureDetail" in script


def test_087_preview_lists_group_analysis_without_checkin_task() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")

    assert 'display_name: "群聊分析"' in script
    assert 'name: "每日签到"' not in script
    assert 'version: "0.8.7"' in script


def test_run_application_uses_explicit_webview_api_allowlist(
    monkeypatch, tmp_path: Path
) -> None:
    exposed: list[str] = []
    create_options: dict[str, object] = {}
    handlers: dict[str, list] = {"shown": [], "closing": [], "closed": []}

    class FakeHook:
        def __init__(self, name: str) -> None:
            self.name = name

        def __iadd__(self, callback):
            handlers[self.name].append(callback)
            return self

    class FakeWindow:
        def __init__(self) -> None:
            self.events = SimpleNamespace(
                shown=FakeHook("shown"),
                closing=FakeHook("closing"),
                closed=FakeHook("closed"),
            )

        def expose(self, *functions) -> None:
            exposed.extend(item.__name__ for item in functions)

    class FakeTray:
        def __init__(self, window, title: str) -> None:
            self.window = window
            self.title = title

        def start(self) -> None:
            return None

        def handle_window_closing(self):
            return False

        def stop(self) -> None:
            return None

    class FakeWebview:
        @staticmethod
        def create_window(*_args, **kwargs):
            create_options.update(kwargs)
            return FakeWindow()

        @staticmethod
        def start(**_kwargs) -> None:
            return None

    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    monkeypatch.setattr(app_shell, "ApplicationApi", lambda _root: api)
    monkeypatch.setattr(
        app_shell,
        "load_robot_settings",
        lambda _root: SimpleNamespace(
            profile=SimpleNamespace(display_name="测试机器人")
        ),
    )
    monkeypatch.setitem(sys.modules, "webview", FakeWebview)
    monkeypatch.setattr(app_shell, "WindowsTrayController", FakeTray)

    assert app_shell.run_application(tmp_path) == 0
    assert "js_api" not in create_options
    assert exposed == list(app_shell.PUBLIC_API_METHODS)
    assert api._window is not None
    assert isinstance(api._tray, FakeTray)
    assert api._tray.title == "测试机器人"
    assert handlers == {
        "shown": [api._tray.start],
        "closing": [api._tray.handle_window_closing],
        "closed": [api._tray.stop],
    }


def test_update_close_bypasses_minimize_to_tray(tmp_path: Path) -> None:
    api = _api(tmp_path)
    calls: list[str] = []
    api._tray = SimpleNamespace(
        permit_programmatic_close=lambda: calls.append("permit")
    )
    api._window = SimpleNamespace(destroy=lambda: calls.append("destroy"))

    api._close_window_for_update()

    assert calls == ["permit", "destroy"]
