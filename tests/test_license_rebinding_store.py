from concurrent.futures import ThreadPoolExecutor
import json
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from services.activation_service.store import LicenseStore
from wechatrebot_manager.licensing.protocol import LicenseError, public_key, sign_request, verify_lease

from test_activation_service import api, env, issue, redeem, request


def bound(env, plan="month", *, owner="customer-1", account="wxid_original"):
    record = redeem(env, issue(env, plan=plan, owner=owner))
    return env.store.bind(env.device, {"license_id": record["id"], "account": account}, env.now)


def change(env, record, account="wxid_next", **overrides):
    body = {"license_id": record["id"], "account": account,
            "expected_account": record["account"], "expected_revision": record["revision"]}
    return env.store.rebind(env.device, {**body, **overrides}, env.now)


def status(env, record):
    return env.store.binding_status(env.device, {"license_id": record["id"]}, env.now)


def rejects(code, call):
    with pytest.raises(LicenseError) as error:
        call()
    assert error.value.code == code


@pytest.mark.parametrize("plan,limit", [("month", 1), ("year", 10), ("trial", 0)])
def test_plan_quota_counts_only_successful_rebindings(env, plan, limit):
    record = bound(env, plan)
    first_revision = record["revision"]
    quota = status(env, record)
    assert quota == {
        "license_id": record["id"], "account": "wxid_original", "plan": plan,
        "revision": first_revision, "rebind_limit": limit, "rebind_used": 0,
        "rebind_remaining": limit, "can_rebind": bool(limit),
    }
    for number in range(limit):
        record = change(env, record, f"wxid_next_{number}")
        quota = status(env, record)
        assert quota["rebind_used"] == number + 1
        assert quota["rebind_remaining"] == limit - number - 1
        assert record["revision"] == first_revision + number + 1
    rejects("rebind_exhausted" if limit else "rebind_unavailable",
            lambda: change(env, record, "wxid_excess"))
    assert status(env, record)["can_rebind"] is False


def test_noop_and_lost_response_retry_never_consume_more_quota(env):
    original = bound(env)
    assert change(env, original, original["account"]) == original
    assert status(env, original)["rebind_used"] == 0
    changed = change(env, original)
    for _ in range(3):
        assert change(env, original) == changed
        env.store = LicenseStore(env.path)
    assert status(env, changed)["rebind_used"] == 1
    with env.store.connection() as db:
        history = [dict(row) for row in db.execute("SELECT * FROM rebindings")]
        audit = db.execute("SELECT COUNT(*) FROM audit WHERE event='rebind'").fetchone()[0]
    assert len(history) == audit == 1
    assert history[0]["previous_revision"] == original["revision"]
    assert history[0]["revision"] == changed["revision"]
    assert "wxid_original" not in json.dumps(history) and "wxid_next" not in json.dumps(history)


def test_renewals_and_plan_changes_preserve_cumulative_usage(env):
    record = change(env, bound(env))
    renewed = redeem(env, issue(env, owner="customer-1", mode="renewal", license_id=record["id"]))
    assert status(env, renewed)["rebind_remaining"] == 0
    upgraded = redeem(env, issue(env, plan="year", owner="customer-1", mode="renewal", license_id=record["id"]))
    assert status(env, upgraded)["rebind_remaining"] == 9
    upgraded = change(env, upgraded, "wxid_year")
    downgraded = redeem(env, issue(env, owner="customer-1", mode="renewal", license_id=record["id"]))
    env.store = LicenseStore(env.path)
    quota = status(env, downgraded)
    assert quota["rebind_used"] == 2 and quota["rebind_limit"] == 1
    assert quota["rebind_remaining"] == 0 and quota["can_rebind"] is False


@pytest.mark.parametrize("case,code", [
    ("expired", "subscription_expired"), ("revoked", "license_revoked"),
    ("trial", "rebind_unavailable"), ("unbound", "rebind_unavailable"),
    ("other_device", "license_unavailable"), ("occupied", "account_unavailable"),
])
def test_ineligible_rebinding_never_changes_account_or_quota(env, case, code):
    record = bound(env, "trial" if case == "trial" else "month")
    if case == "expired":
        env.now = record["valid_until"]
    elif case == "revoked":
        env.store.revoke(now=env.now, license_id=record["id"])
    elif case == "unbound":
        record = redeem(env, issue(env, owner="unbound-customer"))
    elif case == "occupied":
        bound(env, owner="other-customer", account="wxid_next")
    device = "f" * 64 if case == "other_device" else env.device
    before = env.store.refresh(env.device, {"license_id": record["id"]}, env.now)
    body = {"license_id": record["id"], "account": "wxid_next",
            "expected_account": record["account"] or "wxid_old", "expected_revision": record["revision"]}
    rejects(code, lambda: env.store.rebind(device, body, env.now))
    assert env.store.refresh(env.device, {"license_id": record["id"]}, env.now) == before
    assert status(env, record)["rebind_used"] == 0
    if case != "occupied" and case != "other_device":
        assert status(env, record)["can_rebind"] is False


@pytest.mark.parametrize("overrides,code", [
    ({"expected_revision": True}, "invalid_data"),
    ({"expected_revision": "2"}, "invalid_data"),
    ({"expected_revision": 0}, "invalid_data"),
    ({"expected_revision": 999}, "binding_changed"),
    ({"expected_account": "wxid_stale"}, "binding_changed"),
    ({"account": "bad account"}, "invalid_account"),
    ({"extra": "ignored?"}, "invalid_data"),
])
def test_rebinding_validates_body_and_confirmation_revision(env, overrides, code):
    record = bound(env)
    rejects(code, lambda: change(env, record, **overrides))
    assert status(env, record)["rebind_used"] == 0


@pytest.mark.parametrize("same_target", [False, True])
def test_concurrent_rebinding_is_atomic_and_same_target_is_idempotent(env, same_target):
    original = bound(env)

    def attempt(target):
        try:
            return change(env, original, target)
        except LicenseError as error:
            return error.code

    targets = ("wxid_a", "wxid_a" if same_target else "wxid_b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, targets))
    successful = [item for item in results if isinstance(item, dict)]
    assert len(successful) == (2 if same_target else 1)
    if same_target:
        assert successful[0] == successful[1]
    else:
        assert "binding_changed" in results
    assert status(env, original)["rebind_used"] == 1
    assert status(env, original)["revision"] == original["revision"] + 1


def test_history_and_account_change_roll_back_if_audit_cannot_be_written(env, monkeypatch):
    original = bound(env)

    def failed_audit(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(env.store, "_audit", failed_audit)
    with pytest.raises(OSError):
        change(env, original)
    assert env.store.refresh(env.device, {"license_id": original["id"]}, env.now) == original
    assert status(env, original)["rebind_used"] == 0


def test_existing_database_adds_empty_history_without_changing_licenses(env):
    original = bound(env)
    with env.store.connection(write=True) as db:
        db.execute("DROP TABLE rebindings")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    env.store = LicenseStore(env.path)
    assert env.store.refresh(env.device, {"license_id": original["id"]}, env.now) == original
    assert status(env, original)["rebind_used"] == 0
    change(env, original)
    env.store = LicenseStore(env.path)
    assert status(env, original)["rebind_used"] == 1


@pytest.mark.asyncio
async def test_signed_status_and_rebind_keep_strict_lease_compatibility(api, env):
    original = bound(env)
    response, data, _ = await request(api, env, "binding-status", {"license_id": original["id"]})
    assert response.status == 200 and data["ok"] is True
    assert data["data"] == status(env, original) and "lease" not in data["data"]
    body = {"license_id": original["id"], "account": "wxid_next",
            "expected_account": original["account"], "expected_revision": original["revision"]}
    response, data, envelope = await request(api, env, "rebind", body)
    assert response.status == 200 and set(data["data"]) == {"lease"}
    lease = verify_lease(data["data"]["lease"], {"test-key": public_key(env.signer)}, env.device)
    assert lease["account"] == "wxid_next" and lease["revision"] == original["revision"] + 1
    assert lease["request_nonce"] == envelope["nonce"]
    replay = await api.post("/v1/rebind", json=envelope)
    assert replay.status == 401
    assert (await replay.json())["error"]["code"] == "request_replayed"
    # A fresh signed retry restores the lease without consuming another change.
    response, retry, _ = await request(api, env, "rebind", body)
    assert response.status == 200
    assert retry["data"]["lease"]["payload"]["revision"] == lease["revision"]
    assert status(env, original)["rebind_used"] == 1


@pytest.mark.asyncio
async def test_rebinding_routes_require_the_owning_signed_device(api, env):
    original = bound(env)
    other = Ed25519PrivateKey.generate()
    for action in ("binding-status", "rebind"):
        body = {"license_id": original["id"]}
        if action == "rebind":
            body.update(account="wxid_next", expected_account=original["account"], expected_revision=original["revision"])
        response, data, _ = await request(api, env, action, body, key=other)
        assert response.status == 409 and data["error"]["code"] == "license_unavailable"
        envelope = sign_request(env.key, action, body, env.now, secrets.token_hex(16))
        envelope["body"]["license_id"] = "tampered"
        response = await api.post("/v1/" + action, json=envelope)
        assert response.status == 401 and (await response.json())["error"]["code"] == "invalid_request"
    assert status(env, original)["rebind_used"] == 0
