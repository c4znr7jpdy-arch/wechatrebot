"""Run patched production methods in an isolated, process-free runtime fixture."""
from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from wechatrebot_manager.plugin_center import EngineApiClient, PluginCenterError, _json_revision

ROOT = Path(__file__).parents[1]
SOURCE = Path("E:/Project/astrbot_venv/Lib/site-packages")


@pytest.fixture
def service_class(tmp_path):
    relative = Path("astrbot/dashboard/services/config_service.py")
    if not (SOURCE / relative).is_file():
        pytest.skip("Pinned AstrBot source unavailable")
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / relative, target)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "apply", "-p2", "--include=astrbot/dashboard/services/config_service.py",
                    str(ROOT / "patches/astrbot-4.26.6-wechat.patch")], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse(target.read_text("utf-8-sig"))
    assert '"hot_apply_supported": True' in target.read_text("utf-8-sig")
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ConfigFileService")
    cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and
                n.name in {"__init__", "get_plugin_metadata_by_name", "save_plugin_configs", "_verify_hot_config"}]
    namespace = {"asyncio": asyncio, "copy": copy, "AstrBotCoreLifecycle": object,
                 "star_registry": [], "validate_config": lambda values, *_a, **_k: ([], values)}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "patched-config", "exec"), namespace)
    return namespace["ConfigFileService"], namespace["star_registry"]


def runtime_fixture(service_class, modes=(), active=True):
    cls, registry = service_class
    disk = {"value": "old", "unknown": {"keep": True}}
    class Config(dict):
        schema = {}
        def save_config(self, values=None):
            if values:
                self.update(values)
            disk.clear(); disk.update(copy.deepcopy(self))
    def metadata():
        config = Config(copy.deepcopy(disk))
        return SimpleNamespace(name="registered", module_path="data.plugins.delivered.main", config=config,
                               activated=active, star_cls=SimpleNamespace(cached=config["value"]))
    registry[:] = [metadata()]
    calls = []
    async def reload(name):
        assert name == "registered"
        index = len(calls); calls.append(name)
        mode = modes[index] if index < len(modes) else "ok"
        if mode == "failure":
            return False, "private internal failure"
        if mode == "same_instance":
            return True, None
        registry[:] = [metadata()]
        if mode == "wrong_config":
            registry[0].config["value"] = "stale"
        return True, None
    lifecycle = SimpleNamespace(plugin_manager=SimpleNamespace(reload=reload))
    return cls(lifecycle), registry, disk, calls


def test_hot_save_replaces_init_cached_value_before_success(service_class):
    service, registry, disk, calls = runtime_fixture(service_class)
    old = registry[0].star_cls
    result = asyncio.run(service.save_plugin_configs({"value": "new"}, "registered"))
    assert registry[0].star_cls is not old
    assert registry[0].star_cls.cached == "new"
    assert disk["unknown"] == {"keep": True}
    assert result["hot_apply"] == {"verified": True, "state": "applied", "revision": _json_revision(disk)}
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["failure", "same_instance", "wrong_config"])
def test_false_success_rolls_back_and_never_returns_applied(service_class, mode):
    service, registry, disk, _ = runtime_fixture(service_class, (mode, "ok"))
    with pytest.raises(ValueError, match="已恢复原设置"):
        asyncio.run(service.save_plugin_configs({"value": "new"}, "registered"))
    assert disk["value"] == "old"
    assert registry[0].star_cls.cached == "old"


def test_restore_failure_is_explicit(service_class):
    service, _, disk, _ = runtime_fixture(service_class, ("failure", "failure"))
    with pytest.raises(ValueError, match="功能恢复失败"):
        asyncio.run(service.save_plugin_configs({"value": "new"}, "registered"))
    assert disk["value"] == "old"


def test_restore_write_failure_never_claims_recovered(service_class):
    service, registry, _, _ = runtime_fixture(service_class)
    def fail_write(_values=None):
        raise OSError("private path and device details")
    registry[0].config.save_config = fail_write
    with pytest.raises(ValueError, match="旧设置也未能写回") as error:
        asyncio.run(service.save_plugin_configs({"value": "new"}, "registered"))
    assert "private" not in str(error.value)


def test_disabled_function_never_claims_applied(service_class):
    service, _, _, _ = runtime_fixture(service_class, active=False)
    result = asyncio.run(service.save_plugin_configs({"value": "new"}, "registered"))
    assert result["hot_apply"]["state"] == "pending_enable"


def test_unknown_name_never_reloads_other_functions(service_class):
    service, _, _, calls = runtime_fixture(service_class)
    with pytest.raises(ValueError):
        asyncio.run(service.save_plugin_configs({"value": "new"}, "unknown"))
    assert calls == []


def test_stale_revision_rejected_before_disk_or_instance_changes(service_class):
    service, registry, disk, calls = runtime_fixture(service_class)
    with pytest.raises(ValueError, match="其他页面修改"):
        asyncio.run(service.save_plugin_configs({"value": "new"}, "registered", "stale"))
    assert disk["value"] == registry[0].star_cls.cached == "old"
    assert calls == []


def test_runtime_readiness_hook_cannot_report_success_early(service_class):
    service, registry, _, _ = runtime_fixture(service_class)
    async def waiting():
        await asyncio.sleep(0)
        return "pending_setup"
    registry[0].star_cls._commercial_verify_hot_config = waiting
    assert asyncio.run(service._verify_hot_config(registry[0], True)) == "pending_setup"


def test_unknown_reload_target_never_falls_through_to_reload_all(tmp_path):
    relative = Path("astrbot/core/star/star_manager.py")
    if not (SOURCE / relative).is_file():
        pytest.skip("Pinned AstrBot source unavailable")
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / relative, target)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "apply", "-p2", "--include=astrbot/core/star/star_manager.py",
                    str(ROOT / "patches/astrbot-4.26.6-wechat.patch")], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse(target.read_text("utf-8-sig"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "reload")
    scope = {"star_registry": [], "star_map": {}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "patched-reload", "exec"), scope)
    async def run():
        instance = SimpleNamespace(_pm_lock=asyncio.Lock())
        result = await scope["reload"](instance, "missing-folder-name")
        assert result[0] is False
    asyncio.run(run())


def test_enabling_disabled_plugin_unbinds_stale_handlers_before_reload(tmp_path):
    relative = Path("astrbot/core/star/star_manager.py")
    if not (SOURCE / relative).is_file():
        pytest.skip("Pinned AstrBot source unavailable")
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / relative, target)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "apply", "-p2", "--include=astrbot/core/star/star_manager.py",
                    str(ROOT / "patches/astrbot-4.26.6-wechat.patch")], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse(target.read_text("utf-8-sig"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "reload")
    metadata = SimpleNamespace(
        name="jiang_short_drama",
        module_path="data.plugins.jiang_short_drama.main",
        activated=False,
    )
    scope = {
        "star_registry": [metadata],
        "star_map": {metadata.module_path: metadata},
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "patched-reload", "exec"), scope)
    calls = []

    async def terminate(item):
        calls.append(("terminate", item.name))

    async def unbind(name, module_path):
        calls.append(("unbind", name, module_path))

    async def load(module_path):
        calls.append(("load", module_path))
        return True, None

    instance = SimpleNamespace(
        _pm_lock=asyncio.Lock(),
        _terminate_plugin=terminate,
        _unbind_plugin=unbind,
        load=load,
    )
    assert asyncio.run(scope["reload"](instance, "jiang_short_drama")) == (True, None)
    assert calls == [
        ("terminate", "jiang_short_drama"),
        ("unbind", "jiang_short_drama", metadata.module_path),
        ("load", metadata.module_path),
    ]


def test_old_runtime_is_rejected_before_any_write(tmp_path):
    client = EngineApiClient(tmp_path)
    calls = []
    def request(method, path, payload=None):
        calls.append(method)
        return [{"root_dir_name": "folder", "name": "registered", "activated": True}] if path.endswith("/plugins") else {"config": {}}
    client.request = request
    with pytest.raises(PluginCenterError, match="完整软件更新"):
        client.save_config("folder", {})
    assert set(calls) == {"GET"}


def test_success_without_receipt_is_not_hot_success(tmp_path):
    client = EngineApiClient(tmp_path)
    def request(method, path, payload=None):
        if path.endswith("/plugins"):
            return [{"root_dir_name": "folder", "name": "registered", "activated": True}]
        return {"hot_apply_supported": True} if method == "GET" else {}
    client.request = request
    with pytest.raises(PluginCenterError, match="未收到生效确认"):
        client.save_config("folder", {})


@pytest.mark.parametrize("name", ["update_plugin_config_by_id", "update_plugin_config"])
def test_both_save_routes_keep_revision_and_verification_receipt(tmp_path, name):
    relative = Path("astrbot/dashboard/api/plugins.py")
    if not (SOURCE / relative).is_file():
        pytest.skip("Pinned AstrBot source unavailable")
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(SOURCE / relative, target)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "apply", "-p2", "--include=astrbot/dashboard/api/plugins.py",
                    str(ROOT / "patches/astrbot-4.26.6-wechat.patch")], cwd=tmp_path, check=True, capture_output=True)
    tree = ast.parse(target.read_text("utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    node.decorator_list = []
    node.returns = None
    for arg in [*node.args.args, *node.args.kwonlyargs]:
        arg.annotation = None
    node.args.defaults = [ast.Constant(None) for _ in node.args.defaults]
    node.args.kw_defaults = [ast.Constant(None) for _ in node.args.kw_defaults]
    scope = {"_model_dict": lambda payload: payload,
             "_plugin_id_from_body": lambda body: body["id"],
             "ok": lambda data, **kwargs: {"data": data, **kwargs}}
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, "patched-api", "exec"), scope)
    receipt = {"hot_apply": {"verified": True, "state": "applied", "revision": "new"}}
    async def save(config, plugin, revision):
        assert (config, plugin, revision) == ({"value": "new"}, "registered", "old")
        return receipt
    args = {"payload": {"id": "registered", "config": {"value": "new"}, "base_revision": "old"},
            "service": SimpleNamespace(save_plugin_configs=save)}
    if name == "update_plugin_config":
        args["plugin_id"] = "registered"
    assert asyncio.run(scope[name](**args))["data"] == receipt
