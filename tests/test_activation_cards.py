"""Customer-facing card behavior across renewals, recovery and purchased quotas."""
from concurrent.futures import ThreadPoolExecutor
import json
import secrets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from services.activation_service.store import LicenseStore
from wechatrebot_manager.licensing.protocol import LicenseError, fingerprint, public_key
from test_activation_service import env, issue, redeem, assert_error, timestamp
from test_license_rebinding_store import bound, change, status


def card(env, *, mode="duration", **kwargs):
    return env.store.issue_code(reference=secrets.token_hex(8), now=env.now, mode=mode,
                                **({"plan": "month"} if mode == "duration" else {}), **kwargs)


def preview(env, code, record=None):
    return env.store.preview(env.device, {"code": code["code"], **({"license_id": record["id"]} if record else {})}, env.now)


@pytest.mark.parametrize("credits", [0, -1, 1001, True, "5", 1.5])
def test_invalid_credit_amount_never_issues_a_code(env, credits):
    assert_error("invalid_data", lambda: card(env, mode="rebind_credit", rebind_credits=credits))
    assert env.store.inspect()["codes"] == 0


@pytest.mark.parametrize("note", [None, "x" * 257])
def test_invalid_customer_note_never_issues_a_code(env, note):
    assert_error("invalid_data", lambda: card(env, note=note))
    assert env.store.inspect()["codes"] == 0


def test_same_duration_product_opens_renews_and_resumes_expired_subscription(env):
    first = redeem(env, card(env))
    env.store.bind(env.device, {"license_id": first["id"], "account": "wxid_customer"}, env.now)
    second = card(env)
    projected = preview(env, second, first)
    assert projected["mode"] == "renewal" and projected["account"] == "wxid_customer"
    renewed = redeem(env, second)
    assert renewed["id"] == first["id"] and renewed["account"] == "wxid_customer"
    assert renewed["valid_until"] == timestamp("2026-03-31T10:20:30")
    assert env.store.migration_quota(first["id"])["migration_total"] == 2
    env.now = timestamp("2026-05-20T10:20:30")
    resumed = redeem(env, card(env))
    assert resumed["id"] == first["id"] and resumed["valid_until"] == timestamp("2026-06-20T10:20:30")
    assert env.store.inspect()["licenses"] == 1


@pytest.mark.parametrize("original_plan", ["month", "trial"])
def test_duration_adopts_existing_owner_without_manual_customer_reference(env, original_plan):
    original = redeem(env, issue(env, owner="stable-old-customer", plan=original_plan))
    code = card(env, note="老用户订单尾号 2024")
    projected = preview(env, code, original)
    assert projected["license_id"] == original["id"] and projected["mode"] == "renewal"
    updated = redeem(env, code)
    assert updated["id"] == original["id"] and updated["owner_hash"] == original["owner_hash"]
    assert updated["plan"] == "month"
    assert env.store.admin_code_history(now=env.now, owner_ref="stable-old-customer")["total"] == 2
    assert env.store.admin_license_detail(original["id"], now=env.now)["note"] == "老用户订单尾号 2024"


def test_cacheless_duration_recovers_unique_device_authorization_and_rejects_ambiguity(env):
    first = redeem(env, issue(env, owner="one"))
    code = card(env)
    assert preview(env, code)["license_id"] == first["id"]
    second = redeem(env, issue(env, owner="two"))
    assert_error("license_ambiguous", lambda: preview(env, code))
    selected = preview(env, code, second)
    result = env.store.redeem(env.device, env.pub, {"code": code["code"], "preview_id": selected["preview_id"]}, env.now)
    assert result["id"] == second["id"]


def test_duration_cannot_take_over_other_device_or_revive_revoked_authorization(env):
    original = redeem(env, issue(env))
    code = card(env)
    assert_error("license_unavailable", lambda: env.store.preview("a" * 64, {
        "code": code["code"], "license_id": original["id"]}, env.now))
    env.store.revoke(now=env.now, license_id=original["id"])
    assert_error("license_revoked", lambda: preview(env, code))
    assert env.store.admin_code_detail(code["code_id"], now=env.now)["status"] == "unredeemed"


def test_concurrent_first_redemption_cannot_grant_a_card_to_two_devices(env):
    code = card(env)
    other_key = Ed25519PrivateKey.generate()
    other_pub = public_key(other_key)
    identities = [(env.device, env.pub), (fingerprint(other_pub), other_pub)]
    previews = [env.store.preview(device, {"code": code["code"]}, env.now) for device, _ in identities]

    def attempt(index):
        device, pub = identities[index]
        try:
            return env.store.redeem(device, pub, {"code": code["code"], "preview_id": previews[index]["preview_id"]}, env.now)
        except LicenseError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert "code_unavailable" in results and env.store.inspect()["licenses"] == 1


def test_concurrent_cards_require_fresh_preview_instead_of_losing_duration(env):
    original = redeem(env, card(env))
    first, second = card(env), card(env)
    first_preview, second_preview = preview(env, first), preview(env, second)
    args = {"code": first["code"], "preview_id": first_preview["preview_id"]}
    env.store.redeem(env.device, env.pub, args, env.now)
    assert_error("preview_stale", lambda: env.store.redeem(env.device, env.pub,
        {"code": second["code"], "preview_id": second_preview["preview_id"]}, env.now))
    result = redeem(env, second)
    assert result["id"] == original["id"] and result["valid_until"] == timestamp("2026-04-30T10:20:30")


def test_purchased_rebinding_credits_do_not_extend_time_or_repeat_on_retry(env):
    original = change(env, bound(env))
    assert status(env, original)["rebind_remaining"] == 0
    code = card(env, mode="rebind_credit", rebind_credits=5)
    projected = preview(env, code)
    assert (projected["mode"], projected["rebind_before"], projected["rebind_after"]) == ("rebind_credit", 0, 5)
    assert projected["valid_until"] == original["valid_until"]
    for _ in range(2):
        updated = redeem(env, code)
        env.store = LicenseStore(env.path)
        assert (updated["id"], updated["plan"], updated["valid_until"], updated["account"], updated["device"]) == (
            original["id"], original["plan"], original["valid_until"], original["account"], original["device"])
        assert status(env, updated)["rebind_remaining"] == 5
    assert preview(env, code)["rebind_before"] == preview(env, code)["rebind_after"] == 5
    changed = change(env, updated, "wxid_after_topup")
    assert status(env, changed)["rebind_remaining"] == 4


def test_historical_overuse_does_not_consume_new_credits_and_renewals_preserve_balance(env):
    record = bound(env, "year")
    for number in range(3):
        record = change(env, record, f"wxid_change_{number}")
    record = redeem(env, issue(env, owner="customer-1", mode="renewal", license_id=record["id"]))
    assert status(env, record)["rebind_remaining"] == 0
    record = redeem(env, card(env, mode="rebind_credit", rebind_credits=5))
    assert status(env, record)["rebind_remaining"] == 5
    record = change(env, record, "wxid_extra")
    assert status(env, record)["rebind_remaining"] == 4
    record = redeem(env, card(env))
    assert status(env, record)["rebind_remaining"] == 4


def test_expired_paid_authorization_can_store_credits_without_becoming_active(env):
    original = bound(env)
    env.now = original["valid_until"] + 100
    code = card(env, mode="rebind_credit", rebind_credits=2)
    assert "续费后" in preview(env, code)["message"]
    updated = redeem(env, code)
    assert updated["valid_until"] == original["valid_until"]
    assert status(env, updated)["rebind_remaining"] == 3 and not status(env, updated)["can_rebind"]
    assert_error("subscription_expired", lambda: change(env, updated))


@pytest.mark.parametrize("existing,expected", [("none", "paid_license_required"), ("trial", "paid_license_required"), ("revoked", "license_revoked")])
def test_invalid_credit_target_keeps_code_unused(env, existing, expected):
    if existing != "none":
        original = redeem(env, issue(env, plan="trial" if existing == "trial" else "month"))
        if existing == "revoked":
            env.store.revoke(now=env.now, license_id=original["id"])
    code = card(env, mode="rebind_credit", rebind_credits=3)
    assert_error(expected, lambda: preview(env, code))
    assert env.store.admin_code_detail(code["code_id"], now=env.now)["status"] == "unredeemed"


def test_credit_and_consumption_roll_back_together_on_write_failure(env, monkeypatch):
    original = bound(env)
    code = card(env, mode="rebind_credit", rebind_credits=5)
    prepared = preview(env, code)

    def fail(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(env.store, "_audit", fail)
    with pytest.raises(OSError):
        env.store.redeem(env.device, env.pub, {"code": code["code"], "preview_id": prepared["preview_id"]}, env.now)
    assert status(env, original)["rebind_remaining"] == 1
    assert status(env, original)["revision"] == original["revision"]
    assert env.store.admin_code_detail(code["code_id"], now=env.now)["status"] == "unredeemed"


def test_same_credit_code_concurrent_redemption_only_adds_once(env):
    original = bound(env)
    code = card(env, mode="rebind_credit", rebind_credits=5)
    prepared = preview(env, code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: env.store.redeem(env.device, env.pub,
            {"code": code["code"], "preview_id": prepared["preview_id"]}, env.now), range(2)))
    assert results[0] == results[1] and status(env, original)["rebind_remaining"] == 6


def test_backend_shortcut_uses_existing_migration_and_preserves_wechat_balance(env):
    original = bound(env)
    migrated = env.store.issue_code(reference="service-move", now=env.now, mode="migration",
                                    license_id=original["id"], target_device="a" * 64)
    result = redeem(env, migrated, device="a" * 64)
    assert result["id"] == original["id"] and result["account"] == original["account"]
    assert env.store.binding_status("a" * 64, {"license_id": result["id"]}, env.now)["rebind_remaining"] == 1
    assert env.store.migration_quota(result["id"])["migration_remaining"] == 0


def test_legacy_reset_account_migration_cannot_bypass_wechat_quota(env):
    record = change(env, bound(env))
    migration = issue(env, owner="customer-1", mode="migration", license_id=record["id"],
                      target_device=env.device, reset_account=True)
    assert_error("rebind_exhausted", lambda: redeem(env, migration))
    assert env.store.refresh(env.device, {"license_id": record["id"]}, env.now)["account"] == record["account"]
    assert env.store.admin_code_detail(migration["code_id"], now=env.now)["status"] == "unredeemed"


def test_v3_migration_preserves_old_code_and_successful_wechat_changes(env):
    record = change(env, bound(env))
    code = issue(env, owner="old-unused")
    with env.store.connection(write=True) as db:
        db.execute("ALTER TABLE licenses DROP COLUMN encrypted_note")
        db.execute("ALTER TABLE codes DROP COLUMN rebind_credits")
        db.execute("ALTER TABLE rebindings DROP COLUMN extra_used")
        db.execute("PRAGMA user_version=3")
    env.store = LicenseStore(env.path)
    assert status(env, record)["rebind_remaining"] == 0
    assert env.store.admin_code_detail(code["code_id"], now=env.now)["code"] == code["code"]
    credited = redeem(env, card(env, mode="rebind_credit", rebind_credits=3))
    assert status(env, credited)["rebind_remaining"] == 3
    with env.store.connection() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_customer_search_and_notes_are_encrypted_and_keep_codes_out_of_lists(env):
    code = card(env, note="订单尾号 7391")
    record = redeem(env, code)
    env.store.bind(env.device, {"license_id": record["id"], "account": "wxid_find_me"}, env.now)
    for search in ("7391", "wxid_find_me", record["id"], code["code"].lower().replace("-", "")):
        assert env.store.admin_licenses(now=env.now, search=search)["total"] == 1
        result = env.store.admin_code_history(now=env.now, search=search)
        assert result["total"] == 1 and "code" not in result["items"][0]
    env.store.admin_update_note(now=env.now, license_id=record["id"], note="客户新备注 <script>")
    assert env.store.admin_licenses(now=env.now, search="新备注")["total"] == 1
    env.store.admin_update_note(now=env.now, code_id=code["code_id"], note="修改发码备注")
    assert env.store.admin_code_history(now=env.now, search="修改发码")["total"] == 1
    with env.store.connection() as db:
        contents = json.dumps([dict(row) for table in ("licenses", "codes", "audit") for row in db.execute(f"SELECT * FROM {table}")])
        assert all(text not in contents for text in ("7391", "新备注", "修改发码", code["code"]))
    env.store.admin_update_note(now=env.now, license_id=record["id"], note="")
    assert env.store.admin_license_detail(record["id"], now=env.now)["note"] == ""
