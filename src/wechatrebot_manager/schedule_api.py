"""Manager endpoints for scheduled tasks and explicit legacy migration."""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .group_directory import GroupDirectoryError
from .payload import PayloadError
from .plugin_center import PluginCenterError
from .legacy_task_migration import LegacyTaskMigration
from .scheduled_task_service import ScheduledTaskService
from .task_store import inspect_scheduled_command, next_occurrences, schedule_text


class ScheduleApiMixin:
    def get_schedules(self) -> dict[str, object]:
        try:
            return self._ok(self._schedules.list())
        except (OSError, ValueError, sqlite3.Error, PluginCenterError) as exc:
            return self._failed(exc)

    def _scheduled_task_service(self) -> ScheduledTaskService:
        service = getattr(self, "_scheduled_tasks", None)
        if service is None:
            service = ScheduledTaskService(
                self._install_root, groups=getattr(self, "_groups", None),
                contacts=getattr(self, "_contacts", None), account_hint=self._history_account_id,
                stop=self._lifecycle.stop_for_maintenance,
                stopped=self._lifecycle.maintenance_stopped,
            )
            self._scheduled_tasks = service
        return service

    def _legacy_migration_service(self) -> LegacyTaskMigration:
        service = getattr(self, "_legacy_migrations", None)
        if service is None:
            service = LegacyTaskMigration(
                self._schedules, self._scheduled_task_service().store,
                stop=self._lifecycle.stop_for_maintenance,
                stopped=self._lifecycle.maintenance_stopped,
            )
            self._legacy_migrations = service
        return service

    def get_legacy_schedule_migrations(self) -> dict[str, object]:
        return self._scheduled_call(lambda: {"items": self._legacy_migration_service().pending()})

    def preview_legacy_schedule_migration(self, job_id: str, recovery_key: str = "") -> dict[str, object]:
        return self._scheduled_call(self._legacy_migration_service().preview, job_id, recovery_key)

    def _legacy_write(self, operation: Callable, *args: Any) -> dict[str, object]:
        if not self._action_lock.acquire(blocking=False):
            return {"ok": False, "error": {"title": "操作进行中", "message": "请等待当前启动、停止或任务操作完成。"}}
        try:
            return self._scheduled_call(operation, *args)
        finally:
            self._action_lock.release()

    def migrate_legacy_schedule(self, token: str) -> dict[str, object]:
        return self._legacy_write(self._legacy_migration_service().commit, token)

    def cancel_legacy_schedule_migration(self, key: str) -> dict[str, object]:
        return self._legacy_write(self._legacy_migration_service().cancel, key)

    def preview_group_analysis_migration(self, payload: dict[str, Any]) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().preview_group_analysis_migration, payload)

    def migrate_group_analysis(self, token: str) -> dict[str, object]:
        return self._legacy_write(self._scheduled_task_service().migrate_group_analysis, token)

    def cancel_group_analysis_migration(self, account_id: str) -> dict[str, object]:
        return self._legacy_write(self._scheduled_task_service().cancel_group_analysis_migration, account_id)

    def _scheduled_call(self, operation: Callable, *args: Any) -> dict[str, object]:
        try:
            return self._ok(operation(*args))
        except ValueError as exc:
            event_id, saved = self._record_error(exc, "18001", operation=getattr(operation, "__name__", "scheduled_task"))
            return {"ok": False, "error": {"code": "18001", "title": "任务尚未完成配置", "message": str(exc), "event_id": event_id, "recorded": saved}}
        except (OSError, sqlite3.Error, PluginCenterError, GroupDirectoryError, PayloadError) as exc:
            event_id, saved = self._record_error(exc, "18004", operation=getattr(operation, "__name__", "scheduled_task"))
            # Transport exceptions may include upstream addresses or credentials.
            return {"ok": False, "error": {"code": "18004", "title": "定时任务操作未完成", "message": "连接或保存未完成，请检查机器人状态后刷新核对；你的编辑内容仍保留。", "event_id": event_id, "recorded": saved}}

    def get_scheduled_task_context(self, lightweight: bool = False) -> dict[str, object]:
        service = self._scheduled_task_service()
        if lightweight is True:
            return self._scheduled_call(service.context, True)
        return self._scheduled_call(service.context)

    def save_scheduled_task(self, payload: dict[str, Any]) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().save, payload)

    def set_scheduled_task_enabled(self, account_id: str, task_id: str, enabled: bool, revision: int) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().set_enabled, account_id, task_id, enabled, revision)

    def delete_scheduled_task(self, account_id: str, task_id: str, revision: int) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().delete, account_id, task_id, revision)

    def preview_scheduled_task(self, payload: dict[str, Any]) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().preview, payload)

    def test_scheduled_task(self, payload: dict[str, Any]) -> dict[str, object]:
        result = self._scheduled_call(self._scheduled_task_service().test, payload)
        data = result.get("data") if result.get("ok") else None
        if isinstance(data, dict) and data.get("status") in {"failed", "unknown", "skipped"}:
            code = {"failed": "17001", "unknown": "17002", "skipped": "17006"}[data["status"]]
            self._record_error(RuntimeError(str(data.get("message") or "发送未完成")), code, operation="test_scheduled_task")
        return result

    def get_scheduled_task_runs(self, account_id: str, task_id: str) -> dict[str, object]:
        return self._scheduled_call(self._scheduled_task_service().runs, account_id, task_id)

    def parse_scheduled_command(self, command: str) -> dict[str, object]:
        return self._scheduled_call(inspect_scheduled_command, command)

    def preview_scheduled_times(self, schedule: dict[str, Any]) -> dict[str, object]:
        return self._scheduled_call(lambda: {"items": next_occurrences(schedule), "text": schedule_text(schedule)})

    def set_schedule_enabled(self, job_id: str, enabled: bool) -> dict[str, object]:
        return self._legacy_write(self._schedules.set_enabled, job_id, bool(enabled))

    def update_schedule_time(self, job_id: str, value: str) -> dict[str, object]:
        return self._legacy_write(self._schedules.update_time, job_id, value)

    def delete_schedule(self, job_id: str) -> dict[str, object]:
        return self._legacy_write(self._schedules.delete, job_id)
