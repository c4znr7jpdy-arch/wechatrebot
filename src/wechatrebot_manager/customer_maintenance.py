from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable
import zipfile

from . import __version__
from .backup import (
    BackupError,
    _validated_restore_entries,
    backup_source_files,
    create_customer_backup,
    restore_customer_backup,
)
from .diagnostics import collect_diagnostics, white_label_terms
from .json_io import write_json_atomic
from .payload import sha256_file
from .runtime_status import runtime_snapshot


_BACKUP_LABELS = {
    "config": "机器人设置",
    "data": "机器人数据与记忆",
}
_CHECK_LABELS = {
    "install-marker": "安装状态",
    "manager-config": "控制台设置",
    "brand-config": "外观设置",
    "bridge-config": "微信连接设置",
    "engine-config": "回复服务设置",
    "installed-payload": "程序文件清单",
    "feature-bundle": "功能包",
    "python-runtime": "基础运行环境",
    "engine-runtime": "回复服务文件",
    "wechat-connection-launcher": "微信连接程序",
    "wechat-connection-library": "微信连接组件",
    "wechat-connection-integrity": "微信连接完整性",
    "ffmpeg": "视频处理工具",
    "ffprobe": "视频信息工具",
    "wechat-executable": "微信程序",
}
_INLINE_SECRET = re.compile(
    r"(?i)\b(api.?key|authorization|cookie|credential|password|secret|token)"
    r"(\s*[=:]\s*)(?:bearer\s+)?[^\s,;，；]+"
)
_BARE_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_PRIVATE_IDENTIFIER = re.compile(
    r"(?i)\b(?:wxid_[a-z0-9_-]+|\d{5,}@chatroom)\b"
)
_WINDOWS_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\"'<>|*?，；。]+"
)
_POSIX_PATH = re.compile(
    r"(?<![\w:])/(?:home|users|workspace|workspaces|mnt|opt|srv|tmp|var)/"
    r"[^\s\r\n\"'<>，；。]+",
    re.IGNORECASE,
)


class MaintenanceError(RuntimeError):
    """Raised when a customer maintenance action cannot be completed safely."""


class MaintenanceBusyError(MaintenanceError):
    """Raised when a mutating maintenance action conflicts with runtime activity."""


def _default_runtime_active() -> bool:
    snapshot = runtime_snapshot()
    return bool(
        snapshot.engine_ready
        or snapshot.connection_ready
        or snapshot.hook_ready
        or snapshot.heartbeat_fresh
    )


def _customer_safe_text(value: object, *, limit: int = 500) -> str:
    text = str(value or "")
    text = _INLINE_SECRET.sub(r"\1\2***", text)
    text = _BARE_SECRET.sub("***", text)
    text = _PRIVATE_IDENTIFIER.sub("<ACCOUNT_ID>", text)
    text = _WINDOWS_PATH.sub("<LOCAL_PATH>", text)
    text = _POSIX_PATH.sub("<LOCAL_PATH>", text)
    text = str(white_label_terms(text))
    return text[:limit]


def customer_diagnostic_summary(report: dict[str, Any]) -> dict[str, object]:
    """Reduce a full diagnostic report to customer-safe status information.

    The full collector can inspect configuration to determine health.  This
    public projection deliberately drops that captured configuration rather
    than relying only on field-by-field redaction.  It therefore cannot expose
    message bodies, prompts, cookies, credentials, or arbitrary local paths.
    """

    raw_checks = report.get("checks")
    checks: list[dict[str, object]] = []
    if isinstance(raw_checks, list):
        for index, item in enumerate(raw_checks):
            if not isinstance(item, dict):
                continue
            raw_name = _customer_safe_text(item.get("name") or f"check-{index + 1}", limit=80)
            safe_id = re.sub(r"[^a-z0-9-]+", "-", raw_name.casefold()).strip("-")
            if not safe_id or safe_id not in _CHECK_LABELS:
                safe_id = f"check-{index + 1}"
            severity = str(item.get("severity") or "error").casefold()
            if severity not in {"error", "warning", "info"}:
                severity = "error"
            checks.append(
                {
                    "id": safe_id,
                    "label": _CHECK_LABELS.get(raw_name, "运行检查"),
                    "ok": bool(item.get("ok")),
                    "severity": severity,
                    "message": _customer_safe_text(item.get("message") or "未提供检查结果"),
                }
            )

    error_count = sum(
        not item["ok"] and item["severity"] == "error" for item in checks
    )
    warning_count = sum(
        not item["ok"] and item["severity"] == "warning" for item in checks
    )
    safe = {
        "schema_version": 1,
        "product": "WeChat ReBot",
        "manager_version": __version__,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "ok": error_count == 0,
        "status": "healthy" if error_count == 0 else "action_required",
        "summary": {
            "error_count": error_count,
            "warning_count": warning_count,
            "check_count": len(checks),
        },
        "privacy": {
            "contains_configuration_values": False,
            "contains_message_content": False,
            "contains_logs": False,
            "contains_credentials": False,
            "contains_local_paths": False,
        },
        "checks": checks,
    }
    return white_label_terms(safe)


class CustomerMaintenanceService:
    """Safe, path-constrained service layer for the unified customer console."""

    def __init__(
        self,
        install_root: str | Path,
        *,
        is_running: Callable[[], bool] | None = None,
    ) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.backup_root = self.install_root / "backups"
        self.diagnostic_root = self.install_root / "logs"
        self._is_running = is_running or _default_runtime_active
        self._mutation_lock = threading.Lock()

    def _runtime_active(self) -> bool:
        try:
            return bool(self._is_running())
        except Exception as exc:
            raise MaintenanceBusyError(
                "暂时无法确认机器人是否已停止，为保护数据，本次操作已取消。"
            ) from exc

    def _require_stopped(self, action: str) -> None:
        if self._runtime_active():
            raise MaintenanceBusyError(f"请先停止机器人，再{action}。")

    def _backup_path(self, backup_id: str) -> Path:
        value = str(backup_id or "").strip()
        candidate = Path(value)
        if (
            not value
            or candidate.name != value
            or value in {".", ".."}
            or candidate.suffix.casefold() != ".zip"
            or os.path.isreserved(value)
        ):
            raise MaintenanceError("备份名称无效，请从备份列表中重新选择。")
        target = (self.backup_root / value).resolve()
        try:
            target.relative_to(self.backup_root.resolve())
        except ValueError as exc:  # pragma: no cover - guarded by basename check
            raise MaintenanceError("备份位置不安全。") from exc
        if target.is_symlink() or (
            hasattr(target, "is_junction") and target.is_junction()
        ):
            raise MaintenanceError("不能使用链接形式的备份文件。")
        return target

    @staticmethod
    def _content_summary(
        files: tuple[tuple[Path, Path], ...],
    ) -> tuple[list[dict[str, object]], int]:
        groups: dict[str, dict[str, int]] = {}
        total_bytes = 0
        for source, relative in files:
            root_name = relative.parts[0].casefold()
            group = groups.setdefault(root_name, {"file_count": 0, "size": 0})
            size = source.stat().st_size
            group["file_count"] += 1
            group["size"] += size
            total_bytes += size
        contents = [
            {
                "id": name,
                "label": _BACKUP_LABELS.get(name, "机器人数据"),
                "file_count": values["file_count"],
                "size": values["size"],
            }
            for name, values in sorted(groups.items())
        ]
        return contents, total_bytes

    def preview_backup(self) -> dict[str, object]:
        files = backup_source_files(self.install_root)
        contents, total_bytes = self._content_summary(files)
        running = self._runtime_active()
        return {
            "schema_version": 1,
            "file_count": len(files),
            "uncompressed_size": total_bytes,
            "contents": contents,
            "contains_sensitive_configuration": True,
            "runtime_active": running,
            "can_create": not running,
            "notice": "备份包含机器人设置、数据和记忆，请像保管账号密码一样妥善保管。",
        }

    def list_backups(self) -> list[dict[str, object]]:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        items: list[dict[str, object]] = []
        for path in self.backup_root.glob("*.zip"):
            if not path.is_file() or path.is_symlink() or (
                hasattr(path, "is_junction") and path.is_junction()
            ):
                continue
            stat = path.stat()
            items.append(
                {
                    "id": path.name,
                    "file_name": path.name,
                    "kind": (
                        "recovery_point"
                        if path.name.casefold().startswith("pre-restore-")
                        else "customer_backup"
                    ),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(),
                }
            )
        items.sort(key=lambda item: str(item["modified_at"]), reverse=True)
        return items

    def create_backup(self) -> dict[str, object]:
        if not self._mutation_lock.acquire(blocking=False):
            raise MaintenanceBusyError("另一个备份或恢复操作正在进行，请稍后再试。")
        try:
            self._require_stopped("创建备份")
            self.backup_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target = self.backup_root / f"robot-backup-{timestamp}.zip"
            result = create_customer_backup(self.install_root, target)
            return {
                **result,
                "id": target.name,
                "file_name": target.name,
                "contents": self.preview_backup()["contents"],
            }
        finally:
            self._mutation_lock.release()

    def _validate_restore_path(self, source: Path) -> dict[str, object]:
        if not source.is_file():
            raise MaintenanceError("所选备份不存在，请刷新备份列表后重试。")
        try:
            with zipfile.ZipFile(source) as archive:
                manifest, entries = _validated_restore_entries(archive)
                group_counts: dict[str, dict[str, int]] = {}
                total_bytes = 0
                for info, relative, expected_digest in entries:
                    if info.flag_bits & 0x1:
                        raise MaintenanceError("加密压缩包不能直接恢复。")
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(info) as handle:
                        while chunk := handle.read(1024 * 1024):
                            digest.update(chunk)
                            size += len(chunk)
                    if size != info.file_size or digest.hexdigest().upper() != expected_digest:
                        raise MaintenanceError(
                            f"备份内容校验失败：{_BACKUP_LABELS.get(relative.parts[0].casefold(), '机器人数据')}"
                        )
                    root_name = relative.parts[0].casefold()
                    group = group_counts.setdefault(
                        root_name, {"file_count": 0, "size": 0}
                    )
                    group["file_count"] += 1
                    group["size"] += size
                    total_bytes += size
        except MaintenanceError:
            raise
        except (BackupError, RuntimeError, OSError, zipfile.BadZipFile) as exc:
            raise MaintenanceError("备份文件无效或已损坏，无法恢复。") from exc

        contents = [
            {
                "id": name,
                "label": _BACKUP_LABELS.get(name, "机器人数据"),
                "file_count": values["file_count"],
                "size": values["size"],
            }
            for name, values in sorted(group_counts.items())
        ]
        created_at = _customer_safe_text(manifest.get("created_at_utc"), limit=80)
        manager_version = _customer_safe_text(manifest.get("manager_version"), limit=40)
        return {
            "schema_version": 1,
            "valid": True,
            "backup_id": source.name,
            "created_at_utc": created_at or None,
            "manager_version": manager_version or None,
            "file_count": len(entries),
            "uncompressed_size": total_bytes,
            "archive_size": source.stat().st_size,
            "sha256": sha256_file(source),
            "contents": contents,
            "contains_sensitive_configuration": bool(
                manifest.get("contains_sensitive_configuration", True)
            ),
            "can_restore": not self._runtime_active(),
            "notice": "恢复会覆盖备份中包含的设置和数据，并会先自动创建恢复点。",
        }

    def validate_restore(self, backup_id: str) -> dict[str, object]:
        return self._validate_restore_path(self._backup_path(backup_id))

    def restore_backup(self, backup_id: str) -> dict[str, object]:
        if not self._mutation_lock.acquire(blocking=False):
            raise MaintenanceBusyError("另一个备份或恢复操作正在进行，请稍后再试。")
        try:
            self._require_stopped("恢复备份")
            source = self._backup_path(backup_id)
            validation = self._validate_restore_path(source)
            # Re-check immediately before the destructive boundary.  Validation
            # may take time for a large archive, during which the robot could be
            # started from another window.
            self._require_stopped("恢复备份")
            result = restore_customer_backup(self.install_root, source)
            recovery_point = Path(str(result["recovery_point"]))
            return {
                "schema_version": 1,
                "restored_file_count": result["restored_file_count"],
                "backup_id": source.name,
                "backup_sha256": result["backup_sha256"],
                "recovery_point_id": recovery_point.name,
                "validated": validation,
            }
        finally:
            self._mutation_lock.release()

    def diagnostic_summary(self) -> dict[str, object]:
        return customer_diagnostic_summary(collect_diagnostics(self.install_root))

    def export_diagnostics(self, *, include_activity: bool = False, activity_build: int = 0) -> dict[str, object]:
        report = self.diagnostic_summary()
        if include_activity:
            from .activity_service import ActivityService
            report["activity"] = ActivityService(self.install_root, build=activity_build).recent()
            report["privacy"]["contains_logs"] = True
            report["privacy"]["logs_redacted"] = True
        self.diagnostic_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.diagnostic_root / f"robot-diagnostics-{timestamp}.json"
        try:
            write_json_atomic(target, report)
        except OSError as exc:
            raise MaintenanceError("诊断文件暂时无法保存，请稍后重试。") from exc
        return {
            "schema_version": 1,
            "file_name": target.name,
            "path": str(target),
            "size": target.stat().st_size,
            "sha256": sha256_file(target),
            "summary": report["summary"],
        }


__all__ = [
    "CustomerMaintenanceService",
    "MaintenanceBusyError",
    "MaintenanceError",
    "customer_diagnostic_summary",
]
