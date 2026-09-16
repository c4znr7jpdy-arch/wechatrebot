from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("E:/Project/data/plugins/jiang_image")


@pytest.fixture
def patched_image(tmp_path):
    if not (SOURCE / "main.py").is_file():
        pytest.skip("Local image plugin source is unavailable")
    for name in ("main.py", "model_manager.py", "_conf_schema.json"):
        shutil.copy2(SOURCE / name, tmp_path / name)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    for name in ("astrbot-plugin-jiang_image.patch", "astrbot-plugin-jiang_image--network-errors.patch"):
        result = subprocess.run(
            ["git", "apply", "--check", "--ignore-space-change", str(ROOT / "patches" / name)],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        subprocess.run(["git", "apply", "--ignore-space-change", str(ROOT / "patches" / name)],
                       cwd=tmp_path, check=True, capture_output=True)
    schema = json.loads((tmp_path / "_conf_schema.json").read_text("utf-8"))
    assert schema["use_system_proxy"]["type"] == "bool"
    assert schema["use_system_proxy"]["default"] is True
    spec = importlib.util.spec_from_file_location("patched_image_model_manager", tmp_path / "model_manager.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (tmp_path / "main.py").read_text("utf-8"), module


def _method(source, name, namespace):
    node = next(node for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    node.decorator_list = []
    scope = dict(namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "image-plugin-method", "exec"), scope)
    return scope[name]


@pytest.mark.parametrize("proxy", [True, False])
def test_saved_image_config_updates_proxy_and_stale_provider_name(patched_image, proxy):
    source, _module = patched_image
    apply_config = _method(source, "_apply_plugin_config", {})
    changes = []
    profile = {
        "id": "hostcentral-gpt-image-2", "name": "HostCentral GPT Image 2",
        "base_url": "https://api.gemai.cc", "model": "gpt-image-2.5-flare",
        "enabled": True, "fallback_priority": 0, "trust_env": not proxy,
    }
    instance = SimpleNamespace(
        config={"primary_base_url": profile["base_url"], "primary_model": profile["model"],
                "primary_api_key": "stored-secret", "use_system_proxy": proxy},
        model_manager=SimpleNamespace(
            public_config=lambda: {"active_model": profile["id"], "models": [profile]},
            upsert_profile=lambda item: changes.append(item),
        ),
    )
    apply_config(instance)
    assert changes[0]["trust_env"] is proxy
    assert changes[0]["name"] == "首选生图模型"
    assert changes[0]["api_key_source"] == {"type": "stored"}
    assert changes[0]["api_key"] == "stored-secret"


def test_blank_connection_error_names_actual_model_and_repair(patched_image):
    _source, module = patched_image
    manager = module.ImageModelManager.__new__(module.ImageModelManager)
    profile = {
        "name": "HostCentral GPT Image 2", "model": "gpt-image-2.5-flare",
        "base_url": "https://api.gemai.cc", "trust_env": False,
        "api_key_source": {"type": "stored"}, "api_key": "stored-secret",
    }
    message = manager.format_error(profile, httpx.ConnectError(""))
    assert "gpt-image-2.5-flare（api.gemai.cc）" in message
    assert "连接生图服务失败" in message
    assert "启用系统代理" in message
    assert "ConnectError" in message
    assert "HostCentral" not in message
    assert "stored-secret" not in message
    secret_error = manager.format_error(profile, RuntimeError("key: stored-secret"))
    assert "stored-secret" not in secret_error
    assert "[已隐藏密钥]" in secret_error


def test_image_request_uses_proxy_when_enabled(patched_image, monkeypatch):
    _source, module = patched_image
    calls = []
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: calls.append(kwargs) or object())
    manager = module.ImageModelManager.__new__(module.ImageModelManager)
    manager._client({"trust_env": True})
    assert calls[0]["trust_env"] is True


def test_chat_failure_contains_actionable_reason_instead_of_empty_hostcentral_label(patched_image):
    source, module = patched_image
    profile = {
        "id": "hostcentral-gpt-image-2", "name": "HostCentral GPT Image 2",
        "model": "gpt-image-2.5-flare", "base_url": "https://api.gemai.cc",
        "api_key_source": {"type": "stored"}, "api_key": "stored-secret",
        "trust_env": False,
    }
    manager = module.ImageModelManager.__new__(module.ImageModelManager)
    manager.generation_chain = lambda: [profile]
    manager.generate = AsyncMock(side_effect=httpx.ConnectError(""))
    generator = _method(source, "_do_image_gen", {"logger": Mock(), "time": __import__("time")})
    instance = SimpleNamespace(_ensure_models_initialized=lambda: None, model_manager=manager)
    event = SimpleNamespace(plain_result=lambda text: text)

    async def run():
        return [item async for item in generator(instance, event, "小猫图", "1:1")]

    messages = asyncio.run(run())
    assert messages[0].startswith("正在画")
    assert "gpt-image-2.5-flare（api.gemai.cc）" in messages[-1]
    assert "连接生图服务失败" in messages[-1]
    assert "HostCentral" not in messages[-1]
