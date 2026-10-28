from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError, URLError
import sqlite3

from .plugin_errors import ProviderTestError


DEFAULT_ERROR_CODE = "19000"
_PROVIDER_ERROR_CODES = {
    "AuthenticationError": "16002", "PermissionDeniedError": "16003",
    "RateLimitError": "16004", "APITimeoutError": "16007",
    "TimeoutError": "16007", "ReadTimeout": "16007", "ConnectTimeout": "16007",
    "WriteTimeout": "16007", "PoolTimeout": "16007",
    "APIConnectionError": "16006", "ConnectError": "16006",
    "InternalServerError": "16008", "NotFoundError": "16009",
    "BadRequestError": "16010", "UnprocessableEntityError": "16010",
    "EmptyModelResponseError": "16012",
}


def provider_error_code(error_type: str, status_code: int | None = None) -> str:
    """Classify explicit provider exception metadata, never arbitrary error prose."""
    code = _PROVIDER_ERROR_CODES.get(str(error_type).rsplit(".", 1)[-1], "")
    if code:
        return code
    if isinstance(status_code, int) and not isinstance(status_code, bool) and 400 <= status_code <= 599:
        return {401: "16002", 403: "16003", 404: "16009", 429: "16004"}.get(
            status_code, "16008" if status_code >= 500 else "16010"
        )
    return ""


class AccountStatusUnavailableError(OSError):
    """The live account could not be verified; this is not a logout."""


def classify_error(exc: BaseException, fallback: str, operation: str) -> str:
    """Classify evidence from typed exceptions, never guess from arbitrary prose."""
    if isinstance(exc, AccountStatusUnavailableError):
        return "11007"
    if type(exc).__name__ == "ConfigConflictError":
        return "15005"
    if operation in {"test_provider", "discover_provider_models"}:
        cause: BaseException | None = exc
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, ProviderTestError):
                return cause.error_code
            if isinstance(cause, HTTPError):
                return provider_error_code(type(cause).__name__, cause.code) or DEFAULT_ERROR_CODE
            if isinstance(cause, TimeoutError):
                return "16007"
            if isinstance(cause, URLError):
                return "16007" if isinstance(cause.reason, TimeoutError) else "16006"
            cause = cause.__cause__ or cause.__context__
        return "19000"
    if isinstance(exc, sqlite3.Error):
        return "15007"
    if fallback == "15001" and operation.startswith("get_"):
        return "15003"
    return {"create_backup": "15009", "restore_backup": "15010", "export_diagnostics": "19004", "get_diagnostics": "19003"}.get(operation, fallback)


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    code: str
    title: str
    message: str
    solutions: tuple[str, ...]
    retryable: bool = True
    level: str = "error"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "title": self.title,
            "message": self.message,
            "solutions": list(self.solutions),
            "retryable": self.retryable,
            "level": self.level,
        }


class ErrorCatalog:
    def __init__(self, values: Mapping[str, ErrorDefinition]) -> None:
        self._values = dict(values)
        if DEFAULT_ERROR_CODE not in self._values:
            raise ValueError(f"错误码配置缺少 {DEFAULT_ERROR_CODE}。")

    def get(self, code: str | int) -> ErrorDefinition:
        return self._values.get(str(code), self._values[DEFAULT_ERROR_CODE])

    def to_dict(self) -> dict[str, dict[str, object]]:
        return {code: item.to_dict() for code, item in self._values.items()}


def _read_catalog(path: Path) -> dict[str, ErrorDefinition]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_errors = payload.get("errors") if isinstance(payload, dict) else None
    if payload.get("schema_version") != 1 or not isinstance(raw_errors, dict):
        raise ValueError(f"错误码配置格式不正确：{path}")
    values: dict[str, ErrorDefinition] = {}
    for raw_code, raw in raw_errors.items():
        code = str(raw_code)
        if not code.isdigit() or len(code) != 5 or not isinstance(raw, dict):
            raise ValueError(f"错误码配置项无效：{raw_code}")
        title = str(raw.get("title") or "").strip()
        message = str(raw.get("message") or "").strip()
        solutions = raw.get("solutions", [])
        if not title or not message or not isinstance(solutions, list):
            raise ValueError(f"错误码 {code} 缺少必要说明。")
        values[code] = ErrorDefinition(
            code=code,
            title=title,
            message=message,
            solutions=tuple(
                str(item).strip() for item in solutions if str(item).strip()
            ),
            retryable=bool(raw.get("retryable", True)),
            level=str(raw.get("level", "error")) if raw.get("level", "error") in {"info", "warning", "error"} else "error",
        )
    return values


def load_error_catalog(install_root: str | Path | None = None) -> ErrorCatalog:
    packaged = Path(__file__).with_name("error-codes.zh-CN.json")
    values = _read_catalog(packaged)
    if install_root is not None:
        override = (
            Path(install_root).expanduser().resolve()
            / "config"
            / "error-codes.zh-CN.json"
        )
        if override.is_file():
            values.update(_read_catalog(override))
    return ErrorCatalog(values)


__all__ = [
    "DEFAULT_ERROR_CODE",
    "ErrorCatalog",
    "ErrorDefinition",
    "load_error_catalog",
    "provider_error_code",
]
