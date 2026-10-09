from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = ROOT / "vendor/hook_41827_astrbot_bridge"
PATCH_PATH = ROOT / "patches" / "hook-bridge-image-recall-batch.patch"


def test_batched_outgoing_image_callback_binds_recall_receipt(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "app" / "bridge"
    shutil.copytree(BRIDGE_SOURCE / "src", checkout / "src")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    command = [
        "git",
        "apply",
        "--whitespace=error-all",
        "-p2",
        "--directory=app/bridge",
        str(PATCH_PATH),
    ]
    checked = subprocess.run(
        [*command[:2], "--check", *command[2:]],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert checked.returncode == 0, checked.stderr
    applied = subprocess.run(
        command,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert applied.returncode == 0, applied.stderr

    package_root = checkout / "src" / "hook_bridge"
    package_name = "_patched_hook_bridge_image_recall"
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_root / "__init__.py",
        submodule_search_locations=[str(package_root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    app_module = importlib.import_module(f"{package_name}.app")
    try:
        class Router:
            def __init__(self) -> None:
                self.receipts: list[tuple[str, int, str]] = []

            def observe_outgoing_image(self, record) -> bool:
                self.receipts.append(
                    (record.message_id, record.create_time, record.to_user_name)
                )
                return True

        router = Router()
        application = object.__new__(app_module.BridgeApplication)
        application.router = router
        event = SimpleNamespace(
            kind="outgoing",
            account_id="wxid_bot",
            sender_id="wxid_bot",
            conversation_id="room@chatroom",
        )
        payload = {
            "event_type": "个人发送图片消息服务器响应",
            "account_wxid": "wxid_bot",
            "list": [
                {
                    "msgType": 3,
                    "newMsgId": "9009",
                    "createTime": 1760000009,
                    "toUserName": {"String": "room@chatroom"},
                }
            ],
        }

        application._observe_outgoing_image_receipt(payload, event)

        assert router.receipts == [("9009", 1760000009, "room@chatroom")]
    finally:
        for module_name in tuple(sys.modules):
            if module_name == package_name or module_name.startswith(
                f"{package_name}."
            ):
                sys.modules.pop(module_name, None)


@pytest.fixture
def recall_client(tmp_path):
    checkout = tmp_path / "app" / "bridge"
    shutil.copytree(BRIDGE_SOURCE / "src", checkout / "src")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    # The directory patch changes get_friends immediately after recall. Check
    # real staging order so its context cannot break the new recall patch.
    for name in ("hook-bridge-group-directory.patch", "hook-bridge-revoke-any.patch"):
        subprocess.run(
            ["git", "apply", "--whitespace=error-all", "-p2", "--directory=app/bridge",
             str(ROOT / "patches" / name)],
            cwd=tmp_path, check=True, capture_output=True,
        )
    package_root = checkout / "src/hook_bridge"
    name = "_patched_hook_recall_api"
    spec = importlib.util.spec_from_file_location(
        name, package_root / "__init__.py", submodule_search_locations=[str(package_root)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    try:
        yield importlib.import_module(f"{name}.hook_client")
    finally:
        for key in tuple(sys.modules):
            if key == name or key.startswith(f"{name}."):
                sys.modules.pop(key, None)


def test_recall_uses_only_documented_endpoint_and_keeps_exact_receipt(recall_client):
    client = object.__new__(recall_client.HookClient)
    calls = []

    async def post(path, payload, **kwargs):
        calls.append((path, payload.copy()))
        return {"errCode": 1, "data": {"baseResponse": {"ret": 0}}}

    client._post = post
    result = asyncio.run(client.recall("1234567890123456789", 1790236882, "room@chatroom"))
    assert result.ok
    assert calls == [("/api/revoke_any", {
        "newMsgId": 1234567890123456789, "createTime": 1790236882,
        "toUserName": "room@chatroom",
    })]
    assert not hasattr(recall_client, "_is_unsupported_route")


@pytest.mark.parametrize("response", [
    {"errCode": 404, "errMsg": "route not found"},
    {"errCode": 1},
    {"errCode": 1, "data": {"baseResponse": {"ret": -1}}},
    {"errCode": 1, "data": {"baseResponse": {"ret": 0}}, "success": False},
])
def test_failed_or_ambiguous_recall_is_not_retried_or_reported_success(recall_client, response):
    client = object.__new__(recall_client.HookClient)
    calls = []

    async def post(path, payload, **kwargs):
        calls.append(path)
        return response

    client._post = post
    result = asyncio.run(client.recall("9007199254740993", 1790236882, "wxid_test"))
    assert not result.ok
    assert calls == ["/api/revoke_any"]


@pytest.mark.parametrize("status", [401, 404, 405, 500, None])
def test_transport_and_server_errors_are_not_retried(recall_client, status):
    client = object.__new__(recall_client.HookClient)
    calls = []

    async def post(path, payload, **kwargs):
        calls.append(path)
        raise recall_client.HookApiError(path, status, "request failed")

    client._post = post
    with pytest.raises(recall_client.HookApiError):
        asyncio.run(client.recall("9007199254740993", 1790236882, "wxid_test"))
    assert calls == ["/api/revoke_any"]


@pytest.mark.parametrize("payload", [
    {"errCode": 1, "data": {"errCode": 0, "data": "private transport detail"}},
    {"errCode": 1, "data": {"baseResponse": {"ret": -1}, "sysWording": "private account"}},
    {"errCode": 1, "data": None},
])
def test_recall_failure_reports_envelope_codes_without_private_content(recall_client, payload):
    client = object.__new__(recall_client.HookClient)
    calls = []

    async def post(path, body, **kwargs):
        calls.append(path)
        return payload

    client._post = post
    result = asyncio.run(client.recall("9007199254740993", 1790236882, "wxid_fixture"))
    assert not result.ok
    assert calls == ["/api/revoke_any"]
    assert 'recall_response=[{"errCode":1' in result.error
    assert "private" not in result.error
    assert "wxid_fixture" not in result.error
    assert "9007199254740993" not in result.error
    if isinstance(payload["data"], dict):
        if "errCode" in payload["data"]:
            assert '"errCode":0' in result.error
        else:
            assert '"base_ret":-1' in result.error


@pytest.mark.parametrize("payload,category", [
    ({"errCode": 1, "errMsg": "请求处理成功", "data": {
        "errCode": 0, "errMsg": "请求处理失败", "data": {},
    }}, "empty_or_invalid_pb"),
    ({"errCode": 1, "data": {
        "errCode": 0, "errMsg": "未匹配到cmdid 请联系作者", "data": {},
    }}, "missing_cmdid"),
    ({"errCode": 0, "data": {}}, "transport_failure"),
    ({"errCode": 1, "errMsg": "请求处理成功", "data": {}}, "completion_unconfirmed"),
    ({"errCode": 1, "data": {"newMsgId": "12345"}}, "completion_unconfirmed"),
    ({"errCode": 1, "data": {"BaseResponse": {"Ret": -1}}}, "server_rejected"),
    ({"errCode": 1, "data": {"baseResponse": {"ret": 0}},
      "success": False}, "transport_failure"),
    ({"errCode": 1, "data": {"baseResponse": {"ret": False}}}, "completion_unconfirmed"),
    ({"errCode": 1, "data": {"baseResponse": {"ret": 0.1}}}, "completion_unconfirmed"),
    ({"errCode": "PRIVATE_TEST_TOKEN", "data": {
        "baseResponse": {"ret": 0},
    }}, "invalid_response"),
    ({"baseResponse": {"ret": 0}, "data": {
        "baseResponse": {"ret": -1},
    }}, "server_rejected"),
    ({"baseResponse": {"ret": 0}, "data": {"data": {"data": {"data": {
        "errCode": 0,
    }}}}}, "invalid_response"),
])
def test_recall_requires_server_confirmation_even_when_generic_send_parser_accepts(
    recall_client, payload, category,
):
    result = recall_client._recall_result(recall_client.parse_send_result(payload))
    assert not result.ok
    assert f"({category})" in result.error
    assert "PRIVATE_TEST_TOKEN" not in result.error


@pytest.mark.parametrize("payload", [
    {"baseResponse": {"ret": 0}},
    {"errCode": 1, "data": {"baseResponse": {"ret": "0"}}},
    {"code": 0, "data": {"BaseResponse": {"Ret": 0}}},
    {"errCode": 1, "data": json.dumps({"baseResponse": {"ret": 0}})},
])
def test_recall_accepts_explicit_server_success_shapes(recall_client, payload):
    result = recall_client._recall_result(recall_client.parse_send_result(payload))
    assert result.ok
    assert result.error is None


def test_recall_discards_untrusted_error_text_from_diagnostics(recall_client):
    payload = {
        "errCode": 1, "errMsg": "failed for PRIVATE_TEST_TOKEN",
        "data": {"baseResponse": {"ret": -1}, "sysWording": "PRIVATE_ACCOUNT"},
    }
    result = recall_client._recall_result(recall_client.parse_send_result(payload))
    assert not result.ok
    assert "PRIVATE" not in result.error
    assert "server_rejected" in result.error
