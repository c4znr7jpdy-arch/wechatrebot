"""Transactional, single-host activation ledger.

Activation codes and customer references are never stored in plaintext. A redeemed
code remains linked to its original grant, including when signing/delivery fails.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Iterator
from zoneinfo import ZoneInfo

from wechatrebot_manager.licensing.protocol import (
    ACCOUNT_PATTERN, CODE_MODES, MAX_LEASE_SECONDS, PLANS, LicenseError, extend_period,
)

PREVIEW_SECONDS = 300
_CODE = re.compile(r"WRB[A-Z2-7]{32}")
_FINGERPRINT = re.compile(r"[a-f0-9]{64}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, device TEXT NOT NULL,
    public_key TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
    account_hash TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL,
    valid_until INTEGER NOT NULL, anchor_day INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS licenses_owner ON licenses(owner_hash);
CREATE INDEX IF NOT EXISTS licenses_account ON licenses(account_hash);
CREATE TABLE IF NOT EXISTS codes (
    id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
    reference_hash TEXT NOT NULL UNIQUE, owner_hash TEXT NOT NULL,
    plan TEXT NOT NULL, mode TEXT NOT NULL, target_id TEXT,
    target_device TEXT NOT NULL DEFAULT '', target_revision INTEGER,
    reset_account INTEGER NOT NULL DEFAULT 0,
    issued_at INTEGER NOT NULL, redeem_by INTEGER NOT NULL,
    revoked_at INTEGER, redeemed_at INTEGER,
    redeemed_license TEXT, redeemed_device TEXT, redeemed_replaces_id TEXT,
    FOREIGN KEY(target_id) REFERENCES licenses(id),
    FOREIGN KEY(redeemed_license) REFERENCES licenses(id),
    FOREIGN KEY(redeemed_replaces_id) REFERENCES licenses(id)
);
CREATE INDEX IF NOT EXISTS codes_owner ON codes(owner_hash);
CREATE TABLE IF NOT EXISTS previews (
    id TEXT PRIMARY KEY, code_id TEXT NOT NULL, device TEXT NOT NULL,
    license_id TEXT NOT NULL, revision INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    replaces_license_id TEXT NOT NULL DEFAULT '',
    replaces_revision INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(code_id) REFERENCES codes(id)
);
CREATE INDEX IF NOT EXISTS previews_expiry ON previews(expires_at);
CREATE TABLE IF NOT EXISTS trials (
    kind TEXT NOT NULL, identity_hash TEXT NOT NULL, license_id TEXT NOT NULL,
    PRIMARY KEY(kind, identity_hash), FOREIGN KEY(license_id) REFERENCES licenses(id)
);
CREATE TABLE IF NOT EXISTS nonces (
    device TEXT NOT NULL, nonce TEXT NOT NULL, expires_at INTEGER NOT NULL,
    PRIMARY KEY(device, nonce)
);
CREATE INDEX IF NOT EXISTS nonces_expiry ON nonces(expires_at);
CREATE TABLE IF NOT EXISTS rate_limits (
    peer_hash TEXT PRIMARY KEY, tokens REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_limits_expiry ON rate_limits(updated_at);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, happened_at INTEGER NOT NULL,
    actor TEXT NOT NULL, event TEXT NOT NULL, object_id TEXT NOT NULL,
    details TEXT NOT NULL
);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: object, *, limit: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise LicenseError("invalid_data", "请求参数无效。")
    return value.strip()


def code_digest(value: object) -> str:
    value = _text(value)
    normalized = re.sub(r"[-\s]", "", value).upper()
    if not _CODE.fullmatch(normalized):
        raise LicenseError("code_unavailable", "激活码无效或不适用于当前设备。")
    return _hash(normalized)


def _owner(value: str) -> str:
    return _hash(_text(value).casefold())


def _validate_body(body: dict, required: set[str], optional: set[str] | None = None) -> None:
    if not required <= body.keys() or body.keys() - required - (optional or set()):
        raise LicenseError("invalid_data", "请求参数无效。")


class LicenseStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2}:
                raise ValueError("Unsupported activation database schema")
            if version == 1:
                db.execute("ALTER TABLE codes ADD COLUMN redeemed_replaces_id TEXT REFERENCES licenses(id)")
                db.execute("ALTER TABLE previews ADD COLUMN replaces_license_id TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE previews ADD COLUMN replaces_revision INTEGER NOT NULL DEFAULT 0")
            # Execute individually: executescript would commit before the schema
            # and version change, exposing a half-upgraded database to workers.
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("PRAGMA user_version=2")
            db.execute("COMMIT")

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _audit(db, now: int, actor: str, event: str, object_id: str, **details) -> None:
        db.execute(
            "INSERT INTO audit(happened_at,actor,event,object_id,details) VALUES(?,?,?,?,?)",
            (now, actor, event, object_id, json.dumps(details, ensure_ascii=False)),
        )

    @staticmethod
    def _license(db, license_id: str):
        record = db.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
        if record is None:
            raise LicenseError("license_unavailable", "当前设备没有可用的该项授权。")
        return record

    @classmethod
    def _owned(cls, db, license_id: str, device: str):
        record = cls._license(db, license_id)
        if record["device"] != device:
            raise LicenseError("license_unavailable", "当前设备没有可用的该项授权。")
        return record

    @staticmethod
    def _active(record) -> None:
        if record["status"] != "active":
            raise LicenseError("license_revoked", "授权已停用，请联系管理员处理。")

    @staticmethod
    def _account_available(db, account_hash: str, license_id: str, now: int) -> None:
        if account_hash and db.execute(
            "SELECT 1 FROM licenses WHERE account_hash=? AND id<>? AND status='active' AND valid_until>?",
            (account_hash, license_id, now),
        ).fetchone():
            raise LicenseError("account_unavailable", "该机器人账号已关联其他有效授权。")

    @staticmethod
    def _trial(db, kind: str, identity_hash: str, license_id: str = "") -> None:
        found = db.execute(
            "SELECT license_id FROM trials WHERE kind=? AND identity_hash=?",
            (kind, identity_hash),
        ).fetchone()
        if found and found["license_id"] != license_id:
            raise LicenseError("trial_used", "该客户、设备或机器人账号已经使用过试用。")

    @classmethod
    def _remember_trial(cls, db, kind: str, identity_hash: str, license_id: str) -> None:
        cls._trial(db, kind, identity_hash, license_id)
        db.execute("INSERT OR IGNORE INTO trials VALUES(?,?,?)", (kind, identity_hash, license_id))

    def accept_nonce(self, device: str, nonce: str, now: int) -> None:
        # Retain beyond the complete +/- 300 second request acceptance window.
        with self.connection(write=True) as db:
            db.execute("DELETE FROM nonces WHERE expires_at<?", (now,))
            try:
                db.execute("INSERT INTO nonces VALUES(?,?,?)", (device, nonce, now + 601))
            except sqlite3.IntegrityError as exc:
                raise LicenseError("request_replayed", "请求已经处理，请重新发起验证。") from exc

    def allow_peer(
        self, peer: str, now: float, *, requests: int = 120, period: int = 60,
        max_peers: int = 4096,
    ) -> bool:
        """A durable token bucket, including across service restarts/workers."""
        peer_hash = _hash(peer)
        with self.connection(write=True) as db:
            db.execute("DELETE FROM rate_limits WHERE updated_at<?", (now - period,))
            bucket = db.execute("SELECT * FROM rate_limits WHERE peer_hash=?", (peer_hash,)).fetchone()
            if bucket is None:
                if db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] >= max_peers:
                    return False
                tokens = float(requests)
            else:
                tokens = min(requests, bucket["tokens"] + max(0, now - bucket["updated_at"]) * requests / period)
            allowed = tokens >= 1
            db.execute(
                "INSERT INTO rate_limits VALUES(?,?,?) ON CONFLICT(peer_hash) DO UPDATE "
                "SET tokens=excluded.tokens,updated_at=excluded.updated_at",
                (peer_hash, tokens - 1 if allowed else tokens, now),
            )
            return allowed

    def issue_code(
        self, *, owner_ref: str, reference: str, now: int, plan: str | None = None,
        mode: str = "new", license_id: str = "", target_device: str = "",
        reset_account: bool = False, redeem_by: int | None = None,
    ) -> dict:
        owner_hash = _owner(owner_ref)
        reference_hash = _hash(_text(reference))
        redeem_by = now + 30 * 86400 if redeem_by is None else redeem_by
        if mode not in CODE_MODES or type(redeem_by) is not int or redeem_by <= now:
            raise LicenseError("invalid_data", "激活码用途或兑换期限无效。")
        if target_device and not _FINGERPRINT.fullmatch(target_device):
            raise LicenseError("invalid_data", "目标设备指纹无效。")
        with self.connection(write=True) as db:
            target = self._issuance_target(
                db, mode, owner_hash, license_id, target_device, reset_account,
            )
            plan = plan or (target["plan"] if target else "")
            if plan not in PLANS or (mode == "renewal" and plan == "trial"):
                raise LicenseError("invalid_data", "请选择有效套餐，试用不能用于续期。")
            if mode in {"recovery", "migration"} and plan != target["plan"]:
                raise LicenseError("invalid_data", "补领或迁移不能修改原套餐。")
            if plan == "trial" and mode == "new":
                self._trial(db, "owner", owner_hash)
            if db.execute("SELECT 1 FROM codes WHERE reference_hash=?", (reference_hash,)).fetchone():
                raise LicenseError("reference_used", "该人工业务编号已经发过码，请查询原记录。")
            encoded = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
            code = "WRB-" + "-".join(encoded[i:i + 8] for i in range(0, len(encoded), 8))
            code_id = secrets.token_hex(16)
            db.execute(
                "INSERT INTO codes(id,digest,reference_hash,owner_hash,plan,mode,target_id,"
                "target_device,target_revision,reset_account,issued_at,redeem_by) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (code_id, code_digest(code), reference_hash, owner_hash, plan, mode,
                 license_id or None, target_device,
                 target["revision"] if mode == "migration" else None,
                 int(reset_account), now, redeem_by),
            )
            self._audit(db, now, "admin", "issue_code", code_id, mode=mode, plan=plan,
                        license_id=license_id, target_device=target_device)
        return {"code_id": code_id, "code": code, "plan": plan, "mode": mode,
                "license_id": license_id, "redeem_by": redeem_by}

    @classmethod
    def _issuance_target(cls, db, mode, owner_hash, license_id, target_device, reset_account):
        if mode == "new":
            if license_id or target_device or reset_account:
                raise LicenseError("invalid_data", "新开通码不能指定已有授权或迁移参数。")
            return None
        target = cls._license(db, _text(license_id))
        cls._active(target)
        if target["owner_hash"] != owner_hash:
            raise LicenseError("owner_mismatch", "客户参考号与原授权不符。")
        if mode == "migration":
            if not target_device:
                raise LicenseError("invalid_data", "迁移必须提供已核实的新设备指纹。")
        elif target_device or reset_account:
            raise LicenseError("invalid_data", "只有迁移码可以修改设备或机器人绑定。")
        return target

    @classmethod
    def _code_context(cls, db, digest: str, device: str, now: int):
        code = db.execute("SELECT * FROM codes WHERE digest=?", (digest,)).fetchone()
        unavailable = LicenseError("code_unavailable", "激活码无效或不适用于当前设备。")
        if code is None or code["revoked_at"] is not None:
            raise unavailable
        if code["redeemed_at"] is not None:
            record = cls._license(db, code["redeemed_license"])
            if code["redeemed_device"] != device or record["device"] != device:
                raise unavailable
            cls._active(record)
            return code, record, True
        if code["redeem_by"] <= now:
            raise LicenseError("code_expired", "激活码已超过兑换期限，请联系管理员。")
        target = cls._license(db, code["target_id"]) if code["target_id"] else None
        if target:
            expected = code["target_device"] if code["mode"] == "migration" else target["device"]
            if device != expected or target["owner_hash"] != code["owner_hash"]:
                raise unavailable
            cls._active(target)
            if code["mode"] == "migration" and code["target_revision"] != target["revision"]:
                raise LicenseError("code_stale", "原授权已变更，请联系管理员重新核实迁移。")
            if code["mode"] == "renewal":
                cls._account_available(db, target["account_hash"], target["id"], now)
        if code["plan"] == "trial":
            license_id = target["id"] if target else ""
            cls._trial(db, "owner", code["owner_hash"], license_id)
            cls._trial(db, "device", device, license_id)
        return code, target, False

    @classmethod
    def _replacement(cls, db, code, target, device, supplied_id, now, *, redeemed=False):
        if not supplied_id or (target is not None and supplied_id == target["id"]):
            return None
        unavailable = LicenseError("code_unavailable", "激活码无效或不适用于当前设备。")
        if code["mode"] != "migration" or code["target_device"] != device or target is None:
            raise unavailable
        # A lost response may leave the replaced, now revoked ID in the cache.
        # Only the exact replacement committed with this code may recover it.
        if redeemed and code["redeemed_replaces_id"] != supplied_id:
            raise unavailable
        record = db.execute("SELECT * FROM licenses WHERE id=?", (supplied_id,)).fetchone()
        if record is None or record["device"] != device or record["owner_hash"] != target["owner_hash"]:
            raise unavailable
        if not redeemed:
            cls._active(record)
            if record["plan"] != "trial" and record["valid_until"] > now:
                raise LicenseError("active_license_conflict", "本机已有有效付费授权，请联系管理员处理，不能直接替换。")
        return record

    @staticmethod
    def _projected_until(code, target, already_redeemed: bool, now: int) -> int:
        if already_redeemed or code["mode"] in {"recovery", "migration"}:
            return target["valid_until"]
        if code["mode"] == "renewal" and target["valid_until"] > now:
            anchor = None if target["plan"] == "trial" else target["anchor_day"]
            return extend_period(target["valid_until"], code["plan"], anchor)
        return extend_period(now, code["plan"])

    def preview(self, device: str, body: dict, now: int) -> dict:
        _validate_body(body, {"code"}, {"license_id"})
        supplied_id = body.get("license_id", "")
        if not isinstance(supplied_id, str):
            raise LicenseError("invalid_data", "请求参数无效。")
        supplied_id = _text(supplied_id) if supplied_id else ""
        digest = code_digest(body["code"])
        with self.connection(write=True) as db:
            code, target, redeemed = self._code_context(db, digest, device, now)
            replaced = self._replacement(db, code, target, device, supplied_id, now, redeemed=redeemed)
            preview_id = secrets.token_hex(16)
            # Keep completed previews for a day for lost-response recovery.
            db.execute("DELETE FROM previews WHERE expires_at<?", (now - 86400,))
            db.execute("INSERT INTO previews(id,code_id,device,license_id,revision,expires_at,"
                       "replaces_license_id,replaces_revision) VALUES(?,?,?,?,?,?,?,?)", (
                preview_id, code["id"], device, target["id"] if target else "",
                target["revision"] if target else 0, now + PREVIEW_SECONDS,
                replaced["id"] if replaced else "", replaced["revision"] if replaced else 0,
            ))
            message = "该码已兑换，将恢复原授权，不增加时长。" if redeemed else "确认后兑换激活码。"
            if replaced and not redeemed:
                message = "确认后将停用本机原试用或已到期授权，迁入原授权，到期时间不变。"
            return {
                "preview_id": preview_id, "expires_at": now + PREVIEW_SECONDS,
                "plan": target["plan"] if redeemed else code["plan"], "mode": code["mode"],
                "valid_until": self._projected_until(code, target, redeemed, now),
                "license_id": target["id"] if target else "", "already_redeemed": redeemed,
                "replaces_license_id": replaced["id"] if replaced else "", "message": message,
            }

    def redeem(self, device: str, public_key: str, body: dict, now: int) -> dict:
        _validate_body(body, {"code", "preview_id"})
        preview_id = _text(body["preview_id"])
        digest = code_digest(body["code"])
        with self.connection(write=True) as db:
            code, target, redeemed = self._code_context(db, digest, device, now)
            preview = db.execute("SELECT * FROM previews WHERE id=?", (preview_id,)).fetchone()
            if not preview or preview["device"] != device or preview["code_id"] != code["id"]:
                raise LicenseError("preview_required", "请先预览激活码并确认兑换内容。")
            replaced = self._replacement(
                db, code, target, device, preview["replaces_license_id"], now, redeemed=redeemed,
            )
            if redeemed:
                return dict(target)
            revision = target["revision"] if target else 0
            target_id = target["id"] if target else ""
            if (preview["expires_at"] <= now or preview["revision"] != revision
                    or preview["license_id"] != target_id
                    or preview["replaces_revision"] != (replaced["revision"] if replaced else 0)):
                raise LicenseError("preview_stale", "预览已过期或授权已变更，请重新预览后确认。")
            if replaced:
                db.execute("UPDATE licenses SET status='revoked',revision=revision+1 WHERE id=?", (replaced["id"],))
                self._audit(db, now, device, "replace_license", replaced["id"],
                            migrated_license_id=target_id, code_id=code["id"])
            license_id = self._apply_grant(db, code, target, device, public_key, now)
            db.execute(
                "UPDATE codes SET redeemed_at=?,redeemed_license=?,redeemed_device=?,redeemed_replaces_id=? WHERE id=?",
                (now, license_id, device, replaced["id"] if replaced else None, code["id"]),
            )
            self._audit(db, now, device, "redeem", license_id, code_id=code["id"], mode=code["mode"])
            return dict(self._license(db, license_id))

    @classmethod
    def _apply_grant(cls, db, code, target, device: str, public_key: str, now: int) -> str:
        mode = code["mode"]
        until = cls._projected_until(code, target, False, now)
        anchor = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).day
        if mode == "new":
            license_id = secrets.token_hex(16)
            db.execute(
                "INSERT INTO licenses(id,owner_hash,device,public_key,plan,valid_until,anchor_day,"
                "revision,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (license_id, code["owner_hash"], device, public_key, code["plan"], until, anchor, 1, now),
            )
        else:
            license_id = target["id"]
            if mode == "renewal":
                if target["valid_until"] > now:
                    anchor = (datetime.fromtimestamp(target["valid_until"], ZoneInfo("Asia/Shanghai")).day
                              if target["plan"] == "trial" else target["anchor_day"])
                db.execute(
                    "UPDATE licenses SET plan=?,valid_until=?,anchor_day=?,revision=revision+1 WHERE id=?",
                    (code["plan"], until, anchor, license_id),
                )
            elif mode == "migration":
                account = "" if code["reset_account"] else target["account"]
                account_hash = "" if code["reset_account"] else target["account_hash"]
                db.execute(
                    "UPDATE licenses SET device=?,public_key=?,account=?,account_hash=?,revision=revision+1 WHERE id=?",
                    (device, public_key, account, account_hash, license_id),
                )
        if code["plan"] == "trial":
            cls._remember_trial(db, "owner", code["owner_hash"], license_id)
            cls._remember_trial(db, "device", device, license_id)
        return license_id

    def refresh(self, device: str, body: dict, now: int) -> dict:
        _validate_body(body, {"license_id"})
        with self.connection() as db:
            # Signed expired/revoked records let clients display the true reason.
            return dict(self._owned(db, _text(body["license_id"]), device))

    def bind(self, device: str, body: dict, now: int) -> dict:
        _validate_body(body, {"license_id", "account"})
        account = _text(body["account"])
        if not ACCOUNT_PATTERN.fullmatch(account):
            raise LicenseError("invalid_account", "机器人账号格式无效。")
        account_hash = _hash(account.casefold())
        with self.connection(write=True) as db:
            record = self._owned(db, _text(body["license_id"]), device)
            self._active(record)
            if record["account"] == account:
                return dict(record)
            if record["account"]:
                raise LicenseError("account_bound", "该授权已绑定机器人账号，换绑请联系管理员。")
            if record["valid_until"] <= now:
                raise LicenseError("subscription_expired", "订阅已到期，请续期后再绑定机器人账号。")
            self._account_available(db, account_hash, record["id"], now)
            if record["plan"] == "trial":
                self._remember_trial(db, "account", account_hash, record["id"])
            db.execute(
                "UPDATE licenses SET account=?,account_hash=?,revision=revision+1 WHERE id=?",
                (account, account_hash, record["id"]),
            )
            self._audit(db, now, device, "bind", record["id"])
            return dict(self._license(db, record["id"]))

    def revoke(self, *, now: int, license_id: str = "", code_id: str = "") -> dict:
        if bool(license_id) == bool(code_id):
            raise LicenseError("invalid_data", "请选择一个授权编号或未兑换的码编号。")
        with self.connection(write=True) as db:
            if license_id:
                record = self._license(db, license_id)
                if record["status"] != "revoked":
                    db.execute("UPDATE licenses SET status='revoked',revision=revision+1 WHERE id=?", (license_id,))
                    self._audit(db, now, "admin", "revoke_license", license_id)
            else:
                code = db.execute("SELECT * FROM codes WHERE id=?", (code_id,)).fetchone()
                if code is None or code["redeemed_at"] is not None:
                    raise LicenseError("code_unavailable", "码不存在或已兑换；已兑换权益请按授权编号停用。")
                if code["revoked_at"] is None:
                    db.execute("UPDATE codes SET revoked_at=? WHERE id=?", (now, code_id))
                    self._audit(db, now, "admin", "revoke_code", code_id)
        return {"license_id": license_id, "code_id": code_id, "status": "revoked"}

    def inspect(self, *, license_id: str = "", owner_ref: str = "") -> dict:
        with self.connection() as db:
            if license_id:
                licenses = [dict(self._license(db, license_id))]
                owner_hash = licenses[0]["owner_hash"]
            elif owner_ref:
                owner_hash = _owner(owner_ref)
                licenses = [dict(row) for row in db.execute("SELECT * FROM licenses WHERE owner_hash=?", (owner_hash,))]
            else:
                return {"licenses": db.execute("SELECT COUNT(*) FROM licenses").fetchone()[0],
                        "codes": db.execute("SELECT COUNT(*) FROM codes").fetchone()[0]}
            for record in licenses:
                record.pop("public_key")
                record.pop("owner_hash")
                record.pop("account_hash")
            codes = [dict(row) for row in db.execute(
                "SELECT id,plan,mode,target_id,issued_at,redeem_by,revoked_at,redeemed_at,redeemed_license,redeemed_replaces_id "
                "FROM codes WHERE owner_hash=? ORDER BY issued_at DESC", (owner_hash,),
            )]
            return {"licenses": licenses, "codes": codes}


def lease_payload(record: dict, request_nonce: str, now: int) -> dict:
    return {
        "schema_version": 1, "audience": "wechatrebot", "license_id": record["id"],
        "device": record["device"], "account": record["account"], "plan": record["plan"],
        "status": record["status"], "issued_at": now,
        "lease_until": min(now + MAX_LEASE_SECONDS, record["valid_until"]),
        "valid_until": record["valid_until"], "revision": record["revision"],
        "request_nonce": request_nonce,
    }
