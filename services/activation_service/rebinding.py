"""Device-owned account changes with a persistent, transaction-scoped quota."""

from __future__ import annotations

from wechatrebot_manager.licensing.protocol import ACCOUNT_PATTERN, LicenseError


REBIND_LIMITS = {"trial": 0, "month": 1, "year": 10}

REBINDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS rebindings (
    id INTEGER PRIMARY KEY, license_id TEXT NOT NULL, device TEXT NOT NULL,
    old_account_hash TEXT NOT NULL, new_account_hash TEXT NOT NULL,
    previous_revision INTEGER NOT NULL, revision INTEGER NOT NULL,
    happened_at INTEGER NOT NULL,
    UNIQUE(license_id, revision), FOREIGN KEY(license_id) REFERENCES licenses(id)
);
CREATE INDEX IF NOT EXISTS rebindings_license ON rebindings(license_id);
"""


class RebindingMixin:
    @staticmethod
    def _binding_status(db, record, now: int) -> dict:
        used = db.execute(
            "SELECT COUNT(*) FROM rebindings WHERE license_id=?", (record["id"],),
        ).fetchone()[0]
        limit = REBIND_LIMITS.get(record["plan"], 0)
        remaining = max(0, limit - used)
        return {
            "license_id": record["id"], "account": record["account"],
            "plan": record["plan"], "revision": record["revision"],
            "rebind_limit": limit, "rebind_used": used, "rebind_remaining": remaining,
            "can_rebind": bool(record["status"] == "active" and record["valid_until"] > now
                               and record["account"] and remaining),
        }

    def binding_status(self, device: str, body: dict, now: int) -> dict:
        from .store import _text, _validate_body

        _validate_body(body, {"license_id"})
        with self.connection() as db:
            # Keep account/revision and the history count in the same snapshot.
            db.execute("BEGIN")
            record = self._owned(db, _text(body["license_id"]), device)
            return self._binding_status(db, record, now)

    def rebind(self, device: str, body: dict, now: int) -> dict:
        from .store import _hash, _text, _validate_body

        _validate_body(body, {"license_id", "account", "expected_account", "expected_revision"})
        account, expected_account = _text(body["account"]), _text(body["expected_account"])
        if not ACCOUNT_PATTERN.fullmatch(account):
            raise LicenseError("invalid_account", "机器人账号格式无效。")
        expected_revision = body["expected_revision"]
        if type(expected_revision) is not int or expected_revision < 1:
            raise LicenseError("invalid_data", "授权版本无效，请重新读取换绑信息。")
        account_hash = _hash(account.casefold())
        with self.connection(write=True) as db:
            record = self._owned(db, _text(body["license_id"]), device)
            self._active(record)
            if record["valid_until"] <= now:
                raise LicenseError("subscription_expired", "订阅已到期，请续期后再换绑微信。")
            if not record["account"] or not REBIND_LIMITS.get(record["plan"], 0):
                raise LicenseError("rebind_unavailable", "当前授权不支持换绑微信。")
            # A delivered or retried request already at its desired account is
            # a no-op, including when the last successful change used the quota.
            if record["account"] == account:
                return dict(record)
            if record["account"] != expected_account or record["revision"] != expected_revision:
                raise LicenseError("binding_changed", "授权绑定已变更，请重新读取并确认换绑。")
            quota = self._binding_status(db, record, now)
            if not quota["rebind_remaining"]:
                raise LicenseError("rebind_exhausted", "当前套餐的微信换绑次数已用完。")
            self._account_available(db, account_hash, record["id"], now)
            revision = record["revision"] + 1
            db.execute(
                "UPDATE licenses SET account=?,account_hash=?,revision=? WHERE id=?",
                (account, account_hash, revision, record["id"]),
            )
            db.execute(
                "INSERT INTO rebindings(license_id,device,old_account_hash,new_account_hash,"
                "previous_revision,revision,happened_at) VALUES(?,?,?,?,?,?,?)",
                (record["id"], device, record["account_hash"], account_hash,
                 record["revision"], revision, now),
            )
            self._audit(db, now, device, "rebind", record["id"],
                        previous_revision=record["revision"], revision=revision,
                        rebind_used=quota["rebind_used"] + 1)
            return dict(self._license(db, record["id"]))
