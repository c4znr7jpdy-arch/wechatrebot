from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import secrets
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
import pytest_asyncio

from services.activation_service import app as app_module
from services.activation_service.app import create_app
from services.activation_service.cli import main
from services.activation_service.store import LicenseStore, code_digest, lease_payload
from wechatrebot_manager.licensing.protocol import (
    LicenseError, fingerprint, public_key, sign_request, verify_lease,
)


def timestamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


@pytest.fixture
def env(tmp_path):
    key = Ed25519PrivateKey.generate()
    return SimpleNamespace(
        store=LicenseStore(tmp_path / "activation.db"), path=tmp_path / "activation.db",
        key=key, device=fingerprint(public_key(key)), pub=public_key(key),
        signer=Ed25519PrivateKey.generate(), now=timestamp("2026-01-31T10:20:30"),
    )


def issue(env, *, plan="month", owner="qq-customer-1", mode="new", license_id="", **kwargs):
    return env.store.issue_code(owner_ref=owner, reference=secrets.token_hex(8), now=env.now,
                                plan=plan, mode=mode, license_id=license_id, **kwargs)


def redeem(env, code, *, device=None, pub=None, now=None):
    device, pub, now = device or env.device, pub or env.pub, env.now if now is None else now
    preview = env.store.preview(device, {"code": code["code"]}, now)
    record = env.store.redeem(device, pub, {"code": code["code"], "preview_id": preview["preview_id"]}, now)
    return record


def assert_error(code, call):
    with pytest.raises(LicenseError) as caught:
        call()
    assert caught.value.code == code


@pytest_asyncio.fixture
async def api(env):
    client = TestClient(TestServer(create_app(env.store, env.signer, "test-key", clock=lambda: env.now)))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def request(api, env, action, body, key=None):
    envelope = sign_request(key or env.key, action, body, env.now, secrets.token_hex(16))
    response = await api.post("/v1/" + action, json=envelope)
    return response, await response.json(), envelope


def test_issue_hashes_codes_and_customer_references(env):
    code = issue(env)
    with env.store.connection() as db:
        raw = json.dumps(dict(db.execute("SELECT * FROM codes").fetchone()))
    assert code["code"] not in raw and "qq-customer-1" not in raw
    assert env.store.inspect(owner_ref="qq-customer-1")["codes"][0]["id"] == code["code_id"]


def test_admin_reference_is_idempotent_and_error_does_not_expose_code(env):
    kwargs = dict(owner_ref="customer", reference="manual-order-1", plan="month", now=env.now)
    first = env.store.issue_code(**kwargs)
    assert_error("reference_used", lambda: env.store.issue_code(**kwargs))
    assert env.store.inspect()["codes"] == 1
    assert len(first["code"].replace("-", "")) == 35


def test_preview_is_non_consuming_and_new_grant_starts_at_redemption(env):
    code = issue(env, plan="trial")
    preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
    assert preview["valid_until"] == env.now + 3600 and preview["license_id"] == ""
    assert env.store.inspect()["licenses"] == 0
    record = env.store.redeem(env.device, env.pub, {"code": code["code"], "preview_id": preview["preview_id"]}, env.now + 10)
    assert record["valid_until"] == env.now + 3610 and record["account"] == ""
    payload = lease_payload(record, "a" * 32, env.now + 10)
    assert payload["lease_until"] == record["valid_until"]


def test_same_device_redeem_retry_restores_after_code_expiry_without_more_time(env):
    code = issue(env, redeem_by=env.now + 5)
    first = redeem(env, code)
    env.now += 90 * 86400
    repeat = redeem(env, code)
    assert repeat["id"] == first["id"] and repeat["valid_until"] == first["valid_until"]
    assert repeat["revision"] == first["revision"]


def test_code_cannot_be_redeemed_from_other_device(env):
    code = issue(env)
    redeem(env, code)
    other = Ed25519PrivateKey.generate()
    assert_error("code_unavailable", lambda: redeem(env, code, device=fingerprint(public_key(other)), pub=public_key(other)))


@pytest.mark.parametrize("case", ["expired", "wrong_code", "wrong_device", "revoked", "bad_target"])
def test_invalid_preview_or_code_does_not_consume(env, case):
    code = issue(env)
    preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
    device, now, code_text = env.device, env.now, code["code"]
    expected = "preview_required"
    if case == "expired":
        now += 300
        expected = "preview_stale"
    elif case == "wrong_code":
        code_text = issue(env)["code"]
    elif case == "wrong_device":
        device = "1" * 64
    elif case == "revoked":
        env.store.revoke(now=env.now, code_id=code["code_id"])
        expected = "code_unavailable"
    else:
        assert_error("code_unavailable", lambda: env.store.preview(env.device, {"code": code_text, "license_id": "other"}, now))
        return
    assert_error(expected, lambda: env.store.redeem(device, env.pub, {"code": code_text, "preview_id": preview["preview_id"]}, now))
    assert env.store.inspect()["licenses"] == 0


def test_month_renewal_preserves_anchor_through_short_month(env):
    original = redeem(env, issue(env))
    assert original["valid_until"] == timestamp("2026-02-28T10:20:30")
    code = issue(env, mode="renewal", license_id=original["id"])
    renewed = redeem(env, code)
    assert renewed["id"] == original["id"]
    assert renewed["valid_until"] == timestamp("2026-03-31T10:20:30")
    year = redeem(env, issue(env, plan="year", mode="renewal", license_id=original["id"]))
    assert year["valid_until"] == timestamp("2027-03-31T10:20:30")


def test_expired_subscription_renews_from_now_and_bound_account_survives(env):
    original = redeem(env, issue(env))
    env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_first"}, env.now)
    env.now = timestamp("2026-04-12T09:00:00")
    renewed = redeem(env, issue(env, mode="renewal", license_id=original["id"]))
    assert renewed["id"] == original["id"] and renewed["account"] == "wxid_first"
    assert renewed["valid_until"] == timestamp("2026-05-12T09:00:00")


def test_trial_to_paid_anchor_uses_start_of_paid_period(env):
    env.now = timestamp("2026-01-31T23:30:00")
    original = redeem(env, issue(env, plan="trial"))
    renewed = redeem(env, issue(env, mode="renewal", license_id=original["id"]))
    assert renewed["valid_until"] == timestamp("2026-03-01T00:30:00")
    renewed = redeem(env, issue(env, mode="renewal", license_id=original["id"]))
    assert renewed["valid_until"] == timestamp("2026-04-01T00:30:00")


def test_renewal_target_owner_and_existing_new_code_mismatch_are_rejected(env):
    original = redeem(env, issue(env))
    assert_error("owner_mismatch", lambda: issue(env, owner="other", mode="renewal", license_id=original["id"]))
    new_code = issue(env)
    assert_error("code_unavailable", lambda: env.store.preview(env.device, {"code": new_code["code"], "license_id": original["id"]}, env.now))


def test_changed_revision_requires_preview_again(env):
    original = redeem(env, issue(env))
    code = issue(env, mode="renewal", license_id=original["id"])
    preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
    env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_first"}, env.now)
    assert_error("preview_stale", lambda: env.store.redeem(env.device, env.pub, {"code": code["code"], "preview_id": preview["preview_id"]}, env.now))
    assert redeem(env, code)["id"] == original["id"]


def test_reviving_expired_license_cannot_duplicate_an_active_account(env):
    original = redeem(env, issue(env))
    env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_shared"}, env.now)
    env.now = original["valid_until"] + 1
    other = redeem(env, issue(env, owner="other-customer"))
    env.store.bind(env.device, {"license_id": other["id"], "account": "wxid_shared"}, env.now)
    renewal = issue(env, mode="renewal", license_id=original["id"])
    assert_error("account_unavailable", lambda: redeem(env, renewal))
    assert env.store.refresh(env.device, {"license_id": original["id"]}, env.now)["valid_until"] == original["valid_until"]


@pytest.mark.parametrize("identity", ["owner", "device", "account"])
def test_trial_cannot_reset_through_reissue(env, identity):
    original = redeem(env, issue(env, plan="trial"))
    env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_trial"}, env.now)
    env.now += 7200
    if identity == "owner":
        assert_error("trial_used", lambda: issue(env, plan="trial"))
    elif identity == "device":
        other = issue(env, plan="trial", owner="new-customer")
        assert_error("trial_used", lambda: redeem(env, other))
    else:
        key = Ed25519PrivateKey.generate()
        device = fingerprint(public_key(key))
        other = redeem(env, issue(env, plan="trial", owner="new-customer"), device=device, pub=public_key(key))
        assert_error("trial_used", lambda: env.store.bind(device, {"license_id": other["id"], "account": "WXID_TRIAL"}, env.now))


def test_recovery_preserves_expired_term_and_cannot_move_device(env):
    original = redeem(env, issue(env, plan="trial"))
    code = issue(env, plan=None, mode="recovery", license_id=original["id"])
    assert_error("code_unavailable", lambda: env.store.preview("1" * 64, {"code": code["code"]}, env.now))
    env.now += 7200
    restored = redeem(env, code)
    assert restored["id"] == original["id"] and restored["valid_until"] == original["valid_until"]
    assert restored["device"] == original["device"]


def test_migration_is_admin_bound_and_old_device_cannot_reclaim(env):
    original = redeem(env, issue(env))
    original = env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_first"}, env.now)
    new_key = Ed25519PrivateKey.generate()
    new_device, new_pub = fingerprint(public_key(new_key)), public_key(new_key)
    code = issue(env, plan=None, mode="migration", license_id=original["id"], target_device=new_device)
    assert_error("code_unavailable", lambda: redeem(env, code))
    moved = redeem(env, code, device=new_device, pub=new_pub)
    assert moved["id"] == original["id"] and moved["valid_until"] == original["valid_until"]
    assert moved["account"] == original["account"] and moved["device"] == new_device
    assert_error("license_unavailable", lambda: env.store.refresh(env.device, {"license_id": original["id"]}, env.now))
    assert redeem(env, code, device=new_device, pub=new_pub)["revision"] == moved["revision"]


def test_migration_account_reset_does_not_reset_trial(env):
    original = redeem(env, issue(env, plan="trial"))
    original = env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_first"}, env.now)
    code = issue(env, plan=None, mode="migration", license_id=original["id"], target_device=env.device, reset_account=True)
    moved = redeem(env, code)
    assert moved["account"] == "" and moved["valid_until"] == original["valid_until"]
    rebound = env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_second"}, env.now)
    assert rebound["valid_until"] == original["valid_until"]


def test_stale_migration_code_cannot_override_newer_authorization(env):
    original = redeem(env, issue(env))
    migration = issue(env, plan=None, mode="migration", license_id=original["id"], target_device=env.device)
    redeem(env, issue(env, mode="renewal", license_id=original["id"]))
    assert_error("code_stale", lambda: redeem(env, migration))


def migration_with_cached_license(env, *, cached_plan="trial", cached_owner="qq-customer-1"):
    original = redeem(env, issue(env, plan="year"))
    original = env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_original"}, env.now)
    key = Ed25519PrivateKey.generate()
    device, pub = fingerprint(public_key(key)), public_key(key)
    cached_code = issue(env, plan=cached_plan, owner=cached_owner)
    cached = redeem(env, cached_code, device=device, pub=pub)
    code = issue(env, plan=None, mode="migration", license_id=original["id"], target_device=device)
    return SimpleNamespace(original=original, cached=cached, cached_code=cached_code,
                           key=key, device=device, pub=pub, code=code)


def migration_preview(env, migration):
    return env.store.preview(migration.device, {
        "code": migration.code["code"], "license_id": migration.cached["id"],
    }, env.now)


@pytest.mark.parametrize("cached_plan", ["trial", "month"])
def test_migration_can_explicitly_replace_same_owner_trial_or_expired_license(env, cached_plan):
    migration = migration_with_cached_license(env, cached_plan=cached_plan)
    cached = env.store.bind(migration.device, {
        "license_id": migration.cached["id"], "account": "wxid_cached",
    }, env.now)
    if cached_plan == "month":
        env.now = cached["valid_until"]
    with env.store.connection() as db:
        trials = [tuple(row) for row in db.execute("SELECT * FROM trials ORDER BY kind,identity_hash")]
    preview = migration_preview(env, migration)
    assert preview["replaces_license_id"] == cached["id"]
    assert preview["license_id"] == migration.original["id"] and preview["mode"] == "migration"
    assert preview["valid_until"] == migration.original["valid_until"]
    assert env.store.refresh(migration.device, {"license_id": cached["id"]}, env.now)["status"] == "active"
    moved = env.store.redeem(migration.device, migration.pub, {
        "code": migration.code["code"], "preview_id": preview["preview_id"],
    }, env.now)
    assert moved["id"] == migration.original["id"] and moved["device"] == migration.device
    assert moved["valid_until"] == migration.original["valid_until"] and moved["account"] == "wxid_original"
    retired = env.store.refresh(migration.device, {"license_id": cached["id"]}, env.now)
    assert retired["status"] == "revoked" and retired["revision"] == cached["revision"] + 1
    with env.store.connection() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM trials ORDER BY kind,identity_hash")] == trials
        code = db.execute("SELECT * FROM codes WHERE id=?", (migration.code["code_id"],)).fetchone()
        assert code["redeemed_replaces_id"] == cached["id"]
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='replace_license'").fetchone()[0] == 1
    assert_error("license_unavailable", lambda: env.store.refresh(env.device, {"license_id": moved["id"]}, env.now))
    assert_error("license_revoked", lambda: redeem(env, migration.cached_code, device=migration.device, pub=migration.pub))
    if cached_plan == "trial":
        assert_error("trial_used", lambda: issue(env, plan="trial"))


@pytest.mark.parametrize("case,expected", [
    ("other_owner", "code_unavailable"), ("other_device", "code_unavailable"),
    ("active_paid", "active_license_conflict"), ("revoked", "license_revoked"),
    ("missing", "code_unavailable"),
])
def test_migration_replacement_rejects_unrelated_or_live_paid_rights(env, case, expected):
    migration = migration_with_cached_license(
        env, cached_plan="month" if case == "active_paid" else "trial",
        cached_owner="other-customer" if case == "other_owner" else "qq-customer-1",
    )
    if case == "revoked":
        env.store.revoke(now=env.now, license_id=migration.cached["id"])
    supplied = "f" * 32 if case == "missing" else migration.cached["id"]
    device = env.device if case == "other_device" else migration.device
    assert_error(expected, lambda: env.store.preview(device, {
        "code": migration.code["code"], "license_id": supplied,
    }, env.now))
    assert env.store.refresh(env.device, {"license_id": migration.original["id"]}, env.now)["device"] == env.device
    with env.store.connection() as db:
        assert db.execute("SELECT redeemed_at FROM codes WHERE id=?", (migration.code["code_id"],)).fetchone()[0] is None


@pytest.mark.parametrize("mode", ["new", "renewal", "recovery"])
def test_only_device_bound_migration_can_replace_a_cached_id(env, mode):
    migration = migration_with_cached_license(env)
    code = issue(env, plan="year", mode=mode,
                 license_id=migration.original["id"] if mode != "new" else "")
    assert_error("code_unavailable", lambda: env.store.preview(env.device, {
        "code": code["code"], "license_id": migration.cached["id"],
    }, env.now))


@pytest.mark.parametrize("changed,expected", [("source", "code_stale"), ("replacement", "preview_stale")])
def test_migration_rechecks_both_license_revisions_before_retiring_anything(env, changed, expected):
    migration = migration_with_cached_license(env)
    preview = migration_preview(env, migration)
    if changed == "source":
        redeem(env, issue(env, mode="renewal", license_id=migration.original["id"]))
    else:
        env.store.bind(migration.device, {"license_id": migration.cached["id"], "account": "wxid_cached"}, env.now)
    assert_error(expected, lambda: env.store.redeem(migration.device, migration.pub, {
        "code": migration.code["code"], "preview_id": preview["preview_id"],
    }, env.now))
    assert env.store.refresh(migration.device, {"license_id": migration.cached["id"]}, env.now)["status"] == "active"
    assert env.store.refresh(env.device, {"license_id": migration.original["id"]}, env.now)["device"] == env.device


def test_migration_replacement_and_consumption_roll_back_together(env, monkeypatch):
    migration = migration_with_cached_license(env)
    preview = migration_preview(env, migration)
    body = {"code": migration.code["code"], "preview_id": preview["preview_id"]}

    def fail_grant(*args):
        raise OSError("simulated ledger write failure")

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "_apply_grant", fail_grant)
        with pytest.raises(OSError):
            env.store.redeem(migration.device, migration.pub, body, env.now)
    assert env.store.refresh(migration.device, {"license_id": migration.cached["id"]}, env.now) == migration.cached
    assert env.store.refresh(env.device, {"license_id": migration.original["id"]}, env.now) == migration.original
    with env.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='replace_license'").fetchone()[0] == 0
        assert db.execute("SELECT redeemed_at FROM codes WHERE id=?", (migration.code["code_id"],)).fetchone()[0] is None
    assert env.store.redeem(migration.device, migration.pub, body, env.now)["id"] == migration.original["id"]


@pytest.mark.asyncio
async def test_lost_migration_response_recovers_only_the_recorded_replacement(api, env, monkeypatch):
    migration = migration_with_cached_license(env)
    _, result, _ = await request(api, env, "preview", {
        "code": migration.code["code"], "license_id": migration.cached["id"],
    }, migration.key)
    body = {"code": migration.code["code"], "preview_id": result["data"]["preview_id"]}

    def fail_signer(*args):
        raise OSError("simulated signing failure")

    with monkeypatch.context() as patch:
        patch.setattr(app_module, "issue_lease", fail_signer)
        response, _, _ = await request(api, env, "redeem", body, migration.key)
        assert response.status == 503
    retired = env.store.refresh(migration.device, {"license_id": migration.cached["id"]}, env.now)
    assert retired["status"] == "revoked"
    env.now = migration.code["redeem_by"] + 1
    response, result, _ = await request(api, env, "redeem", body, migration.key)
    assert response.status == 200
    restored = verify_lease(result["data"]["lease"], {"test-key": public_key(env.signer)}, migration.device)
    env.store = LicenseStore(env.path)
    preview = migration_preview(env, migration)
    assert preview["already_redeemed"] and preview["replaces_license_id"] == retired["id"]
    repeated = env.store.redeem(migration.device, migration.pub, {
        "code": migration.code["code"], "preview_id": preview["preview_id"],
    }, env.now)
    assert restored["license_id"] == repeated["id"] == migration.original["id"]
    assert repeated["valid_until"] == migration.original["valid_until"]
    assert repeated["revision"] == migration.original["revision"] + 1
    assert_error("code_unavailable", lambda: env.store.preview(migration.device, {
        "code": migration.code["code"], "license_id": "f" * 32,
    }, env.now))
    assert env.store.refresh(migration.device, {"license_id": retired["id"]}, env.now)["revision"] == retired["revision"]


def test_concurrent_migrations_replace_cached_trial_once(env):
    migration = migration_with_cached_license(env)
    second = issue(env, plan=None, mode="migration", license_id=migration.original["id"], target_device=migration.device)
    requests = []
    for code in (migration.code, second):
        preview = env.store.preview(migration.device, {"code": code["code"], "license_id": migration.cached["id"]}, env.now)
        requests.append({"code": code["code"], "preview_id": preview["preview_id"]})

    def execute(body):
        try:
            return env.store.redeem(migration.device, migration.pub, body, env.now)
        except LicenseError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, requests))
    assert len([row for row in results if isinstance(row, dict)]) == 1
    assert "code_stale" in results
    assert env.store.refresh(migration.device, {"license_id": migration.cached["id"]}, env.now)["revision"] == migration.cached["revision"] + 1


def test_v1_database_migration_preserves_existing_code_and_preview(env, tmp_path):
    path = tmp_path / "legacy.db"
    code = "WRB-" + "A" * 32
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE licenses (
                id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, device TEXT NOT NULL,
                public_key TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
                account_hash TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL,
                valid_until INTEGER NOT NULL, anchor_day INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE codes (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, reference_hash TEXT NOT NULL UNIQUE,
                owner_hash TEXT NOT NULL, plan TEXT NOT NULL, mode TEXT NOT NULL, target_id TEXT,
                target_device TEXT NOT NULL DEFAULT '', target_revision INTEGER,
                reset_account INTEGER NOT NULL DEFAULT 0, issued_at INTEGER NOT NULL,
                redeem_by INTEGER NOT NULL, revoked_at INTEGER, redeemed_at INTEGER,
                redeemed_license TEXT, redeemed_device TEXT,
                FOREIGN KEY(target_id) REFERENCES licenses(id),
                FOREIGN KEY(redeemed_license) REFERENCES licenses(id)
            );
            CREATE TABLE previews (
                id TEXT PRIMARY KEY, code_id TEXT NOT NULL, device TEXT NOT NULL,
                license_id TEXT NOT NULL, revision INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                FOREIGN KEY(code_id) REFERENCES codes(id)
            );
            PRAGMA user_version=1;
        """)
        db.execute("INSERT INTO codes(id,digest,reference_hash,owner_hash,plan,mode,issued_at,redeem_by) "
                   "VALUES(?,?,?,?,?,?,?,?)", ("old-code", code_digest(code), "reference", "owner", "month", "new", env.now, env.now + 86400))
        db.execute("INSERT INTO previews VALUES(?,?,?,?,?,?)", ("old-preview", "old-code", env.device, "", 0, env.now + 300))
    store = LicenseStore(path)
    record = store.redeem(env.device, env.pub, {"code": code, "preview_id": "old-preview"}, env.now)
    assert record["plan"] == "month"
    reopened = LicenseStore(path)
    assert reopened.refresh(env.device, {"license_id": record["id"]}, env.now) == record
    with reopened.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT redeemed_replaces_id FROM codes WHERE id='old-code'").fetchone()[0] is None


def test_revoked_license_cannot_be_restored_or_renewed(env):
    original = redeem(env, issue(env))
    code = issue(env, mode="renewal", license_id=original["id"])
    env.store.revoke(now=env.now, license_id=original["id"])
    assert_error("license_revoked", lambda: redeem(env, code))
    assert_error("license_revoked", lambda: issue(env, mode="renewal", license_id=original["id"]))
    record = env.store.refresh(env.device, {"license_id": original["id"]}, env.now)
    assert record["status"] == "revoked" and record["valid_until"] == original["valid_until"]


@pytest.mark.parametrize("same_device", [True, False])
def test_concurrent_code_redemption_has_exactly_one_grant(env, same_device):
    code = issue(env)
    keys = [env.key, env.key if same_device else Ed25519PrivateKey.generate()]
    requests = []
    for key in keys:
        device = fingerprint(public_key(key))
        preview = env.store.preview(device, {"code": code["code"]}, env.now)
        requests.append((device, public_key(key), {"code": code["code"], "preview_id": preview["preview_id"]}))

    def execute(args):
        try:
            return env.store.redeem(*args, env.now)
        except LicenseError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, requests))
    successes = [row for row in results if isinstance(row, dict)]
    assert len(successes) == (2 if same_device else 1)
    assert len({row["id"] for row in successes}) == 1
    assert env.store.inspect()["licenses"] == 1


def test_nonce_and_rate_limits_survive_restart_and_have_bounded_storage(env):
    env.store.accept_nonce(env.device, "a" * 32, env.now)
    assert env.store.allow_peer("127.0.0.1", env.now, requests=1)
    reopened = LicenseStore(env.path)
    assert_error("request_replayed", lambda: reopened.accept_nonce(env.device, "a" * 32, env.now + 300))
    assert not reopened.allow_peer("127.0.0.1", env.now, requests=1)
    assert reopened.allow_peer("127.0.0.2", env.now, requests=1, max_peers=2)
    assert not reopened.allow_peer("127.0.0.3", env.now, requests=1, max_peers=2)
    assert reopened.allow_peer("127.0.0.3", env.now + 61, requests=1, max_peers=2)
    reopened.accept_nonce(env.device, "a" * 32, env.now + 602)
    with reopened.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0] == 1


def test_two_preissued_trials_for_one_owner_cannot_grant_twice(env):
    codes = [issue(env, plan="trial"), issue(env, plan="trial")]
    other = Ed25519PrivateKey.generate()
    requests = []
    for code, key in zip(codes, [env.key, other]):
        device = fingerprint(public_key(key))
        preview = env.store.preview(device, {"code": code["code"]}, env.now)
        requests.append((device, public_key(key), {"code": code["code"], "preview_id": preview["preview_id"]}))

    def execute(args):
        try:
            return env.store.redeem(*args, env.now)
        except LicenseError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, requests))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "trial_used" in results and env.store.inspect()["licenses"] == 1


def test_grant_and_consumption_roll_back_together(env, monkeypatch):
    code = issue(env)
    preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
    body = {"code": code["code"], "preview_id": preview["preview_id"]}

    def fail_audit(*args, **kwargs):
        raise OSError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "_audit", fail_audit)
        with pytest.raises(OSError):
            env.store.redeem(env.device, env.pub, body, env.now)
    assert env.store.inspect()["licenses"] == 0
    assert env.store.inspect(owner_ref="qq-customer-1")["codes"][0]["redeemed_at"] is None
    assert env.store.redeem(env.device, env.pub, body, env.now)["plan"] == "month"


@pytest.mark.asyncio
async def test_api_full_trial_expiry_renewal_bind_and_revocation(api, env):
    code = issue(env, plan="trial")
    response, preview, _ = await request(api, env, "preview", {"code": code["code"]})
    assert response.status == 200
    _, data, envelope = await request(api, env, "redeem", {"code": code["code"], "preview_id": preview["data"]["preview_id"]})
    keys = {"test-key": public_key(env.signer)}
    lease = verify_lease(data["data"]["lease"], keys, env.device)
    assert lease["account"] == "" and lease["request_nonce"] == envelope["nonce"]
    license_id = lease["license_id"]
    _, data, _ = await request(api, env, "bind", {"license_id": license_id, "account": "wxid_bot"})
    bound = verify_lease(data["data"]["lease"], keys, env.device)
    assert bound["account"] == "wxid_bot" and bound["revision"] == 2
    env.now += 3600
    _, data, _ = await request(api, env, "refresh", {"license_id": license_id})
    expired = verify_lease(data["data"]["lease"], keys, env.device)
    assert expired["lease_until"] == expired["valid_until"] == env.now
    renewed = redeem(env, issue(env, mode="renewal", license_id=license_id))
    assert renewed["valid_until"] > env.now
    env.store.revoke(now=env.now, license_id=license_id)
    _, data, _ = await request(api, env, "refresh", {"license_id": license_id})
    revoked = verify_lease(data["data"]["lease"], keys, env.device)
    assert revoked["status"] == "revoked"


@pytest.mark.asyncio
async def test_lost_signing_response_keeps_consumed_code_recoverable(api, env, monkeypatch, caplog):
    code = issue(env)
    _, result, _ = await request(api, env, "preview", {"code": code["code"]})
    body = {"code": code["code"], "preview_id": result["data"]["preview_id"]}
    original_signer = app_module.issue_lease

    def failed_signer(*args):
        raise OSError(code["code"])

    monkeypatch.setattr(app_module, "issue_lease", failed_signer)
    response, result, _ = await request(api, env, "redeem", body)
    assert response.status == 503 and result["error"]["code"] == "service_unavailable"
    assert env.store.inspect()["licenses"] == 1 and code["code"] not in caplog.text
    monkeypatch.setattr(app_module, "issue_lease", original_signer)
    response, result, _ = await request(api, env, "redeem", body)
    assert response.status == 200 and env.store.inspect()["licenses"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["signature", "time", "action", "replay", "duplicate_json", "oversize"])
async def test_api_auth_and_input_boundaries(api, env, tamper):
    envelope = sign_request(env.key, "preview", {"code": issue(env)["code"]}, env.now, secrets.token_hex(16))
    status, expected = 401, "invalid_request"
    if tamper == "signature":
        envelope["body"]["code"] += "A"
    elif tamper == "time":
        envelope = sign_request(env.key, "preview", envelope["body"], env.now - 301, envelope["nonce"])
    elif tamper == "action":
        envelope = sign_request(env.key, "refresh", envelope["body"], env.now, envelope["nonce"])
    elif tamper == "replay":
        assert (await api.post("/v1/preview", json=envelope)).status == 200
        expected = "request_replayed"
    elif tamper == "duplicate_json":
        response = await api.post("/v1/preview", data='{"action":"preview","action":"refresh"}')
        assert response.status == 409 and (await response.json())["error"]["code"] == "invalid_data"
        return
    else:
        response = await api.post("/v1/preview", data="x" * 17000)
        assert response.status == 413 and (await response.json())["error"]["code"] == "request_too_large"
        return
    response = await api.post("/v1/preview", json=envelope)
    assert response.status == status and (await response.json())["error"]["code"] == expected
    assert env.store.inspect()["licenses"] == 0


@pytest.mark.asyncio
async def test_http_limit_ignores_forged_forwarded_headers_and_persists(env):
    for run in range(2):
        client = TestClient(TestServer(create_app(LicenseStore(env.path), env.signer, "key", clock=lambda: env.now, rate_requests=1)))
        async with client:
            response = await client.get("/health", headers={"X-Forwarded-For": f"198.51.100.{run}"})
            assert response.status == (200 if run == 0 else 429)
            assert response.headers["Cache-Control"] == "no-store"


def test_cli_initialization_and_manual_code_lifecycle(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path)]
    assert main([*args, "init"]) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["data"]["public_key"] and initialized["data"]["key_id"]
    key_bytes = (tmp_path / "licensing-private.pem").read_bytes()
    assert main([*args, "init"]) == 1
    assert (tmp_path / "licensing-private.pem").read_bytes() == key_bytes
    capsys.readouterr()
    assert main([*args, "issue", "--owner-ref", "QQ-test", "--reference", "order-1", "--plan", "trial"]) == 0
    issued = json.loads(capsys.readouterr().out)["data"]
    assert issued["mode"] == "new"
    assert main([*args, "inspect", "--owner-ref", "QQ-test"]) == 0
    inspected = capsys.readouterr().out
    assert issued["code"] not in inspected and "QQ-test" not in inspected
    assert main([*args, "revoke", "--code-id", issued["code_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "revoked"
