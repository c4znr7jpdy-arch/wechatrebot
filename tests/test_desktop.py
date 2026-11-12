import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

from wechatrebot_manager import desktop as desktop_module
from wechatrebot_manager.desktop import (
    find_bundled_payload,
    install_root_from_executable,
)
from wechatrebot_manager.single_instance import SingleInstanceLock


def test_find_bundled_payload_next_to_executable(tmp_path: Path) -> None:
    executable = tmp_path / "WeChatReBot-Setup.exe"
    executable.write_bytes(b"exe")
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "release-manifest.json").write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )
    assert find_bundled_payload(executable=executable) == payload.resolve()


def test_find_bundled_archive_next_to_executable(tmp_path: Path) -> None:
    executable = tmp_path / "WeChatReBot.exe"
    executable.write_bytes(b"exe")
    payload = tmp_path / "payload.wrb"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps({"schema_version": 1}))

    assert find_bundled_payload(executable=executable) == payload.resolve()


def test_installed_manager_discovers_its_install_root(tmp_path: Path) -> None:
    manager = tmp_path / "install" / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(b"exe")
    (tmp_path / "install" / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    assert install_root_from_executable(manager) == (tmp_path / "install").resolve()


@pytest.mark.parametrize("relaunch", ["absent", "other_install"])
def test_manual_start_during_update_does_not_claim_manager_lock(
    tmp_path: Path, monkeypatch, relaunch: str,
) -> None:
    install = tmp_path.resolve()
    messages = []
    monkeypatch.setattr(
        desktop_module, "_show_startup_notice",
        lambda message, **kwargs: messages.append((message, kwargs)),
    )
    monkeypatch.setattr(
        desktop_module, "run_application",
        lambda _root: pytest.fail("an updating installation must not open"),
    )
    monkeypatch.delenv("WECHATREBOT_UPDATE_RELAUNCH_ROOT", raising=False)
    monkeypatch.setenv("WECHATREBOT_UPDATE_HELPER", "1")
    if relaunch == "other_install":
        monkeypatch.setenv("WECHATREBOT_UPDATE_RELAUNCH_ROOT", str(tmp_path / "other"))

    with SingleInstanceLock(f"update-helper:{install}"):
        assert desktop_module.run_desktop(install_root=install) == 4
        # The updater can still acquire the manager's own instance lock when it
        # launches the installed build, even while the notification is shown.
        with SingleInstanceLock(install):
            pass

    assert messages == [(
        "更新正在安装，完成后会自动打开管理器，请稍候。", {"close_after_ms": 3000},
    )]
    assert "WECHATREBOT_UPDATE_RELAUNCH_ROOT" not in os.environ


def test_updater_relaunch_consumes_matching_installation_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    install = tmp_path.resolve()
    opened = []
    monkeypatch.setenv("WECHATREBOT_UPDATE_RELAUNCH_ROOT", str(install))
    monkeypatch.setattr(
        desktop_module, "_show_startup_notice",
        lambda _message, **_kwargs: pytest.fail("the updater's relaunch must be allowed"),
    )

    def open_application(root):
        assert "WECHATREBOT_UPDATE_RELAUNCH_ROOT" not in os.environ
        opened.append(root)
        return 7

    monkeypatch.setattr(desktop_module, "run_application", open_application)
    with SingleInstanceLock(f"update-helper:{install}"):
        assert desktop_module.run_desktop(install_root=install) == 7
        # The marker is one-shot: a second start in this environment must wait.
        monkeypatch.setattr(desktop_module, "_show_startup_notice", lambda _message, **_kwargs: None)
        assert desktop_module.run_desktop(install_root=install) == 4

    assert opened == [install]


def test_stale_journal_without_live_helper_allows_normal_recovery(
    tmp_path: Path, monkeypatch,
) -> None:
    install = tmp_path.resolve()
    (install / "updates").mkdir()
    (install / "updates/update-journal.json").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("WECHATREBOT_UPDATE_RELAUNCH_ROOT", raising=False)

    def open_application(root):
        assert root == install
        # Probing for an updater must release its lock before normal recovery.
        with SingleInstanceLock(f"update-helper:{install}"):
            pass
        concurrent = SingleInstanceLock(install)
        assert concurrent.acquire() is False
        return 0

    monkeypatch.setattr(desktop_module, "run_application", open_application)
    assert desktop_module.run_desktop(install_root=install) == 0
    with SingleInstanceLock(install):
        pass


def test_running_manager_retains_existing_single_instance_notice(
    tmp_path: Path, monkeypatch,
) -> None:
    install = tmp_path.resolve()
    messages = []
    monkeypatch.delenv("WECHATREBOT_UPDATE_RELAUNCH_ROOT", raising=False)
    monkeypatch.setattr(desktop_module, "_show_startup_notice", messages.append)
    monkeypatch.setattr(
        desktop_module, "run_application",
        lambda _root: pytest.fail("an existing instance must keep ownership"),
    )
    with SingleInstanceLock(install):
        assert desktop_module.run_desktop(install_root=install) == 3
    assert messages == ["管理器已经在运行。"]


def test_update_notice_closes_automatically_without_modal_dialog(monkeypatch) -> None:
    events = []

    class Root:
        def withdraw(self):
            pass

        def title(self, value):
            assert value == "微信机器人"

        def resizable(self, *_args):
            pass

        def protocol(self, _name, callback):
            assert callback == self.quit

        def quit(self):
            events.append("quit")

        def after(self, milliseconds, callback):
            events.append(("after", milliseconds))
            self.callback = callback

        def deiconify(self):
            events.append("shown")

        def mainloop(self):
            events.append("mainloop")
            self.callback()

        def destroy(self):
            events.append("destroyed")

    widget = SimpleNamespace(pack=lambda **_kwargs: None)
    ttk = SimpleNamespace(**{
        name: lambda *_args, **_kwargs: widget for name in ("Frame", "Label", "Button")
    })
    tk = SimpleNamespace(
        Tk=Root, ttk=ttk,
        messagebox=SimpleNamespace(showinfo=lambda *_args, **_kwargs:
                                   pytest.fail("the update notice must not remain modal")),
    )
    monkeypatch.setattr(desktop_module.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    desktop_module._show_startup_notice("更新正在安装。", close_after_ms=3000)
    assert events == [("after", 3000), "shown", "mainloop", "quit", "destroyed"]
