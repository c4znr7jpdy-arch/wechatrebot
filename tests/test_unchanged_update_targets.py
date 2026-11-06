"""Unchanged mapped binaries need neither installation nor rollback replacement."""

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from wechatrebot_manager import payload, updates
from wechatrebot_manager.update_errors import UpdateFailure


HOOK = "app/bridge/hook.dll"


def write(root: Path, relative: str, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_payload(root: Path, files: dict[str, bytes]) -> None:
    entries = []
    for relative, content in files.items():
        write(root, relative, content)
        entries.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest().upper()})
    write(root, "release-manifest.json", json.dumps({
        "schema_version": 1, "release_version": "1.0.7", "release_build": 5,
        "files": entries,
    }).encode())


def rollback_journal(install: Path, files: dict[str, bytes]) -> tuple[dict, Path]:
    rollback = install / "updates/.rollback-test"
    entries = []
    for index, (relative, content) in enumerate(files.items()):
        backup = f"files/{index:08X}"
        write(rollback, backup, content)
        entries.append({"path": relative, "existed": True, "backup": backup})
    return {"rollback_root": rollback.relative_to(install).as_posix(), "files": entries}, rollback


@pytest.mark.parametrize("packed", [False, True], ids=["directory", "archive"])
def test_install_skips_locked_identical_binary_but_updates_changed_files(tmp_path, monkeypatch, packed):
    incoming = tmp_path / "payload"
    contents = {HOOK: b"same hook", "manager/WeChatReBot.exe": b"new manager"}
    make_payload(incoming, contents)
    source = incoming
    if packed:
        source = tmp_path / "payload.wrb"
        with zipfile.ZipFile(source, "w") as archive:
            for path in incoming.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(incoming).as_posix())
    install = tmp_path / "install"
    hook = write(install, HOOK, contents[HOOK])
    manager = write(install, "manager/WeChatReBot.exe", b"old manager")
    original_replace = payload._replace_file
    replaced = []

    def replace(source, target):
        replaced.append(target)
        if target == hook:
            raise PermissionError("synthetic mapped DLL lock")
        original_replace(source, target)

    monkeypatch.setattr(payload, "_replace_file", replace)
    progress = []
    result = payload.install_payload(source, install, progress_callback=lambda *event: progress.append(event))

    assert replaced == [manager]
    assert hook.read_bytes() == b"same hook"
    assert manager.read_bytes() == b"new manager"
    assert progress == [("prepare", 1, 2), ("prepare", 2, 2), ("install", 1, 2), ("install", 2, 2)]
    installed = json.loads((install / "config/installed-payload.json").read_text())
    assert installed["release_version"] == "1.0.7" and installed["release_build"] == 5
    expected = {name: hashlib.sha256(content).hexdigest().upper() for name, content in contents.items()}
    assert {entry["path"]: entry["sha256"] for entry in installed["files"]} == expected
    assert {item.relative_path.as_posix(): item.sha256 for item in result.files} == expected
    assert not list((install / "updates").glob(".rollback-*"))


def test_matching_installed_file_does_not_bypass_incoming_payload_verification(tmp_path, monkeypatch):
    incoming = tmp_path / "payload"
    make_payload(incoming, {HOOK: b"expected hook"})
    write(incoming, HOOK, b"tampered incoming hook")
    install = tmp_path / "install"
    hook = write(install, HOOK, b"expected hook")

    def forbidden_replace(*_args):
        raise AssertionError("unverified payload must not replace installed files")

    monkeypatch.setattr(payload, "_replace_file", forbidden_replace)
    with pytest.raises(payload.PayloadError, match="校验失败"):
        payload.install_payload(incoming, install)
    assert hook.read_bytes() == b"expected hook"
    assert not (install / "config/installed-payload.json").exists()


@pytest.mark.parametrize("rollback_locked", [False, True])
def test_changed_files_still_replace_and_roll_back_or_preserve_recovery_material(tmp_path, monkeypatch, rollback_locked):
    incoming = tmp_path / "payload"
    originals = {HOOK: b"same hook", "app/feature.py": b"old feature", "manager/WeChatReBot.exe": b"old manager"}
    make_payload(incoming, {HOOK: b"same hook", "app/feature.py": b"new feature", "manager/WeChatReBot.exe": b"new manager"})
    install = tmp_path / "install"
    for relative, content in originals.items():
        write(install, relative, content)
    original_manifest = b'{"schema_version":1,"release_version":"1.0.6","files":[]}'
    write(install, "config/installed-payload.json", original_manifest)
    original_replace = payload._replace_file
    replaced = []

    def replace(source, target):
        replaced.append(target.relative_to(install).as_posix())
        if target == install / HOOK:
            raise AssertionError("identical hook must not be replaced")
        if target == install / "manager/WeChatReBot.exe":
            raise PermissionError("synthetic changed manager lock")
        if rollback_locked and ".rollback-" in str(source):
            raise PermissionError("synthetic feature rollback lock")
        original_replace(source, target)

    monkeypatch.setattr(payload, "_replace_file", replace)
    if rollback_locked:
        with pytest.raises(payload.PayloadError, match="回滚失败"):
            payload.install_payload(incoming, install)
        recoveries = list((install / "updates").glob(".rollback-*"))
        assert len(recoveries) == 1
        assert any(path.read_bytes() == b"old feature" for path in recoveries[0].iterdir())
        assert (install / "app/feature.py").read_bytes() == b"new feature"
    else:
        with pytest.raises(PermissionError, match="changed manager lock"):
            payload.install_payload(incoming, install)
        assert (install / "app/feature.py").read_bytes() == b"old feature"
        assert not list((install / "updates").glob(".rollback-*"))
    assert replaced == ["app/feature.py", "manager/WeChatReBot.exe", "app/feature.py"]
    assert (install / HOOK).read_bytes() == b"same hook"
    assert (install / "manager/WeChatReBot.exe").read_bytes() == b"old manager"
    assert (install / "config/installed-payload.json").read_bytes() == original_manifest


@pytest.mark.parametrize("parent_link", [False, True])
def test_matching_install_target_cannot_bypass_link_validation(tmp_path, monkeypatch, parent_link):
    incoming = tmp_path / "payload"
    make_payload(incoming, {HOOK: b"same hook"})
    install = tmp_path / "install"
    real = write(install, "real/hook.dll", b"same hook")
    try:
        if parent_link:
            (install / "app").mkdir()
            (install / "app/bridge").symlink_to(real.parent, target_is_directory=True)
        else:
            (install / HOOK).parent.mkdir(parents=True)
            (install / HOOK).symlink_to(real)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")

    def forbidden_hash(_path):
        raise AssertionError("target links must be rejected before content comparison")

    monkeypatch.setattr(payload, "sha256_file", forbidden_hash)
    with pytest.raises(payload.PayloadError, match="符号链接|目录联接"):
        payload.install_payload(incoming, install)
    assert real.read_bytes() == b"same hook"


def test_rollback_skips_locked_identical_binary_and_restores_changed_file(tmp_path, monkeypatch):
    install = tmp_path / "install"
    hook = write(install, HOOK, b"same hook")
    changed = write(install, "app/feature.py", b"new feature")
    journal, rollback = rollback_journal(install, {HOOK: b"same hook", "app/feature.py": b"old feature"})
    original_replace = updates.os.replace
    replaced = []

    def replace(source, target):
        replaced.append(target)
        if target == hook:
            raise PermissionError("synthetic mapped DLL lock")
        original_replace(source, target)

    monkeypatch.setattr(updates.os, "replace", replace)
    updates._restore_update_targets(journal, install)
    assert replaced == [changed]
    assert changed.read_bytes() == b"old feature"
    assert hook.read_bytes() == b"same hook"
    assert (rollback / "files/00000000").read_bytes() == b"same hook"
    assert not list(hook.parent.glob(".*.restore-*"))


def test_changed_rollback_target_failure_keeps_authoritative_backup(tmp_path, monkeypatch):
    install = tmp_path / "install"
    hook = write(install, HOOK, b"new hook")
    journal, rollback = rollback_journal(install, {HOOK: b"old hook"})
    attempted = []

    def locked_replace(source, target):
        attempted.append(target)
        raise PermissionError("synthetic changed hook lock")

    monkeypatch.setattr(updates.os, "replace", locked_replace)
    with pytest.raises(UpdateFailure) as error:
        updates._restore_update_targets(journal, install)
    assert error.value.definition.code == "UPD-1010"
    assert attempted == [hook]
    assert hook.read_bytes() == b"new hook"
    assert (rollback / "files/00000000").read_bytes() == b"old hook"


def test_matching_rollback_target_still_requires_backup(tmp_path):
    install = tmp_path / "install"
    hook = write(install, HOOK, b"same hook")
    journal, rollback = rollback_journal(install, {HOOK: b"same hook"})
    (rollback / "files/00000000").unlink()
    with pytest.raises(UpdateFailure) as error:
        updates._restore_update_targets(journal, install)
    assert error.value.definition.code == "UPD-1010"
    assert "备份缺失" in error.value.detail
    assert hook.read_bytes() == b"same hook"


@pytest.mark.parametrize("linked", ["target", "target-parent", "backup", "backup-parent", "rollback-root"])
def test_matching_rollback_content_cannot_bypass_link_validation(tmp_path, monkeypatch, linked):
    install = tmp_path / "install"
    hook = write(install, HOOK, b"same hook")
    journal, rollback = rollback_journal(install, {HOOK: b"same hook"})
    actual = write(install, "real/hook.dll", b"same hook")
    try:
        if linked == "target":
            hook.unlink()
            hook.symlink_to(actual)
        elif linked == "target-parent":
            hook.unlink()
            hook.parent.rmdir()
            hook.parent.symlink_to(actual.parent, target_is_directory=True)
        elif linked == "backup":
            backup = rollback / "files/00000000"
            backup.unlink()
            backup.symlink_to(actual)
        elif linked == "backup-parent":
            (rollback / "files").rename(rollback / "actual-files")
            (rollback / "files").symlink_to(rollback / "actual-files", target_is_directory=True)
        else:
            real_rollback = rollback.with_name("actual-rollback")
            rollback.rename(real_rollback)
            rollback.symlink_to(real_rollback, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")

    def forbidden_hash(_path):
        raise AssertionError("rollback links must be rejected before content comparison")

    monkeypatch.setattr(updates, "sha256_file", forbidden_hash)
    with pytest.raises(UpdateFailure) as error:
        updates._restore_update_targets(journal, install)
    assert error.value.definition.code == "UPD-1010"
    assert "符号链接" in error.value.detail or "目录联接" in error.value.detail
    assert actual.read_bytes() == b"same hook"
