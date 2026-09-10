"""Exercise delivered page save/cache paths without starting any bot or server."""
from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

STAGED = Path(__file__).parents[1] / "artifacts/staging-hot-config-validation-20260907-final/app/astrbot/data/plugins"
LOG = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, exception=lambda *a, **k: None)


def method(module, file, name, namespace=None):
    path = STAGED / module / file
    if not path.is_file():
        pytest.skip("Source-only staging fixture unavailable")
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


def voice_fixture():
    values = dict(background_queue_size=8, max_concurrency=1, job_persistence_enabled=True,
                  job_recovery_max_age_hours=24, job_history_size=20, default_context="old")
    def normalize(value):
        return {**values, **value}
    update = method("astrbot_plugin_mimo_tts_clone", "main.py", "_update_runtime_config", {
        "normalize_config": normalize, "build_plugin_config": lambda v: SimpleNamespace(**v),
        "EmotionRouter": lambda **_: object(),
    })
    values["emotion_contexts"] = {}
    native = Config(**values)
    instance = SimpleNamespace(config=copy.deepcopy(values), plugin_config=SimpleNamespace(**values),
        _job_manager=None, _native_config=native, _style_director_cache={}, _voice_data_cache={}, logger=LOG,
        _build_reliability_controller=lambda: object(), _persist_local_config=lambda: None)
    return update, instance


def test_voice_config_updates_current_runtime_and_native_store():
    update, instance = voice_fixture()
    result = update(instance, {"default_context": "new"})
    assert result["native"] is True
    assert instance.plugin_config.default_context == instance._native_config.disk["default_context"] == "new"


def test_voice_busy_queue_cannot_silently_defer_config():
    update, instance = voice_fixture()
    instance._job_manager = SimpleNamespace(snapshot=lambda: {"queued_jobs": 1, "running_jobs": 0})
    with pytest.raises(ValueError, match="本次未保存"):
        update(instance, {"background_queue_size": 20})
    assert instance.config["background_queue_size"] == instance._native_config.disk["background_queue_size"] == 8


def test_voice_persistence_failure_preserves_current_settings():
    update, instance = voice_fixture()
    instance._native_config.fail = True
    with pytest.raises(ValueError, match="已保留原设置"):
        update(instance, {"default_context": "new"})
    assert instance.config["default_context"] == instance.plugin_config.default_context == "old"


def test_voice_native_config_wins_over_outdated_local_copy():
    path = STAGED / "astrbot_plugin_mimo_tts_clone/main.py"
    if not path.is_file():
        pytest.skip("Source-only staging fixture unavailable")
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


def test_memory_missing_service_returns_pending_setup_not_applied():
    check = method("astrbot_plugin_mnemosyne", "main.py", "_commercial_verify_hot_config")
    async def run():
        instance = SimpleNamespace(_commercial_init_task=asyncio.create_task(asyncio.sleep(0)),
                                   _commercial_setup_pending=lambda: True)
        assert await check(instance) == "pending_setup"
    asyncio.run(run())
