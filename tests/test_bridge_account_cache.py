"""A login change retires private caches and any operation holding the old data."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401


A = "wxid_synthetic_a"
B = "wxid_synthetic_b"
TARGET = "wxid_synthetic_contact"
GROUP = "synthetic@chatroom"


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    module = importlib.import_module("wechatrebot_manager.licensing.runtime_guard")
    value = SimpleNamespace(
        status=lambda *_: {"enabled": False, "can_run": True},
        require=lambda *_, **__: {"enabled": False, "can_run": True},
        require_hook=lambda *_: None,
        queue_event=lambda value: dict(value),
        delivery_event=lambda value: dict(value),
    )
    monkeypatch.setattr(module, "_guard", value)
    return value


def router_for(bridge, tmp_path):
    state = bridge.onebot.BridgeState(600, 256, bot_wxid=A)
    hook = SimpleNamespace(recall=AsyncMock(return_value=bridge.onebot.SendResult(ok=True)))
    router = bridge.onebot.OneBotActionRouter(
        hook, SimpleNamespace(), state, policy_path=tmp_path / "group-policies.json"
    )
    return router


def message(account=A, *, group=False, message_id=1):
    return {
        "self_id": account, "post_type": "message", "message_id": message_id,
        "message_type": "group" if group else "private", "user_id": TARGET,
        "group_id": GROUP if group else "", "raw_message": "synthetic old content",
    }


def fill_caches(bridge, state):
    private, group = message(), message(group=True, message_id=2)
    state.remember_message(1, private)
    state.remember_message(2, group)
    state.remember_group_message(group)
    state.remember_reply_target(
        TARGET, bridge.onebot.ReplyTarget("old-inbound", 100, TARGET, "contact", "old quote"),
        message_id=1,
    )
    state.remember_recall(3, bridge.onebot.RecallRecord("old-receipt", 101, TARGET))
    state.accept_message("old-dedup")
    identity = bridge.app.GroupMemberIdentity("old member", "old card")
    state.replace_group_member_snapshot(GROUP, {TARGET: identity})
    state.friends[TARGET] = {"nickname": "old contact"}
    state.groups[GROUP] = {"nickname": "old group"}


def test_identity_change_clears_account_caches_and_keeps_sequence_and_limits(bridge):
    state = bridge.onebot.BridgeState(600, 256, history_capacity=3, bot_wxid=A)
    fill_caches(bridge, state)
    first = state.next_message_id()
    assert not state.set_identity(A, "new nickname", "new avatar")
    assert state.get_message(1) and state.get_recall(3)
    assert state.set_identity(B, "account b")
    assert state.account_epoch == 1 and state.next_message_id() == first + 1
    assert state.history_capacity == 3 and state.dedup_capacity == 256
    assert state.dedup_ttl_seconds == 600
    for name in (
        "friends", "groups", "group_members", "_dedup", "_recalls", "_latest_by_conversation",
        "_group_history", "_messages", "_latest_message_by_conversation", "_pending_reply_by_conversation",
        "_reply_target_by_message_id", "_reply_target_conversation_by_message_id", "_group_member_snapshots",
    ):
        assert not getattr(state, name), name
    assert state.set_identity("")
    assert state.account_epoch == 2


def test_new_account_cannot_query_old_messages_history_quotes_or_recalls(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        fill_caches(bridge, router.state)
        client = bridge.onebot.OneBotClient("ws://127.0.0.1:1", router)
        client._self_id = A
        await client.set_identity(B)
        response = await router.handle({"action": "get_msg", "params": {"message_id": 1}})
        assert response["status"] == "failed"
        history = await router.handle({"action": "get_group_msg_history", "params": {"group_id": GROUP}})
        assert history["data"]["messages"] == []
        assert router.state.take_reply_target_by_message_id(1) is None
        deleted = await router.handle({"action": "delete_msg", "params": {"message_id": 3}})
        assert deleted["status"] == "failed"
        router.hook.recall.assert_not_awaited()
        router.state.remember_message(4, message(B, message_id=4))
        fresh = await router.handle({"action": "get_msg", "params": {"message_id": 4}})
        assert fresh["data"]["self_id"] == B

    asyncio.run(run())


@pytest.mark.parametrize("switch_back", [False, True], ids=["a-to-b", "a-to-b-to-a"])
def test_inflight_send_cannot_cache_or_journal_receipt_for_new_account(bridge, tmp_path, switch_back):
    async def run():
        router = router_for(bridge, tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        journal = []
        router.history = SimpleNamespace(record_outbound=lambda *a, **k: journal.append((a, k)))

        async def execute(*_):
            entered.set()
            await release.wait()
            return bridge.onebot.SendResult(ok=True, message_id="old-send", create_time=102, to_user_name=TARGET)

        router.hook.send_text = execute
        task = asyncio.create_task(router.handle({"action": "send_private_msg", "params": {
            "user_id": TARGET, "message": "synthetic old reply",
        }}))
        await asyncio.wait_for(entered.wait(), 1)
        router.state.set_identity(B)
        if switch_back:
            router.state.set_identity(A)
        release.set()
        result = await asyncio.wait_for(task, 1)
        assert result["status"] == "failed" and result["retcode"] == 1403
        assert not router.state._recalls and not journal

    asyncio.run(run())


@pytest.mark.parametrize("action,method,params,rows", [
    ("get_friend_list", "get_friends", {}, [{"wxid": TARGET, "nickName": "old contact"}]),
    ("get_group_list", "get_groups", {}, [{"wxid": GROUP, "nickName": "old group"}]),
    ("get_group_member_list", "get_group_members", {"group_id": GROUP}, [{"wxid": TARGET, "nickName": "old member"}]),
])
def test_inflight_directory_reads_cannot_populate_new_account_caches(bridge, tmp_path, action, method, params, rows):
    async def run():
        router = router_for(bridge, tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def read(*_):
            entered.set()
            await release.wait()
            return rows

        setattr(router.hook, method, read)
        task = asyncio.create_task(router.handle({"action": action, "params": params}))
        await asyncio.wait_for(entered.wait(), 1)
        router.state.set_identity(B)
        release.set()
        response = await asyncio.wait_for(task, 1)
        assert response["status"] == "failed" and response["retcode"] == 1403
        assert not router.state.friends and not router.state.groups and not router.state.group_members
        assert not router.state._messages and not router.state._recalls

    asyncio.run(run())


def test_old_event_retry_is_discarded_even_after_returning_to_same_account(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        client = bridge.onebot.OneBotClient("ws://127.0.0.1:1", router)
        client._self_id = A
        client._connection = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        await client.send_event(message())
        old = await client._events.get()
        router.state.set_identity(B)
        router.state.set_identity(A)
        await client._send_json(old)
        client._events.task_done()
        client._connection.send.assert_not_awaited()
        await client.send_event(message(message_id=4))
        fresh = await client._events.get()
        await client._send_json(fresh)
        client._events.task_done()
        await asyncio.wait_for(client._events.join(), 1)
        client._connection.send.assert_awaited_once()
        sent = json.loads(client._connection.send.await_args.args[0])
        assert sent["message_id"] == 4 and "_hook_bridge_account_epoch" not in sent

    asyncio.run(run())


def test_sender_waiting_for_lock_checks_account_after_lock_is_released(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        client = bridge.onebot.OneBotClient("ws://127.0.0.1:1", router)
        client._self_id = A
        client._connection = SimpleNamespace(send=AsyncMock())
        await client._send_lock.acquire()
        task = asyncio.create_task(client._send_json(message()))
        await asyncio.sleep(0)
        router.state.set_identity(B)
        client._send_lock.release()
        await asyncio.wait_for(task, 1)
        client._connection.send.assert_not_awaited()

    asyncio.run(run())


def test_late_image_receipt_cannot_repopulate_new_account_cache(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        waiter = router._new_image_receipt_waiter(TARGET)
        module = importlib.import_module(bridge.onebot.__package__ + ".state")
        with module.account_scope(router.state):
            task = asyncio.create_task(router._recall_when_image_receipt_arrives(TARGET, waiter, 1, 0))
        router.state.set_identity(B)
        assert not router.observe_outgoing_image(bridge.onebot.RecallRecord("old-image", 103, TARGET))
        await asyncio.gather(task, return_exceptions=True)
        assert not router.state._recalls and not router._pending_image_receipts
        router.hook.recall.assert_not_awaited()

    asyncio.run(run())


def test_old_waiter_cannot_steal_current_accounts_image_receipt(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        old = router._new_image_receipt_waiter(TARGET)
        router.state.set_identity(B)
        current = router._new_image_receipt_waiter(TARGET)
        receipt = bridge.onebot.RecallRecord("current-image", 104, TARGET)
        assert router.observe_outgoing_image(receipt)
        assert old.cancelled() and current.result() == receipt
        router._cancel_image_receipt_waiter(TARGET, current)
        assert not router._pending_image_receipts and not router._image_receipt_epochs

    asyncio.run(run())


def test_identity_change_cancels_pending_tasks_and_waiters_without_touching_new_tasks(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        client = bridge.onebot.OneBotClient("ws://127.0.0.1:1", router)
        client._self_id = A
        waiter = router._new_image_receipt_waiter(TARGET)
        old = asyncio.create_task(asyncio.Event().wait())
        router._scheduled.add(old)
        old.add_done_callback(router._scheduled.discard)
        await client.set_identity("")
        assert waiter.cancelled() and not router._pending_image_receipts
        await asyncio.gather(old, return_exceptions=True)
        assert old.cancelled() and not router._scheduled
        await client.set_identity(B)
        fresh = router._new_image_receipt_waiter(TARGET)
        await client.set_identity(B, "updated nickname")
        assert not fresh.done() and router.state.account_epoch == 2
        router._cancel_image_receipt_waiter(TARGET, fresh)

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["groups", "members"])
def test_late_group_poll_cannot_restore_previous_account_baseline(bridge, stage):
    async def run():
        state = bridge.onebot.BridgeState(600, 256, bot_wxid=A)
        app = object.__new__(bridge.app.BridgeApplication)
        app.state = state
        app._native_download_generation = 0
        app._hook_check_overlapped_native_download = lambda _: False
        app._polled_group_members, app._polled_group_names = {}, {}
        entered, release = asyncio.Event(), asyncio.Event()

        async def block(result):
            entered.set()
            await release.wait()
            return result

        groups = [{"wxid": GROUP, "nickname": "old group"}]
        members = [{"wxid": TARGET, "nickName": "old member"}]
        app.hook = SimpleNamespace(
            get_groups=(lambda: block(groups)) if stage == "groups" else AsyncMock(return_value=groups),
            get_group_members=(lambda *_: block(members)) if stage == "members" else AsyncMock(),
        )
        task = asyncio.create_task(app._poll_group_member_changes_once())
        await asyncio.wait_for(entered.wait(), 1)
        state.set_identity(B)
        release.set()
        module = importlib.import_module(bridge.onebot.__package__ + ".state")
        with pytest.raises(module.AccountChangedError):
            await task
        assert not app._polled_group_members
        assert not state.group_members
        if stage == "groups":
            assert not app._polled_group_names
            app.hook.get_group_members.assert_not_awaited()

    asyncio.run(run())


def test_consumer_drops_old_inbound_preload_then_processes_login(bridge, tmp_path, monkeypatch):
    async def run():
        router = router_for(bridge, tmp_path)
        app = object.__new__(bridge.app.BridgeApplication)
        app.state, app.router = router.state, router
        app.onebot = bridge.onebot.OneBotClient("ws://127.0.0.1:1", router)
        app.onebot._self_id = A
        app.event_queue = asyncio.Queue()
        app._polled_group_members, app._polled_group_names = {}, {}
        app._observe_outgoing_image_receipt = lambda *_: None
        entered, release = asyncio.Event(), asyncio.Event()

        async def members(*_):
            entered.set()
            await release.wait()
            return [{"wxid": TARGET, "nickName": "old member"}]

        app.hook = SimpleNamespace(get_group_detail_members=members)
        monkeypatch.setattr(bridge.app, "normalize_event", lambda payload: payload["event"])
        event = bridge.app.NormalizedEvent(kind="message", account_id=A, conversation_id=GROUP,
                                           sender_id=TARGET, message_id="old-inbound", message_type=1)
        app.event_queue.put_nowait({"event": event})
        task = asyncio.create_task(app._consume_events())
        await asyncio.wait_for(entered.wait(), 1)
        app.state.set_identity(B)
        release.set()
        app.event_queue.put_nowait({"event": SimpleNamespace(kind="login", account_id=B, sender_name="b")})
        await asyncio.wait_for(app.event_queue.join(), 1)
        assert not task.done() and app.state.bot_wxid == B
        assert not app.state.group_members and not app.state._messages
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_materialization_resumed_after_switch_never_reaches_hook(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def materialize(*_):
            entered.set()
            await release.wait()
            return tmp_path / "synthetic.png"

        router.media.materialize = materialize
        router.hook = bridge.hook.HookClient(
            "http://127.0.0.1:1", SimpleNamespace(post=lambda *a, **k: pytest.fail("stale send reached HOOK"))
        )
        task = asyncio.create_task(router.handle({"action": "send_private_msg", "params": {
            "user_id": TARGET, "message": [{"type": "image", "data": {"file": "synthetic.png"}}],
        }}))
        await asyncio.wait_for(entered.wait(), 1)
        router.state.set_identity("")
        router.state.set_identity(B)
        release.set()
        response = await asyncio.wait_for(task, 1)
        assert response["status"] == "failed" and response["retcode"] == 1403
        assert not router.state._recalls and not router._pending_image_receipts

    asyncio.run(run())


def test_old_delayed_recall_never_reaches_new_account_hook(bridge, tmp_path):
    async def run():
        router = router_for(bridge, tmp_path)
        router.state.remember_recall(1, bridge.onebot.RecallRecord("old-receipt", 105, TARGET))
        response = await router.handle({"action": "auto_recall_last_msg", "params": {"user_id": TARGET, "delay": 0}})
        assert response["status"] == "ok"
        tasks = tuple(router._scheduled)
        router.state.set_identity(B)
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        router.hook.recall.assert_not_awaited()

    asyncio.run(run())


def test_login_callback_clears_poll_baseline_once_and_keeps_consumer_running(bridge, tmp_path, monkeypatch):
    async def run():
        router = router_for(bridge, tmp_path)
        client = bridge.onebot.OneBotClient("ws://127.0.0.1:1", router)
        client._self_id = A
        app = object.__new__(bridge.app.BridgeApplication)
        app.state, app.router, app.onebot = router.state, router, client
        app._polled_group_members, app._polled_group_names = {GROUP: {}}, {GROUP: "old name"}
        app._observe_outgoing_image_receipt = lambda *_: None
        app.event_queue = asyncio.Queue()
        monkeypatch.setattr(bridge.app, "normalize_event", lambda payload: payload["event"])
        login = SimpleNamespace(kind="login", account_id=B, sender_name="b")
        app.event_queue.put_nowait({"event": login})
        task = asyncio.create_task(app._consume_events())
        await asyncio.wait_for(app.event_queue.join(), 1)
        assert not task.done() and not app._polled_group_members and not app._polled_group_names
        app.state.remember_message(4, message(B, message_id=4))
        app._polled_group_names[GROUP] = "current name"
        app.event_queue.put_nowait({"event": login})
        await asyncio.wait_for(app.event_queue.join(), 1)
        assert app.state.get_message(4) and app._polled_group_names[GROUP] == "current name"
        assert app.state.account_epoch == 1 and not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_old_callback_is_ignored_before_outgoing_receipt_binding(bridge, monkeypatch):
    async def run():
        app = object.__new__(bridge.app.BridgeApplication)
        app.state = bridge.onebot.BridgeState(600, 256, bot_wxid=B)
        app.event_queue = asyncio.Queue()
        app.event_queue.put_nowait({"synthetic": True})
        event = SimpleNamespace(kind="outgoing", account_id=A)
        monkeypatch.setattr(bridge.app, "normalize_event", lambda _: event)
        app._observe_outgoing_image_receipt = lambda *_: pytest.fail("old receipt must not bind B's waiter")
        task = asyncio.create_task(app._consume_events())
        await asyncio.wait_for(app.event_queue.join(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("envelope", [1, 7, "个人发送图片消息服务器响应"])
def test_native_old_account_image_receipt_cannot_bind_new_waiter(bridge, tmp_path, envelope):
    async def run():
        router = router_for(bridge, tmp_path)
        router.state.set_identity(B)
        current = router._new_image_receipt_waiter(TARGET)
        app = object.__new__(bridge.app.BridgeApplication)
        app.state, app.router = router.state, router
        app.event_queue = asyncio.Queue()
        payload = {"event_type": envelope, "account_wxid": A, "messageType": "个人发送图片消息服务器响应",
                   "data": {"msgType": 3, "newMsgId": "old-image", "createTime": 106, "toUserName": TARGET}}
        event = bridge.app.normalize_event(payload)
        app._observe_outgoing_image_receipt(payload, event)
        assert not current.done()
        app.event_queue.put_nowait(payload)
        task = asyncio.create_task(app._consume_events())
        await asyncio.wait_for(app.event_queue.join(), 1)
        assert not current.done() and not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        payload["account_wxid"] = B
        app._observe_outgoing_image_receipt(payload, bridge.app.normalize_event(payload))
        assert current.done() and current.result().message_id == "old-image"
        router._cancel_image_receipt_waiter(TARGET, current)

    asyncio.run(run())


def test_recall_capacity_and_ttl_preserve_recent_record_then_fail_unavailable(bridge, monkeypatch):
    module = importlib.import_module(bridge.onebot.__package__ + ".state")
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    state = bridge.onebot.BridgeState(10, 256, history_capacity=2, bot_wxid=A)
    for index, target in [(1, "first"), (2, TARGET), (3, TARGET)]:
        state.remember_recall(index, bridge.onebot.RecallRecord(str(index), 100, target))
        clock[0] += 1
    assert len(state._recalls) == 2 and state.get_recall(1) is None
    assert state.latest_recall("first") is None
    assert state.get_recall(2).message_id == "2" and state.latest_recall(TARGET).message_id == "3"
    state.forget_recall(2)
    assert state.latest_recall(TARGET).message_id == "3"
    clock[0] = 112.0
    assert state.get_recall(3) is None and state.latest_recall(TARGET) is None
    assert not state._recalls and not state._latest_by_conversation
