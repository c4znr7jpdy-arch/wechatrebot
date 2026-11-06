from concurrent.futures import ThreadPoolExecutor
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from services.activation_service.store import LicenseStore, MIGRATION_CREDITS, lease_payload
from wechatrebot_manager.licensing.protocol import LicenseError, fingerprint, public_key
from test_activation_service import env, issue, redeem


def quota(total, used, *, remaining=None):
    return {"migration_total": total, "migration_used": used,
            "migration_remaining": max(0, total - used) if remaining is None else remaining}


def migration(env, license_id, *, device=None, reset_account=False):
    return issue(env, plan=None, mode="migration", license_id=license_id,
                 target_device=device or env.device, reset_account=reset_account)


def legacy_redemption(env, license_id, *, mode="migration", plan="month", redeemed_at=None, code_id=None):
    """Import a pre-enforcement ledger event without relying on new-code rules."""
    code_id = code_id or secrets.token_hex(16)
    redeemed_at = env.now - 50 if redeemed_at is None else redeemed_at
    with env.store.connection(write=True) as db:
        db.execute(
            "INSERT INTO codes(id,digest,reference_hash,owner_hash,plan,mode,target_id,issued_at,"
            "redeem_by,redeemed_at,redeemed_license) SELECT ?,?,?,owner_hash,?,?,id,?,?,?,id "
            "FROM licenses WHERE id=?",
            (code_id, "digest-" + code_id, "reference-" + code_id, plan, mode,
             redeemed_at - 100, redeemed_at + 100, redeemed_at, license_id),
        )


def test_month_credit_is_granted_only_on_first_successful_purchase_and_spent_on_migration(env):
    purchase = issue(env)
    assert env.store.admin_code_detail(purchase["code_id"], now=env.now)["migration_total"] is None
    original = redeem(env, purchase)
    license_id = original["id"]
    assert env.store.migration_quota(license_id) == quota(1, 0)
    assert redeem(env, purchase) == original
    assert env.store.migration_quota(license_id) == quota(1, 0)
    code = migration(env, license_id)
    preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
    assert "当前剩余 1 次" in preview["message"] and "发码和预览不扣" in preview["message"]
    assert env.store.migration_quota(license_id) == quota(1, 0)
    moved = env.store.redeem(env.device, env.pub, {"code": code["code"], "preview_id": preview["preview_id"]}, env.now)
    assert moved["valid_until"] == original["valid_until"]
    assert env.store.migration_quota(license_id) == quota(1, 1)
    assert env.store.redeem(env.device, env.pub, {"code": code["code"], "preview_id": preview["preview_id"]}, env.now) == moved
    assert redeem(env, code) == moved
    assert env.store.migration_quota(license_id) == quota(1, 1)
    with pytest.raises(LicenseError) as caught:
        migration(env, license_id)
    assert caught.value.code == "code_unavailable"
    recovered = redeem(env, issue(env, plan=None, mode="recovery", license_id=license_id))
    assert recovered == moved and env.store.migration_quota(license_id) == quota(1, 1)


def test_year_grants_ten_migrations_including_same_device_account_reset(env):
    assert MIGRATION_CREDITS == {"month": 1, "year": 10}
    original = redeem(env, issue(env, plan="year"))
    for used in range(1, 11):
        code = migration(env, original["id"], reset_account=used % 2 == 0)
        assert env.store.migration_quota(original["id"]) == quota(10, used - 1)
        assert redeem(env, code)["id"] == original["id"]
        assert env.store.migration_quota(original["id"]) == quota(10, used)
    with pytest.raises(LicenseError, match="换绑次数已用完"):
        migration(env, original["id"])


def test_successful_renewals_accumulate_leftover_quota_across_plan_changes_and_expiry(env):
    original = redeem(env, issue(env))
    license_id = original["id"]
    monthly = issue(env, mode="renewal", license_id=license_id)
    assert env.store.migration_quota(license_id) == quota(1, 0)
    redeem(env, monthly)
    assert env.store.migration_quota(license_id) == quota(2, 0)
    redeem(env, migration(env, license_id))
    assert env.store.migration_quota(license_id) == quota(2, 1)
    yearly = issue(env, plan="year", mode="renewal", license_id=license_id)
    paid = redeem(env, yearly)
    assert paid["plan"] == "year" and env.store.migration_quota(license_id) == quota(12, 1)
    assert redeem(env, yearly) == paid and env.store.migration_quota(license_id) == quota(12, 1)
    env.now = paid["valid_until"] + 1
    assert env.store.migration_quota(license_id) == quota(12, 1)
    redeem(env, issue(env, plan="month", mode="renewal", license_id=license_id))
    assert env.store.migration_quota(license_id) == quota(13, 1)


def test_new_device_migration_moves_the_same_ledger_and_keeps_other_licenses_separate(env):
    original = redeem(env, issue(env))
    key = Ed25519PrivateKey.generate()
    device, pub = fingerprint(public_key(key)), public_key(key)
    other = redeem(env, issue(env, plan="year", owner="different-customer"), device=device, pub=pub)
    code = migration(env, original["id"], device=device)
    moved = redeem(env, code, device=device, pub=pub)
    assert moved["id"] == original["id"] and moved["device"] == device
    assert env.store.migration_quota(original["id"]) == quota(1, 1)
    assert env.store.migration_quota(other["id"]) == quota(10, 0)


@pytest.mark.parametrize("failure", ["wrong_device", "missing_preview", "expired_preview", "revoked", "expired_code", "write_failure"])
def test_failed_or_unredeemed_operations_never_consume_migration_quota(env, monkeypatch, failure):
    original = redeem(env, issue(env))
    code = migration(env, original["id"])
    preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
    body = {"code": code["code"], "preview_id": preview["preview_id"]}
    if failure == "wrong_device":
        operation = lambda: env.store.preview("a" * 64, {"code": code["code"]}, env.now)
    elif failure == "missing_preview":
        body["preview_id"] = "missing-preview"
        operation = lambda: env.store.redeem(env.device, env.pub, body, env.now)
    elif failure == "expired_preview":
        operation = lambda: env.store.redeem(env.device, env.pub, body, env.now + 301)
    elif failure == "revoked":
        env.store.revoke(now=env.now, code_id=code["code_id"])
        operation = lambda: env.store.redeem(env.device, env.pub, body, env.now)
    elif failure == "expired_code":
        operation = lambda: env.store.preview(env.device, {"code": code["code"]}, code["redeem_by"])
    else:
        def fail_after_grant(db, grant, target, device, pub, now):
            LicenseStore._apply_grant(db, grant, target, device, pub, now)
            raise OSError("simulated transaction failure")
        monkeypatch.setattr(env.store, "_apply_grant", fail_after_grant)
        operation = lambda: env.store.redeem(env.device, env.pub, body, env.now)
    with pytest.raises((LicenseError, OSError)):
        operation()
    assert env.store.migration_quota(original["id"]) == quota(1, 0)
    assert env.store.refresh(env.device, {"license_id": original["id"]}, env.now) == original


def test_final_redemption_rechecks_ledger_after_preview(env):
    original = redeem(env, issue(env))
    code = migration(env, original["id"])
    preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
    # A previously successful historical event becomes visible after preview.
    legacy_redemption(env, original["id"], redeemed_at=env.now)
    with pytest.raises(LicenseError) as caught:
        env.store.redeem(env.device, env.pub, {"code": code["code"], "preview_id": preview["preview_id"]}, env.now)
    assert caught.value.code == "code_unavailable"
    with pytest.raises(LicenseError) as caught:
        env.store.preview(env.device, {"code": code["code"]}, env.now)
    assert caught.value.code == "code_unavailable"
    assert env.store.migration_quota(original["id"]) == quota(1, 1)


@pytest.mark.parametrize("same_code", [True, False])
def test_concurrent_redemptions_cannot_spend_more_than_remaining_quota(env, same_code):
    original = redeem(env, issue(env))
    first = migration(env, original["id"])
    codes = [first, first if same_code else migration(env, original["id"])]
    requests = []
    for code in codes:
        preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
        requests.append({"code": code["code"], "preview_id": preview["preview_id"]})
    def attempt(body):
        try:
            return env.store.redeem(env.device, env.pub, body, env.now)
        except LicenseError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, requests))
    assert sum(isinstance(row, dict) for row in results) == (2 if same_code else 1)
    assert env.store.migration_quota(original["id"]) == quota(1, 1)


def test_legacy_redemptions_are_recounted_without_encrypted_history_or_schema_changes(env):
    original = redeem(env, issue(env))
    license_id = original["id"]
    env.now += 200
    for mode, plan in (("renewal", "year"), ("migration", "month"), ("migration", "trial"), ("recovery", "year")):
        # This purchase sorts after migration IDs, so the same-second credit-first
        # convention must take precedence over the deterministic ID tiebreaker.
        legacy_redemption(env, license_id, mode=mode, plan=plan, code_id="f" * 32 if mode == "renewal" else None)
    with env.store.connection(write=True) as db:
        db.execute("UPDATE codes SET encrypted_payload=''")
    env.store = LicenseStore(env.path)
    assert env.store.migration_quota(license_id) == quota(11, 1)
    assert not env.store.admin_code_detail(env.store.inspect(license_id=license_id)["codes"][0]["id"], now=env.now)["history_available"]
    with env.store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    for _ in range(12):
        legacy_redemption(env, license_id)
    assert env.store.migration_quota(license_id) == quota(11, 13)
    with pytest.raises(LicenseError, match="换绑次数已用完"):
        migration(env, license_id)
    # Historical excess is kept in used, but cannot consume new paid credits.
    renewal = issue(env, plan="month", mode="renewal", license_id=license_id)
    renewed = redeem(env, renewal)
    assert env.store.migration_quota(license_id) == quota(12, 13, remaining=1)
    env.store = LicenseStore(env.path)
    assert redeem(env, renewal) == renewed
    assert env.store.migration_quota(license_id) == quota(12, 13, remaining=1)
    assert redeem(env, migration(env, license_id))["id"] == license_id
    assert env.store.migration_quota(license_id) == quota(12, 14, remaining=0)
    # This is a later event; pre-enforcement timestamps were second-resolution.
    env.now += 1
    yearly = issue(env, plan="year", mode="renewal", license_id=license_id)
    paid = redeem(env, yearly)
    assert env.store.migration_quota(license_id) == quota(22, 14, remaining=10)
    env.store = LicenseStore(env.path)
    assert redeem(env, yearly) == paid
    assert env.store.migration_quota(license_id) == quota(22, 14, remaining=10)


def test_expired_trial_can_migrate_after_reinstallation_then_renew_to_paid(env):
    account = "wxid_trial_migration"
    original = env.store.claim_trial(env.device, env.pub, {"account": account, "machine_id": env.device}, env.now)
    license_id = original["id"]
    assert env.store.migration_quota(license_id) == quota(0, 0)
    env.now = original["valid_until"] + 1
    key = Ed25519PrivateKey.generate()
    device, pub = fingerprint(public_key(key)), public_key(key)
    code = issue(env, plan=None, owner=account, mode="migration", license_id=license_id, target_device=device)
    moved = redeem(env, code, device=device, pub=pub)
    assert moved["valid_until"] == original["valid_until"] and env.store.migration_quota(license_id) == quota(0, 0)
    renewal = issue(env, owner=account, plan="month", mode="renewal", license_id=license_id)
    paid = redeem(env, renewal, device=device, pub=pub)
    assert paid["plan"] == "month" and env.store.migration_quota(license_id) == quota(1, 0)
    detail = env.store.admin_code_detail(code["code_id"], now=env.now)
    assert detail["plan"] == "trial" and detail["license_plan"] == "month"


def test_admin_queries_share_quota_without_changing_signed_lease(env):
    code = issue(env)
    unassociated = env.store.admin_code_detail(code["code_id"], now=env.now)
    assert unassociated["license_plan"] == ""
    assert all(unassociated[name] is None for name in quota(0, 0))
    original = redeem(env, code)
    moved = redeem(env, migration(env, original["id"]))
    expected = quota(1, 1)
    for record in (env.store.inspect(license_id=original["id"])["licenses"][0],
                   env.store.inspect(owner_ref="qq-customer-1")["licenses"][0],
                   env.store.admin_code_detail(code["code_id"], now=env.now)):
        assert {name: record[name] for name in expected} == expected
    rows = env.store.admin_code_history(now=env.now)["items"]
    assert len(rows) == 2 and all({name: row[name] for name in expected} == expected for row in rows)
    assert all(row["license_plan"] == "month" for row in rows)
    assert not expected.keys() & lease_payload(moved, "test-nonce", env.now).keys()
    env.store.revoke(now=env.now, license_id=original["id"])
    assert env.store.migration_quota(original["id"]) == expected


def test_unknown_license_quota_uses_existing_domain_error(env):
    with pytest.raises(LicenseError) as caught:
        env.store.migration_quota("missing-license")
    assert caught.value.code == "license_unavailable"
