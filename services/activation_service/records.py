"""Administrator-only authorization search, encrypted notes and code history."""
from __future__ import annotations

from wechatrebot_manager.licensing.protocol import LicenseError
from .code_history import KEY_FILENAME, decrypt_note, decrypt_payload, encrypt_note, encrypt_payload, load_key


class RecordsMixin:
    def inspect(self, *, license_id: str = "", owner_ref: str = "") -> dict:
        from .store import _LICENSE_QUOTA_SELECT, _owner
        with self.connection() as db:
            if license_id:
                record = db.execute(_LICENSE_QUOTA_SELECT + " WHERE l.id=?", (license_id,)).fetchone()
                if record is None:
                    raise LicenseError("license_unavailable", "当前设备没有可用的该项授权。")
                licenses = [dict(record)]
                owner_hash = licenses[0]["owner_hash"]
            elif owner_ref:
                owner_hash = _owner(owner_ref)
                licenses = [dict(row) for row in db.execute(_LICENSE_QUOTA_SELECT + " WHERE l.owner_hash=?", (owner_hash,))]
            else:
                return {"licenses": db.execute("SELECT COUNT(*) FROM licenses").fetchone()[0],
                        "codes": db.execute("SELECT COUNT(*) FROM codes").fetchone()[0]}
            for record in licenses:
                record.pop("public_key")
                record.pop("owner_hash")
                record.pop("account_hash")
                record.pop("encrypted_note", None)
            codes = [dict(row) for row in db.execute(
                "SELECT id,plan,mode,target_id,issued_at,redeem_by,revoked_at,redeemed_at,redeemed_license,redeemed_replaces_id "
                "FROM codes WHERE owner_hash=? ORDER BY issued_at DESC", (owner_hash,),
            )]
            return {"licenses": licenses, "codes": codes}

    def admin_counts(self) -> dict[str, int]:
        with self.connection() as db:
            return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("licenses", "codes", "trials")}

    def _history_key(self, db, *, create: bool = False) -> bytes:
        existing = db.execute("SELECT id,encrypted_payload FROM codes WHERE encrypted_payload<>'' LIMIT 1").fetchone()
        note = db.execute("SELECT id,encrypted_note FROM licenses WHERE encrypted_note<>'' LIMIT 1").fetchone()
        key = load_key(self.path.parent / KEY_FILENAME, create=create and existing is None and note is None)
        if existing is not None:
            # Detect a replaced key before accepting new history under a different key.
            decrypt_payload(key, existing["id"], existing["encrypted_payload"])
        if note is not None:
            decrypt_note(key, note["id"], note["encrypted_note"])
        return key

    @staticmethod
    def _admin_history_item(row, key: bytes | None, now: int, *, include_code: bool = False) -> dict:
        item = dict(row)
        payload = item.pop("encrypted_payload")
        license_note = item.pop("license_note")
        item.pop("digest")
        item.pop("owner_hash")
        item.pop("reference_hash")
        linked_status = item.pop("linked_status")
        item["license_status"] = ("" if linked_status is None else "revoked" if linked_status == "revoked"
                                  else "expired" if item["valid_until"] <= now else "active")
        item["history_available"] = bool(payload)
        saved = decrypt_payload(key, item["id"], payload) if payload else {"code": "", "owner_ref": "", "reference": "", "note": ""}
        item.update(saved)
        item["customer_note"] = (decrypt_note(key, item["redeemed_license"] or item["target_id"], license_note)
                                 if license_note else item["note"])
        item["reset_account"] = bool(item["reset_account"])
        for name in ("account", "device", "license_plan"):
            item[name] = item[name] or ""
        if not include_code:
            item.pop("code")
        return item

    def admin_code_history(
        self, *, now: int, owner_ref: str = "", reference: str = "", license_id: str = "",
        code_id: str = "", status: str = "", page: int = 1, page_size: int = 20,
        search: str = "", kind: str = "",
    ) -> dict:
        from .store import _HISTORY_SELECT, _hash, _owner, _text

        if (type(now) is not int or type(page) is not int or page < 1 or type(page_size) is not int
                or not 1 <= page_size <= 100 or not isinstance(status, str)
                or status not in {"", "unredeemed", "redeemed", "expired", "revoked"}
                or not isinstance(search, str) or len(search) > 256 or kind not in {"", "duration", "rebind_credit", "service"}):
            raise LicenseError("invalid_data", "历史查询条件或分页参数无效。")
        if (page - 1) * page_size > 2**63 - 1:
            raise LicenseError("invalid_data", "历史查询页码超出范围。")
        conditions, parameters = [], [now]
        for value, column, convert in ((owner_ref, "owner_hash", _owner), (reference, "reference_hash", lambda x: _hash(_text(x))),
                                       (code_id, "id", _text), (status, "status", _text)):
            if value != "":
                conditions.append(f"{column}=?")
                parameters.append(convert(value))
        if license_id != "":
            conditions.append("(target_id=? OR redeemed_license=?)")
            parameters.extend([_text(license_id)] * 2)
        if kind:
            conditions.append({"duration": "mode IN ('new','renewal','duration')", "rebind_credit": "mode='rebind_credit'",
                               "service": "mode IN ('migration','recovery')"}[kind])
        query = " FROM (" + _HISTORY_SELECT + ") WHERE " + (" AND ".join(conditions) or "1=1")
        with self.connection() as db:
            db.execute("BEGIN")
            if search.strip():
                items, total = [], 0
                for row, item in self._history_search(db, query, parameters, now, search):
                    if (page - 1) * page_size <= total < page * page_size:
                        items.append(item)
                    total += 1
                return {"items": items, "total": total, "page": page, "page_size": page_size}
            total = db.execute("SELECT COUNT(*)" + query, parameters).fetchone()[0]
            rows = db.execute("SELECT *" + query + " ORDER BY issued_at DESC,id DESC LIMIT ? OFFSET ?",
                              [*parameters, page_size, (page - 1) * page_size]).fetchall()
            key = self._history_key(db) if any(row["encrypted_payload"] or row["license_note"] for row in rows) else None
            items = [self._admin_history_item(row, key, now) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def admin_code_detail(self, code_id: str, *, now: int) -> dict:
        from .store import _HISTORY_SELECT, _text

        if type(now) is not int:
            raise LicenseError("invalid_data", "历史查询参数无效。")
        with self.connection() as db:
            row = db.execute("SELECT * FROM (" + _HISTORY_SELECT + ") WHERE id=?", (now, _text(code_id))).fetchone()
            if row is None:
                raise LicenseError("code_unavailable", "码编号不存在，请核对历史记录。")
            key = self._history_key(db) if row["encrypted_payload"] or row["license_note"] else None
            return self._admin_history_item(row, key, now, include_code=True)

    def _history_search(self, db, query, parameters, now, search):
        from .store import _hash, code_digest

        search = search.strip()
        try:
            digest = code_digest(search)
        except LicenseError:
            digest = ""
        key, needle = None, search.casefold()
        for row in db.execute("SELECT *" + query + " ORDER BY issued_at DESC,id DESC", parameters):
            if key is None and (row["encrypted_payload"] or row["license_note"]):
                key = self._history_key(db)
            item = self._admin_history_item(row, key, now)
            if (row["digest"] == digest or row["owner_hash"] == _hash(needle) or row["reference_hash"] == _hash(search)
                    or any(needle in str(item.get(name) or "").casefold() for name in (
                        "id", "target_id", "redeemed_license", "account", "note", "customer_note", "owner_ref", "reference"))):
                yield row, item

    def _admin_license_item(self, db, row, now: int) -> dict:
        item = {name: row[name] for name in ("id", "plan", "status", "account", "device", "valid_until", "created_at",
                                            "revision", "migration_total", "migration_used", "migration_remaining")}
        item["license_status"] = "revoked" if row["status"] == "revoked" else "expired" if row["valid_until"] <= now else "active"
        item["note"] = ""
        if row["encrypted_note"]:
            item["note"] = decrypt_note(self._history_key(db), row["id"], row["encrypted_note"])
        if not row["encrypted_note"]:
            for code in db.execute("SELECT id,encrypted_payload FROM codes WHERE (redeemed_license=? OR target_id=?) "
                                   "AND encrypted_payload<>'' ORDER BY issued_at DESC,id DESC", (row["id"], row["id"])):
                note = decrypt_payload(self._history_key(db), code["id"], code["encrypted_payload"])["note"]
                if note:
                    item["note"] = note
                    break
        item.update(self._binding_status(db, row, now))
        return item

    def admin_licenses(self, *, now: int, search: str = "", page: int = 1, page_size: int = 20) -> dict:
        from .store import _HISTORY_SELECT, _LICENSE_QUOTA_SELECT, _hash

        if (type(now) is not int or not isinstance(search, str) or len(search) > 256 or type(page) is not int or not 1 <= page <= 1000000
                or type(page_size) is not int or not 1 <= page_size <= 100):
            raise LicenseError("invalid_data", "客户查询条件或页码无效。")
        needle, related = search.strip().casefold(), set()
        with self.connection() as db:
            db.execute("BEGIN")
            if not needle:
                total = db.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
                rows = db.execute(_LICENSE_QUOTA_SELECT + " ORDER BY l.created_at DESC,l.id DESC LIMIT ? OFFSET ?",
                                  (page_size, (page - 1) * page_size)).fetchall()
                return {"items": [self._admin_license_item(db, row, now) for row in rows],
                        "total": total, "page": page, "page_size": page_size}
            if needle:
                for row, _ in self._history_search(db, " FROM (" + _HISTORY_SELECT + ")", [now], now, search):
                    related.update((row["target_id"], row["redeemed_license"]))
            items, total = [], 0
            for row in db.execute(_LICENSE_QUOTA_SELECT + " ORDER BY l.created_at DESC,l.id DESC"):
                item = self._admin_license_item(db, row, now)
                if needle and not (row["id"] in related or row["owner_hash"] == _hash(needle)
                                   or any(needle in str(item[name]).casefold() for name in ("id", "account", "note", "device"))):
                    continue
                if (page - 1) * page_size <= total < page * page_size:
                    items.append(item)
                total += 1
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def admin_license_detail(self, license_id: str, *, now: int) -> dict:
        from .store import _LICENSE_QUOTA_SELECT, _text

        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute(_LICENSE_QUOTA_SELECT + " WHERE l.id=?", (_text(license_id),)).fetchone()
            if row is None:
                raise LicenseError("license_unavailable", "找不到该授权，请核对客户信息。")
            return self._admin_license_item(db, row, now)

    def admin_update_note(self, *, now: int, note: str, code_id: str = "", license_id: str = "") -> None:
        from .store import _text

        if bool(code_id) == bool(license_id) or not isinstance(note, str) or len(note) > 256:
            raise LicenseError("invalid_data", "请选择一条记录，备注最多 256 字。")
        with self.connection(write=True) as db:
            if license_id:
                record = self._license(db, _text(license_id))
                encrypted = encrypt_note(self._history_key(db, create=True), record["id"], note.strip())
                db.execute("UPDATE licenses SET encrypted_note=? WHERE id=?", (encrypted, record["id"]))
            else:
                row = db.execute("SELECT * FROM codes WHERE id=?", (_text(code_id),)).fetchone()
                if row is None or not row["encrypted_payload"]:
                    raise LicenseError("history_unavailable", "旧码未保存完整资料，请在关联授权上添加客户备注。")
                key = self._history_key(db)
                value = decrypt_payload(key, code_id, row["encrypted_payload"])
                value["note"] = note.strip()
                encrypted = encrypt_payload(key, code_id, **value)
                db.execute("UPDATE codes SET encrypted_payload=? WHERE id=?", (encrypted, code_id))
            self._audit(db, now, "admin-web", "update_note", license_id or code_id)

    def admin_audit(self, now: int, event: str, object_id: str = "", **details) -> None:
        with self.connection(write=True) as db:
            self._audit(db, now, "admin-web", event, object_id, **details)
