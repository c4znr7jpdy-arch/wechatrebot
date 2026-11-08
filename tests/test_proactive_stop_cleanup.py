"""Exercise the shipped lifecycle without starting AstrBot or sending messages."""
import ast
import asyncio
import copy
from pathlib import Path
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import zoneinfo

import pytest

from test_plugin_portability import _patched


ROOT = Path(__file__).parents[1]
PLUGIN = "astrbot_plugin_proactive_chat"


@pytest.fixture(scope="module")
def lifecycle_class(tmp_path_factory):
    directory = tmp_path_factory.mktemp("proactive-stop")
    source = ROOT / "vendor/data/plugins" / PLUGIN
    for relative in ("main.py", "core/plugin_lifecycle.py"):
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / relative).read_bytes())
    main = _patched(directory, PLUGIN, "main.py", all_patches=True)
    lifecycle = (directory / "core/plugin_lifecycle.py").read_text("utf-8")
    selected = []
    for text, wanted in (
        (main, {"__init__", "_track_task", "_cleanup_telemetry_tasks"}),
        (lifecycle, {"initialize", "terminate"}),
        ((source / "core/task_scheduler.py").read_text("utf-8"),
         {"_handle_group_silence_callback"}),
    ):
        tree = ast.parse(text)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        selected.extend(copy.deepcopy(node) for node in cls.body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name in wanted)

    class Base:
        def __init__(self, context):
            self.context = context

    def service(*args, **kwargs):
        return NS(start=AsyncMock(), stop=AsyncMock())

    def telemetry(*, config, **kwargs):
        return NS(enabled=config["telemetry_config"]["enabled"],
                  track_shutdown=AsyncMock(), track_error=AsyncMock(), close=AsyncMock())

    def scheduler(**kwargs):
        instance = Mock(running=False)
        instance.get_jobs.return_value = [NS(id="pending-chat")]
        instance.start.side_effect = lambda: setattr(instance, "running", True)
        instance.shutdown.side_effect = lambda: setattr(instance, "running", False)
        return instance

    namespace = {"Base": Base, "asyncio": asyncio, "time": time,
                 "zoneinfo": zoneinfo, "logger": Mock(),
                 "star": NS(StarTools=NS(get_data_dir=lambda name: directory)),
                 "SessionOverrideManager": service, "NotificationCenter": service,
                 "WebAdminServer": service, "TelemetryManager": telemetry,
                 "get_plugin_version": lambda: "test", "AsyncIOScheduler": scheduler}
    node = ast.ClassDef(name="LifecycleHarness", bases=[ast.Name(id="Base", ctx=ast.Load())],
                        keywords=[], body=selected, decorator_list=[])
    module = ast.Module(body=[*ast.parse("from __future__ import annotations").body, node],
                        type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "proactive-lifecycle", "exec"), namespace)
    return namespace["LifecycleHarness"]


def _owner(cls, *, enabled):
    owner = cls(NS(get_config=lambda: {"timezone": "UTC"}),
                {"telemetry_config": {"enabled": enabled}})
    for name in ("_validate_config", "_load_data_internal", "_save_data_internal",
                 "_init_jobs_from_data", "_setup_auto_triggers_for_enabled_sessions",
                 "_deferred_startup_telemetry", "_heartbeat_loop"):
        setattr(owner, name, AsyncMock())
    owner._normalize_session_data = lambda: False
    owner._get_session_log_str = lambda *args: "test-session"
    owner._handle_asyncio_exception = Mock()
    return owner


@pytest.mark.parametrize("mode", ["disabled", "absent", "enabled", "disabled-after-init"])
def test_stop_cleans_timers_scheduler_state_and_services(lifecycle_class, mode):
    async def run():
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()
        owner = _owner(lifecycle_class, enabled=mode in {"enabled", "disabled-after-init"})
        if mode == "absent":
            owner.telemetry = None
        try:
            await owner.initialize()
            if mode == "disabled-after-init":
                owner.telemetry.enabled = False
            group_timer = loop.call_later(3600, lambda: None)
            auto_timer = loop.call_later(3600, lambda: None)
            owner.group_timers["test-group"] = group_timer
            owner.auto_trigger_timers["test-private"] = auto_timer

            await owner.terminate()

            assert group_timer.cancelled()
            assert auto_timer.cancelled()
            assert owner.group_timers == owner.auto_trigger_timers == {}
            owner.scheduler.remove_job.assert_called_once_with("pending-chat")
            owner.scheduler.shutdown.assert_called_once_with()
            owner._save_data_internal.assert_awaited_once_with()
            owner.web_admin_server.stop.assert_awaited_once_with()
            owner.notification_center.stop.assert_awaited_once_with()
            assert loop.get_exception_handler() is original_handler
            if owner.telemetry:
                owner.telemetry.close.assert_awaited_once_with()
        finally:
            loop.set_exception_handler(original_handler)
            for timer in (*owner.group_timers.values(), *owner.auto_trigger_timers.values()):
                timer.cancel()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["disabled", "absent", "enabled", "disabled-after-init"])
def test_stop_cancels_tracked_business_callbacks_regardless_of_telemetry(lifecycle_class, mode):
    async def run():
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()
        owner = _owner(lifecycle_class, enabled=mode in {"enabled", "disabled-after-init"})
        if mode == "absent":
            owner.telemetry = None
        started, blocked = asyncio.Event(), asyncio.Event()
        cancellation_state = []

        async def callback(*args, **kwargs):
            started.set()
            try:
                await blocked.wait()
            finally:
                cancellation_state.append((not owner.group_timers,
                                           not owner.auto_trigger_timers,
                                           owner.scheduler.running))
            raise AssertionError("Business callback resumed after plugin stop")

        try:
            await owner.initialize()
            # Isolate the callback-cleanup bug from the absent-constructor-field bug.
            if not hasattr(owner, "_exception_handler_installed"):
                owner._exception_handler_installed = False
            if mode == "disabled-after-init":
                owner.telemetry.enabled = False
            owner._get_session_config = lambda session: {"enable": True}
            owner._schedule_next_chat_and_save = callback
            owner.group_timers["test-group"] = Mock()
            owner.auto_trigger_timers["test-private"] = Mock()
            await owner._handle_group_silence_callback("test-group", 10)
            await started.wait()
            task = next(task for task in owner._telemetry_tasks if not task.done())
            await owner.terminate()
            assert task.cancelled()
            assert not owner._telemetry_tasks
            assert cancellation_state == [(True, True, True)]
        finally:
            loop.set_exception_handler(original_handler)
            for task in list(owner._telemetry_tasks):
                task.cancel()
            await asyncio.gather(*list(owner._telemetry_tasks), return_exceptions=True)

    asyncio.run(run())
