"""Activation-code client shared by the manager and the independent runtime."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..single_instance import SingleInstanceLock
from .cache import LeaseCache, empty_state
from .config import LicenseChannel, load_channel
from .device import DeviceIdentity, Protection, WindowsProtection, boot_session_id, load_device
from .protocol import (
    ACCOUNT_PATTERN, CODE_MODES, PLANS, LicenseError, canonical, display_time, integer,
    parse_json, sign_request, verify_lease,
)


REFRESH_SECONDS = 1800
CLOCK_ROLLBACK_TOLERANCE = 5
_REMOTE_DENIALS = {"license_unavailable", "license_revoked", "subscription_expired"}
_ERROR_MESSAGES = {
    "code_unavailable": "激活码不可用，请检查输入或向 QQ 群管理员领取适用的激活码。",
    "code_expired": "激活码已过领取期限，请联系 QQ 群管理员。",
    "code_stale": "授权绑定已变更，请联系 QQ 群管理员重新领取迁移码。",
    "preview_required": "请先预览激活码，再确认兑换。",
    "preview_stale": "预览已失效，请重新预览并确认兑换。",
    "trial_used": "此设备或微信账号已使用过试用，请领取月付或年付激活码。",
    "license_unavailable": "当前设备授权不可用，请联系管理员恢复或迁移授权。",
    "license_revoked": "授权已撤销，自动化已暂停，请联系管理员。",
    "subscription_expired": "订阅已到期，请加入 QQ 群领取续期码。",
    "account_bound": "授权已绑定其他微信账号，请联系管理员处理换绑。",
    "account_unavailable": "此微信账号无法绑定当前授权，请联系管理员。",
    "active_license_conflict": "本机已有有效付费授权，请联系管理员处理，不能直接替换。",
    "invalid_account": "请先连接微信并确认实际登录账号。",
    "invalid_request": "设备请求验证失败，请校准系统时间后重试。",
    "request_replayed": "请求已失效，请重试。",
    "rate_limited": "请求过于频繁，请稍后重试。",
    "invalid_data": "授权请求格式无效，请检查输入。",
    "service_unavailable": "授权服务暂时无法连接，有效离线凭证仍可使用。",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Activation codes and device requests must stay at the configured service.
        raise LicenseError("service_unavailable", _ERROR_MESSAGES["service_unavailable"])


def http_transport(url: str, payload: dict) -> dict:
    request = Request(url, data=canonical(payload), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
    })
    try:
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError()
        return parse_json(raw)
    except (OSError, URLError, ValueError) as exc:
        raise LicenseError("service_unavailable", _ERROR_MESSAGES["service_unavailable"]) from exc


class LicenseClient:
    def __init__(self, root: str | Path, *, transport=None, clock=None,
                 protection: Protection | None = None):
        self.root = Path(root).resolve()
        self.directory = self.root / "data" / "licensing"
        self.transport = transport or http_transport
        self.clock = clock or time
        self._boot_reader = getattr(self.clock, "boot_id", None) or boot_session_id
        self.protection = protection or WindowsProtection()
        self.cache = LeaseCache(self.directory, self.protection)
        self.lock_key = f"licensing:{self.root}"

    @contextmanager
    def _locked(self):
        lock = SingleInstanceLock(self.lock_key)
        try:
            try:
                acquired = lock.acquire()
            except OSError as exc:
                raise LicenseError("storage_error", "无法锁定授权记录，请检查当前用户权限。") from exc
            if not acquired:
                raise LicenseError("busy", "授权操作正在进行，请稍后重试。")
            yield
        finally:
            try:
                lock.release()
            except OSError as exc:
                raise LicenseError("storage_error", "无法释放授权记录锁，请稍后重试。") from exc

    @staticmethod
    def _channel_required(channel: LicenseChannel) -> None:
        if not channel.enabled:
            raise LicenseError("licensing_disabled", "此安装尚未启用订阅授权。")
        if not channel.ready:
            raise LicenseError("configuration_pending", "授权服务尚未配置，请等待维护者配置服务后再激活。")

    def _load(self, *, create: bool = False) -> tuple[LicenseChannel, DeviceIdentity | None, dict, dict | None]:
        channel = load_channel(self.root)
        if not channel.ready:
            return channel, None, empty_state(""), None
        device = load_device(self.directory, self.protection, create=create)
        if device is None:
            return channel, None, empty_state(""), None
        state = self.cache.read(device.device_id)
        payload = None
        if state["lease"] is not None:
            payload = verify_lease(state["lease"], channel.public_keys, device.device_id)
            if payload["revision"] != state["revision"] or payload["issued_at"] != state["issued_at"]:
                raise LicenseError("lease_rollback", "授权记录版本不一致，请重新联网验证。")
        return channel, device, state, payload

    def _now(self, state: dict) -> tuple[float, bool]:
        wall = self.clock.time()
        if not state["issued_at"]:
            return wall, False
        wall_delta = wall - state["wall_time"]
        mono_delta = self.clock.monotonic() - state["monotonic_time"]
        # A restored cache must not become younger after a reboot and wall-clock
        # rollback. Offline use is restricted to the same verified Windows boot.
        elapsed = max(0, wall_delta, mono_delta)
        try:
            same_boot = self._boot_id() == state["boot_id"]
        except LicenseError:
            same_boot = False
        rollback = (wall_delta < -CLOCK_ROLLBACK_TOLERANCE or mono_delta < 0
                    or not same_boot or state["requires_online"])
        return state["trusted_time"] + elapsed, bool(rollback)

    def _boot_id(self) -> str:
        try:
            value = self._boot_reader()
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
                raise ValueError()
            return value
        except (OSError, ValueError) as exc:
            raise LicenseError(
                "boot_identity", "无法确认 Windows 启动会话，自动化已暂停；请修复系统环境后联网验证。"
            ) from exc

    @staticmethod
    def _reason(state: dict, payload: dict | None, now: float, clock_error: bool, account: str = "") -> str:
        if payload is None:
            return "unactivated"
        if clock_error:
            return "clock_error"
        if state["remote_denial"]:
            return state["remote_denial"]
        if payload["status"] == "revoked":
            return "revoked"
        if now >= payload["valid_until"]:
            return "expired"
        if now >= payload["lease_until"]:
            return "offline_expired"
        if account and payload["account"] and account != payload["account"]:
            return "account_mismatch"
        return ""

    @staticmethod
    def _grant_matches(grant: dict | None, payload: dict, now: float) -> bool:
        return bool(grant and grant["license_id"] == payload["license_id"]
                    and grant["device_id"] == payload["device"]
                    and grant["account"] == payload["account"]
                    and grant["revision"] == payload["revision"]
                    and now < grant["valid_until"])

    def _maintain(self, state: dict, payload: dict | None, account: str = "", *, persist: bool = True) -> None:
        if payload is None:
            return
        now, clock_error = self._now(state)
        reason = self._reason(state, payload, now, clock_error, account)
        changed = False
        if reason:
            changed = bool(state["run_grant"] or state["pause_reason"] != reason)
            state["run_grant"] = None
            state["pause_reason"] = reason
        if clock_error and not state["requires_online"]:
            state["requires_online"] = True
            state["refresh_after"] = 0
            state["refresh_failures"] = 0
            changed = True
        if not clock_error and now >= state["trusted_time"] + 30:
            state.update(trusted_time=now, wall_time=self.clock.time(), monotonic_time=self.clock.monotonic())
            changed = True
        if changed and persist:
            self.cache.write(state)

    @staticmethod
    def _base(channel: LicenseChannel | None = None) -> dict:
        return {
            "enabled": channel.enabled if channel else True,
            "configured": channel.ready if channel else False,
            "state": "unactivated", "message": "请加入 QQ 群领取激活码，并在此预览、确认兑换。",
            "authorized": False, "can_connect": False, "can_run": False,
            "account": "", "license_id": "", "device_id": "", "plan": "",
            "valid_until": 0, "lease_until": 0, "expires_text": "", "remaining_seconds": 0,
            "qq_group": channel.qq_group if channel else "", "qq_url": channel.qq_url if channel else "",
            "plans": [{"id": key, **spec} for key, spec in PLANS.items()],
            "last_error": "", "refresh_after": 0, "run_id": "", "run_granted_at": 0,
        }

    def _snapshot(self, channel: LicenseChannel, device: DeviceIdentity | None, state: dict,
                  payload: dict | None, account: str = "", *, persist: bool = True) -> dict:
        result = self._base(channel)
        if not channel.enabled:
            result.update(state="legacy", message="此安装尚未启用订阅授权。", can_connect=True, can_run=True)
            return result
        if not channel.ready:
            result.update(state="configuration_pending", message="授权服务尚未配置，请等待维护者完成配置。")
            return result
        if device:
            result["device_id"] = device.device_id
        if payload is None:
            return result
        self._maintain(state, payload, account, persist=persist)
        now, clock_error = self._now(state)
        reason = self._reason(state, payload, now, clock_error, account)
        authorized = not reason or reason == "account_mismatch"
        can_run = bool(authorized and account and account == payload["account"]
                       and self._grant_matches(state["run_grant"], payload, now))
        result.update(
            authorized=authorized, can_connect=authorized, can_run=can_run,
            account=payload["account"], license_id=payload["license_id"], plan=payload["plan"],
            valid_until=payload["valid_until"], lease_until=payload["lease_until"],
            expires_text=display_time(payload["valid_until"]),
            remaining_seconds=max(0, int(payload["valid_until"] - now)),
            last_error=state["last_error"], refresh_after=state["refresh_after"],
            run_id=state["run_grant"]["run_id"] if can_run else "",
            run_granted_at=state["run_grant"]["granted_at"] if can_run else 0,
        )
        if reason:
            messages = {
                "expired": "订阅已到期，自动化已暂停；数据保留，续期后请手动恢复。",
                "offline_expired": "离线授权已过期，自动化已暂停；联网验证后请手动恢复。",
                "revoked": "授权已撤销，自动化已暂停，请联系管理员。",
                "clock_error": "系统已重启或时间校验异常，自动化已暂停；请校准时间并联网验证。",
                "account_mismatch": "实际登录微信与授权绑定账号不一致，自动化已暂停。",
                **_ERROR_MESSAGES,
            }
            result.update(state=reason, message=messages.get(reason, "授权暂不可用，请重新验证。"))
        elif not payload["account"]:
            result.update(state="active_unbound", message="设备已激活，请连接微信并确认绑定实际账号。")
        elif can_run:
            result.update(state="running", message="授权有效，自动化运行许可已开启。")
        elif state["pause_reason"]:
            result.update(state="paused", message="授权有效，自动化已暂停；请手动启动恢复。")
        else:
            result.update(state="ready", message="授权成功，请手动启动自动化。")
        if authorized and state["last_error"] == "service_unavailable":
            result["message"] += " 授权服务暂不可达，当前有效离线凭证仍可使用。"
        return result

    def snapshot(self, account: str = "") -> dict:
        """Read local status only; neither network nor first-use device creation."""
        try:
            try:
                with self._locked():
                    return self._snapshot(*self._load(), account)
            except LicenseError as exc:
                if exc.code != "busy":
                    raise
                # Network refresh can hold the writer lock. Atomic files still give
                # the independent runtime a coherent, locally verifiable lease.
                return self._snapshot(*self._load(), account, persist=False)
        except LicenseError as exc:
            try:
                channel = load_channel(self.root)
            except LicenseError:
                channel = None
            result = self._base(channel)
            result.update(state=exc.code, message=str(exc))
            return result

    @staticmethod
    def _code(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", value.strip()):
            raise LicenseError("invalid_data", "请输入完整的激活码。")
        return value.strip()

    def _request(self, channel: LicenseChannel, device: DeviceIdentity, state: dict,
                 action: str, body: dict) -> tuple[dict, str, float]:
        self._boot_id()  # Do not consume an activation code when boot identity cannot be verified.
        now, clock_error = self._now(state)
        nonce = secrets.token_hex(16)
        signed = sign_request(device.key, action, body, int(self.clock.time() if clock_error else now), nonce)
        started = self.clock.monotonic()
        try:
            response = self.transport(f"{channel.service_url}/v1/{action}", signed)
        except (OSError, TimeoutError) as exc:
            raise LicenseError("service_unavailable", _ERROR_MESSAGES["service_unavailable"]) from exc
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise LicenseError("invalid_response", "授权服务响应无效，请稍后重试。")
        if not response["ok"]:
            error = response.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or code not in _ERROR_MESSAGES:
                code = "service_unavailable"
            if code in _REMOTE_DENIALS and action in {"refresh", "bind"}:
                state.update(remote_denial=code, run_grant=None, pause_reason=code)
                self.cache.write(state)
            raise LicenseError(code, _ERROR_MESSAGES[code])
        data = response.get("data")
        if not isinstance(data, dict):
            raise LicenseError("invalid_response", "授权服务响应无效，请稍后重试。")
        return data, nonce, max(0, self.clock.monotonic() - started)

    def preview(self, code: str) -> dict:
        code = self._code(code)
        with self._locked():
            channel, device, state, payload = self._load(create=True)
            self._channel_required(channel)
            body = {"code": code}
            if payload:
                body["license_id"] = payload["license_id"]
            data, _, _ = self._request(channel, device, state, "preview", body)
            try:
                if (not isinstance(data["preview_id"], str) or not 1 <= len(data["preview_id"]) <= 256
                        or data["plan"] not in PLANS or type(data["already_redeemed"]) is not bool
                        or data["mode"] not in CODE_MODES
                        or not isinstance(data["license_id"], str) or len(data["license_id"]) > 128
                        or not isinstance(data.get("replaces_license_id", ""), str)):
                    raise ValueError()
                integer(data["valid_until"])
                integer(data["expires_at"])
                replaced = data.get("replaces_license_id", "")
                if replaced and (data["mode"] != "migration" or not payload
                                 or replaced != payload["license_id"] or not data["license_id"]
                                 or data["license_id"] == replaced):
                    raise ValueError()
            except (ValueError, TypeError, KeyError) as exc:
                raise LicenseError("invalid_response", "激活码预览无效，请重新预览。") from exc
            result = {key: data[key] for key in (
                "preview_id", "plan", "mode", "valid_until", "license_id", "already_redeemed", "expires_at",
            )} | {"message": str(data.get("message", ""))[:300], "replaces_license_id": replaced}
            state["pending_preview"] = {
                **{key: result[key] for key in (
                    "preview_id", "mode", "license_id", "replaces_license_id", "plan", "valid_until", "expires_at",
                )},
                "code_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "current_license_id": payload["license_id"] if payload else "",
            }
            self._maintain(state, payload, persist=False)
            self.cache.write(state)
            return result

    def prepare_device(self) -> dict:
        """Explicitly create a device fingerprint before requesting a migration code."""
        with self._locked():
            channel, device, state, payload = self._load(create=True)
            self._channel_required(channel)
            return self._snapshot(channel, device, state, payload, payload["account"] if payload else "")

    @staticmethod
    def _response_payload(channel: LicenseChannel, device: DeviceIdentity, state: dict, old: dict | None,
                          data: dict, nonce: str, account: str, approved: dict | None) -> tuple[dict, bool]:
        envelope = data.get("lease")
        if not isinstance(envelope, dict):
            raise LicenseError("invalid_lease", "授权服务未返回有效凭证，请重新验证。")
        payload = verify_lease(envelope, channel.public_keys, device.device_id)
        if payload["request_nonce"] != nonce:
            raise LicenseError("lease_replayed", "授权响应已失效，请重新验证。")
        switching = bool(old and payload["license_id"] != old["license_id"])
        permitted_switch = bool(approved and approved["mode"] == "migration" and old
                                and approved["replaces_license_id"] == old["license_id"]
                                and approved["license_id"] == payload["license_id"])
        if (switching and not permitted_switch) or (account and payload["account"] != account):
            raise LicenseError("identity_mismatch", "返回的授权身份不匹配，请重新验证。")
        if approved and approved["mode"] == "migration" and (
            payload["license_id"] != approved["license_id"] or payload["plan"] != approved["plan"]
            or payload["valid_until"] != approved["valid_until"]
        ):
            raise LicenseError("migration_mismatch", "迁入授权与已确认的预览不一致，请重新预览。")
        if (not switching and payload["revision"] < state["revision"]) or payload["issued_at"] < state["issued_at"]:
            raise LicenseError("lease_rollback", "授权响应版本已过期，请重新验证。")
        if old and not switching and payload["revision"] == state["revision"]:
            if any(payload[field] != old[field] for field in ("plan", "status", "valid_until", "account")):
                raise LicenseError("lease_rollback", "授权响应版本不一致，请重新验证。")
        return payload, switching

    def _accept(self, channel: LicenseChannel, device: DeviceIdentity, state: dict,
                old: dict | None, data: dict, nonce: str, round_trip: float, *, account: str = "",
                approved: dict | None = None) -> dict:
        boot_id = self._boot_id()
        payload, switching = self._response_payload(channel, device, state, old, data, nonce, account, approved)
        self._maintain(state, old)
        now, clock_error = self._now(state)
        grant = state["run_grant"]
        migrating = bool(approved and approved["mode"] == "migration")
        preserve_run = bool(old and not switching and not migrating
                            and not self._reason(state, old, now, clock_error)
                            and self._grant_matches(grant, old, now)
                            and payload["account"] == old["account"]
                            and payload["status"] == "active"
                            and payload["issued_at"] < payload["lease_until"])
        if preserve_run:
            grant.update(revision=payload["revision"], valid_until=payload["valid_until"])
        else:
            grant = None
        state.update(
            lease=data["lease"], revision=payload["revision"], issued_at=payload["issued_at"],
            # Count the whole request round trip conservatively: transmission
            # latency must never buy extra seconds after subscription expiry.
            trusted_time=payload["issued_at"] + round_trip,
            wall_time=self.clock.time(), monotonic_time=self.clock.monotonic(),
            boot_id=boot_id,
            requires_online=False, remote_denial="", last_error="", refresh_failures=0,
            refresh_after=max(payload["issued_at"] + 60,
                              min(payload["issued_at"] + REFRESH_SECONDS, payload["lease_until"] - 60)),
            run_grant=grant,
        )
        if migrating:
            state["pause_reason"] = "migration"
        self._maintain(state, payload, persist=False)
        self.cache.write(state)
        return self._snapshot(channel, device, state, payload, account or payload["account"])

    def redeem(self, code: str, preview_id: str) -> dict:
        code = self._code(code)
        if not isinstance(preview_id, str) or not 1 <= len(preview_id) <= 256:
            raise LicenseError("preview_required", _ERROR_MESSAGES["preview_required"])
        with self._locked():
            channel, device, state, payload = self._load()
            self._channel_required(channel)
            if not device:
                raise LicenseError("preview_required", _ERROR_MESSAGES["preview_required"])
            approved = state["pending_preview"]
            if (not approved or approved["preview_id"] != preview_id
                    or approved["code_hash"] != hashlib.sha256(code.encode("utf-8")).hexdigest()):
                raise LicenseError("preview_required", _ERROR_MESSAGES["preview_required"])
            current_id = payload["license_id"] if payload else ""
            if approved["replaces_license_id"] and current_id not in {
                approved["replaces_license_id"], approved["license_id"],
            }:
                raise LicenseError("preview_stale", _ERROR_MESSAGES["preview_stale"])
            data, nonce, elapsed = self._request(channel, device, state, "redeem", {"code": code, "preview_id": preview_id})
            return self._accept(channel, device, state, payload, data, nonce, elapsed, approved=approved)

    def _refresh(self, *, force: bool) -> dict:
        with self._locked():
            channel, device, state, payload = self._load()
            if not force and (not channel.ready or payload is None):
                return self._snapshot(channel, device, state, payload)
            self._channel_required(channel)
            if not device or payload is None:
                raise LicenseError("unactivated", "请先使用激活码激活设备。")
            self._maintain(state, payload)
            now, _ = self._now(state)
            if not force and now < state["refresh_after"]:
                return self._snapshot(channel, device, state, payload, payload["account"])
            try:
                data, nonce, elapsed = self._request(channel, device, state, "refresh", {"license_id": payload["license_id"]})
                return self._accept(channel, device, state, payload, data, nonce, elapsed)
            except LicenseError as exc:
                self._maintain(state, payload, persist=False)
                state["refresh_failures"] = min(state["refresh_failures"] + 1, 20)
                state["refresh_after"] = now + min(REFRESH_SECONDS, 30 * 2 ** (state["refresh_failures"] - 1))
                state["last_error"] = exc.code
                self.cache.write(state)
                raise

    def refresh(self) -> dict:
        return self._refresh(force=True)

    def refresh_if_due(self) -> dict:
        """Call from runtime maintenance, not from manager status polling."""
        try:
            return self._refresh(force=False)
        except LicenseError:
            return self.snapshot()

    def bind(self, account: str) -> dict:
        if not isinstance(account, str) or not ACCOUNT_PATTERN.fullmatch(account):
            raise LicenseError("invalid_account", _ERROR_MESSAGES["invalid_account"])
        with self._locked():
            channel, device, state, payload = self._load()
            self._channel_required(channel)
            if not device or payload is None:
                raise LicenseError("unactivated", "请先使用激活码激活设备。")
            data, nonce, elapsed = self._request(channel, device, state, "bind", {
                "license_id": payload["license_id"], "account": account,
            })
            return self._accept(channel, device, state, payload, data, nonce, elapsed, account=account)

    def authorize_run(self, account: str) -> dict:
        with self._locked():
            channel, device, state, payload = self._load()
            if not channel.enabled:
                return self._snapshot(channel, device, state, payload, account)
            self._channel_required(channel)
            self._maintain(state, payload, account)
            now, clock_error = self._now(state)
            reason = self._reason(state, payload, now, clock_error, account)
            if reason or not account or not payload["account"]:
                result = self._snapshot(channel, device, state, payload, account)
                raise LicenseError(reason or "account_required", result["message"] if reason else "请先连接并绑定实际微信账号。")
            if not self._grant_matches(state["run_grant"], payload, now):
                state["run_grant"] = {
                    "license_id": payload["license_id"], "device_id": device.device_id,
                    "account": account, "valid_until": payload["valid_until"],
                    "revision": payload["revision"], "granted_at": int(now), "run_id": secrets.token_hex(16),
                }
            state["pause_reason"] = ""
            self.cache.write(state)
            return self._snapshot(channel, device, state, payload, account)

    def pause(self) -> dict:
        with self._locked():
            channel, device, state, payload = self._load()
            if device and payload:
                state.update(run_grant=None, pause_reason="user")
                self.cache.write(state)
            return self._snapshot(channel, device, state, payload, payload["account"] if payload else "")


__all__ = ["LicenseClient"]
