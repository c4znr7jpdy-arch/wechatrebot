"""One environment installation with the existing durable rollback journal."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid

from .install_paths import extended_windows_path
from .json_io import write_json_atomic
from .payload import _checked_target, _safe_relative_path, install_payload, verify_payload
from .runtime_deployment import prepare_deployment, verify_deployment_environment
from .runtime_manifest import ENVIRONMENT_JOURNAL
from .single_instance import SingleInstanceLock
from .update_manifest import UpdateChannel, UpdateError
from .update_rollback import CONTROL_FILES, UpdateFileBackup, restore_update_targets, snapshot_update_targets


def recover_environment(root: Path) -> None:
    """Caller holds the shared update-helper lock and has confirmed services stopped."""
    path = _checked_target(root, ENVIRONMENT_JOURNAL)
    if not path.exists():
        return
    journal = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(journal, dict) or journal.get("environment_transaction") != 1:
        raise UpdateError("环境恢复记录无效。")
    rollback = _checked_target(root, _safe_relative_path(journal.get("rollback_root")), allow_directory=True)
    if not rollback.name.startswith(".environment-rollback-") or rollback.parent != root / "updates":
        raise UpdateError("环境回滚路径无效。")
    if not isinstance(journal.get("files"), list):
        raise UpdateError("环境恢复文件清单无效。")
    from .runtime_deployment import validate_deployment_path
    for entry in journal["files"]:
        if not isinstance(entry, dict):
            raise UpdateError("环境恢复文件条目无效。")
        relative = _safe_relative_path(entry.get("path"))
        backup = _safe_relative_path(entry.get("backup"))
        if len(backup.parts) != 2 or backup.parts[0] != "files":
            raise UpdateError("环境恢复备份路径无效。")
        if relative not in CONTROL_FILES:
            validate_deployment_path(relative, kind="environment", removing=True)
    if journal.get("state") != "committed":
        restore_update_targets(journal, root)
    shutil.rmtree(rollback, ignore_errors=True)
    path.unlink()


def install_environment(root: Path, channel: UpdateChannel, *, progress_callback=None) -> None:
    from .launcher import installation_services_stopped

    lock = SingleInstanceLock(f"update-helper:{root}")
    if not lock.acquire():
        raise UpdateError("应用或环境正在更新，请稍后重试。")
    working = None
    try:
        if not installation_services_stopped(root):
            raise UpdateError("请先停止机器人，再安装运行环境。")
        recover_environment(root)
        identifier = uuid.uuid4().hex
        working = _checked_target(root, Path(f"updates/.environment-{identifier}"), allow_directory=True)
        working.mkdir(parents=True)
        staging = prepare_deployment(None, root, working, channel, progress_callback=progress_callback)
        if not installation_services_stopped(root):
            raise UpdateError("机器人已开始运行，环境安装已取消。")
        verify_deployment_environment(staging, root, channel)
        verified = verify_payload(staging)
        rollback = root / "updates" / f".environment-rollback-{identifier}"
        journal = snapshot_update_targets(staging, root, rollback, update_id=identifier,
                                          version="environment", verified_payload=verified)
        journal["environment_transaction"] = 1
        path = _checked_target(root, ENVIRONMENT_JOURNAL)
        write_json_atomic(path, journal)
        try:
            install_payload(staging, root, verified_payload=verified, preserve_existing_user_config=True,
                            before_replace=UpdateFileBackup(journal, root).retain,
                            progress_callback=progress_callback)
            journal["state"] = "committed"
            write_json_atomic(path, journal)
        except Exception:
            restore_update_targets(journal, root)
            path.unlink()
            shutil.rmtree(rollback, ignore_errors=True)
            raise
        recover_environment(root)
    finally:
        if working is not None:
            shutil.rmtree(extended_windows_path(working), ignore_errors=True)
        lock.release()
