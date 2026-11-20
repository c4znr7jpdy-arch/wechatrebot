"""Bounded model test history, scoped to the exact configuration tested."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import threading
from typing import TYPE_CHECKING
from uuid import uuid4

from .json_io import write_json_atomic

if TYPE_CHECKING:
    from .customer_config import ProviderService


_LOCK = threading.RLock()
_LATEST_ATTEMPTS: dict[tuple[Path, str], str] = {}
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderTestAttempt:
    provider_id: str
    fingerprint: str
    token: str


class ProviderTestHistoryError(OSError):
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        super().__init__("本次测试已完成，但测试记录未能保存。")


def _path(root: str | Path) -> Path:
    return Path(root).resolve() / "data/manager/provider-test-results.json"


def _fingerprint(service: ProviderService) -> str:
    # Credentials participate only in this one-way configuration digest. Neither
    # the payload nor the digest is included in the public model catalog.
    payload = {
        "id": service.provider_id, "model": service.model,
        "api_base": service.api_base, "api_key": service.api_key,
        "service_kind": service.service_kind,
        "thinking_enabled": service.thinking_enabled,
        "modalities": service.modalities,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _records(path: Path) -> dict[str, object]:
    try:
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _LOGGER.warning("Model test history could not be read; prior results are unavailable.")
        return {}
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or not isinstance(payload.get("results"), dict)):
        _LOGGER.warning("Model test history has an invalid format; prior results are unavailable.")
        return {}
    return payload["results"]


def _public_result(record: object) -> dict[str, object] | None:
    if not isinstance(record, dict) or record.get("status") not in ("success", "error"):
        return None
    elapsed = record.get("elapsed_ms")
    if (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
            or elapsed < 0 or elapsed > 86400000 or not math.isfinite(elapsed)):
        return None
    tested_at = record.get("tested_at")
    error_code = record.get("error_code")
    if not isinstance(tested_at, str) or not isinstance(error_code, str):
        return None
    try:
        timestamp = datetime.fromisoformat(tested_at)
    except ValueError:
        return None
    if timestamp.tzinfo is None or not re.fullmatch(r"(?:\d{5})?", error_code):
        return None
    return {"status": record["status"], "elapsed_ms": elapsed,
            "tested_at": tested_at, "error_code": error_code}


def load_provider_test_results(
    root: str | Path, services: Iterable[ProviderService],
) -> dict[str, dict[str, object]]:
    with _LOCK:
        records = _records(_path(root))
        results = {}
        for service in services:
            record = records.get(service.provider_id)
            if isinstance(record, dict) and record.get("fingerprint") == _fingerprint(service):
                public = _public_result(record)
                if public is not None:
                    results[service.provider_id] = public
        return results


def begin_provider_test(root: str | Path, service: ProviderService) -> ProviderTestAttempt:
    attempt = ProviderTestAttempt(service.provider_id, _fingerprint(service), uuid4().hex)
    with _LOCK:
        _LATEST_ATTEMPTS[(_path(root), service.provider_id)] = attempt.token
    return attempt


def finish_provider_test(
    root: str | Path, attempt: ProviderTestAttempt, services: Iterable[ProviderService], *,
    status: str, elapsed_ms: float, error_code: str = "",
) -> dict[str, object] | None:
    """Commit only the newest attempt against an unchanged, existing service."""
    path = _path(root)
    key = (path, attempt.provider_id)
    with _LOCK:
        if _LATEST_ATTEMPTS.get(key) != attempt.token:
            return None
        try:
            current = {service.provider_id: _fingerprint(service) for service in services}
            if current.get(attempt.provider_id) != attempt.fingerprint:
                return None
            result = {"status": status, "elapsed_ms": round(elapsed_ms, 1),
                      "tested_at": datetime.now(timezone.utc).isoformat(), "error_code": error_code}
            if _public_result(result) is None:
                raise ValueError("模型测试记录格式无效。")
            records = {
                provider_id: record for provider_id, record in _records(path).items()
                if isinstance(record, dict) and current.get(provider_id) == record.get("fingerprint")
                and _public_result(record) is not None
            }
            records[attempt.provider_id] = {**result, "fingerprint": attempt.fingerprint}
            try:
                write_json_atomic(path, {"schema_version": 1, "results": records})
            except OSError as exc:
                raise ProviderTestHistoryError(result) from exc
            return result
        finally:
            _LATEST_ATTEMPTS.pop(key, None)
