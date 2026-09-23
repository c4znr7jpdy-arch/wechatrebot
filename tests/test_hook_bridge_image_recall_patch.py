from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = Path(r"E:\Project\hook_41827_astrbot_bridge")
PATCH_PATH = ROOT / "patches" / "hook-bridge-image-recall-batch.patch"


def test_batched_outgoing_image_callback_binds_recall_receipt(
    tmp_path: Path,
) -> None:
    if not (BRIDGE_SOURCE / "src/hook_bridge/app.py").is_file():
        pytest.skip("HOOK 4.1.8.27 bridge source checkout is unavailable")

    checkout = tmp_path / "app" / "bridge"
    shutil.copytree(BRIDGE_SOURCE / "src", checkout / "src")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    command = [
        "git",
        "apply",
        "--ignore-space-change",
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
