import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from wechatrebot_manager.payload import (
    PayloadError,
    install_payload,
    installed_payload_fingerprint,
    payload_relative_paths,
    payload_removed_paths,
    payload_requires_runtime_stop,
    validate_incremental_base,
    verify_payload,
)


def _payload(root: Path, *, path: str = "app/example.txt") -> Path:
    source = root / Path(*path.split("/"))
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("commercial payload\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest().upper()
    (root / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [{"path": path, "sha256": digest}],
            }
        ),
        encoding="utf-8",
    )
    return source


def test_verified_payload_installs_and_records_hashes(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    install = tmp_path / "install"
    install.mkdir()

    result = install_payload(payload, install)

    assert result.files[0].sha256 == hashlib.sha256(source.read_bytes()).hexdigest().upper()
    assert (install / "app" / "example.txt").read_text(encoding="utf-8") == "commercial payload\n"
    installed = json.loads(
        (install / "config" / "installed-payload.json").read_text(encoding="utf-8")
    )
    assert installed["schema_version"] == 1
    assert "source_manifest_status" not in installed


def test_tampered_payload_is_rejected_before_install(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    source.write_text("tampered", encoding="utf-8")
    with pytest.raises(PayloadError, match="校验失败"):
        verify_payload(payload)


def test_archive_member_case_must_match_manifest(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    archive = tmp_path / "case-mismatch.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.write(payload / "release-manifest.json", "release-manifest.json")
        package.write(source, "App/Example.txt")
    with pytest.raises(PayloadError, match="大小写"):
        verify_payload(archive)


def test_streaming_install_rejects_tamper_before_replacing_target(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    source.write_text("tampered", encoding="utf-8")
    install = tmp_path / "install"
    target = install / "app" / "example.txt"
    target.parent.mkdir(parents=True)
    target.write_text("existing installation\n", encoding="utf-8")

    with pytest.raises(PayloadError, match="校验失败"):
        install_payload(payload, install)

    assert target.read_text(encoding="utf-8") == "existing installation\n"


@pytest.mark.parametrize(
    "relative",
    ["config/defaults.json", "app/astrbot/data/config/cmd_config.json", "app/astrbot/data/cmd_config.json"],
)
def test_update_preserves_existing_user_configuration(
    tmp_path: Path, relative: str
) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload, path=relative)
    install = tmp_path / "install"
    target = install / Path(*relative.split("/"))
    target.parent.mkdir(parents=True)
    target.write_text("customer settings\n", encoding="utf-8")

    result = install_payload(
        payload,
        install,
        preserve_existing_user_config=True,
    )

    assert target.read_text(encoding="utf-8") == "customer settings\n"
    installed = json.loads(
        (install / "config" / "installed-payload.json").read_text(encoding="utf-8")
    )
    assert installed["files"][0]["path"] == relative
    assert installed["files"][0]["sha256"] == hashlib.sha256(
        target.read_bytes()
    ).hexdigest().upper()
    assert result.files[0].source_path == target


def test_incremental_update_merges_prior_installed_file_manifest(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    runtime = install / "runtime" / "python.exe"
    manager = install / "manager" / "WeChatReBot.exe"
    runtime.parent.mkdir(parents=True)
    manager.parent.mkdir(parents=True)
    runtime.write_bytes(b"unchanged runtime")
    manager.write_bytes(b"old manager")
    config = install / "config"
    config.mkdir()
    (config / "installed-payload.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_version": "0.9.8",
                "release_build": 7,
                "files": [
                    {
                        "path": "runtime/python.exe",
                        "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest().upper(),
                    },
                    {
                        "path": "manager/WeChatReBot.exe",
                        "sha256": hashlib.sha256(manager.read_bytes()).hexdigest().upper(),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "manager-update"
    updated_manager = _payload(payload, path="manager/WeChatReBot.exe")
    release_manifest = json.loads((payload / "release-manifest.json").read_text(encoding="utf-8"))
    release_manifest["release_version"] = "0.9.9"
    (payload / "release-manifest.json").write_text(
        json.dumps(release_manifest), encoding="utf-8"
    )

    install_payload(payload, install, preserve_existing_user_config=True)

    installed = json.loads(
        (config / "installed-payload.json").read_text(encoding="utf-8")
    )
    entries = {entry["path"]: entry["sha256"] for entry in installed["files"]}
    assert set(entries) == {"runtime/python.exe", "manager/WeChatReBot.exe"}
    assert entries["runtime/python.exe"] == hashlib.sha256(
        runtime.read_bytes()
    ).hexdigest().upper()
    assert entries["manager/WeChatReBot.exe"] == hashlib.sha256(
        updated_manager.read_bytes()
    ).hexdigest().upper()
    assert installed["release_version"] == "0.9.9"
    assert installed["release_build"] == 7


def test_legacy_payload_status_does_not_block_verification(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _payload(payload)
    manifest_path = payload / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_manifest_status"] = "phase-incomplete"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert verify_payload(payload).files


def test_path_preflight_reads_manifest_without_walking_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = tmp_path / "payload"
    _payload(payload)

    def fail_if_walked(_path: Path, _pattern: str):
        raise AssertionError("path preflight must not traverse the payload")

    monkeypatch.setattr(Path, "rglob", fail_if_walked)

    assert payload_relative_paths(payload) == (Path("app/example.txt"),)


def test_manifest_cannot_escape_or_overwrite_user_data(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _payload(payload)
    manifest_path = payload / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside.txt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PayloadError, match="不安全"):
        verify_payload(payload)

    payload = tmp_path / "payload-user-data"
    _payload(payload, path="data/private.json")
    with pytest.raises(PayloadError, match="用户数据"):
        verify_payload(payload)


def test_install_payload_reports_each_phase(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _payload(payload)
    events: list[tuple[str, int, int]] = []

    install_payload(
        payload,
        tmp_path / "install",
        progress_callback=lambda phase, current, total: events.append(
            (phase, current, total)
        ),
    )

    assert events == [
        ("prepare", 1, 1),
        ("install", 1, 1),
    ]


def test_windows_file_replace_retries_transient_lock(monkeypatch, tmp_path: Path) -> None:
    from wechatrebot_manager import payload as payload_module

    source = tmp_path / "source.exe"
    target = tmp_path / "target.exe"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    real_replace = payload_module.os.replace
    attempts = 0

    def flaky_replace(current_source: Path, current_target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporarily locked")
        real_replace(current_source, current_target)

    monkeypatch.setattr(payload_module.os, "name", "nt")
    monkeypatch.setattr(payload_module.os, "replace", flaky_replace)
    monkeypatch.setattr(payload_module.time, "sleep", lambda _seconds: None)

    payload_module._replace_file(source, target, timeout_seconds=1)

    assert attempts == 3
    assert target.read_bytes() == b"new"


def test_packed_payload_installs_and_verifies_in_one_stream(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    archive_path = tmp_path / "payload.wrb"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(payload / "release-manifest.json", "release-manifest.json")
        archive.write(source, "app/example.txt")
    events: list[str] = []

    result = install_payload(
        archive_path,
        tmp_path / "install",
        progress_callback=lambda phase, _current, _total: events.append(phase),
    )

    assert result.root == archive_path.resolve()
    assert events == ["prepare", "install"]
    assert (tmp_path / "install" / "app" / "example.txt").read_text(
        encoding="utf-8"
    ) == "commercial payload\n"


def test_packed_payload_rejects_unlisted_file(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    archive_path = tmp_path / "payload.wrb"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(payload / "release-manifest.json", "release-manifest.json")
        archive.write(source, "app/example.txt")
        archive.writestr("app/unlisted.txt", "unexpected")

    with pytest.raises(PayloadError, match="未登记"):
        verify_payload(archive_path)


def test_install_uses_flat_transaction_paths_for_deep_payload(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "payload.wrb"
    relative = "runtime/" + "deep/" * 17 + "module.py"
    content = b"payload"
    digest = hashlib.sha256(content).hexdigest().upper()
    manifest = {
        "schema_version": 1,
        "files": [{"path": relative, "sha256": digest}],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        archive.writestr(relative, content)
    install_root = tmp_path / "install"
    legacy_transaction_chars = (
        len(str(install_root)) + len("\\updates\\.rollback-") + 32 + 1 + len(relative)
    )
    assert len(str(install_root / Path(*relative.split("/")))) <= 259
    assert legacy_transaction_chars > 259

    install_payload(archive_path, install_root)

    assert (install_root / Path(*relative.split("/"))).read_bytes() == content
    assert not tuple((install_root / "updates").glob(".*-*"))


def test_install_rejects_final_target_path_overflow_before_creating_root(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "payload-too-deep.wrb"
    relative = "runtime/" + "deep/" * 45 + "module.py"
    content = b"payload"
    manifest = {
        "schema_version": 1,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest().upper(),
            }
        ],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        archive.writestr(relative, content)
    install_root = tmp_path / "rejected-install"

    with pytest.raises(PayloadError, match="路径过长"):
        install_payload(archive_path, install_root)

    assert not install_root.exists()


def _installed_tree(root: Path, contents: dict[str, bytes]) -> dict[str, str]:
    hashes = {}
    for relative, content in contents.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        hashes[relative] = hashlib.sha256(content).hexdigest().upper()
    (root / "config").mkdir(exist_ok=True)
    (root / "config/installed-payload.json").write_text(json.dumps({
        "schema_version": 1,
        "release_version": "0.9.9",
        "release_build": 7,
        "files": [{"path": path, "sha256": digest} for path, digest in hashes.items()],
    }), encoding="utf-8")
    return hashes


def _incremental_payload(
    root: Path, install: Path, *, files: dict[str, bytes] | None = None,
    remove: dict[str, str] | None = None,
) -> Path:
    root.mkdir(parents=True)
    entries = []
    for relative, content in (files or {}).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        entries.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest().upper()})
    (root / "release-manifest.json").write_text(json.dumps({
        "schema_version": 2,
        "update_kind": "incremental",
        "base_fingerprint": installed_payload_fingerprint(install),
        "files": entries,
        "remove": [{"path": path, "sha256": digest} for path, digest in (remove or {}).items()],
    }), encoding="utf-8")
    return root


@pytest.mark.parametrize("packed", [False, True])
def test_schema2_updates_adds_deletes_and_preserves_full_inventory(tmp_path: Path, packed: bool) -> None:
    install = tmp_path / "install"
    hashes = _installed_tree(install, {
        "app/shared/main.py": b"old", "app/shared/obsolete.py": b"remove me",
        "runtime/python.exe": b"runtime", "app/astrbot/data/cmd_config.json": b"user settings",
    })
    delta = _incremental_payload(tmp_path / "delta", install,
        files={"app/shared/main.py": b"new", "app/shared/new.py": b"added"},
        remove={"app/shared/obsolete.py": hashes["app/shared/obsolete.py"]})
    if packed:
        archive = tmp_path / "delta.wrb"
        with zipfile.ZipFile(archive, "w") as handle:
            for file in delta.rglob("*"):
                if file.is_file():
                    handle.write(file, file.relative_to(delta).as_posix())
        delta = archive
    validate_incremental_base(delta, install)
    assert payload_requires_runtime_stop(delta)
    assert payload_removed_paths(delta) == (Path("app/shared/obsolete.py"),)
    install_payload(delta, install)
    assert (install / "app/shared/main.py").read_bytes() == b"new"
    assert (install / "app/shared/new.py").read_bytes() == b"added"
    assert not (install / "app/shared/obsolete.py").exists()
    assert (install / "app/astrbot/data/cmd_config.json").read_bytes() == b"user settings"
    manifest = json.loads((install / "config/installed-payload.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["release_build"] == 7
    assert {entry["path"] for entry in manifest["files"]} == {
        "app/shared/main.py", "app/shared/new.py", "runtime/python.exe", "app/astrbot/data/cmd_config.json",
    }


def test_schema2_delete_only_payload(tmp_path: Path) -> None:
    install = tmp_path / "install"
    hashes = _installed_tree(install, {"legal/obsolete.txt": b"old"})
    delta = _incremental_payload(tmp_path / "delta", install, remove=hashes)
    assert verify_payload(delta).files == ()
    assert payload_relative_paths(delta) == ()
    assert not payload_requires_runtime_stop(delta)
    install_payload(delta, install)
    assert not (install / "legal/obsolete.txt").exists()
    assert json.loads((install / "config/installed-payload.json").read_text())["files"] == []


@pytest.mark.parametrize("change", ["replace", "remove", "missing", "unregistered", "base", "new-content"])
def test_schema2_rejects_drift_before_changing_any_installed_file(tmp_path: Path, change: str) -> None:
    install = tmp_path / "install"
    hashes = _installed_tree(install, {"app/first.py": b"old", "app/last.py": b"old last"})
    delta = _incremental_payload(tmp_path / "delta", install,
        files={"app/first.py": b"new", "app/added.py": b"added"},
        remove={"app/last.py": hashes["app/last.py"]})
    if change == "replace":
        (install / "app/first.py").write_bytes(b"local edit")
    elif change == "remove":
        (install / "app/last.py").write_bytes(b"local edit")
    elif change == "missing":
        (install / "app/last.py").unlink()
    elif change == "unregistered":
        (install / "app/added.py").write_bytes(b"user file")
    elif change == "base":
        manifest_path = delta / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["base_fingerprint"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
    else:
        (delta / "app/added.py").write_bytes(b"tampered")
    before = {str(path.relative_to(install)): path.read_bytes() for path in install.rglob("*") if path.is_file()}
    with pytest.raises(PayloadError):
        install_payload(delta, install)
    after = {str(path.relative_to(install)): path.read_bytes() for path in install.rglob("*") if path.is_file()}
    assert after == before


def test_schema2_manifest_write_failure_rolls_back_deletes_adds_and_replacements(tmp_path: Path, monkeypatch) -> None:
    from wechatrebot_manager import payload as payload_module

    install = tmp_path / "install"
    hashes = _installed_tree(install, {"app/first.py": b"old", "app/last.py": b"old last"})
    before_manifest = (install / "config/installed-payload.json").read_bytes()
    delta = _incremental_payload(tmp_path / "delta", install,
        files={"app/first.py": b"new", "app/added.py": b"added"},
        remove={"app/last.py": hashes["app/last.py"]})
    real_write = payload_module.write_json_atomic

    def write_then_fail(path, content):
        real_write(path, content)
        raise OSError("write failure")

    monkeypatch.setattr(payload_module, "write_json_atomic", write_then_fail)
    with pytest.raises(OSError, match="write failure"):
        install_payload(delta, install)
    assert (install / "app/first.py").read_bytes() == b"old"
    assert (install / "app/last.py").read_bytes() == b"old last"
    assert not (install / "app/added.py").exists()
    assert (install / "config/installed-payload.json").read_bytes() == before_manifest
    assert not tuple((install / "updates").glob(".*-*"))


@pytest.mark.parametrize("path", [
    "config/defaults.json", "app/astrbot/data/cmd_config.json",
    "app/astrbot/data/config/plugin.json", "app/astrbot/data/plugin_data/state.db",
    "data/private.json", "runtime/python.exe", "app/bridge/native.dll",
    "config/installed-payload.json", "app/../outside.py",
])
@pytest.mark.parametrize("removing", [False, True])
def test_schema2_rejects_protected_and_unsafe_changes(tmp_path: Path, path: str, removing: bool) -> None:
    install = tmp_path / "install"
    _installed_tree(install, {"app/ok.py": b"ok"})
    delta = _incremental_payload(tmp_path / "delta", install, files={"app/new.py": b"new"})
    manifest_path = delta / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = {"path": path, "sha256": "0" * 64}
    manifest["files"] = [] if removing else [entry]
    manifest["remove"] = [entry] if removing else []
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(PayloadError):
        validate_incremental_base(delta, install)


def test_fingerprint_ignores_mutable_config_and_canonicalizes_inventory(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _installed_tree(install, {"runtime/python.exe": b"runtime", "app/ok.py": b"ok",
        "app/astrbot/data/cmd_config.json": b"config"})
    original = installed_payload_fingerprint(install)
    manifest_path = install / "config/installed-payload.json"
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["files"]:
        entry["path"] = entry["path"].upper()
        entry["sha256"] = entry["sha256"].lower()
        if "CMD_CONFIG" in entry["path"]:
            entry["sha256"] = "0" * 64
    manifest["files"].reverse()
    manifest_path.write_text(json.dumps(manifest))
    assert installed_payload_fingerprint(install) == original
    manifest["files"].append({"path": "runtime/extra.py", "sha256": "0" * 64})
    manifest_path.write_text(json.dumps(manifest))
    assert installed_payload_fingerprint(install) != original


def test_schema2_does_not_hash_unchanged_runtime(tmp_path: Path, monkeypatch) -> None:
    from wechatrebot_manager import payload as payload_module

    install = tmp_path / "install"
    _installed_tree(install, {"runtime/python.exe": b"runtime", "app/ok.py": b"old"})
    delta = _incremental_payload(tmp_path / "delta", install, files={"app/ok.py": b"new"})
    real_hash = payload_module.sha256_file

    def guarded_hash(path):
        assert path != install / "runtime/python.exe", "unchanged runtime must not be scanned"
        return real_hash(path)

    monkeypatch.setattr(payload_module, "sha256_file", guarded_hash)
    validate_incremental_base(delta, install)
    install_payload(delta, install)


@pytest.mark.parametrize("parent_link", [False, True])
def test_schema2_rejects_symlink_target_or_parent(tmp_path: Path, parent_link: bool) -> None:
    install = tmp_path / "install"
    _installed_tree(install, {"app/real/file.py": b"old"})
    delta = _incremental_payload(tmp_path / "delta", install, files={"app/linked/file.py": b"new"})
    linked = install / "app/linked"
    try:
        if parent_link:
            linked.symlink_to(install / "app/real", target_is_directory=True)
        else:
            linked.mkdir()
            (linked / "file.py").symlink_to(install / "app/real/file.py")
    except OSError:
        pytest.skip("symbolic link privilege unavailable")
    with pytest.raises(PayloadError, match="符号链接"):
        install_payload(delta, install)
    assert (install / "app/real/file.py").read_bytes() == b"old"
