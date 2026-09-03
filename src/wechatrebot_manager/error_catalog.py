from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping


DEFAULT_ERROR_CODE = "19000"


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    code: str
    title: str
    message: str
    solutions: tuple[str, ...]
    retryable: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "title": self.title,
            "message": self.message,
            "solutions": list(self.solutions),
            "retryable": self.retryable,
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
]
