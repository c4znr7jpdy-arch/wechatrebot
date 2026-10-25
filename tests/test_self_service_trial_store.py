"""Trial uniqueness and recovery use the existing transactional ledger."""

from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from test_activation_service import assert_error, env, issue, redeem
from wechatrebot_manager.licensing.protocol import LicenseError, fingerprint, public_key


def body(account="wxid_trial", machine="a" * 64):
    return {"account": account, "machine_id": machine}


def identity():
    key = Ed25519PrivateKey.generate()
    return fingerprint(public_key(key)), public_key(key)


def claim(env, *, device=None, pub=None, account="wxid_trial", machine="a" * 64):
    return env.store.claim_trial(device or env.device, pub or env.pub, body(account, machine), env.now)


def test_status_is_non_consuming_and_claim_starts_exactly_one_hour(env):
    assert env.store.trial_status(env.device, body(), env.now) == {
        "state": "eligible", "eligible": True, "recoverable": False, "account": "wxid_trial",
        "valid_until": 0, "remaining_seconds": 0,
    }
    assert env.store.inspect()["licenses"] == 0
    with env.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 0
    env.now += 60
    record = claim(env)
    assert record["account"] == "wxid_trial" and record["valid_until"] == env.now + 3600
    assert env.store.inspect(owner_ref="wxid_trial")["licenses"][0]["id"] == record["id"]
    env.now += 50
    repeated = claim(env)
    assert repeated["id"] == record["id"] and repeated["valid_until"] == record["valid_until"]
    assert repeated["revision"] == record["revision"]
    assert env.store.trial_status(env.device, body(), env.now)["remaining_seconds"] == 3550


@pytest.mark.parametrize("same_machine,same_account,same_key", [
    (True, False, True), (True, False, False), (False, True, False), (False, True, True),
])
def test_either_used_identity_blocks_a_new_trial(env, same_machine, same_account, same_key):
    original = claim(env)
    device, pub = (env.device, env.pub) if same_key else identity()
    request = body("wxid_trial" if same_account else "wxid_other", "a" * 64 if same_machine else "b" * 64)
    assert env.store.trial_status(device, request, env.now)["state"] == "used"
    assert_error("trial_used", lambda: env.store.claim_trial(device, pub, request, env.now))
    assert env.store.inspect()["licenses"] == 1
    assert env.store.refresh(env.device, {"license_id": original["id"]}, env.now) == original


def test_fresh_machine_and_account_can_claim_independently(env):
    original = claim(env)
    device, pub = identity()
    fresh = claim(env, device=device, pub=pub, machine="b" * 64, account="wxid_other")
    assert original["id"] != fresh["id"] and env.store.inspect()["licenses"] == 2


def test_reinstall_rotates_signing_identity_but_retains_original_deadline(env):
    original = claim(env)
    env.now += 120
    device, pub = identity()
    status = env.store.trial_status(device, body(), env.now)
    assert status["state"] == "active" and status["recoverable"]
    restored = claim(env, device=device, pub=pub)
    assert restored["id"] == original["id"] and restored["valid_until"] == original["valid_until"]
    assert restored["revision"] == original["revision"] + 1
    assert_error("license_unavailable", lambda: env.store.refresh(env.device, {"license_id": original["id"]}, env.now))
    with env.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM trials WHERE kind='device'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM trials WHERE kind='machine'").fetchone()[0] == 1


def test_expired_trial_is_ended_on_another_computer_without_recovery_or_old_details(env):
    original = claim(env)
    env.now = original["valid_until"]
    device, pub = identity()
    status = env.store.trial_status(device, body(machine="b" * 64), env.now)
    assert status == {"state": "ended", "eligible": False, "recoverable": False,
                      "account": "wxid_trial", "valid_until": 0, "remaining_seconds": 0}
    assert_error("trial_used", lambda: claim(env, device=device, pub=pub, machine="b" * 64))
    assert_error("trial_used", lambda: claim(env))


def test_revoked_trial_does_not_recover(env):
    record = claim(env)
    env.store.revoke(now=env.now, license_id=record["id"])
    assert env.store.trial_status(env.device, body(), env.now)["state"] == "used"
    assert_error("trial_used", lambda: claim(env))


@pytest.mark.parametrize("same_device", [True, False])
def test_active_paid_entitlement_is_preserved(env, same_device):
    paid = redeem(env, issue(env))
    paid = env.store.bind(env.device, {"license_id": paid["id"], "account": "wxid_trial"}, env.now)
    device, pub = (env.device, env.pub) if same_device else identity()
    assert env.store.trial_status(device, body(), env.now)["state"] == "paid"
    assert_error("active_license_conflict", lambda: claim(env, device=device, pub=pub))
    assert env.store.refresh(env.device, {"license_id": paid["id"]}, env.now) == paid


@pytest.mark.parametrize("bound", [True, False])
def test_legacy_trial_history_is_preserved_and_only_bound_original_pair_recovers(env, bound):
    original = redeem(env, issue(env, plan="trial"))
    with env.store.connection(write=True) as db:
        db.execute("DELETE FROM trials WHERE kind='machine'")  # Model a pre-hardware-history installation.
    if bound:
        original = env.store.bind(env.device, {"license_id": original["id"], "account": "wxid_trial"}, env.now)
        recovered = claim(env)
        assert recovered["id"] == original["id"] and recovered["valid_until"] == original["valid_until"]
        assert_error("trial_used", lambda: claim(env, machine="b" * 64))
    else:
        assert_error("trial_used", lambda: claim(env))
    assert env.store.inspect()["licenses"] == 1


@pytest.mark.parametrize("shared", ["machine", "account"])
def test_parallel_claims_do_not_issue_twice_for_shared_identity(env, shared):
    keys = [identity() for _ in range(6)]

    def attempt(index):
        device, pub = keys[index]
        request = body("wxid_trial" if shared == "account" else f"wxid_{index}",
                       "a" * 64 if shared == "machine" else f"{index + 1:064x}")
        try:
            return env.store.claim_trial(device, pub, request, env.now)["id"]
        except LicenseError as exc:
            assert exc.code == "trial_used"
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert sum(result is not None for result in outcomes) == 1
    assert env.store.inspect()["licenses"] == 1


def test_failed_account_binding_rolls_back_new_grant_and_all_trial_history(env, monkeypatch):
    def fail(*_):
        raise LicenseError("account_unavailable", "account changed")

    monkeypatch.setattr(env.store, "_bind_account", fail)
    assert_error("account_unavailable", lambda: claim(env))
    assert env.store.inspect()["licenses"] == 0
    assert env.store.trial_status(env.device, body(), env.now)["eligible"]


@pytest.mark.parametrize("trial_request", [body(machine=""), body(machine="x" * 64), body(account=""),
                                     {**body(), "license_id": "injected"}, {"account": "wxid_trial"}])
def test_invalid_trial_requests_cannot_consume_history(env, trial_request):
    with pytest.raises(LicenseError):
        env.store.claim_trial(env.device, env.pub, trial_request, env.now)
    assert env.store.inspect()["licenses"] == 0


@pytest.mark.parametrize("first", ["self_service", "manual"])
def test_manual_trial_code_cannot_bypass_machine_history_after_reinstall(env, first):
    if first == "self_service":
        claim(env)
        machine = "a" * 64
    else:
        redeem(env, issue(env, plan="trial"))
        machine = env.device
    device, pub = identity()
    next_code = issue(env, plan="trial", owner="different-customer")
    trial_request = {"code": next_code["code"], "machine_id": machine}
    assert_error("trial_used", lambda: env.store.preview(device, trial_request, env.now))
    assert_error("trial_used", lambda: env.store.redeem(device, pub, {**trial_request, "preview_id": "unused"}, env.now))
    assert env.store.inspect()["licenses"] == 1


def test_self_service_cannot_bypass_manual_trial_machine_history(env):
    redeem(env, issue(env, plan="trial"))
    device, pub = identity()
    assert_error("trial_used", lambda: claim(env, device=device, pub=pub, machine=env.device, account="wxid_new"))


def test_trial_code_missing_hardware_is_rejected_but_existing_recovery_survives(env):
    code = issue(env, plan="trial")
    assert_error("device_identity_required", lambda: env.store.preview(env.device, {"code": code["code"]}, env.now))
    preview = env.store.preview(env.device, {"code": code["code"], "machine_id": env.device}, env.now)
    request = {"code": code["code"], "preview_id": preview["preview_id"]}
    assert_error("device_identity_required", lambda: env.store.redeem(env.device, env.pub, request, env.now))
    assert env.store.inspect()["licenses"] == 0
    original = env.store.redeem(env.device, env.pub, {**request, "machine_id": env.device}, env.now)
    env.now += 30
    assert env.store.preview(env.device, {"code": code["code"]}, env.now)["already_redeemed"]
    assert env.store.redeem(env.device, env.pub, request, env.now)["valid_until"] == original["valid_until"]


def test_paid_code_still_works_without_readable_hardware(env):
    code = issue(env)
    preview = env.store.preview(env.device, {"code": code["code"]}, env.now)
    result = env.store.redeem(env.device, env.pub, {"code": code["code"], "preview_id": preview["preview_id"]}, env.now)
    assert result["plan"] == "month"


def test_trial_code_rechecks_machine_after_preview_before_consumption(env):
    code = issue(env, plan="trial")
    preview = env.store.preview(env.device, {"code": code["code"], "machine_id": "a" * 64}, env.now)
    device, pub = identity()
    claim(env, device=device, pub=pub)
    assert_error("trial_used", lambda: env.store.redeem(env.device, env.pub, {
        "code": code["code"], "preview_id": preview["preview_id"], "machine_id": "a" * 64,
    }, env.now))
    assert env.store.inspect()["licenses"] == 1


def test_manual_trial_binding_remembers_account_for_other_machine_checks(env):
    record = redeem(env, issue(env, plan="trial"))
    env.store.bind(env.device, {"license_id": record["id"], "account": "wxid_trial"}, env.now)
    device, pub = identity()
    assert_error("trial_used", lambda: claim(env, device=device, pub=pub, machine="b" * 64))


def test_parallel_manual_codes_for_different_owners_cannot_reuse_one_machine(env):
    codes = [issue(env, plan="trial", owner=f"owner-{index}") for index in range(2)]
    requests = []
    for code in codes:
        device, pub = identity()
        preview = env.store.preview(device, {"code": code["code"], "machine_id": "a" * 64}, env.now)
        requests.append((device, pub, {"code": code["code"], "machine_id": "a" * 64, "preview_id": preview["preview_id"]}))

    def attempt(request):
        try:
            return env.store.redeem(*request, env.now)["id"]
        except LicenseError as exc:
            assert exc.code == "trial_used"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(result is not None for result in pool.map(attempt, requests)) == 1
    assert env.store.inspect()["licenses"] == 1


def test_old_machine_cannot_reclaim_a_trial_after_explicit_admin_migration(env):
    original = claim(env)
    target_device, target_pub = identity()
    migration = issue(env, owner="wxid_trial", plan=None, mode="migration",
                      license_id=original["id"], target_device=target_device)
    moved = redeem(env, migration, device=target_device, pub=target_pub)
    reinstall_device, reinstall_pub = identity()
    assert env.store.trial_status(reinstall_device, body(), env.now)["state"] == "used"
    assert_error("trial_used", lambda: claim(env, device=reinstall_device, pub=reinstall_pub))
    assert env.store.refresh(target_device, {"license_id": original["id"]}, env.now) == moved
    with env.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM trials WHERE kind='machine'").fetchone()[0] == 1
