"""Persist reports with real Windows files and SQLite, without starting a robot."""
import asyncio
import base64
from collections.abc import Mapping
from contextlib import nullcontext
import datetime as dt
import os
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_group_analysis_report import modules, PACKAGE, PLUGIN  # noqa: F401
from test_hot_config_runtime import _runtime_function


LEGACY = "manual_日常\x11交流群\x10\x7f_end"
CLEAN = "manual_日常交流群_end"
GROUP = "room@chatroom"


@pytest.fixture
def runtime(modules, monkeypatch, tmp_path):
    logger = ModuleType(PACKAGE + ".utils.logger")
    logger.logger = Mock()
    monkeypatch.setitem(sys.modules, logger.__name__, logger)
    trace = modules("shared.trace_context")
    store = modules("infrastructure.persistence.trace_sqlite_store").TraceSQLiteStore(tmp_path / "trace.db")
    monkeypatch.setattr(trace, "_global_trace_store", store)
    monkeypatch.setattr(trace, "_global_active_task_manager", None)
    monkeypatch.setattr(trace.TraceContext, "_enable_metrics", False)

    def function(relative, name):
        package = PACKAGE + "." + str(Path(relative).parent).replace("\\", ".").replace("/", ".")
        scope = dict(trace.__dict__)
        scope.update(
            __package__=package, __spec__=None, Path=Path, datetime=dt.datetime, dt=dt, os=os,
            time=time, time_mod=time, asyncio=asyncio, base64=base64,
            nullcontext=nullcontext, Mapping=Mapping, logger=Mock(),
        )
        return _runtime_function((PLUGIN / "src" / relative).read_text("utf-8"), name, scope)

    source = tmp_path / "source.jpg"
    source.write_bytes(b"test-image")
    return NS(trace=trace, store=store, function=function, source=source, directory=tmp_path)


@pytest.mark.parametrize("codepoint", [*range(32), 127])
def test_group_name_control_characters_never_enter_generated_trace(runtime, codepoint):
    trace_id = runtime.trace.TraceContext.generate("manual", f"群{chr(codepoint)}聊😀")
    assert not any(ord(character) < 32 or ord(character) == 127 for character in trace_id)
    assert "群聊😀" in trace_id


@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_report_save_preserves_database_identity(runtime, explicit):
    save = runtime.function("application/handlers/analysis_command_handler.py", "_save_report_to_history")
    owner = NS(plugin_data_dir=runtime.directory)
    with runtime.trace.TraceContext(trace_id=LEGACY, group_id=GROUP) as trace:
        path = save(owner, str(runtime.source), GROUP, trace_id=LEGACY if explicit else None)
        assert path is not None and path.read_bytes() == b"test-image"
        assert CLEAN in path.name
        assert trace.trace_id == LEGACY
        assert trace.metadata["report_files"][0]["filename"] == path.name
    stored = runtime.store.get_trace(LEGACY)
    assert stored["trace_id"] == LEGACY
    assert stored["report_files"][0]["filename"] == path.name
    assert runtime.store.get_trace(CLEAN) is None


@pytest.mark.parametrize("trace_id", [LEGACY, "\x10", "\x00\x11\x7f"])
def test_legacy_trace_scans_only_its_cleaned_report_filename(runtime, trace_id):
    runtime.store.save_trace({"trace_id": trace_id, "group_id": GROUP, "status": "success"})
    reports = runtime.directory / "reports"
    reports.mkdir()
    expected = reports / f"report_{GROUP}_{CLEAN}.jpg"
    expected.write_bytes(b"report")
    (reports / "unrelated.jpg").write_bytes(b"other")
    stored = runtime.store.get_trace(trace_id)
    assert stored["trace_id"] == trace_id
    assert [report["filename"] for report in stored["report_files"]] == (
        [expected.name] if trace_id == LEGACY else []
    )


@pytest.mark.parametrize("format", ["image", "html", "html-fallback"])
def test_legacy_rerender_saves_file_and_keeps_original_checkpoint_key(runtime, modules, format):
    rerender = runtime.function("application/services/analysis_recovery_service.py", "rerender_report")
    calls = []

    async def render_html(**kwargs):
        calls.append(kwargs)
        if format == "html-fallback":
            return None, None
        path = runtime.directory / "reports" / kwargs["custom_filename"]
        path.write_text("<html>report</html>", "utf-8")
        return str(path), None

    values = modules("domain.value_objects")
    statistics = values.GroupStatistics(1, 1, 1, "12:00", [], 0)
    owner = NS(
        history_manager=NS(get_analysis=AsyncMock(return_value={"statistics": statistics})),
        checkpoint_store=None, html_render=None,
        report_generator=NS(
            generate_html_report=render_html,
            generate_image_report=AsyncMock(return_value=(str(runtime.source), None)),
            html_templates=NS(render_template=lambda *_args, **_kwargs: "<html>fallback</html>"),
        ),
    )
    rerender.__globals__.update(
        StarTools=NS(get_data_dir=lambda _plugin: runtime.directory),
        PLUGIN_NAME="analysis", GroupStatistics=values.GroupStatistics,
    )
    runtime.store.save_trace({"trace_id": LEGACY, "group_id": GROUP, "status": "success"})
    result = asyncio.run(rerender(owner, GROUP, "2026-11-08", "scrapbook",
                                  render_format="image" if format == "image" else "html", trace_id=LEGACY))
    assert result["success"]
    assert Path(result["report_path"]).is_file()
    assert CLEAN in result["filename"]
    assert result["trace_id"] == LEGACY
    stored = runtime.store.get_trace(LEGACY)
    assert stored["trace_id"] == LEGACY
    assert stored["report_files"][0]["filename"] == result["filename"]
    if calls:
        assert calls[0]["trace_id"] == LEGACY


def test_legacy_comic_writes_file_before_fake_transport(runtime):
    trigger = runtime.function("application/handlers/comic_command_handler.py", "_trigger_comic_generation")
    adapter = NS(send_image=AsyncMock(return_value=False))
    owner = NS(
        _comic_semaphore=asyncio.Semaphore(1), terminating=False, active_task_manager=None,
        comic_service=NS(generate_comic=AsyncMock(return_value=(b"comic", None))),
        detect_image_ext=lambda _data: ".png", plugin_data_dir=runtime.directory,
        bot_manager=NS(get_adapter=lambda _platform: adapter), config_manager=None,
    )
    with runtime.trace.TraceContext(trace_id=LEGACY, group_id=GROUP) as trace:
        asyncio.run(trigger(owner, [], GROUP, "wechat", "test-umo", trace=trace))
        adapter.send_image.assert_awaited_once()
        path = Path(adapter.send_image.call_args.args[1])
        assert path.read_bytes() == b"comic"
        assert CLEAN in path.name
        assert trace.trace_id == LEGACY


def test_legacy_dispatcher_keeps_history_file_with_fake_transport(runtime):
    dispatch = runtime.function("infrastructure/reporting/dispatcher.py", "_dispatch_image")
    owner = NS(
        report_generator=NS(data_dir=runtime.directory,
            generate_image_report=AsyncMock(return_value=(str(runtime.source), None))),
        _html_render_func=AsyncMock(), _is_qq_official=lambda _platform: False,
        config_manager=NS(get_report_template=lambda: "scrapbook", get_show_report_caption=lambda: False),
        message_sender=NS(send_image_smart=AsyncMock(return_value=True)), _try_upload_image=AsyncMock(),
    )
    with runtime.trace.TraceContext(trace_id=LEGACY, group_id=GROUP) as trace:
        assert asyncio.run(dispatch(owner, GROUP, {}, "wechat"))
        files = list((runtime.directory / "reports").iterdir())
        assert len(files) == 1 and files[0].read_bytes() == b"test-image"
        assert CLEAN in files[0].name
        assert trace.metadata["report_files"][0]["filename"] == files[0].name


def test_html_template_trace_component_matches_cleaned_history_identity(runtime, modules, monkeypatch):
    # CI tests manager code without optional plugin wheels. The identifier's
    # randomness is outside this real filename/path regression.
    ulid = ModuleType("ulid")
    ulid.new = lambda: "01TESTREPORTIDENTIFIER"
    monkeypatch.setitem(sys.modules, "ulid", ulid)
    builder = modules("infrastructure.reporting.render_diagnostics").build_safe_report_path
    path = builder(runtime.directory, "report_${trace_id}.html", GROUP, "20261108", LEGACY)
    assert path.name == f"report_{CLEAN}.html"
    path.write_text("report", "utf-8")
    assert path.is_file()


def test_legacy_posix_filename_still_matches_original_trace(runtime, monkeypatch):
    runtime.store.save_trace({"trace_id": LEGACY, "group_id": GROUP, "status": "success"})
    reports = runtime.directory / "reports"
    reports.mkdir()
    filename = f"report_{LEGACY}.jpg"
    legacy_file = NS(
        name=filename, suffix=".jpg", is_file=lambda: True,
        stat=lambda: NS(st_mtime=1, st_size=6), resolve=lambda: reports / filename,
    )
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: iter([legacy_file]) if path == reports else original(path))
    assert [report["filename"] for report in runtime.store.get_trace(LEGACY)["report_files"]] == [filename]


@pytest.mark.parametrize("name, expected", [("日常\x11交流群\x10\x7f", "日常交流群"), ("\x00\x11", GROUP)])
def test_group_file_upload_name_is_cleaned_before_fake_transport(runtime, modules, name, expected):
    upload = runtime.function("application/handlers/analysis_command_handler.py", "_try_upload_image")
    protocols = modules("domain.repositories.platform_adapter_repository")
    upload.__globals__.update(
        GroupFileSupportProtocol=protocols.GroupFileSupportProtocol,
        GroupAlbumSupportProtocol=protocols.GroupAlbumSupportProtocol,
    )
    adapter = NS(
        get_group_info=AsyncMock(return_value=NS(group_name=name)),
        upload_group_file_to_folder=AsyncMock(), find_or_create_folder=AsyncMock(),
        create_group_file_folder=AsyncMock(), get_group_file_root_folders=AsyncMock(),
    )
    owner = NS(
        config_manager=NS(get_enable_group_file_upload=lambda: True,
            get_enable_group_album_upload=lambda: False, get_group_file_folder=lambda: ""),
        bot_manager=NS(get_adapter=lambda _platform: adapter),
    )
    asyncio.run(upload(owner, GROUP, str(runtime.source), "wechat"))
    adapter.upload_group_file_to_folder.assert_awaited_once()
    filename = adapter.upload_group_file_to_folder.call_args.kwargs["filename"]
    assert expected in filename
    assert not any(ord(character) < 32 or ord(character) == 127 for character in filename)
