"""Real HTTP trial grants and recovery, without starting a robot or WeChat."""

import asyncio
import shutil
from types import SimpleNamespace

from aiohttp.test_utils import TestServer
import pytest
import pytest_asyncio

from services.activation_service.app import create_app
from services.activation_service.store import LicenseStore
from wechatrebot_manager.licensing.client import LicenseClient, http_transport
from wechatrebot_manager.licensing.protocol import LicenseError
from wechatrebot_manager.licensing.runtime_guard import RuntimeGuard

from test_license_client import setup as license_setup


ACCOUNT = "wxid_http_trial"
MACHINE = "a" * 64


@pytest_asyncio.fixture
async def trial_http(license_setup, tmp_path):
    client, service, clock, protection = license_setup
    store = LicenseStore(tmp_path / "server/activation.db")
    server = TestServer(create_app(store, service.key, "test", clock=lambda: clock.server))
    await server.start_server()
    failures = {"drop_next_claim": False}

    def transport(url, request):
        action = url.rsplit("/", 1)[-1]
        response = http_transport(str(server.make_url("/v1/" + action)), request)
        if action == "trial-claim" and failures["drop_next_claim"]:
            failures["drop_next_claim"] = False
            raise OSError("simulated response loss after server commit")
        return response

    client.transport = transport
    client.machine_id_source = lambda _root: MACHINE

    def replacement(root, machine=MACHINE):
        config = root / "config/licensing.json"
        if root != client.root:
            config.parent.mkdir(parents=True)
            config.write_bytes((client.root / "config/licensing.json").read_bytes())
        return LicenseClient(root, transport=transport, clock=clock, protection=protection,
                             machine_id_source=lambda _root: machine)

    try:
        yield SimpleNamespace(client=client, store=store, clock=clock,
                              failures=failures, replacement=replacement)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_lost_claim_response_recovers_original_deadline(trial_http):
    case = trial_http
    status = await asyncio.to_thread(case.client.trial_status, ACCOUNT)
    assert status["state"] == "eligible" and status["eligible"]
    assert case.store.inspect()["licenses"] == 0
    assert not case.client.snapshot(ACCOUNT)["can_run"]

    case.failures["drop_next_claim"] = True
    with pytest.raises(LicenseError) as error:
        await asyncio.to_thread(case.client.claim_trial, ACCOUNT)
    assert error.value.code == "service_unavailable"
    original = case.store.inspect(owner_ref=ACCOUNT)["licenses"][0]
    assert original["valid_until"] == case.clock.server + 3600
    assert not case.client.snapshot(ACCOUNT)["authorized"]

    case.clock.advance(100)
    status = await asyncio.to_thread(case.client.trial_status, ACCOUNT)
    assert status["recoverable"] and not status["eligible"]
    recovered = await asyncio.to_thread(case.client.claim_trial, ACCOUNT)
    assert recovered["license_id"] == original["id"]
    assert recovered["valid_until"] == original["valid_until"]
    assert recovered["remaining_seconds"] == 3500
    assert recovered["account"] == ACCOUNT and not recovered["can_run"]
    assert case.store.inspect()["licenses"] == 1


@pytest.mark.asyncio
async def test_same_machine_reinstall_recovers_only_the_original_account(trial_http):
    case = trial_http
    original = await asyncio.to_thread(case.client.claim_trial, ACCOUNT)
    original_device = original["device_id"]
    case.clock.advance(120)
    shutil.rmtree(case.client.directory)
    reinstalled = case.replacement(case.client.root)
    assert reinstalled.snapshot(ACCOUNT)["state"] == "unactivated"

    other_account = "wxid_other_trial"
    status = await asyncio.to_thread(reinstalled.trial_status, other_account)
    assert status["state"] == "used" and not status["eligible"]
    with pytest.raises(LicenseError) as error:
        await asyncio.to_thread(reinstalled.claim_trial, other_account)
    assert error.value.code == "trial_used"
    assert not reinstalled.snapshot(other_account)["authorized"]

    recovered = await asyncio.to_thread(reinstalled.claim_trial, ACCOUNT)
    assert recovered["device_id"] != original_device
    assert recovered["license_id"] == original["license_id"]
    assert recovered["valid_until"] == original["valid_until"]
    assert recovered["remaining_seconds"] == 3480
    assert recovered["account"] == ACCOUNT and not recovered["can_run"]
    assert case.store.inspect()["licenses"] == 1


@pytest.mark.asyncio
async def test_other_machine_cannot_reclaim_account_or_consume_its_unused_device(trial_http, tmp_path):
    case = trial_http
    original = await asyncio.to_thread(case.client.claim_trial, ACCOUNT)
    other = case.replacement(tmp_path / "other-machine", "b" * 64)
    status = await asyncio.to_thread(other.trial_status, ACCOUNT)
    assert status["state"] == "used" and not status["recoverable"]
    with pytest.raises(LicenseError) as error:
        await asyncio.to_thread(other.claim_trial, ACCOUNT)
    assert error.value.code == "trial_used"
    assert not other.snapshot(ACCOUNT)["authorized"]
    assert case.store.inspect()["licenses"] == 1

    new_account = "wxid_new_machine"
    status = await asyncio.to_thread(other.trial_status, new_account)
    assert status["eligible"], "A denied claim must not consume its unused device identity"
    granted = await asyncio.to_thread(other.claim_trial, new_account)
    assert granted["license_id"] != original["license_id"]
    assert case.store.inspect()["licenses"] == 2


@pytest.mark.asyncio
async def test_trial_expiry_blocks_runtime_until_paid_renewal_and_explicit_start(trial_http):
    case = trial_http
    granted = await asyncio.to_thread(case.client.claim_trial, ACCOUNT)
    guard = RuntimeGuard(case.client.root, client=case.client, account_source=lambda: ACCOUNT)
    with pytest.raises(LicenseError):
        guard.require()
    running = case.client.authorize_run(ACCOUNT)
    case.clock.advance(3599)
    assert guard.require()["can_run"]
    case.clock.advance(1)
    assert case.client.snapshot(ACCOUNT)["state"] == "expired"
    with pytest.raises(LicenseError):
        guard.require()
    status = await asyncio.to_thread(case.client.trial_status, ACCOUNT)
    assert status["state"] == "ended" and not status["eligible"] and not status["recoverable"]
    with pytest.raises(LicenseError) as error:
        await asyncio.to_thread(case.client.claim_trial, ACCOUNT)
    assert error.value.code == "trial_used"

    code = case.store.issue_code(owner_ref=ACCOUNT, reference="trial-to-paid", now=case.clock.server,
                                plan="month", mode="renewal", license_id=granted["license_id"])
    preview = await asyncio.to_thread(case.client.preview, code["code"])
    renewed = await asyncio.to_thread(case.client.redeem, code["code"], preview["preview_id"])
    assert renewed["license_id"] == granted["license_id"] and renewed["plan"] == "month"
    assert renewed["authorized"] and not renewed["can_run"]
    with pytest.raises(LicenseError):
        guard.require()
    resumed = case.client.authorize_run(ACCOUNT)
    assert resumed["can_run"] and resumed["run_id"] != running["run_id"]
    assert guard.require()["can_run"]
    with pytest.raises(LicenseError):
        guard.require(session=running["run_id"])
    status = await asyncio.to_thread(case.client.trial_status, ACCOUNT)
    assert status["state"] == "paid" and not status["eligible"]
