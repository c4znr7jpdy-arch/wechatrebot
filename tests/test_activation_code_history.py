from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat

import pytest

from services.activation_service.code_history import KEY_FILENAME
from services.activation_service.store import LicenseStore
from wechatrebot_manager.licensing.protocol import LicenseError
from test_activation_service import env, redeem


def issue_record(env, *, reference="manual-record-1", owner_ref="customer-history-1", **kwargs):
    return env.store.issue_code(owner_ref=owner_ref, reference=reference, now=env.now, plan="month", **kwargs)


def test_history_is_encrypted_at_rest_and_survives_restart(env):
    assert not (env.path.parent / KEY_FILENAME).exists()
    code = issue_record(env)
    restarted = LicenseStore(env.path)
    detail = restarted.admin_code_detail(code["code_id"], now=env.now)
    assert detail["code"] == code["code"]
    assert detail["owner_ref"] == "customer-history-1" and detail["reference"] == "manual-record-1"
    assert detail["history_available"] and detail["status"] == "unredeemed"
    assert detail["license_status"] == "" and detail["account"] == detail["device"] == ""
    assert detail["valid_until"] is None
    page = restarted.admin_code_history(now=env.now)
    assert page["total"] == 1 and page["page"] == 1 and page["page_size"] == 20
    assert "code" not in page["items"][0]
    assert not {"encrypted_payload", "owner_hash", "reference_hash", "digest"} & page["items"][0].keys()
    with restarted.connection() as db:
        contents = json.dumps([dict(r) for r in db.execute("SELECT * FROM codes")])
        contents += json.dumps([dict(r) for r in db.execute("SELECT * FROM audit")])
        for secret in (code["code"], "customer-history-1", "manual-record-1"):
            assert secret not in contents
            assert secret.encode() not in env.path.read_bytes()
            wal = env.path.with_name(env.path.name + "-wal")
            if wal.exists():
                assert secret.encode() not in wal.read_bytes()
    redacted = json.dumps(restarted.inspect(owner_ref="customer-history-1"))
    assert code["code"] not in redacted and "customer-history-1" not in redacted
    key = env.path.parent / KEY_FILENAME
    assert key.stat().st_size == 32
    if os.name == "posix":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600


@pytest.mark.parametrize("damage", ["missing", "wrong", "short"])
def test_missing_or_wrong_key_does_not_create_new_history_or_block_redemption(env, damage):
    code = issue_record(env)
    key = env.path.parent / KEY_FILENAME
    original_key = key.read_bytes()
    if damage == "missing":
        key.unlink()
    else:
        key.write_bytes(b"x" * (32 if damage == "wrong" else 5))
    restarted = LicenseStore(env.path)
    for operation in (
        lambda: restarted.admin_code_detail(code["code_id"], now=env.now),
        lambda: restarted.admin_code_history(now=env.now),
        lambda: restarted.issue_code(owner_ref="other", reference="other-record", now=env.now, plan="month"),
    ):
        with pytest.raises(LicenseError) as caught:
            operation()
        assert caught.value.code == "history_unavailable"
        assert code["code"] not in str(caught.value)
    assert restarted.inspect()["codes"] == 1
    if damage == "missing":
        assert not key.exists()
    env.store = restarted
    assert redeem(env, code)["plan"] == "month"
    key.write_bytes(original_key)
    assert restarted.admin_code_detail(code["code_id"], now=env.now)["code"] == code["code"]


def test_history_ciphertext_is_bound_to_its_code_id(env):
    first = issue_record(env)
    second = issue_record(env, reference="manual-record-2")
    with env.store.connection(write=True) as db:
        payload = db.execute("SELECT encrypted_payload FROM codes WHERE id=?", (first["code_id"],)).fetchone()[0]
        db.execute("UPDATE codes SET encrypted_payload=? WHERE id=?", (payload, second["code_id"]))
    with pytest.raises(LicenseError, match="加密资料不可用"):
        env.store.admin_code_detail(second["code_id"], now=env.now)


def test_history_filters_pagination_and_linked_authorization_state(env):
    original_code = issue_record(env)
    original = redeem(env, original_code)
    migration = issue_record(env, reference="migration-record", mode="migration", license_id=original["id"],
                             target_device="a" * 64, reset_account=True)
    expired = issue_record(env, reference="expired-record", owner_ref="someone-else", redeem_by=env.now + 10)
    revoked = issue_record(env, reference="revoked-record")
    env.store.revoke(now=env.now, code_id=revoked["code_id"])
    now = env.now + 11
    result = env.store.admin_code_history(now=now, page_size=2)
    other = env.store.admin_code_history(now=now, page=2, page_size=2)
    assert result["total"] == other["total"] == 4
    ids = [r["id"] for r in result["items"] + other["items"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 4
    for status, code in (("redeemed", original_code), ("unredeemed", migration), ("expired", expired), ("revoked", revoked)):
        rows = env.store.admin_code_history(now=now, status=status)["items"]
        assert [r["id"] for r in rows] == [code["code_id"]]
    assert env.store.admin_code_history(now=now, owner_ref=" CUSTOMER-HISTORY-1 ")["total"] == 3
    assert env.store.admin_code_history(now=now, reference=" migration-record ")["total"] == 1
    assert env.store.admin_code_history(now=now, reference="MIGRATION-record")["total"] == 0
    assert env.store.admin_code_history(now=now, license_id=original["id"])["total"] == 2
    detail = env.store.admin_code_detail(migration["code_id"], now=now)
    assert detail["target_id"] == original["id"] and detail["target_device"] == "a" * 64
    assert detail["reset_account"] and detail["license_status"] == "active"
    assert detail["valid_until"] == original["valid_until"] and detail["device"] == env.device
    assert env.store.admin_code_history(now=now, code_id=expired["code_id"])["items"][0]["status"] == "expired"
    assert env.store.admin_code_detail(original_code["code_id"], now=original["valid_until"])["license_status"] == "expired"
    env.store.revoke(now=now, license_id=original["id"])
    assert env.store.admin_code_detail(original_code["code_id"], now=now)["license_status"] == "revoked"
    assert env.store.admin_code_detail(original_code["code_id"], now=now)["status"] == "redeemed"


@pytest.mark.parametrize("filters", [{"page": 0}, {"page": 10**30}, {"page_size": 0}, {"page_size": 101},
                                     {"page": True}, {"status": "active"}, {"status": []}, {"owner_ref": " "}])
def test_invalid_history_filters_fail_without_database_errors(env, filters):
    with pytest.raises(LicenseError) as caught:
        env.store.admin_code_history(now=env.now, **filters)
    assert caught.value.code == "invalid_data"


def test_unknown_code_history_returns_domain_error(env):
    with pytest.raises(LicenseError) as caught:
        env.store.admin_code_detail("nonexistent-code", now=env.now)
    assert caught.value.code == "code_unavailable"


def test_v2_history_migration_preserves_legacy_code_redemption(env):
    code = issue_record(env)
    with env.store.connection(write=True) as db:
        db.execute("ALTER TABLE codes DROP COLUMN encrypted_payload")
        db.execute("PRAGMA user_version=2")
    (env.path.parent / KEY_FILENAME).unlink()
    env.store = LicenseStore(env.path)
    detail = env.store.admin_code_detail(code["code_id"], now=env.now)
    assert not detail["history_available"] and detail["code"] == detail["owner_ref"] == detail["reference"] == ""
    assert redeem(env, code)["plan"] == "month"
    assert issue_record(env, reference="after-migration")["code"]
    with env.store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_concurrent_duplicate_issue_keeps_one_code_and_one_encryption_key(env):
    def attempt(_):
        try:
            return issue_record(env)
        except LicenseError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "reference_used" in results and env.store.inspect()["codes"] == 1
    code = next(result for result in results if isinstance(result, dict))
    assert LicenseStore(env.path).admin_code_detail(code["code_id"], now=env.now)["code"] == code["code"]
