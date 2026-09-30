from __future__ import annotations

import json
from pathlib import Path

import pytest

from wechatrebot_manager.install_paths import INSTALL_MARKER
from wechatrebot_manager import uninstaller


def _installation(root: Path) -> Path:
    manager = root / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(b"manager-v1")
    (root / INSTALL_MARKER).write_text(
        json.dumps({"schema_version": 1, "state": "ready"}),
        encoding="utf-8",
    )
    (root / "data").mkdir()
    (root / "data" / "messages.db").write_bytes(b"customer data")
    return root


def test_uninstall_root_requires_product_marker(tmp_path: Path) -> None:
    root = tmp_path / "ordinary-folder"
    root.mkdir()

    with pytest.raises(uninstaller.UninstallError, match="安装标记"):
        uninstaller.validate_uninstall_root(root)


def test_uninstall_executable_is_created_and_refreshed(tmp_path: Path) -> None:
    root = _installation(tmp_path / "install")

    assert uninstaller.ensure_uninstall_executable(root)
    target = root / uninstaller.UNINSTALL_EXECUTABLE_NAME
    assert target.read_bytes() == b"manager-v1"

    source = root / "manager" / "WeChatReBot.exe"
    source.write_bytes(b"manager-v2-updated")
    assert uninstaller.ensure_uninstall_executable(root)
    assert target.read_bytes() == b"manager-v2-updated"


def test_native_uninstaller_prevents_duplicate_and_uses_native_arguments(monkeypatch, tmp_path):
    root = _installation(tmp_path / "install")
    native = root / "unins000.exe"
    native.write_bytes(b"inno-uninstaller")
    native.with_suffix(".dat").write_bytes(b"inno-data")
    legacy = root / uninstaller.UNINSTALL_EXECUTABLE_NAME
    legacy.write_bytes(b"legacy")
    assert uninstaller.ensure_uninstall_executable(root)
    assert not legacy.exists()
    assert native.read_bytes() == b"inno-uninstaller"
    assert native.with_suffix(".dat").read_bytes() == b"inno-data"
    calls = []
    monkeypatch.setattr(uninstaller.subprocess, "Popen", lambda args, **kw: calls.append(args))
    uninstaller.launch_installed_uninstaller(root)
    assert calls == [[str(native)]]
    assert uninstaller.ensure_uninstall_executable(root)
    assert not legacy.exists()


def test_incomplete_native_uninstaller_keeps_legacy_recovery(tmp_path):
    root = _installation(tmp_path / "install")
    (root / "unins000.exe").write_bytes(b"incomplete")
    assert uninstaller.ensure_uninstall_executable(root)
    assert (root / uninstaller.UNINSTALL_EXECUTABLE_NAME).is_file()


def test_remove_installation_cleans_product_and_local_residuals(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path / "install")
    local = tmp_path / "local-app-data"
    residual = local / "WeChatReBot"
    residual.mkdir(parents=True)
    (residual / "install-location.json").write_text("{}", encoding="utf-8")
    calls: list[str] = []

    uninstaller.remove_installation(
        root,
        local_app_data=local,
        process_stopper=lambda value: calls.append(f"stop:{Path(value).name}") or 0,
        shortcut_remover=lambda: calls.append("shortcuts") or True,
        registry_remover=lambda: calls.append("registry") or True,
    )

    assert not root.exists()
    assert not residual.exists()
    assert calls == ["stop:install", "shortcuts", "registry"]


def test_remove_installation_preserves_stable_chat_history(tmp_path: Path) -> None:
    root = _installation(tmp_path / "install")
    local = tmp_path / "local-app-data"
    residual = local / "WeChatReBot"
    history = residual / "data" / "chat-history.db"
    history.parent.mkdir(parents=True)
    history.write_bytes(b"chat history")
    (residual / "install-location.json").write_text("{}", encoding="utf-8")
    (residual / "logs").mkdir()
    (residual / "logs" / "startup-error.log").write_text("error", encoding="utf-8")

    uninstaller.remove_installation(
        root,
        local_app_data=local,
        process_stopper=lambda _root: 0,
        shortcut_remover=lambda: True,
        registry_remover=lambda: True,
    )

    assert history.read_bytes() == b"chat history"
    assert not (residual / "logs").exists()


def test_apply_uninstall_plan_removes_everything_without_ui(
    tmp_path: Path,
) -> None:
    root = _installation(tmp_path / "install")
    helper_source = tmp_path / "manager.exe"
    helper_source.write_bytes(b"exe")
    plan = uninstaller.create_uninstall_plan(
        root,
        executable=helper_source,
        parent_pid=0,
        temp_root=tmp_path / "temporary",
    )

    result = uninstaller.apply_uninstall_plan(
        plan.plan_path,
        show_result=False,
        schedule_cleanup=False,
        process_stopper=lambda _root: 0,
        shortcut_remover=lambda: True,
        registry_remover=lambda: True,
        local_app_data=tmp_path / "local-app-data",
    )

    assert result == 0
    assert not root.exists()
    assert plan.helper_executable.is_file()


def test_chinese_uninstaller_filename_is_double_click_entry() -> None:
    assert uninstaller.is_uninstall_entrypoint(r"D:\Robot\卸载机器人.exe")
    assert not uninstaller.is_uninstall_entrypoint(r"D:\Robot\WeChatReBot.exe")


@pytest.mark.parametrize("state", ["failed", "installing", "payload-installed", "ready"])
def test_double_click_uninstaller_accepts_incomplete_install(monkeypatch, tmp_path: Path, state: str) -> None:
    from wechatrebot_manager import cli

    root = _installation(tmp_path / "install")
    (root / INSTALL_MARKER).write_text(
        json.dumps({"schema_version": 1, "state": state}), encoding="utf-8"
    )
    monkeypatch.setattr(cli.sys, "executable", str(root / uninstaller.UNINSTALL_EXECUTABLE_NAME))
    prompts = []
    monkeypatch.setattr(uninstaller, "_message_box", lambda *args: prompts.append(args) or 7)
    assert cli.main([]) == 0
    assert "确认彻底卸载" in prompts[0][1]
    assert root.exists()


def test_double_click_uninstaller_missing_marker_displays_error(monkeypatch, tmp_path: Path) -> None:
    from wechatrebot_manager import cli

    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / uninstaller.UNINSTALL_EXECUTABLE_NAME))
    prompts = []
    monkeypatch.setattr(uninstaller, "_message_box", lambda *args: prompts.append(args) or 0)
    assert cli.main([]) == 1
    assert "无法卸载" in prompts[0][1]


def test_visible_uninstall_reports_real_stages_before_deletion(monkeypatch, tmp_path: Path) -> None:
    from wechatrebot_manager import uninstall_ui

    root = _installation(tmp_path / "install")
    plan = uninstaller.create_uninstall_plan(
        root, executable=root / "manager" / "WeChatReBot.exe",
        parent_pid=0, temp_root=tmp_path / "temporary",
    )
    stages = []
    def show_progress(operation):
        assert root.exists()
        operation(lambda message: stages.append((message, root.exists())))
    monkeypatch.setattr(uninstall_ui, "run_uninstall_progress", show_progress)
    assert uninstaller.apply_uninstall_plan(
        plan.plan_path, schedule_cleanup=False,
        process_stopper=lambda _root: 0,
        shortcut_remover=lambda: True, registry_remover=lambda: True,
        local_app_data=tmp_path / "local",
    ) == 0
    assert len(stages) == 5
    assert "等待" in stages[0][0]
    assert "删除程序" in stages[3][0] and stages[3][1]
    assert "残留" in stages[4][0] and not stages[4][1]


def test_ui_failure_does_not_silently_remove_files(monkeypatch, tmp_path: Path) -> None:
    from wechatrebot_manager import uninstall_ui

    root = _installation(tmp_path / "install")
    plan = uninstaller.create_uninstall_plan(
        root, executable=root / "manager" / "WeChatReBot.exe",
        parent_pid=0, temp_root=tmp_path / "temporary",
    )
    def unavailable(_operation):
        raise RuntimeError("测试窗口创建失败")
    prompts = []
    monkeypatch.setattr(uninstall_ui, "run_uninstall_progress", unavailable)
    monkeypatch.setattr(uninstaller, "_message_box", lambda *args: prompts.append(args) or 0)
    assert uninstaller.apply_uninstall_plan(plan.plan_path, schedule_cleanup=False) == 1
    assert root.exists()
    assert "卸载失败" in prompts[0][1]


def test_uninstall_helper_uses_independent_frozen_environment(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(uninstaller.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    plan = uninstaller.UninstallPlan(tmp_path / "plan.json", tmp_path / "helper.exe")
    uninstaller.launch_uninstall_helper(plan)
    assert calls[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_unrelated_untrusted_process_path_does_not_block_stop(monkeypatch, tmp_path: Path) -> None:
    from wechatrebot_manager import launcher

    root = _installation(tmp_path / "install")
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda: (987654,))
    monkeypatch.setattr(
        launcher, "process_identity",
        lambda _pid: (_ for _ in ()).throw(OSError(448, "untrusted mount point")),
    )

    assert uninstaller.stop_installation_processes(root) == 0
