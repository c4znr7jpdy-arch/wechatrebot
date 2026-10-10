"""Shared errors for plugin configuration and the local engine API."""
from http import HTTPStatus


class PluginCenterError(RuntimeError):
    status_code = HTTPStatus.BAD_REQUEST


class EngineUnavailableError(PluginCenterError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE


class ConfigConflictError(PluginCenterError):
    status_code = HTTPStatus.CONFLICT
