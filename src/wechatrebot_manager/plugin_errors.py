"""Shared errors for plugin configuration and the local engine API."""
from http import HTTPStatus

from .customer_logs import sanitize_log_text


class PluginCenterError(RuntimeError):
    status_code = HTTPStatus.BAD_REQUEST


class EngineUnavailableError(PluginCenterError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE


class ConfigConflictError(PluginCenterError):
    status_code = HTTPStatus.CONFLICT


class ProviderTestError(PluginCenterError):
    """A model probe failed or did not prove a usable model response."""

    def __init__(self, message: str, *, code: str = "16012") -> None:
        self.error_code = code
        super().__init__(sanitize_log_text(message).strip()[:4000])
