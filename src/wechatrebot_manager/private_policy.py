"""Account-scoped feature availability for private WeChat conversations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any

from .feature_policy import DeliveryFeature, load_delivery_features
from .json_io import write_json_atomic


_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
_CONTACT_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_MODULE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_STORE_LOCK = threading.RLock()


class PrivatePolicyError(ValueError):
    """A safe, user-facing private-chat policy error."""


@dataclass(frozen=True, slots=True)
class PrivateChatPolicy:
    allowed_features: tuple[str, ...] = ()
    updated_at: str = ""
    updated_by: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_features": list(self.allowed_features),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class PrivateChatPolicyService:
    """Persist private-chat feature scopes without retaining contact profiles."""

    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.path = (
            self.install_root / "data" / "manager" / "private-chat-policies.json"
        )

    @staticmethod
    def _validate_scope(account_id: str, contact_id: str) -> tuple[str, str]:
        account = str(account_id or "").strip()
        contact = str(contact_id or "").strip()
        if not _ACCOUNT_ID.fullmatch(account):
            raise PrivatePolicyError("机器人账号标识无效，请重新连接微信后刷新联系人。")
        if (
            not _CONTACT_ID.fullmatch(contact)
            or contact.endswith("@chatroom")
        ):
            raise PrivatePolicyError("联系人标识无效，请从当前账号的联系人列表重新选择。")
        return account, contact

    def _features(self) -> tuple[DeliveryFeature, ...]:
        return load_delivery_features(self.install_root) or ()

    def _default_features(self) -> tuple[str, ...]:
        # An upgrade must not unexpectedly disable existing private-chat
        # capabilities. New contacts therefore inherit the globally enabled
        # delivery set until an operator saves an explicit contact scope.
        return tuple(
            feature.module
            for feature in self._features()
            if feature.visible and feature.default_enabled
        )

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"schema_version": 1, "accounts": {}, "audit": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise PrivatePolicyError("私聊功能范围无法读取，原配置未被修改。") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("accounts"), dict)
            or not isinstance(payload.get("audit", []), list)
        ):
            raise PrivatePolicyError("私聊功能范围格式不正确，原配置未被修改。")
        payload.setdefault("audit", [])
        return payload

    @staticmethod
    def _raw_policy(
        payload: dict[str, Any], account_id: str, contact_id: str
    ) -> dict[str, Any]:
        account = payload.get("accounts", {}).get(account_id)
        contacts = account.get("contacts") if isinstance(account, dict) else None
        raw = contacts.get(contact_id) if isinstance(contacts, dict) else None
        return raw if isinstance(raw, dict) else {}

    def _policy(self, raw: dict[str, Any]) -> PrivateChatPolicy:
        allowed = raw.get("allowed_features")
        configured = isinstance(allowed, list)
        values = allowed if configured else self._default_features()
        features = tuple(
            item
            for item in values
            if isinstance(item, str) and _MODULE_ID.fullmatch(item)
        )
        return PrivateChatPolicy(
            allowed_features=tuple(dict.fromkeys(features)),
            updated_at=str(raw.get("updated_at") or ""),
            updated_by=str(raw.get("updated_by") or ""),
        )

    @staticmethod
    def _revision(policy: PrivateChatPolicy) -> str:
        canonical = json.dumps(policy.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def _public(
        self,
        account_id: str,
        contact_id: str,
        policy: PrivateChatPolicy,
        *,
        configured: bool,
    ) -> dict[str, object]:
        return {
            "account_id": account_id,
            "contact_id": contact_id,
            "configured": configured,
            **policy.to_dict(),
            "revision": self._revision(policy),
        }

    def overview(self, account_id: str, contact_id: str) -> dict[str, object]:
        account, contact = self._validate_scope(account_id, contact_id)
        with _STORE_LOCK:
            payload = self._read()
            raw = self._raw_policy(payload, account, contact)
        policy = self._policy(raw)
        allowed = set(policy.allowed_features)
        features = [
            {
                "id": feature.module,
                "name": feature.display_name,
                "allowed": feature.module in allowed,
                "globally_enabled_by_default": feature.default_enabled,
            }
            for feature in self._features()
            if feature.visible
        ]
        return {
            "policy": self._public(
                account, contact, policy, configured=bool(raw)
            ),
            "features": features,
        }

    def save(
        self,
        account_id: str,
        contact_id: str,
        values: dict[str, Any],
        *,
        expected_revision: str | None = None,
        actor_id: str = "local-console",
    ) -> dict[str, object]:
        account, contact = self._validate_scope(account_id, contact_id)
        if not isinstance(values, dict) or set(values) != {"allowed_features"}:
            raise PrivatePolicyError("联系人可用功能格式不正确。")
        allowed = values.get("allowed_features")
        if not isinstance(allowed, list) or any(
            not isinstance(item, str) or not _MODULE_ID.fullmatch(item)
            for item in allowed
        ):
            raise PrivatePolicyError("联系人可用功能清单格式不正确。")
        allowed = list(dict.fromkeys(allowed))
        available = {feature.module for feature in self._features() if feature.visible}
        if set(allowed).difference(available):
            raise PrivatePolicyError("联系人可用功能包含未交付或已移除的功能。")
        actor = str(actor_id or "local-console").strip()
        if len(actor) > 128 or any(ord(char) < 32 for char in actor):
            raise PrivatePolicyError("操作人标识格式不正确。")

        with _STORE_LOCK:
            payload = self._read()
            raw = self._raw_policy(payload, account, contact)
            current = self._policy(raw)
            if expected_revision and expected_revision != self._revision(current):
                raise PrivatePolicyError("联系人功能范围已在其他窗口修改，请刷新后重试。")
            normalized = PrivateChatPolicy(
                allowed_features=tuple(allowed),
                updated_at=datetime.now(timezone.utc).isoformat(),
                updated_by=actor or "local-console",
            )
            accounts = payload.setdefault("accounts", {})
            account_payload = accounts.setdefault(account, {"contacts": {}})
            if not isinstance(account_payload, dict):
                raise PrivatePolicyError("当前账号的私聊功能范围格式不正确，原配置未被修改。")
            contacts = account_payload.setdefault("contacts", {})
            if not isinstance(contacts, dict):
                raise PrivatePolicyError("当前账号的私聊功能范围格式不正确，原配置未被修改。")
            contacts[contact] = normalized.to_dict()
            audit = payload.setdefault("audit", [])
            audit.append(
                {
                    "time": normalized.updated_at,
                    "account_id": account,
                    "contact_id": contact,
                    "actor_id": normalized.updated_by,
                    "action": "save_private_chat_policy",
                    "changed_fields": (
                        ["allowed_features"]
                        if current.allowed_features != normalized.allowed_features
                        else []
                    ),
                }
            )
            payload["audit"] = audit[-500:]
            write_json_atomic(self.path, payload)

        return {
            **self._public(account, contact, normalized, configured=True),
            "saved": True,
            "apply_state": "saved",
            "message": "联系人可用功能已保存。",
        }


__all__ = ["PrivateChatPolicy", "PrivateChatPolicyService", "PrivatePolicyError"]
