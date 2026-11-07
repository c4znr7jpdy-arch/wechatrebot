"""Delivered callback behavior for a paused run, without live WeChat requests."""
import asyncio
import importlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401
from test_license_runtime_guard import runtime  # noqa: F401


def callback_app(bridge, hook, account="wxid_actual"):
    callback = importlib.import_module(bridge.app.__package__ + ".callback_server")
    return callback.create_callback_app(
        asyncio.Queue(), 1024, manager_hook=hook, manager_query_token="local-test-token",
        status_provider=lambda: {"wechat_identity": account},
    )


def headers(**extra):
    return {"X-WeChatReBot-Token": "local-test-token", "X-WeChatReBot-Account": "wxid_actual", **extra}


async def request(app, *, request_headers=None, count=1):
    async with TestClient(TestServer(app)) as client:
        results = []
        for _ in range(count):
            response = await client.get(
                "/api/manager/groups/room@chatroom/qrcode", headers=request_headers or headers(),
            )
            results.append((response.status, await response.json(), response.headers["Cache-Control"]))
        return results


def guarded_hook(bridge):
    class Session:
        def post(self, *_args, **_kwargs):
            pytest.fail("A denied QR request reached the real hook transport")

    hook = bridge.hook.HookClient("http://127.0.0.1:19088", Session())

    async def groups():
        return [{"wxid": "room@chatroom"}]

    hook.get_groups_strict = groups
    return hook


def test_paused_group_qrcode_keeps_final_guard_without_error_logs(bridge, runtime, caplog):
    runtime.client.state.update(can_run=False, authorized=True, state="paused")
    with caplog.at_level(logging.ERROR):
        results = asyncio.run(request(callback_app(bridge, guarded_hook(bridge)), count=3))

    for status, body, cache in results:
        assert status == 403 and cache == "no-store"
        assert body == {"ok": False, "error": {
            "code": "LIC-automation_paused",
            "message": "自动化已暂停，群二维码暂不可用。请在运行中心点击启动机器人后重试。",
            "retryable": False,
        }}
    assert not caplog.records


@pytest.mark.parametrize("change", [
    {"authorized": False, "state": "license_revoked"},
    {"authorized": False, "state": "license_expired"},
    {"authorized": True, "state": "paused", "account": "wxid_changed"},
    {"authorized": True, "state": "running", "run_id": "b" * 32},
])
def test_other_license_denials_are_never_misreported_as_manual_pause(bridge, runtime, caplog, change):
    runtime.client.state.update(can_run=False, **change)
    with caplog.at_level(logging.ERROR):
        [(status, body, _)] = asyncio.run(request(callback_app(bridge, guarded_hook(bridge))))
    assert status == 403 and body["ok"] is False
    assert isinstance(body["error"], str) and "授权" in body["error"]
    assert "data" not in body and "LIC-automation_paused" not in str(body)
    assert not caplog.records


def test_group_qrcode_pause_never_skips_request_authentication(bridge, runtime):
    runtime.client.state.update(can_run=False, authorized=True, state="paused")
    [(status, body, _)] = asyncio.run(request(
        callback_app(bridge, guarded_hook(bridge)), request_headers=headers(**{"Origin": "https://example.invalid"}),
    ))
    assert status == 403 and isinstance(body["error"], str)
    assert "LIC-automation_paused" not in str(body)


def test_group_qrcode_recovers_after_a_new_explicit_run(bridge, runtime):
    runtime.client.state.update(can_run=False, authorized=True, state="paused")
    [(status, _, _)] = asyncio.run(request(callback_app(bridge, guarded_hook(bridge))))
    assert status == 403
    runtime.client.state.update(can_run=True, state="running", run_id="b" * 32)
    calls = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def post(self, url, **_options):
            calls.append(url)
            return Response()

    hook = guarded_hook(bridge)
    hook.session = Session()
    hook._response_json = AsyncMock(return_value=({
        "baseResponse": {"ret": 0},
        "qrcode": {"iLen": 16, "buffer": "iVBORw0KGgpJRU5ErkJggg=="},
    }, {}))
    [(status, body, _)] = asyncio.run(request(callback_app(bridge, hook)))
    assert status == 200 and body["ok"] is True
    assert calls == ["http://127.0.0.1:19088/api/get_my_qrocde"]


def test_unexpected_group_qrcode_failure_keeps_error_logging(bridge, caplog):
    async def groups():
        return [{"wxid": "room@chatroom"}]

    async def broken(_group):
        raise RuntimeError("synthetic hook failure")

    hook = SimpleNamespace(get_groups_strict=groups, get_group_qrcode=broken)
    with caplog.at_level(logging.ERROR):
        [(status, body, _)] = asyncio.run(request(callback_app(bridge, hook)))
    assert status == 502 and body["ok"] is False
    assert "LIC-automation_paused" not in str(body)
    assert len(caplog.records) == 1 and caplog.records[0].exc_info
