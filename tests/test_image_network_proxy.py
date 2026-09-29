from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest


def test_image_models_allow_empty_then_show_new_primary_after_disk_reload(
    patched_image, tmp_path,
):
    _source, module = patched_image
    manager = module.ImageModelManager(tmp_path / "plugin", tmp_path / "data", Mock())
    manager.initialize()
    initial = manager.public_config()
    assert len(initial["models"]) == 1

    profile = initial["models"][0]
    manager.delete_profile(profile["id"])
    assert manager.reload()["models"] == []
    assert manager.public_config()["active_model"] == ""

    profile.update({"id": "new-primary", "name": "新主模型", "fallback_priority": 0})
    manager.upsert_profile(profile)
    reread = manager.reload()
    assert reread["active_model"] == "new-primary"
    assert [item["id"] for item in reread["models"]] == ["new-primary"]


def test_image_models_limit_runtime_to_primary_and_one_fallback(patched_image, tmp_path):
    _source, module = patched_image
    manager = module.ImageModelManager(tmp_path / "plugin", tmp_path / "data", Mock())
    manager.initialize()
    primary = manager.public_config()["models"][0]
    fallback = {
        **primary,
        "id": "fallback",
        "name": "备用模型",
        "fallback_priority": 1,
    }
    manager.upsert_profile(fallback)
    assert [item["id"] for item in manager.generation_chain()] == [primary["id"], "fallback"]
    with pytest.raises(module.ImageModelConfigError, match="一个主模型和一个备用模型"):
        manager.upsert_profile({**fallback, "id": "third", "name": "第三个模型"})


def test_image_model_page_uses_progressive_fallback_and_refreshes_saved_list(patched_image):
    script = (
        Path(patched_image[1].__file__).parent / "pages" / "model-manager" / "app.js"
    ).read_text("utf-8")
    markup = (
        Path(patched_image[1].__file__).parent / "pages" / "model-manager" / "index.html"
    ).read_text("utf-8")

    assert 'addButton.textContent = config.models.length ? "添加备用模型" : "添加主模型";' in script
    assert 'await apiPost("models/save", { model: collectForm() });\n    await loadConfig();' in script
    assert 'await apiPost("models/delete", { id });\n      await loadConfig();' in script
    assert "回退优先级" not in markup
    assert 'name="fallback_priority" type="hidden"' in markup


def _method(source, name, namespace):
    node = next(node for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    node.decorator_list = []
    scope = dict(namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "image-plugin-method", "exec"), scope)
    return scope[name]


def test_image_command_stops_propagation_before_generation(patched_image, monkeypatch):
    source, _module = patched_image
    import sys
    from types import ModuleType

    astrbot = ModuleType("astrbot")
    astrbot_api = ModuleType("astrbot.api")
    message_components = ModuleType("astrbot.api.message_components")
    message_components.At = type("At", (), {})
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", astrbot_api)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", message_components)

    async def quoted_image(_event):
        return None

    handle_image = _method(
        source,
        "handle_image",
        {
            "AstrMessageEvent": object,
            "_get_plain_text": lambda _event: "生成一张小猫图",
            "_get_quoted_image_path": quoted_image,
            "detect_image_edit_intent": lambda _text: None,
            "detect_image_intent": lambda _text: "小猫图",
            "_parse_aspect": lambda prompt: (prompt, "1:1"),
        },
    )
    timeline = []

    class Event:
        message_obj = None

        def get_sender_id(self):
            return "user"

        def get_self_id(self):
            return "bot"

        def get_group_id(self):
            return ""

        def stop_event(self):
            timeline.append("stopped")

        async def send(self, result):
            timeline.append(("sent", result))

    async def generate(_event, prompt, aspect):
        timeline.append(("generate", prompt, aspect))
        yield "image"

    instance = SimpleNamespace(_do_image_gen=generate)

    asyncio.run(handle_image(instance, Event()))
    assert timeline == ["stopped", ("generate", "小猫图", "1:1"), ("sent", "image")]


@pytest.mark.parametrize("proxy, port", [(True, "7890"), (True, ""), (False, "7890")])
def test_saved_image_config_updates_proxy_and_stale_provider_name(patched_image, proxy, port):
    source, _module = patched_image
    apply_config = _method(source, "_apply_plugin_config", {"re": __import__("re"), "ImageModelConfigError": ValueError})
    changes = []
    profile = {
        "id": "hostcentral-gpt-image-2", "name": "HostCentral GPT Image 2",
        "base_url": "https://api.gemai.cc", "model": "gpt-image-2.5-flare",
        "enabled": True, "fallback_priority": 0, "trust_env": not proxy,
    }
    instance = SimpleNamespace(
        config={"primary_base_url": profile["base_url"], "primary_model": profile["model"],
                "primary_api_key": "stored-secret", "use_system_proxy": proxy,
                "local_proxy_port": port},
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
    assert instance.model_manager.local_proxy_port == (port if proxy else "")


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
    assert "启用本地代理" in message
    assert "ConnectError" in message
    assert "HostCentral" not in message
    assert "stored-secret" not in message
    secret_error = manager.format_error(profile, RuntimeError("key: stored-secret"))
    assert "stored-secret" not in secret_error
    assert "[已隐藏密钥]" in secret_error


@pytest.mark.parametrize("enabled, port, expected_proxy, expected_trust_env", [
    (False, "", None, False),
    (True, "", None, True),
    (True, "7890", "http://127.0.0.1:7890", False),
    (False, "7890", None, False),
])
def test_image_request_uses_selected_proxy_route(
    patched_image, monkeypatch, enabled, port, expected_proxy, expected_trust_env,
):
    _source, module = patched_image
    calls = []
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: calls.append(kwargs) or object())
    manager = module.ImageModelManager.__new__(module.ImageModelManager)
    manager.local_proxy_port = port
    manager._client({"trust_env": enabled})
    assert calls[0]["trust_env"] is expected_trust_env
    assert calls[0]["proxy"] == expected_proxy


@pytest.mark.parametrize("port", ["0", "65536", "-1", "7890abc"])
def test_invalid_local_proxy_port_is_rejected_before_plugin_load(patched_image, port):
    source, _module = patched_image
    apply_config = _method(source, "_apply_plugin_config", {"re": __import__("re"), "ImageModelConfigError": ValueError})
    instance = SimpleNamespace(config={"use_system_proxy": True, "local_proxy_port": port}, model_manager=Mock())
    with pytest.raises(ValueError, match="本地代理端口"):
        apply_config(instance)


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
