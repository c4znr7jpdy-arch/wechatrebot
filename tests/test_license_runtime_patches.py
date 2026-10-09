import ast
import asyncio
from collections.abc import AsyncGenerator
import importlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wechatrebot_manager.licensing.protocol import LicenseError
from wechatrebot_manager.licensing.runtime_guard import guard_provider_class
from test_license_runtime_guard import runtime  # Shared real guard with controllable local client.

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
guard_module = importlib.import_module("wechatrebot_manager.licensing.runtime_guard")


@pytest.fixture(scope="module")
def patched(tmp_path_factory):
    checkout = tmp_path_factory.mktemp("license-patch-check")
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True, capture_output=True)
    for relative in ("astrbot/core/core_lifecycle.py", "astrbot/core/pipeline/scheduler.py",
                     "astrbot/core/provider/provider.py"):
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(VENDOR / "engine" / relative, target)
    shutil.copytree(VENDOR / "hook_41827_astrbot_bridge/src",
                    checkout / "hook_41827_astrbot_bridge/src")
    core_patch = ROOT / "patches/astrbot-4.26.6-license.patch"
    subprocess.run(["git", "apply", "--check", "-p2", str(core_patch)], cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "apply", "-p2", str(core_patch)], cwd=checkout, check=True, capture_output=True)
    for patch in sorted((ROOT / "patches").glob("hook-bridge-*.patch")):
        result = subprocess.run(["git", "apply", "--check", "--ignore-space-change", str(patch)], cwd=checkout, capture_output=True, text=True)
        assert result.returncode == 0, f"{patch.name}: {result.stderr}"
        subprocess.run(["git", "apply", "--ignore-space-change", str(patch)], cwd=checkout, check=True, capture_output=True)
    # The production staging step copies this shared scheduler model unchanged.
    shutil.copy2(ROOT / "src/wechatrebot_manager/task_store.py",
                 checkout / "hook_41827_astrbot_bridge/src/hook_bridge/task_store.py")
    for source in checkout.rglob("*.py"):
        ast.parse(source.read_text("utf-8"), filename=str(source))
    return checkout


@pytest.fixture
def gate(monkeypatch):
    class Gate:
        allowed = False
        calls = []

        def require_core(self, account=""):
            self.calls.append(account)
            if not self.allowed:
                raise LicenseError("automation_paused", "paused")
            return {"enabled": True, "can_run": True, "run_id": "a" * 32}

        def claim_core_session(self):
            self.require_core()

        def require(self, *args, **kwargs):
            return self.require_core()

        def require_hook(self, path):
            if path != "/api/get_profile_cache":
                self.require_core()

        def status(self):
            return {"can_run": self.allowed}

    value = Gate()
    monkeypatch.setattr(guard_module, "_guard", value)
    return value


def load_class(source: Path, name: str, namespace: dict):
    nodes = ast.parse(source.read_text("utf-8")).body
    selected = next(node for node in nodes if isinstance(node, ast.ClassDef) and node.name == name)
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(source), "exec"), namespace)
    return namespace[name]


def test_core_patch_checks_authorization_before_initialize_and_start(patched, gate):
    cls = load_class(patched / "astrbot/core/core_lifecycle.py", "AstrBotCoreLifecycle", {
        "runtime_guard": lambda: gate,
    })
    lifecycle = cls.__new__(cls)
    lifecycle._load = lambda: pytest.fail("Core tasks must not load without authorization")
    with pytest.raises(LicenseError):
        asyncio.run(lifecycle.initialize())
    with pytest.raises(LicenseError):
        asyncio.run(lifecycle.start())


def test_core_provider_subclasses_cannot_skip_guard_via_override(patched, gate):
    import abc
    cls = load_class(patched / "astrbot/core/provider/provider.py", "AbstractProvider", {
        "abc": abc, "guard_provider_class": guard_provider_class,
    })
    calls = []

    class Provider(cls):
        async def text_chat(self):
            calls.append("model request")
            return "reply"

    with pytest.raises(LicenseError):
        asyncio.run(Provider({}).text_chat())
    assert calls == []
    gate.allowed = True
    assert asyncio.run(Provider({}).text_chat()) == "reply"
    assert calls == ["model request"]


def test_pipeline_rechecks_between_stages_and_cleans_up_on_expiry(patched, gate):
    calls = []
    registry = SimpleNamespace(register=lambda event: calls.append("register"), unregister=lambda event: calls.append("unregister"))
    cls = load_class(patched / "astrbot/core/pipeline/scheduler.py", "PipelineScheduler", {
        "runtime_guard": lambda: gate, "active_event_registry": registry,
        "AsyncGenerator": AsyncGenerator,
    })
    gate.allowed = True

    async def expire(event):
        calls.append("stage_one")
        gate.allowed = False

    async def forbidden(event):
        pytest.fail("A stage ran after authorization expired")

    scheduler = cls.__new__(cls)
    scheduler.stages = [SimpleNamespace(process=expire), SimpleNamespace(process=forbidden)]
    event = SimpleNamespace(get_self_id=lambda: "wxid_actual", is_stopped=lambda: False,
                            cleanup_temporary_local_files=lambda: calls.append("cleanup"))
    with pytest.raises(LicenseError):
        asyncio.run(scheduler.execute(event))
    assert calls == ["register", "stage_one", "cleanup", "unregister"]


@pytest.fixture(scope="module")
def bridge(patched):
    package = "_license_patched_bridge"
    source = patched / "hook_41827_astrbot_bridge/src/hook_bridge"
    spec = importlib.util.spec_from_file_location(package, source / "__init__.py", submodule_search_locations=[str(source)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    try:
        yield SimpleNamespace(
            hook=importlib.import_module(package + ".hook_client"),
            app=importlib.import_module(package + ".app"),
            onebot=importlib.import_module(package + ".onebot_client"),
        )
    finally:
        for name in list(sys.modules):
            if name == package or name.startswith(package + "."):
                del sys.modules[name]


def test_hook_patch_checks_writes_at_http_boundary_and_allows_identity_reads(bridge, gate):
    calls = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def post(self, url, **kwargs):
            calls.append(url)
            return Response()

    client = bridge.hook.HookClient("http://127.0.0.1:19088", Session())
    client._response_json = AsyncMock(return_value=({}, {}))
    with pytest.raises(LicenseError):
        asyncio.run(client._post_response("/api/send_text_msg", {"msg": "blocked"}))
    assert calls == []
    asyncio.run(client._post_response("/api/get_profile_cache", {}))
    assert calls == ["http://127.0.0.1:19088/api/get_profile_cache"]


def test_bridge_keeps_login_identity_handling_while_business_input_is_paused(bridge, gate, monkeypatch):
    observed = []
    application = bridge.app.BridgeApplication.__new__(bridge.app.BridgeApplication)
    application.state = SimpleNamespace(set_identity=lambda *args: observed.append(args))
    application.onebot = SimpleNamespace(set_identity=AsyncMock())
    application._observe_outgoing_image_receipt = lambda *args: None
    application._enrich_group_sender = AsyncMock(side_effect=AssertionError("paused input reached business handling"))
    monkeypatch.setattr(bridge.app, "normalize_event", lambda payload: SimpleNamespace(
        kind=payload["kind"], account_id="wxid_actual", sender_name="owner",
    ))

    async def consume():
        application.event_queue = asyncio.Queue()
        application.event_queue.put_nowait({"kind": "login", "data": {}})
        application.event_queue.put_nowait({"kind": "message"})
        task = asyncio.create_task(application._consume_events())
        await asyncio.wait_for(application.event_queue.join(), timeout=1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(consume())
    assert observed == [("wxid_actual", "owner", "")]
    application.onebot.set_identity.assert_awaited_once_with("wxid_actual", "owner")
    application._enrich_group_sender.assert_not_called()


def test_bridge_queue_drops_previous_run_and_never_transmits_private_marker(bridge, runtime):
    connection = SimpleNamespace(send=AsyncMock())
    client = bridge.onebot.OneBotClient("ws://127.0.0.1:6198", SimpleNamespace())
    client._connection = connection
    event = {"post_type": "message", "self_id": "wxid_actual", "time": 101, "message": "first"}

    async def send():
        await client.send_event(event)
        queued = await client._events.get()
        await client._send_json(queued)
        assert connection.send.await_count == 1
        assert "_wechatrebot_license_run_id" not in connection.send.await_args.args[0]
        runtime.client.state.update(run_id="b" * 32, run_granted_at=102)
        await client._send_json(queued)
        assert connection.send.await_count == 1
        await client.send_event(event)
        assert client._events.empty()
        runtime.client.state["can_run"] = False
        await client._send_json({"post_type": "meta_event", "meta_event_type": "heartbeat"})
        assert connection.send.await_count == 2

    asyncio.run(send())


def test_started_action_cannot_borrow_new_permission_after_await(bridge, runtime):
    posts = []

    def forbidden_post(*args, **kwargs):
        posts.append(args)
        pytest.fail("An old action reached HOOK using a later run's authorization")

    hook = bridge.hook.HookClient("http://127.0.0.1:19088", SimpleNamespace(post=forbidden_post))
    router = bridge.onebot.OneBotActionRouter(hook, SimpleNamespace(), SimpleNamespace(bot_wxid="wxid_actual"))
    original = hook.send_app_xml

    async def send():
        started, release = asyncio.Event(), asyncio.Event()

        async def pending_media(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)

        hook.send_app_xml = pending_media
        task = asyncio.create_task(router.handle({
            "action": "send_raw_xml", "params": {"to_wxid": "wxid_friend", "content": "<msg/>"},
        }))
        await asyncio.wait_for(started.wait(), timeout=1)
        runtime.client.state.update(can_run=False, run_id="")
        runtime.client.state.update(can_run=True, run_id="b" * 32)
        release.set()
        with pytest.raises(LicenseError):
            await task

    asyncio.run(send())
    assert posts == []


def test_bridge_cached_identity_is_not_authoritative_when_hook_is_unready(bridge, runtime):
    settings = SimpleNamespace(dedup_ttl_seconds=60, dedup_capacity=10, queue_size=10)
    state = SimpleNamespace(bot_wxid="wxid_actual")
    application = bridge.app.BridgeApplication(settings, launch_injector=False, state=state)
    assert application._hook_ready is False
    assert runtime.guard.account_source() == ""
    application._hook_ready = True
    assert runtime.guard.account_source() == "wxid_actual"
    application._hook_ready = False
    assert not runtime.guard.status()["can_run"]
