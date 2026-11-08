"""A new manager session may reuse only archives bound to the current release."""

from dataclasses import replace
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
import zipfile

import pytest

from test_app_shell import _api, _download_ready
from test_updates import _DownloadResponse, _channel_and_key, _ready_incremental_update, _ready_payload, _release_for_archive
from wechatrebot_manager import update_api, update_cache, updates
from wechatrebot_manager.payload import sha256_file
from wechatrebot_manager.update_errors import UpdateFailure


def _archive(payload):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for source in payload.rglob("*"):
            if source.is_file():
                archive.write(source, "payload/" + source.relative_to(payload).as_posix())
    return buffer.getvalue()


@pytest.fixture
def downloaded(tmp_path, monkeypatch):
    content = _archive(_ready_payload(tmp_path / "source"))
    release = _release_for_archive(content)
    channel, _ = _channel_and_key()
    calls = []
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *args, **kwargs:
                        calls.append(args[0].full_url) or _DownloadResponse(content))
    root = tmp_path / "install"
    prepared = updates.prepare_update(release, channel, root)
    return root, release, channel, prepared, calls


def test_new_api_session_restores_download_and_requires_a_new_confirmation(downloaded, monkeypatch):
    root, release, channel, original, calls = downloaded
    monkeypatch.setattr(update_api.sys, "frozen", True, raising=False)
    tokens = []
    for _ in range(2):
        api = _api(root)
        api._available_update, api._update_channel = release, channel
        status = _download_ready(api)
        assert api._prepared_update.release == release
        assert api._prepared_update.working_root != original.working_root
        tokens.append(status["confirmation_token"])
    assert tokens[0] != tokens[1]
    assert calls == [release.package_url]


def test_restored_archive_ignores_modified_extracted_files_and_untrusted_plan(downloaded):
    root, release, channel, original, calls = downloaded
    manager = original.payload_root / "manager/WeChatReBot.exe"
    manager.write_bytes(b"untrusted-extracted-manager")
    manifest = original.payload_root / "release-manifest.json"
    content = json.loads(manifest.read_text(encoding="utf-8"))
    content["files"][0]["sha256"] = sha256_file(manager)
    manifest.write_text(json.dumps(content), encoding="utf-8")
    (original.working_root / "update-plan.json").write_text(
        json.dumps({"payload_root": str(root.parent / "outside"), "install_root": str(root.parent)}), encoding="utf-8",
    )

    restored = updates.prepare_update(release, channel, root)

    assert (restored.payload_root / "manager/WeChatReBot.exe").read_bytes() == b"new-manager"
    assert restored.payload_root.is_relative_to(restored.working_root)
    assert calls == [release.package_url]


@pytest.mark.parametrize("damage", ["archive", "receipt", "size"])
def test_untrusted_cache_falls_back_to_a_fresh_download(downloaded, damage):
    root, release, channel, original, calls = downloaded
    if damage == "archive":
        original.archive_path.write_bytes(b"x" * release.package_size)
    elif damage == "size":
        original.archive_path.write_bytes(b"truncated")
    else:
        (original.working_root / "prepared-update.json").write_bytes(b"{broken")

    restored = updates.prepare_update(release, channel, root)

    assert restored.archive_path.read_bytes() != b"x" * release.package_size
    assert calls == [release.package_url, release.package_url]


@pytest.mark.parametrize("change", ["url", "build"])
def test_cache_requires_current_release_identity_even_when_archive_digest_matches(downloaded, change):
    root, release, channel, _original, calls = downloaded
    current = replace(release, **({"package_url": "https://updates.example.test/repacked.zip"}
                                 if change == "url" else {"build": 7}))

    prepared = updates.prepare_update(current, channel, root)

    assert prepared.release == current
    assert calls == [release.package_url, current.package_url]


def test_cached_incremental_package_rechecks_changed_installed_files(tmp_path, monkeypatch):
    root, staged = _ready_incremental_update(tmp_path)
    content = _archive(staged.payload_root)
    release = _release_for_archive(content)
    channel, _ = _channel_and_key()
    calls = []
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *args, **kwargs:
                        calls.append(True) or _DownloadResponse(content))
    updates.prepare_update(release, channel, root)
    changed = root / "app/astrbot/data/plugins/example/main.py"
    changed.write_bytes(b"changed-after-download")

    with pytest.raises(UpdateFailure) as failure:
        updates.prepare_update(release, channel, root)

    assert failure.value.definition.code == "UPD-1003"
    assert changed.read_bytes() == b"changed-after-download"
    assert calls == [True]


def test_recent_receipt_survives_startup_cleanup_and_old_cache_expires(downloaded):
    root, release, channel, original, calls = downloaded
    two_hours_ago = time.time() - 2 * 60 * 60
    os.utime(original.working_root, (two_hours_ago, two_hours_ago))

    assert updates.cleanup_stale_update_workdirs(root) == 0
    restored = updates.prepare_update(release, channel, root)
    assert calls == [release.package_url]
    # The superseded receipt no longer gets the extended retention period.
    os.utime(original.working_root, (two_hours_ago, two_hours_ago))
    assert updates.cleanup_stale_update_workdirs(root) == 1
    eight_days_ago = time.time() - 8 * 24 * 60 * 60
    os.utime(restored.working_root, (eight_days_ago, eight_days_ago))
    assert updates.cleanup_stale_update_workdirs(root) == 1


@pytest.mark.parametrize("damage", ["oversized", "utf8", "wrong_identity"])
def test_invalid_cache_receipts_are_ignored(downloaded, damage):
    root, release, channel, original, calls = downloaded
    receipt = original.working_root / "prepared-update.json"
    if damage == "oversized":
        receipt.write_bytes(b" " * 1025)
    elif damage == "utf8":
        receipt.write_bytes(b"\xff")
    else:
        receipt.write_text(json.dumps({"schema_version": 1, "release_id": "0" * 64}), encoding="utf-8")

    updates.prepare_update(release, channel, root)

    assert calls == [release.package_url, release.package_url]


def test_cache_copy_failure_falls_back_to_download(downloaded, monkeypatch):
    from wechatrebot_manager import update_cache
    root, release, channel, _original, calls = downloaded
    def unavailable(*_args, **_kwargs):
        raise OSError("cached file was removed concurrently")
    monkeypatch.setattr(update_cache, "_copy_stream_and_hash", unavailable)

    restored = updates.prepare_update(release, channel, root)

    assert restored.payload_root.is_dir()
    assert calls == [release.package_url, release.package_url]


def test_receipt_write_failure_does_not_fail_verified_download(downloaded, monkeypatch):
    root, release, channel, _original, calls = downloaded
    def unavailable(*_args, **_kwargs):
        raise OSError("receipt unavailable")
    monkeypatch.setattr(updates, "record_update_archive", unavailable)

    prepared = updates.prepare_update(release, channel, root)

    assert prepared.payload_root.is_dir()
    assert calls == [release.package_url]


def test_cache_that_grows_during_copy_is_bounded_and_redownloaded(downloaded, monkeypatch):
    root, release, channel, original, calls = downloaded
    open_path = Path.open
    def changed_open(path, *args, **kwargs):
        if path == original.archive_path:
            return io.BytesIO(b"x" * (release.package_size + 1))
        return open_path(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", changed_open)

    prepared = updates.prepare_update(release, channel, root)

    assert prepared.archive_path.stat().st_size == release.package_size
    assert calls == [release.package_url, release.package_url]


def _ordered_scan(monkeypatch, updates_root, candidates):
    scandir = os.scandir
    visited = []
    @contextmanager
    def scan(path):
        if Path(path) == updates_root:
            def entries():
                for candidate in candidates:
                    visited.append(candidate)
                    yield SimpleNamespace(name=candidate.name, path=str(candidate))
            yield entries()
        else:
            with scandir(path) as entries:
                yield entries
    monkeypatch.setattr(update_cache.os, "scandir", scan)
    return visited


@pytest.mark.parametrize("age_offset,uses_cache", [(-1, False), (0, False), (1, True)])
def test_cache_age_is_enforced_during_restore_without_cleanup(downloaded, monkeypatch, age_offset, uses_cache):
    root, release, channel, original, calls = downloaded
    now = time.time()
    monkeypatch.setattr(update_cache.time, "time", lambda: now)
    modified = now - update_cache.UPDATE_CACHE_MAX_AGE_SECONDS + age_offset
    os.utime(original.working_root, (modified, modified))

    updates.prepare_update(release, channel, root)

    assert len(calls) == (1 if uses_cache else 2)


@pytest.mark.parametrize("before_limit", [True, False])
def test_directory_scan_stops_at_entry_limit(downloaded, monkeypatch, before_limit):
    root, release, channel, original, calls = downloaded
    limit = update_cache.UPDATE_CACHE_MAX_SCAN_ENTRIES
    misses = [root / "updates" / f"unrelated-{index}" for index in range(limit - int(before_limit))]
    for path in misses:
        path.touch()
    visited = _ordered_scan(monkeypatch, root / "updates", [*misses, original.working_root])

    updates.prepare_update(release, channel, root)

    assert len(visited) == limit
    assert len(calls) == (1 if before_limit else 2)


@pytest.mark.parametrize("before_limit", [True, False])
def test_repeated_damaged_archives_have_a_copy_attempt_limit(downloaded, monkeypatch, before_limit):
    root, release, channel, original, calls = downloaded
    limit = update_cache.UPDATE_CACHE_MAX_COPY_ATTEMPTS
    damaged = []
    for index in range(limit - int(before_limit)):
        candidate = root / "updates" / f".pending-damaged-{index}"
        shutil.copytree(original.working_root, candidate)
        (candidate / "update.zip").write_bytes(b"x" * release.package_size)
        damaged.append(candidate)
    _ordered_scan(monkeypatch, root / "updates", [*damaged, original.working_root])
    copied = []
    copy = update_cache._copy_stream_and_hash
    def track(reader, target, **kwargs):
        copied.append(reader.name)
        return copy(reader, target, **kwargs)
    monkeypatch.setattr(update_cache, "_copy_stream_and_hash", track)

    updates.prepare_update(release, channel, root)

    assert len(copied) == limit
    assert len(calls) == (1 if before_limit else 2)
