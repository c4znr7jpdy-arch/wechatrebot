"""Subscription actions exposed by the existing local application API."""
from __future__ import annotations

from typing import Callable

from .client import LicenseClient
from .contact_client import ContactClient
from .protocol import LicenseError


class LicensingApiMixin:
    def _license_client(self) -> LicenseClient:
        client = getattr(self._lifecycle, "licenses", None)
        if client is None:
            client = getattr(self, "_licenses", None)
        if client is None:
            self._licenses = client = LicenseClient(self._install_root)
        return client

    @staticmethod
    def _license_failed(exc: LicenseError) -> dict:
        return {"ok": False, "error": {"code": f"LIC-{exc.code}", "message": str(exc)}}

    def _license_operation(self, action: Callable[[], dict]) -> dict:
        try:
            return self._ok(action())
        except LicenseError as exc:
            return self._license_failed(exc)
        except (OSError, ValueError):
            return self._license_failed(LicenseError("operation_failed", "授权操作未完成，请稍后重试。"))

    def _license_account(self, *, required: bool = False) -> str:
        try:
            account = self._history_account_id()
        except OSError:
            account = ""
        if required and not account:
            raise LicenseError("account_required", "请先连接微信，登录后再确认账号。")
        return account

    def _license_status(self) -> dict:
        account = self._license_account()
        return {**self._license_client().snapshot(account), "current_account": account}

    def get_license_status(self) -> dict:
        return self._license_operation(self._license_status)

    def prepare_license_device(self) -> dict:
        return self._license_operation(self._license_client().prepare_device)

    def preview_activation_code(self, code: str) -> dict:
        return self._license_operation(lambda: self._license_client().preview(code))

    def redeem_activation_code(self, code: str, preview_id: str) -> dict:
        def redeem() -> dict:
            self._license_client().redeem(code, preview_id)
            return self._license_status()
        return self._license_operation(redeem)

    def refresh_license(self) -> dict:
        def refresh() -> dict:
            self._license_client().refresh()
            return self._license_status()
        return self._license_operation(refresh)

    def bind_license_account(self, expected_account: str) -> dict:
        def bind() -> dict:
            actual = self._license_account(required=True)
            if not isinstance(expected_account, str) or expected_account != actual:
                raise LicenseError("account_changed", "微信登录账号已变化，请重新查看并确认绑定。")
            self._license_client().bind(actual)
            return self._license_status()
        return self._license_operation(bind)

    def _check_license_start(self) -> None:
        client = self._license_client()
        status = client.snapshot()
        if not status["enabled"]:
            return
        actual = self._license_account(required=True)
        status = client.snapshot(actual)
        if not status["authorized"]:
            raise LicenseError(status["state"], status["message"])
        if not status["account"] or status["account"] != actual:
            raise LicenseError("account_mismatch", "请先连接并确认授权绑定的微信账号。")

    def connect_license_wechat(self) -> dict:
        if self._mandatory_update:
            return self.start_robot()  # Reuse the existing mandatory-update response.
        try:
            status = self._license_client().snapshot()
            if not status["enabled"] or not status["can_connect"]:
                raise LicenseError(status["state"], status["message"])
        except LicenseError as exc:
            return self._license_failed(exc)
        return self._queue_robot_action("connect_wechat", "正在连接微信，请完成登录。", "12001")

    def get_license_contact(self, force: bool = False) -> dict:
        def read_contact() -> dict:
            client = getattr(self, "_license_contacts", None)
            if client is None:
                self._license_contacts = client = ContactClient(self._install_root)
            return client.snapshot(force=force)
        return self._license_operation(read_contact)
