"""Image recall ownership survives partial sends and never survives abandoned sends."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401
from wechatrebot_manager.licensing.protocol import LicenseError


TARGET = "wxid_fixture_target"
IMAGE = {"type": "image", "data": {"file": "synthetic.png"}}
TEXT = {"type": "text", "data": {"text": "synthetic caption"}}


def router_for(bridge, tmp_path):
    result = bridge.onebot.SendResult
    hook = SimpleNamespace(recall=AsyncMock(return_value=result(ok=True)))
    router = bridge.onebot.OneBotActionRouter(
        hook,
        SimpleNamespace(materialize=AsyncMock(return_value=tmp_path / "synthetic.png")),
        bridge.onebot.BridgeState(600, 256, bot_wxid="wxid_fixture_bot"),
        policy_path=tmp_path / "group-policies.json",
    )
    router._execute_command = AsyncMock(return_value=result(ok=True))
    return router


def params(message=None, **extra):
    return {"user_id": TARGET, "message": message or [IMAGE], "auto_recall_delay": 0, **extra}


async def finish_recalls(router):
    tasks = tuple(router._scheduled)
    assert tasks, "an already sent image must retain its automatic recall"
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    assert not router._pending_image_receipts


@pytest.mark.parametrize("stage", ["preparation", "command"])
def test_cancelled_image_cannot_steal_next_receipt_or_recall_unrelated_image(bridge, tmp_path, stage):
    async def run():
        router = router_for(bridge, tmp_path)
        entered = asyncio.Event()

        async def block(*args):
            entered.set()
            await asyncio.Event().wait()

        owner = router.media if stage == "preparation" else router
        attribute = "materialize" if stage == "preparation" else "_execute_command"
        original = getattr(owner, attribute)
        setattr(owner, attribute, block)
        task = asyncio.create_task(router._send_message("send_private_msg", params(), "cancelled"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not router._pending_image_receipts
            assert not router._scheduled

            setattr(owner, attribute, original)
            response = await router._send_message("send_private_msg", params(), "next")
            assert response["status"] == "ok"
            assert router.observe_outgoing_image(bridge.onebot.RecallRecord("next-send", 100, TARGET))
            assert not router.observe_outgoing_image(bridge.onebot.RecallRecord("unrelated-manual-image", 101, TARGET))
            await finish_recalls(router)
            router.hook.recall.assert_awaited_once_with("next-send", 100, TARGET)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await router.close()

    asyncio.run(run())


@pytest.mark.parametrize("error", [RuntimeError("synthetic failure"), LicenseError("automation_paused", "paused")])
def test_unexpected_send_exception_releases_current_image_waiter(bridge, tmp_path, error):
    async def run():
        router = router_for(bridge, tmp_path)
        router._execute_command.side_effect = error
        try:
            with pytest.raises(type(error)):
                await router._send_message("send_private_msg", params(), "failed")
            assert not router._pending_image_receipts
            assert not router._scheduled
        finally:
            await router.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["rejected", "transport", "cancelled", "unexpected"])
def test_later_text_failure_keeps_sent_image_recall(bridge, tmp_path, failure):
    async def run():
        router = router_for(bridge, tmp_path)
        entered = asyncio.Event()

        async def execute(command, *_args):
            if command.kind == "image":
                return bridge.onebot.SendResult(ok=True)
            if failure == "transport":
                raise OSError("synthetic transport failure")
            if failure == "unexpected":
                raise RuntimeError("synthetic unexpected failure")
            if failure == "cancelled":
                entered.set()
                await asyncio.Event().wait()
            return bridge.onebot.SendResult(ok=False, error="synthetic caption failure")

        router._execute_command = execute
        task = asyncio.create_task(router._send_message("send_private_msg", params([IMAGE, TEXT]), "partial"))
        try:
            if failure == "cancelled":
                await asyncio.wait_for(entered.wait(), timeout=1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif failure == "unexpected":
                with pytest.raises(RuntimeError, match="synthetic unexpected failure"):
                    await task
            else:
                response = await task
                assert response["status"] == "failed"
                assert "after 1 completed" in response["message"]
            assert router.observe_outgoing_image(bridge.onebot.RecallRecord("delivered-first-image", 102, TARGET))
            assert not router.observe_outgoing_image(bridge.onebot.RecallRecord("unrelated-manual-image", 103, TARGET))
            await finish_recalls(router)
            router.hook.recall.assert_awaited_once_with("delivered-first-image", 102, TARGET)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await router.close()

    asyncio.run(run())


def test_scheduled_authorization_failure_keeps_previous_image_recall(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        router.hook.send_image = AsyncMock(return_value=bridge.onebot.SendResult(ok=True))
        router.scheduled_policy.authorize = AsyncMock(side_effect=[
            SimpleNamespace(allowed=True), SimpleNamespace(allowed=True),
            SimpleNamespace(allowed=False, message="synthetic task paused"),
        ])
        try:
            response = await router._send_message(
                "send_private_msg", params([IMAGE, TEXT], _wechatrebot_delivery="scheduled",
                                           _wechatrebot_account="wxid_fixture_bot"), "partial",
            )
            assert response["status"] == "failed"
            assert router.observe_outgoing_image(bridge.onebot.RecallRecord("scheduled-first-image", 104, TARGET))
            await finish_recalls(router)
            router.hook.recall.assert_awaited_once_with("scheduled-first-image", 104, TARGET)
        finally:
            await router.close()

    asyncio.run(run())


def test_cancelling_second_image_releases_only_its_own_waiter(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        entered = asyncio.Event()
        sent = 0

        async def execute(*_args):
            nonlocal sent
            sent += 1
            if sent == 1:
                return bridge.onebot.SendResult(ok=True)
            entered.set()
            await asyncio.Event().wait()

        router._execute_command = execute
        task = asyncio.create_task(router._send_message("send_private_msg", params([IMAGE, IMAGE]), "partial"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(router._pending_image_receipts[TARGET]) == 1
            assert router.observe_outgoing_image(bridge.onebot.RecallRecord("first-of-two-images", 107, TARGET))
            assert not router.observe_outgoing_image(bridge.onebot.RecallRecord("unrelated-manual-image", 108, TARGET))
            await finish_recalls(router)
            router.hook.recall.assert_awaited_once_with("first-of-two-images", 107, TARGET)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await router.close()

    asyncio.run(run())


def test_close_before_recall_task_starts_releases_its_waiter(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        response = await router._send_message("send_private_msg", params(), "closing")
        assert response["status"] == "ok"
        assert router._scheduled
        await router.close()
        assert not router._scheduled
        assert not router._pending_image_receipts
        router.hook.recall.assert_not_awaited()

    asyncio.run(run())


def test_direct_image_receipt_remains_exact_when_later_segment_fails(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        router._execute_command.side_effect = [
            bridge.onebot.SendResult(ok=True, message_id="9007199254740993", create_time=105, to_user_name=TARGET),
            bridge.onebot.SendResult(ok=False, error="synthetic caption failure"),
        ]
        try:
            response = await router._send_message("send_private_msg", params([IMAGE, TEXT]), "partial")
            assert response["status"] == "failed"
            assert not router.observe_outgoing_image(bridge.onebot.RecallRecord("unrelated-manual-image", 106, TARGET))
            await finish_recalls(router)
            router.hook.recall.assert_awaited_once_with("9007199254740993", 105, TARGET)
        finally:
            await router.close()

    asyncio.run(run())
