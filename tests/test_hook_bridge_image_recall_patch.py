from __future__ import annotations

import asyncio
import importlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = ROOT / "tests/fixtures/patch_sources/hook_41827_astrbot_bridge"
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
    for name in ("hook-bridge-group-directory.patch", "hook-bridge-recall-api-fallback.patch"):
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


@pytest.mark.parametrize("unsupported", [404, 405, "body", None])
def test_recall_routes_keep_exact_receipt(recall_client, unsupported):
    client = object.__new__(recall_client.HookClient)
    calls = []

    async def post(path, payload, **kwargs):
        calls.append((path, payload.copy()))
        if path == "/api/revoke_msg" and unsupported is not None:
            if unsupported == "body":
                return {"errCode": 404, "errMsg": "route not found"}
            raise recall_client.HookApiError(path, unsupported, "route unavailable")
        return {"errCode": 1, "data": {"baseResponse": {"ret": 0}}}

    client._post = post
    result = asyncio.run(client.recall("1234567890123456789", 1790236882, "room@chatroom"))
    assert result.ok
    routes = ["/api/revoke_msg"] + (["/api/revoke_any"] if unsupported else [])
    assert [path for path, payload in calls] == routes
    assert all(payload == {"newMsgId": 1234567890123456789, "createTime": 1790236882,
                           "toUserName": "room@chatroom"} for path, payload in calls)


@pytest.mark.parametrize("response", [
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
    assert calls == ["/api/revoke_msg"]


@pytest.mark.parametrize("status", [401, 500, None])
def test_transport_and_server_errors_are_not_retried(recall_client, status):
    client = object.__new__(recall_client.HookClient)
    calls = []

    async def post(path, payload, **kwargs):
        calls.append(path)
        raise recall_client.HookApiError(path, status, "request failed")

    client._post = post
    with pytest.raises(recall_client.HookApiError):
        asyncio.run(client.recall("9007199254740993", 1790236882, "wxid_test"))
    assert calls == ["/api/revoke_msg"]
