"""Manager diagnostic endpoints; no service start or message sending."""
from __future__ import annotations

from datetime import datetime
import threading
import time
import traceback

from .activity_service import ActivityService, diagnostic_text


class ActivityApiMixin:
    def _activity_service(self) -> ActivityService:
        return ActivityService(self._install_root, build=getattr(self, "_build_number", 0))

    def get_activity(self) -> dict:
        try:
            return self._ok(self._activity_service().recent())
        except (OSError, ValueError) as exc:
            return self._failed(exc, "19003")

    def _record_error(self, exc: BaseException, code: str, *, operation: str = "") -> tuple[str, bool]:
        details = {
            "operation": operation, "exception_type": type(exc).__name__,
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        }
        safe = diagnostic_text(details["traceback"], self._install_root)
        try:
            event = self._lifecycle.events.emit(
                "APP_ERROR", "failed", code=code,
                message=diagnostic_text(str(exc), self._install_root, limit=4000),
                details={"operation": operation, "technical": safe},
            )
            return getattr(event, "event_id", ""), True
        except OSError:
            # The original error must survive even when its log cannot be saved.
            return "", False

    def record_ui_error(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            return {"ok": False, "error": {"code": "19001", "message": "页面错误记录格式无效。"}}
        lock = getattr(self, "_ui_error_lock", None)
        if lock is None:
            self._ui_error_lock = lock = threading.Lock()
        with lock:
            now = time.monotonic()
            recent = [t for t in getattr(self, "_ui_error_times", []) if now - t < 60]
            if len(recent) >= 20:
                return {"ok": False, "error": {"code": "19001", "message": "页面错误记录过于频繁，请刷新后查看已保存记录。"}}
            self._ui_error_times = [*recent, now]
            detail = {key: str(payload.get(key) or "")[:12000] for key in ("page", "operation", "message", "stack")}
            detail["received_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            try:
                record = self._lifecycle.events.emit(
                    "APP_ERROR", "failed", code="19001",
                    message=diagnostic_text(detail["message"], self._install_root, limit=4000),
                    details={"operation": diagnostic_text(detail["operation"], self._install_root, limit=160),
                             "page": diagnostic_text(detail["page"], self._install_root, limit=160),
                             "technical": diagnostic_text(detail["stack"], self._install_root)},
                )
            except OSError:
                return {"ok": False, "error": {"code": "19003", "message": "页面错误未能保存，请截图并联系技术支持。"}}
        return self._ok({"event_id": getattr(record, "event_id", ""), "saved": True})

    def export_activity_diagnostics(self) -> dict:
        try:
            return self._ok(self._maintenance_service().export_diagnostics(
                include_activity=True, activity_build=getattr(self, "_build_number", 0),
            ))
        except (OSError, ValueError, RuntimeError) as exc:
            return self._failed(exc, "19004")
