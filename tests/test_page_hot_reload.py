"""Exercise delivered page save/cache paths without starting any bot or server."""
from __future__ import annotations

import ast
import asyncio
import copy
import pathlib
import runpy
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

STAGED = None
LOG = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, exception=lambda *a, **k: None)


@pytest.fixture(autouse=True)
def plugin_inputs(patched_plugins, monkeypatch):
    monkeypatch.setattr(__import__(__name__), "STAGED", patched_plugins)


def method(module, file, name, namespace=None):
    vendored = Path(__file__).parents[1] / "runtime" / module / file
    path = vendored if module == "astrbot_plugin_qq_group_daily_analysis" else STAGED / module / file
    if not path.is_file():
        pytest.fail("Source-only staging fixture unavailable")
    tree = ast.parse(path.read_text("utf-8-sig"))
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    scope = {"asyncio": asyncio, "copy": copy, "logger": LOG, "Any": object, **(namespace or {})}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
    return scope[name]


class Config(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.disk = copy.deepcopy(values)
        self.fail = False

    def save_config(self, value=None):
        if self.fail:
            raise OSError("unwritable")
        if value:
            self.update(value)
        self.disk = copy.deepcopy(dict(self))


@pytest.mark.parametrize("hook_failure", [False, True])
def test_memory_page_refreshes_cache_or_rolls_back(hook_failure):
    config = Config(value="old")
    cached = ["old"]
    async def payload():
        return {"values": {"value": "new"}}
    def hook():
        if hook_failure and config["value"] == "new":
            raise RuntimeError("cache refresh failed")
        cached[0] = config["value"]
    save = method("astrbot_plugin_angel_heart", "web_api/__init__.py", "save_config", {
        "_get_json": payload, "_convert_value": lambda v, *_: ([], v),
        "_ok": lambda **kw: {"ok": True, **kw}, "_err": lambda s: {"ok": False, "message": s},
    })
    instance = SimpleNamespace(_config=config, _schema={"value": {}}, _plugin=SimpleNamespace(on_config_saved=hook))
    result = asyncio.run(save(instance))
    expected = "old" if hook_failure else "new"
    assert result["ok"] is not hook_failure
    assert cached[0] == config.disk["value"] == config["value"] == expected


def voice_fixture(**overrides):
    plugin = STAGED / "astrbot_plugin_mimo_tts_clone"
    config_module = runpy.run_path(str(plugin / "core/config.py"))
    reliability_module = runpy.run_path(str(plugin / "core/tts_reliability.py"))
    normalize = config_module["normalize_config"]
    values = normalize({"background_queue_size": 8, "max_concurrency": 1, "job_persistence_enabled": True,
                        "job_recovery_max_age_hours": 24, "job_history_size": 20,
                        "default_context": "old", **overrides})
    update = method("astrbot_plugin_mimo_tts_clone", "main.py", "_update_runtime_config", {
        "normalize_config": normalize, "build_plugin_config": config_module["build_plugin_config"],
        "EmotionRouter": lambda **_: object(),
    })
    build_reliability = method("astrbot_plugin_mimo_tts_clone", "main.py", "_build_reliability_controller", {
        "ReliabilityController": lambda **kw: reliability_module["ReliabilityController"](**kw, clock=lambda: 100.0),
    })
    native = Config(**values)
    instance = SimpleNamespace(config=copy.deepcopy(values), plugin_config=config_module["build_plugin_config"](values),
        _job_manager=None, _native_config=native, _style_director_cache={}, _voice_data_cache={}, logger=LOG,
        _tts_sem=asyncio.Semaphore(values["max_concurrency"]), _synthesis_inflight=0,
        _persist_local_config=lambda: None)
    instance._build_reliability_controller = lambda: build_reliability(instance)
    instance._reliability = instance._build_reliability_controller()
    return update, instance


def voice_synthesizer(instance, tmp_path, operation):
    synthesize = method("astrbot_plugin_mimo_tts_clone", "main.py", "_synthesize_text_to_file", {
        "pathlib": pathlib, "time": time, "VoiceProfile": object,
        "MimoRateLimitError": ConnectionError, "MimoTransientError": TimeoutError,
    })
    instance.data_dir = tmp_path
    client = SimpleNamespace(synthesize_to_file=operation)
    instance._acquire_client = lambda: client
    instance._release_client = lambda _: None
    async def run(text="voice", *, voice_data_url="data:audio/wav;base64,AA=="):
        return await synthesize(instance, text, SimpleNamespace(style_tags=""),
                                voice_data_url=voice_data_url, output_path=tmp_path / f"{text}.wav")
    return run


VOICE_LIMIT_CHANGES = [
    ("max_concurrency", 2),
    ("tts_rate_limit_rpm", 91),
    ("tts_max_retries", 3),
    ("tts_retry_backoff_base_seconds", 2.0),
    ("tts_retry_backoff_max_seconds", 16.0),
    ("circuit_failure_threshold", 6),
    ("circuit_recovery_seconds", 61),
]


def test_voice_config_updates_current_runtime_and_native_store():
    update, instance = voice_fixture()
    result = update(instance, {"default_context": "new"})
    assert result["native"] is True
    assert instance.plugin_config.default_context == instance._native_config.disk["default_context"] == "new"


@pytest.mark.parametrize("changes", [{"background_queue_size": 20}, *({key: value} for key, value in VOICE_LIMIT_CHANGES)])
def test_voice_busy_queue_cannot_silently_defer_config(changes):
    update, instance = voice_fixture()
    instance._job_manager = SimpleNamespace(snapshot=lambda: {"queued_jobs": 1, "running_jobs": 0})
    before = copy.deepcopy(instance.config)
    semaphore, reliability = instance._tts_sem, instance._reliability
    with pytest.raises(ValueError, match="本次未保存"):
        update(instance, changes)
    assert instance.config == instance._native_config.disk == before
    assert instance._tts_sem is semaphore and instance._reliability is reliability


def test_voice_busy_queue_allows_unrelated_save_without_resetting_limits():
    update, instance = voice_fixture()
    instance._job_manager = SimpleNamespace(snapshot=lambda: {"queued_jobs": 1, "running_jobs": 1})
    semaphore, reliability = instance._tts_sem, instance._reliability
    update(instance, {"default_context": "new"})
    assert instance.plugin_config.default_context == "new"
    assert instance._tts_sem is semaphore and instance._reliability is reliability


@pytest.mark.parametrize("changes", [{"default_context": "new"}, {"max_concurrency": 2}, {"tts_rate_limit_rpm": 91}])
def test_voice_persistence_failure_preserves_current_settings(changes):
    update, instance = voice_fixture()
    before = copy.deepcopy(instance.config)
    semaphore, reliability = instance._tts_sem, instance._reliability
    instance._native_config.fail = True
    with pytest.raises(ValueError, match="已保留原设置"):
        update(instance, changes)
    assert instance.config == instance._native_config.disk == before
    assert instance.config["default_context"] == instance.plugin_config.default_context == "old"
    assert instance._tts_sem is semaphore and instance._reliability is reliability
    assert instance._synthesis_inflight == 0


@pytest.mark.parametrize("changes", [{"default_context": "new"}, {"max_concurrency": "1"}, {"tts_rate_limit_rpm": "90"}])
def test_voice_save_keeps_existing_requests_in_one_concurrency_limit(tmp_path, changes):
    async def run():
        update, instance = voice_fixture()
        entered = {text: asyncio.Event() for text in ("first", "second", "third")}
        release = {text: asyncio.Event() for text in entered}
        active = peak = 0
        async def operation(*, text, output_path, **_):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered[text].set()
            try:
                await release[text].wait()
                return output_path
            finally:
                active -= 1
        synthesize = voice_synthesizer(instance, tmp_path, operation)
        first = asyncio.create_task(synthesize("first"))
        await asyncio.wait_for(entered["first"].wait(), 1)
        second = asyncio.create_task(synthesize("second"))
        await asyncio.sleep(0)
        assert instance._synthesis_inflight == 2
        semaphore, reliability = instance._tts_sem, instance._reliability
        update(instance, changes)
        assert instance._tts_sem is semaphore and instance._reliability is reliability
        third = asyncio.create_task(synthesize("third"))
        await asyncio.sleep(0)
        assert instance._synthesis_inflight == 3
        assert not entered["second"].is_set() and not entered["third"].is_set()
        release["first"].set()
        await asyncio.wait_for(first, 1)
        await asyncio.wait_for(entered["second"].wait(), 1)
        assert not entered["third"].is_set()
        release["second"].set()
        await asyncio.wait_for(second, 1)
        await asyncio.wait_for(entered["third"].wait(), 1)
        release["third"].set()
        await asyncio.wait_for(third, 1)
        assert peak == 1 and instance._synthesis_inflight == 0
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["preparing", "waiting", "active"])
@pytest.mark.parametrize("key,value", VOICE_LIMIT_CHANGES)
def test_voice_direct_request_blocks_limit_changes_until_cancelled(tmp_path, stage, key, value):
    async def run():
        update, instance = voice_fixture()
        started, release = asyncio.Event(), asyncio.Event()
        async def operation(*, output_path, **_):
            started.set()
            await release.wait()
            return output_path
        async def prepare(_):
            started.set()
            await release.wait()
            return "data:audio/wav;base64,AA=="
        synthesize = voice_synthesizer(instance, tmp_path, operation)
        instance._voice_data_url = prepare
        if stage == "waiting":
            await instance._tts_sem.acquire()
        task = asyncio.create_task(synthesize(voice_data_url=None if stage == "preparing" else "data:audio/wav;base64,AA=="))
        if stage == "waiting":
            await asyncio.sleep(0)
            assert not started.is_set()
        else:
            await asyncio.wait_for(started.wait(), 1)
        assert instance._synthesis_inflight == 1
        before = copy.deepcopy(instance.config)
        semaphore, reliability = instance._tts_sem, instance._reliability
        with pytest.raises(ValueError, match="本次未保存"):
            update(instance, {key: value})
        assert instance.config == instance._native_config.disk == before
        assert getattr(instance.plugin_config, key) == before[key]
        assert instance._tts_sem is semaphore and instance._reliability is reliability
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if stage == "waiting":
            instance._tts_sem.release()
        assert instance._synthesis_inflight == 0
        update(instance, {key: value})
        assert getattr(instance.plugin_config, key) == instance._native_config.disk[key] == value
        assert (instance._tts_sem is semaphore) is (key != "max_concurrency")
        assert (instance._reliability is reliability) is (key == "max_concurrency")
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["text", "preparation", "client_acquisition", "api"])
def test_voice_request_failure_releases_runtime_limit_guard(tmp_path, failure):
    async def run():
        update, instance = voice_fixture()
        async def operation(*, output_path, **_):
            if failure == "api":
                raise ValueError("synthesis failed")
            return output_path
        async def prepare(_):
            raise ValueError("preparation failed")
        def acquire():
            raise ValueError("client acquisition failed")
        synthesize = voice_synthesizer(instance, tmp_path, operation)
        instance._voice_data_url = prepare
        if failure == "client_acquisition":
            instance._acquire_client = acquire
        text = "x" * (instance.plugin_config.max_text_chars + 1) if failure == "text" else "voice"
        with pytest.raises((RuntimeError, ValueError)):
            await synthesize(text, voice_data_url=None if failure == "preparation" else "data:audio/wav;base64,AA==")
        assert instance._synthesis_inflight == 0
        update(instance, {"max_concurrency": 2})
        assert instance.plugin_config.max_concurrency == 2
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{}, {"default_context": "new"}, {"tts_max_retries": "0"}])
def test_voice_save_preserves_rate_history_and_open_circuit(changes):
    update, instance = voice_fixture(tts_max_retries=0, circuit_failure_threshold=1)
    reliability = instance._reliability
    async def fail():
        raise RuntimeError("upstream unavailable")
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        asyncio.run(reliability.execute(fail, retryable=lambda _: True))
    snapshot = reliability.snapshot()
    timestamps = tuple(reliability._limiter._timestamps)
    assert snapshot["circuit_state"] == "open" and timestamps
    update(instance, changes)
    assert instance._reliability is reliability
    assert reliability.snapshot() == snapshot
    assert tuple(reliability._limiter._timestamps) == timestamps


def test_voice_native_config_wins_over_outdated_local_copy():
    path = STAGED / "astrbot_plugin_mimo_tts_clone/main.py"
    if not path.is_file():
        pytest.fail("Source-only staging fixture unavailable")
    tree = ast.parse(path.read_text("utf-8-sig"))
    assign = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and
                  ast.unparse(n.targets[0]) == "self.config" and "native_config" in ast.unparse(n.value))
    value = eval(compile(ast.Expression(assign.value), "voice", "eval"), {
        "normalize_config": lambda v: v, "native_config": {"model": "new"}, "persisted_config": {"model": "old"}})
    assert value["model"] == "new"


@pytest.mark.parametrize("fail", [False, True])
def test_image_model_cache_commits_after_disk(fail):
    commit = method("jiang_image", "model_manager.py", "_commit")
    def write(_):
        if fail:
            raise OSError("unwritable")
    instance = SimpleNamespace(_config={"active_model": "old"}, _validate_config=lambda c: c, _write_config=write, logger=LOG)
    if fail:
        with pytest.raises(OSError):
            commit(instance, {"active_model": "new", "models": []})
        assert instance._config["active_model"] == "old"
    else:
        commit(instance, {"active_model": "new", "models": []})
        assert instance._config["active_model"] == "new"


def test_schedule_registration_failure_is_not_swallowed():
    async def fail(**kw):
        raise RuntimeError("scheduler unavailable")
    register = method("jiang_schedule", "main.py", "_register_task", {
        "_make_job_name": lambda _: "news_test", "_task_to_cron": lambda _: "0 8 * * *",
        "_CALLBACKS": {"news": lambda: None}, "_type_label": lambda _: "新闻"})
    with pytest.raises(RuntimeError, match="尚未确认生效"):
        asyncio.run(register(SimpleNamespace(cron_manager=SimpleNamespace(add_basic_job=fail)), {"type": "news"}))


def test_drama_old_schedule_delete_failure_blocks_new_schedule():
    async def fail(**kw):
        raise RuntimeError("scheduler unavailable")
    remove = method("jiang_short_drama", "main.py", "_remove_schedule_jobs", {"_SCHEDULE_JOB_PREFIX": "drama_"})
    with pytest.raises(RuntimeError, match="停止应用新计划"):
        asyncio.run(remove(SimpleNamespace(context=SimpleNamespace(cron_manager=SimpleNamespace(list_jobs=fail)))))


def test_meme_rules_refresh_current_personas_before_success():
    cached = []
    async def payload():
        return {"rules": [{"scope": "session", "target": "test"}]}
    save = method("meme_manager", "mixins/web_api.py", "_api_settings_rules", {
        "request": SimpleNamespace(method="POST", get_json=payload),
        "get_selection_rules": lambda: {"rules": []},
        "save_selection_rules": lambda rules: {"rules": rules}, "jsonify": lambda v: v,
    })
    result, status = asyncio.run(save(SimpleNamespace(_reload_personas=lambda: cached.append("refreshed"))))
    assert status == 200 and cached == ["refreshed"]
    assert result["rules"][0]["target"] == "test"


def test_group_analysis_verification_waits_for_initialization():
    check = method("astrbot_plugin_qq_group_daily_analysis", "main.py", "_commercial_verify_hot_config")
    async def run():
        instance = SimpleNamespace(_initialized=False)
        async def initialize():
            await asyncio.sleep(0)
            instance._initialized = True
        instance._init_task = asyncio.create_task(initialize())
        assert await check(instance) == "applied"
        instance._initialized = False
        with pytest.raises(RuntimeError):
            await check(instance)
    asyncio.run(run())
