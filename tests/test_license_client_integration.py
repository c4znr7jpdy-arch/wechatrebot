"""Exercise the shipping client against the real HTTP activation service."""

import asyncio

from aiohttp.test_utils import TestServer
import pytest

from services.activation_service.app import create_app
from services.activation_service.store import LicenseStore
from wechatrebot_manager.licensing.client import LicenseClient, http_transport
from wechatrebot_manager.licensing.protocol import LicenseError

from test_license_client import setup  # Shared clock, channel and protected device fixture.


async def exchange(client, code):
    preview = await asyncio.to_thread(client.preview, code)
    result = await asyncio.to_thread(client.redeem, code, preview["preview_id"])
    return preview, result


def replacement_client(client, root, transport, clock, protection):
    config = root / "config/licensing.json"
    config.parent.mkdir(parents=True)
    config.write_bytes((client.root / "config/licensing.json").read_bytes())
    return LicenseClient(root, transport=transport, clock=clock, protection=protection)


@pytest.mark.asyncio
async def test_http_activation_trial_expiry_renewal_and_revocation(setup, tmp_path):
    client, service, clock, _ = setup
    store = LicenseStore(tmp_path / "server/activation.db")
    server = TestServer(create_app(store, service.key, "test", clock=lambda: clock.server))
    await server.start_server()
    try:
        client.transport = lambda url, request: http_transport(
            str(server.make_url("/v1/" + url.rsplit("/", 1)[-1])), request,
        )
        issued = store.issue_code(owner_ref="qq-owner", reference="order-trial", now=clock.server, plan="trial")
        preview, result = await exchange(client, issued["code"])
        assert preview["expires_at"] > clock.server and preview["license_id"] == ""
        assert result["authorized"] and result["state"] == "active_unbound"
        license_id = result["license_id"]
        await asyncio.to_thread(client.bind, "wxid_http_customer")
        assert client.authorize_run("wxid_http_customer")["can_run"]
        clock.advance(3600)
        assert client.snapshot("wxid_http_customer")["state"] == "expired"
        renewal = store.issue_code(
            owner_ref="qq-owner", reference="order-month", now=clock.server,
            plan="month", mode="renewal", license_id=license_id,
        )
        preview, result = await exchange(client, renewal["code"])
        assert preview["mode"] == "renewal" and preview["license_id"] == license_id
        assert result["authorized"] and result["state"] == "paused" and not result["can_run"]
        assert result["plan"] == "month" and result["valid_until"] > clock.server + 27 * 86400
        assert not (await asyncio.to_thread(client.refresh))["can_run"]
        client.authorize_run("wxid_http_customer")
        store.revoke(now=clock.server, license_id=license_id)
        assert (await asyncio.to_thread(client.refresh))["state"] == "revoked"
        assert not client.snapshot("wxid_http_customer")["can_run"]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_http_lost_redemption_response_can_be_recovered_without_extra_time(setup, tmp_path):
    client, service, clock, _ = setup
    store = LicenseStore(tmp_path / "server/activation.db")
    server = TestServer(create_app(store, service.key, "test", clock=lambda: clock.server))
    await server.start_server()
    lose_response = True

    def transport(url, request):
        nonlocal lose_response
        action = url.rsplit("/", 1)[-1]
        response = http_transport(str(server.make_url("/v1/" + action)), request)
        if action == "redeem" and lose_response:
            lose_response = False
            raise OSError("simulated response loss")
        return response

    try:
        client.transport = transport
        issued = store.issue_code(owner_ref="qq-owner", reference="order-year", now=clock.server, plan="year")
        preview = await asyncio.to_thread(client.preview, issued["code"])
        with pytest.raises(LicenseError) as error:
            await asyncio.to_thread(client.redeem, issued["code"], preview["preview_id"])
        assert error.value.code == "service_unavailable"
        first = store.inspect(owner_ref="qq-owner")["licenses"][0]
        assert not client.snapshot()["authorized"]
        clock.advance(100)
        preview, result = await exchange(client, issued["code"])
        assert preview["already_redeemed"]
        assert result["authorized"] and result["license_id"] == first["id"]
        assert result["valid_until"] == first["valid_until"]
        assert store.inspect()["licenses"] == 1
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_http_explicit_new_device_migration_invalidates_old_device_on_refresh(setup, tmp_path):
    client, service, clock, protection = setup
    store = LicenseStore(tmp_path / "server/activation.db")
    server = TestServer(create_app(store, service.key, "test", clock=lambda: clock.server))
    await server.start_server()
    try:
        transport = lambda url, request: http_transport(
            str(server.make_url("/v1/" + url.rsplit("/", 1)[-1])), request,
        )
        client.transport = transport
        issued = store.issue_code(owner_ref="qq-owner", reference="original-order", now=clock.server, plan="month")
        _, result = await exchange(client, issued["code"])
        await asyncio.to_thread(client.bind, "wxid_http_customer")
        client.authorize_run("wxid_http_customer")
        replacement = replacement_client(client, tmp_path / "new-install", transport, clock, protection)
        assert replacement.snapshot()["device_id"] == ""
        prepared = replacement.prepare_device()
        assert prepared["device_id"] and not prepared["authorized"]
        migration = store.issue_code(
            owner_ref="qq-owner", reference="replacement-order", now=clock.server, mode="migration",
            license_id=result["license_id"], target_device=prepared["device_id"],
        )
        _, migrated = await exchange(replacement, migration["code"])
        assert migrated["authorized"] and not migrated["can_run"]
        assert migrated["account"] == "wxid_http_customer"
        with pytest.raises(LicenseError) as error:
            await asyncio.to_thread(client.refresh)
        assert error.value.code == "license_unavailable"
        assert not client.snapshot("wxid_http_customer")["can_run"]
        assert replacement.authorize_run("wxid_http_customer")["can_run"]
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "response_loss", "cache_write"])
async def test_http_migration_replaces_cached_trial_and_recovers_after_restart(setup, tmp_path, monkeypatch, failure):
    client, service, clock, protection = setup
    store = LicenseStore(tmp_path / "server/activation.db")
    server = TestServer(create_app(store, service.key, "test", clock=lambda: clock.server))
    await server.start_server()
    drop_code = ""
    dropped = False

    def transport(url, request):
        nonlocal dropped
        action = url.rsplit("/", 1)[-1]
        result = http_transport(str(server.make_url("/v1/" + action)), request)
        if failure == "response_loss" and not dropped and action == "redeem" and request["body"]["code"] == drop_code:
            dropped = True
            raise OSError("response interrupted after server commit")
        return result

    try:
        client.transport = transport
        original_code = store.issue_code(owner_ref="same-owner", reference="original-month", now=clock.server, plan="month")
        _, original = await exchange(client, original_code["code"])
        await asyncio.to_thread(client.bind, "wxid_original")
        replacement = replacement_client(client, tmp_path / "new-install", transport, clock, protection)
        trial_code = store.issue_code(owner_ref="same-owner", reference="new-computer-trial", now=clock.server, plan="trial")
        _, trial = await exchange(replacement, trial_code["code"])
        await asyncio.to_thread(replacement.bind, "wxid_trial")
        old_run = replacement.authorize_run("wxid_trial")
        migration = store.issue_code(
            owner_ref="same-owner", reference="explicit-replacement", now=clock.server,
            mode="migration", license_id=original["license_id"], target_device=trial["device_id"],
        )
        preview = await asyncio.to_thread(replacement.preview, migration["code"])
        assert preview["mode"] == "migration" and preview["replaces_license_id"] == trial["license_id"]
        assert preview["license_id"] == original["license_id"]
        assert replacement.snapshot("wxid_trial")["can_run"]  # Preview does not retire anything.
        drop_code = migration["code"]

        def unavailable_storage(_):
            raise LicenseError("storage_error", "Local cache write failed")

        if failure != "none":
            if failure == "cache_write":
                monkeypatch.setattr(replacement.cache, "write", unavailable_storage)
            with pytest.raises(LicenseError) as error:
                await asyncio.to_thread(replacement.redeem, migration["code"], preview["preview_id"])
            assert error.value.code == ("service_unavailable" if failure == "response_loss" else "storage_error")
            replacement = LicenseClient(replacement.root, transport=transport, clock=clock, protection=protection)
            assert (await asyncio.to_thread(replacement.refresh))["state"] == "revoked"
            clock.advance(301)  # A committed redemption may retry its original preview after expiry.
            replacement = LicenseClient(replacement.root, transport=transport, clock=clock, protection=protection)
            assert replacement.snapshot()["license_id"] == trial["license_id"]
        migrated = await asyncio.to_thread(replacement.redeem, migration["code"], preview["preview_id"])
        assert migrated["license_id"] == original["license_id"]
        assert migrated["valid_until"] == original["valid_until"] and migrated["plan"] == "month"
        assert migrated["account"] == "wxid_original" and not migrated["can_run"]
        retired = store.inspect(license_id=trial["license_id"])["licenses"][0]
        assert retired["status"] == "revoked"
        assert replacement.snapshot("wxid_trial")["state"] == "account_mismatch"
        resumed = replacement.authorize_run("wxid_original")
        assert resumed["can_run"] and resumed["run_id"] != old_run["run_id"]
        with pytest.raises(LicenseError) as error:
            await asyncio.to_thread(client.refresh)
        assert error.value.code == "license_unavailable"
    finally:
        await server.close()
