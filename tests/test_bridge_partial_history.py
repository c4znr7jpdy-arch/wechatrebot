"""The durable journal describes only segments accepted by the send boundary."""

import asyncio
import importlib
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401
from wechatrebot_manager.chat_history import ChatHistoryService
from wechatrebot_manager.licensing.protocol import LicenseError


ACCOUNT = "wxid_synthetic_bot"
TARGET = "wxid_synthetic_contact"
IMAGE = {"type": "image", "data": {"file": "synthetic.png"}}
TEXT = {"type": "text", "data": {"text": "synthetic caption"}}


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    module = importlib.import_module("wechatrebot_manager.licensing.runtime_guard")
    monkeypatch.setattr(module, "_guard", SimpleNamespace(
        status=lambda *_: {"enabled": False, "can_run": True},
        require=lambda *_, **__: {"enabled": False, "can_run": True},
        require_hook=lambda *_: None,
    ))


def router_for(bridge, tmp_path):
    image = tmp_path / "synthetic.png"
    Image.new("RGB", (24, 24), "red").save(image)
    hook = SimpleNamespace(recall=AsyncMock(return_value=bridge.onebot.SendResult(ok=True)))
    router = bridge.onebot.OneBotActionRouter(
        hook, SimpleNamespace(materialize=AsyncMock(return_value=image)),
        bridge.onebot.BridgeState(600, 256, bot_wxid=ACCOUNT, bot_nickname="synthetic bot"),
        policy_path=tmp_path / "group-policies.json",
    )
    history = importlib.import_module(bridge.onebot.__package__ + ".history_store")
    service = ChatHistoryService(tmp_path)
    router.history = history.MessageHistoryStore(service.bridge_source)
    return router, service


def receipt(bridge, *, message_id="9007199254740993"):
    return bridge.onebot.SendResult(
        ok=True, message_id=message_id, create_time=100, to_user_name=TARGET,
    )


def rows(router):
    with sqlite3.connect(router.history.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY id")]


@pytest.mark.parametrize("failure", ["rejected", "transport", "media", "unexpected", "license", "cancelled"])
def test_later_failure_records_only_confirmed_image_and_keeps_recall(bridge, tmp_path, failure):
    async def run():
        router, service = router_for(bridge, tmp_path)
        entered = asyncio.Event()

        async def execute(command, *_):
            if command.kind == "image":
                return receipt(bridge)
            if failure == "transport":
                raise OSError("synthetic transport failure")
            if failure == "media":
                raise bridge.onebot.MediaError("synthetic media failure")
            if failure == "unexpected":
                raise RuntimeError("synthetic unexpected failure")
            if failure == "license":
                raise LicenseError("automation_paused", "synthetic paused")
            if failure == "cancelled":
                entered.set()
                await asyncio.Event().wait()
            return bridge.onebot.SendResult(ok=False, error="synthetic caption failure")

        router._execute_command = execute
        task = asyncio.create_task(router._send_message("send_private_msg", {
            "user_id": TARGET, "message": [IMAGE, TEXT], "auto_recall_delay": 60,
        }, "partial"))
        try:
            if failure == "cancelled":
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif failure in {"unexpected", "license"}:
                expected = RuntimeError if failure == "unexpected" else LicenseError
                with pytest.raises(expected):
                    await task
            else:
                response = await task
                assert response["status"] == "failed" and response["data"] is None
                assert response["echo"] == "partial" and "after 1 completed" in response["message"]
            journal = rows(router)
            assert len(journal) == 1
            assert journal[0]["content"] == "[图片]"
            assert journal[0]["account_id"] == ACCOUNT
            assert journal[0]["dedup_key"] == f"out:{ACCOUNT}:{TARGET}:9007199254740993"
            assert journal[0]["timestamp"] == 100
            assert len(json.loads(journal[0]["metadata"])["media_previews"]) == 1
            imported = service.messages(TARGET, account_id=ACCOUNT)
            assert len(imported) == 1 and imported[0]["content"] == "[图片]"
            assert len(imported[0]["media_previews"]) == 1
            assert router._scheduled, "confirmed image keeps its existing recall owner"
            assert not router.observe_outgoing_image(bridge.onebot.RecallRecord("unrelated", 101, TARGET))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await router.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["rejected", "materialize", "cancelled"])
def test_first_unconfirmed_segment_never_creates_history(bridge, tmp_path, failure):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        entered = asyncio.Event()

        async def block(*_):
            entered.set()
            await asyncio.Event().wait()

        router._execute_command = AsyncMock(return_value=bridge.onebot.SendResult(ok=False))
        if failure == "materialize":
            router.media.materialize.side_effect = OSError("synthetic unavailable media")
        elif failure == "cancelled":
            router._execute_command = block
        task = asyncio.create_task(router._send_message("send_private_msg", {
            "user_id": TARGET, "message": [IMAGE, TEXT],
        }, None))
        try:
            if failure == "cancelled":
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert (await task)["status"] == "failed"
            assert rows(router) == []
        finally:
            await router.close()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["send", "materialize"])
def test_failed_later_image_never_contributes_preview_or_content(bridge, tmp_path, stage):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        router._execute_command = AsyncMock(side_effect=[
            receipt(bridge), bridge.onebot.SendResult(ok=False, error="synthetic image rejected"),
        ])
        if stage == "materialize":
            router.media.materialize.side_effect = OSError("synthetic image unavailable")
        try:
            response = await router._send_message("send_private_msg", {
                "user_id": TARGET, "message": [TEXT, IMAGE],
            }, None)
            assert response["status"] == "failed"
            journal = rows(router)
            assert len(journal) == 1 and journal[0]["content"] == "synthetic caption"
            assert json.loads(journal[0]["metadata"])["media_previews"] == []
        finally:
            await router.close()

    asyncio.run(run())


@pytest.mark.parametrize("has_receipt", [True, False])
def test_full_success_still_records_one_combined_message(bridge, tmp_path, has_receipt):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        result = receipt(bridge) if has_receipt else bridge.onebot.SendResult(ok=True)
        router._execute_command = AsyncMock(return_value=result)
        try:
            response = await router._send_message("send_private_msg", {
                "user_id": TARGET, "message": [IMAGE, TEXT],
            }, "success")
            assert response["status"] == "ok" and response["echo"] == "success"
            assert isinstance(response["data"]["message_id"], int)
            journal = rows(router)
            assert len(journal) == 1 and journal[0]["content"] == "[图片]\nsynthetic caption"
            if has_receipt:
                assert router.state.get_recall(response["data"]["message_id"]).message_id == "9007199254740993"
            else:
                assert journal[0]["dedup_key"].startswith("out:")
        finally:
            await router.close()

    asyncio.run(run())


@pytest.mark.parametrize("switch_back", [False, True])
def test_account_change_does_not_journal_previous_confirmed_prefix(bridge, tmp_path, switch_back):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def execute(*_):
            nonlocal count
            count += 1
            if count == 1:
                return receipt(bridge)
            entered.set()
            await release.wait()
            return bridge.onebot.SendResult(ok=False)

        router._execute_command = execute
        task = asyncio.create_task(router.handle({"action": "send_private_msg", "params": {
            "user_id": TARGET, "message": [IMAGE, TEXT],
        }}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            router.state.set_identity("wxid_synthetic_other")
            if switch_back:
                router.state.set_identity(ACCOUNT)
            release.set()
            response = await task
            assert response["status"] == "failed" and response["retcode"] == 1403
            assert rows(router) == []
        finally:
            await router.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["rejected", "unexpected", "cancelled", "success"])
def test_history_write_failure_preserves_send_result_or_exception(bridge, tmp_path, caplog, outcome):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        entered = asyncio.Event()

        def fail_write(*_, **__):
            raise sqlite3.OperationalError("synthetic history unavailable")

        async def execute(command, *_):
            if command.kind == "image" or outcome == "success":
                return receipt(bridge)
            if outcome == "unexpected":
                raise RuntimeError("synthetic original failure")
            if outcome == "cancelled":
                entered.set()
                await asyncio.Event().wait()
            return bridge.onebot.SendResult(ok=False, error="synthetic original rejection")

        router.history = SimpleNamespace(record_outbound=fail_write)
        router._execute_command = execute
        task = asyncio.create_task(router._send_message("send_private_msg", {
            "user_id": TARGET, "message": [IMAGE, TEXT],
        }, None))
        try:
            if outcome == "cancelled":
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "unexpected":
                with pytest.raises(RuntimeError, match="synthetic original failure"):
                    await task
            else:
                response = await task
                assert response["status"] == ("ok" if outcome == "success" else "failed")
                if outcome == "rejected":
                    assert "synthetic original rejection" in response["message"]
            assert "failed to persist outbound message history" in caplog.text
        finally:
            await router.close()

    asyncio.run(run())


def test_cancellation_during_sqlite_worker_keeps_confirmed_commit_and_cancel_result(bridge, tmp_path):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        store = router.history

        def record(*args, **kwargs):
            entered.set()
            try:
                assert release.wait(2)
                return store.record_outbound(*args, **kwargs)
            finally:
                finished.set()

        router.history = SimpleNamespace(record_outbound=record)
        router._execute_command = AsyncMock(return_value=receipt(bridge))
        task = asyncio.create_task(router._send_message("send_private_msg", {
            "user_id": TARGET, "message": [TEXT],
        }, None))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            router.history = store
            journal = rows(router)
            assert len(journal) == 1 and journal[0]["content"] == "synthetic caption"
        finally:
            release.set()
            await router.close()

    asyncio.run(run())


def test_scheduled_authorization_failure_records_confirmed_prefix(bridge, tmp_path):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        router.hook.send_image = AsyncMock(return_value=receipt(bridge))
        router.hook.send_text = AsyncMock()
        router.scheduled_policy.authorize = AsyncMock(side_effect=[
            SimpleNamespace(allowed=True), SimpleNamespace(allowed=True),
            SimpleNamespace(allowed=False, message="synthetic task paused"),
        ])
        try:
            response = await router._send_message("send_private_msg", {
                "user_id": TARGET, "message": [IMAGE, TEXT],
                "_wechatrebot_delivery": "scheduled", "_wechatrebot_account": ACCOUNT,
            }, None)
            assert response["status"] == "failed" and response["retcode"] == 1403
            assert response["message"] == "synthetic task paused"
            journal = rows(router)
            assert len(journal) == 1 and journal[0]["content"] == "[图片]"
            router.hook.send_text.assert_not_awaited()
        finally:
            await router.close()

    asyncio.run(run())


def test_group_quote_keeps_first_segment_reply_and_journals_only_sent_text(bridge, tmp_path):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        group = "synthetic@chatroom"
        quote = bridge.onebot.ReplyTarget("native-inbound", 99, TARGET, "synthetic user", "quoted content")
        router.state.remember_reply_target(group, quote, message_id=7)
        router.outbound_policy.authorize = AsyncMock(return_value=SimpleNamespace(allowed=True))
        router.hook.send_reply_text = AsyncMock(return_value=bridge.onebot.SendResult(
            ok=True, message_id="native-reply", create_time=100, to_user_name=group,
        ))
        router.hook.send_text = AsyncMock()
        router.hook.send_image = AsyncMock(return_value=bridge.onebot.SendResult(ok=False))
        try:
            response = await router._send_message("send_group_msg", {
                "group_id": group, "message": [
                    {"type": "reply", "data": {"id": "7"}}, TEXT, IMAGE,
                    {"type": "text", "data": {"text": "synthetic unsent ending"}},
                ],
            }, None)
            assert response["status"] == "failed" and "after 1 completed" in response["message"]
            router.hook.send_reply_text.assert_awaited_once_with(group, "synthetic caption", quote)
            router.hook.send_text.assert_not_awaited()
            journal = rows(router)
            assert len(journal) == 1 and journal[0]["content"] == "synthetic caption"
            assert journal[0]["dedup_key"] == f"out:{ACCOUNT}:{group}:native-reply"
            assert json.loads(journal[0]["metadata"])["media_previews"] == []
        finally:
            await router.close()

    asyncio.run(run())


def test_forward_failure_records_completed_node_and_only_confirmed_second_node_prefix(bridge, tmp_path):
    async def run():
        router, _ = router_for(bridge, tmp_path)
        router._execute_command = AsyncMock(side_effect=[
            receipt(bridge, message_id="node-one"), receipt(bridge, message_id="node-two-image"),
            bridge.onebot.SendResult(ok=False, error="synthetic final caption rejected"),
        ])
        try:
            response = await router.handle({"action": "send_private_forward_msg", "params": {
                "user_id": TARGET, "messages": [
                    {"type": "node", "data": {"content": [TEXT]}},
                    {"type": "node", "data": {"content": [IMAGE, TEXT]}},
                ],
            }})
            assert response["status"] == "failed"
            assert "forward node 2 failed after 1 completed" in response["message"]
            journal = rows(router)
            assert [row["content"] for row in journal] == ["synthetic caption", "[图片]"]
            assert [row["dedup_key"] for row in journal] == [
                f"out:{ACCOUNT}:{TARGET}:node-one", f"out:{ACCOUNT}:{TARGET}:node-two-image",
            ]
        finally:
            await router.close()

    asyncio.run(run())
