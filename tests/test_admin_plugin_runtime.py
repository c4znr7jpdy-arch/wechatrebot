from __future__ import annotations

import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import pytest

from test_hot_config_runtime import _runtime_function


ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / "runtime/jiang_capability_router"
SOURCE = (RUNTIME / "main.py").read_text("utf-8")
SPEC = importlib.util.spec_from_file_location("admin_plugin_runtime_control", RUNTIME / "plugin_control.py")
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


class Event:
    unified_msg_origin = "aiocqhttp:FriendMessage:wxid_admin"

    def __init__(self, sender="wxid_admin", platform="aiocqhttp", message="关闭天气插件"):
        self.sender, self.platform = sender, platform
        self.message_obj = NS(message_str=message, message=[])
        self.extras = {}

    def get_sender_id(self):
        return self.sender

    def get_platform_name(self):
        return self.platform

    def is_admin(self):
        return True

    def get_extra(self, name, default=None):
        return self.extras.get(name, default)

    def set_extra(self, name, value):
        self.extras[name] = value


def context(admins=None):
    return NS(get_config=lambda: {"admins_id": ["wxid_admin"] if admins is None else admins},
              get_all_stars=lambda: [])


def runtime_function(name, **extra):
    namespace = {
        "copy": copy,
        "is_configured_admin": control.is_configured_admin,
        "valid_module_name": control.valid_module_name,
        "PluginControlError": control.PluginControlError,
        "_original_message_text": lambda event: event.message_obj.message_str.strip(),
        "_GLOBAL_PLUGIN_TOOL_NAMES": {"global_plugin_list", "global_plugin_set_enabled"},
        "get_recent_actions": lambda _session: [],
        "_session": lambda event: event.unified_msg_origin,
    }
    namespace.update(extra)
    return _runtime_function(SOURCE, name, namespace)


def descriptor(tmp_path, monkeypatch, **changes):
    path = tmp_path / "data/manager/plugin-control.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"url": "http://127.0.0.1:12345/internal/plugin-control", "token": "test-bridge-secret-token"}
    value.update(changes)
    path.write_text(json.dumps(value), "utf-8")
    monkeypatch.setattr(control, "_install_root", lambda: tmp_path)
    return path


def test_bridge_resolves_source_and_installed_roots(monkeypatch, tmp_path):
    assert control._install_root() == ROOT
    installed = tmp_path / "app/astrbot/data/plugins/jiang_capability_router/plugin_control.py"
    monkeypatch.setattr(control, "__file__", str(installed))
    assert control._install_root() == tmp_path


@pytest.mark.parametrize("event,admins,allowed", [
    (Event(), ["wxid_admin"], True),
    (Event(sender="wxid_other"), ["wxid_admin"], False),
    (Event(platform="qq"), ["wxid_admin"], False),
    (Event(sender=""), [""], False),
    (Event(), [], False),
    (Event(), "wxid_admin", False),
])
def test_admin_gate_uses_configured_wechat_identity(event, admins, allowed):
    assert control.is_configured_admin(context(admins), event) is allowed


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:12345/internal/plugin-control",
    "http://localhost:12345/internal/plugin-control",
    "http://example.invalid:12345/internal/plugin-control",
    "http://127.0.0.1:12345/internal/plugin-control/",
    "http://user:password@127.0.0.1:12345/internal/plugin-control",
    "http://127.0.0.1:12345/internal/plugin-control?x=1",
    "http://127.0.0.1:12345/internal/plugin-control#fragment",
    "http://127.0.0.1:invalid/internal/plugin-control",
    "http://127.0.0.1:65536/internal/plugin-control",
    "http://[invalid:12345/internal/plugin-control",
])
def test_bridge_rejects_noncanonical_or_invalid_endpoint(tmp_path, monkeypatch, url):
    descriptor(tmp_path, monkeypatch, url=url)
    with pytest.raises(control.PluginControlError, match="配置无效"):
        control._read_descriptor()


def test_bridge_missing_or_corrupt_descriptor_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "_install_root", lambda: tmp_path)
    with pytest.raises(control.PluginControlError, match="未就绪"):
        control._read_descriptor()
    path = descriptor(tmp_path, monkeypatch)
    path.write_text("private broken token", "utf-8")
    with pytest.raises(control.PluginControlError) as error:
        control._read_descriptor()
    assert "private" not in str(error.value)
    path.write_bytes(b" " * (control._MAX_DESCRIPTOR_BYTES + 1))
    with pytest.raises(control.PluginControlError, match="配置过大"):
        control._read_descriptor()


@pytest.fixture
def manager_server(tmp_path, monkeypatch):
    state = {"status": 200, "body": {"ok": True, "data": {"plugins": []}}, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append({
                "path": self.path, "auth": self.headers.get("Authorization"),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            raw = state["body"]
            if isinstance(raw, dict):
                raw = json.dumps(raw, ensure_ascii=False).encode("utf-8")
            self.send_response(state["status"])
            if "location" in state:
                self.send_header("Location", state["location"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    descriptor(tmp_path, monkeypatch, url=f"http://127.0.0.1:{server.server_port}/internal/plugin-control")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_bridge_posts_authenticated_json_without_environment_proxy(manager_server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    payload = {"action": "list", "sender_id": "wxid_admin", "message": "查看全局插件"}
    assert asyncio.run(control.request_control(payload)) == {"plugins": []}
    assert manager_server["requests"] == [{"path": "/internal/plugin-control",
        "auth": "Bearer test-bridge-secret-token", "body": payload}]


def test_bridge_http_work_runs_outside_event_loop(monkeypatch):
    current_thread = threading.get_ident()
    monkeypatch.setattr(control, "_request", lambda _payload: {"thread": threading.get_ident()})
    assert asyncio.run(control.request_control({}))["thread"] != current_thread


def test_bridge_preserves_safe_manager_rejection(manager_server):
    manager_server.update(status=403, body={"ok": False, "message": "只有管理员可操作"})
    with pytest.raises(control.PluginControlError, match="只有管理员可操作"):
        control._request({"action": "list"})


def test_bridge_redacts_secret_from_error_response(manager_server):
    manager_server["body"] = {"ok": False, "message": "test-bridge-secret-token"}
    with pytest.raises(control.PluginControlError) as error:
        control._request({"action": "list"})
    assert "test-bridge-secret-token" not in str(error.value)


def test_bridge_does_not_follow_redirects(manager_server):
    manager_server.update(status=307, location="http://127.0.0.1:1/secret", body=b"redirect")
    with pytest.raises(control.PluginControlError):
        control._request({"action": "list"})
    assert len(manager_server["requests"]) == 1


@pytest.mark.parametrize("body,error", [
    (b"not JSON", "无法识别"),
    (b"[]", "格式无效"),
    ({"ok": True}, "格式无效"),
    (b"x" * (control._MAX_RESPONSE_BYTES + 1), "内容过大"),
], ids=["invalid-json", "non-object", "missing-data", "oversized"])
def test_bridge_rejects_invalid_or_oversized_response(manager_server, body, error):
    manager_server["body"] = body
    with pytest.raises(control.PluginControlError, match=error):
        control._request({"action": "list"})


def test_bridge_network_failure_does_not_claim_success_or_retry(monkeypatch, tmp_path):
    descriptor(tmp_path, monkeypatch)
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise TimeoutError("test-bridge-secret-token")

    monkeypatch.setattr(control, "build_opener", lambda *_handlers: NS(open=fail))
    with pytest.raises(control.PluginControlError) as error:
        control._request({"action": "set_enabled"})
    assert len(calls) == 1
    assert "未能确认" in str(error.value) and "重新查询" in str(error.value)
    assert "test-bridge-secret-token" not in str(error.value)


def test_admin_toggle_uses_actual_sender_original_message_and_receipt():
    calls = []

    async def request(payload):
        calls.append(payload)
        return {"module": "jiang_weather", "enabled": False, "applied": True,
                "message": "天气插件已全局关闭"}

    toggle = runtime_function("global_plugin_set_enabled_tool", request_control=request)
    event = Event(message="姜小妹，关闭天气插件")
    event.get_message_str = lambda: "模型不能替换的文本"
    result = asyncio.run(toggle(NS(context=context()), event, "jiang_weather", False))
    assert result == "天气插件已全局关闭"
    assert calls == [{"action": "set_enabled", "sender_id": "wxid_admin",
                      "message": "姜小妹，关闭天气插件", "module": "jiang_weather", "enabled": False}]
    assert asyncio.run(toggle(NS(context=context()), event, "jiang_weather", False)) == result
    assert len(calls) == 1


@pytest.mark.parametrize("module,enabled,event", [
    ("jiang_weather", False, Event(sender="wxid_other")),
    ("../../secret", False, Event()),
    ("jiang_weather", "false", Event()),
    ("jiang_weather", 0, Event()),
])
def test_invalid_or_unauthorized_toggle_never_reaches_manager(module, enabled, event):
    async def forbidden(_payload):
        pytest.fail("unauthorized or invalid operation reached manager")

    toggle = runtime_function("global_plugin_set_enabled_tool", request_control=forbidden)
    assert "已全局" not in asyncio.run(toggle(NS(context=context()), event, module, enabled))


@pytest.mark.parametrize("receipt", [{}, {"module": "other", "enabled": False},
                                      {"module": "jiang_weather", "enabled": True},
                                      {"module": "jiang_weather", "enabled": False}])
def test_toggle_requires_matching_readback_before_success(receipt):
    async def request(_payload):
        return {"message": "已完成", **receipt}

    toggle = runtime_function("global_plugin_set_enabled_tool", request_control=request)
    assert "尚未确认" in asyncio.run(toggle(NS(context=context()), Event(), "jiang_weather", False))


def test_toggle_saved_but_not_applied_reports_pending_state():
    async def request(_payload):
        return {"module": "jiang_weather", "enabled": False, "applied": False,
                "message": "配置已保存，但尚未确认运行中的插件生效，请恢复管理接口后重试。"}

    toggle = runtime_function("global_plugin_set_enabled_tool", request_control=request)
    result = asyncio.run(toggle(NS(context=context()), Event(), "jiang_weather", False))
    assert "尚未确认运行中的插件生效" in result
    assert "已全局停用" not in result


def test_failed_toggle_is_not_automatically_repeated():
    calls = []

    async def request(payload):
        calls.append(payload)
        raise control.PluginControlError("结果未确认，请先查询状态")

    toggle = runtime_function("global_plugin_set_enabled_tool", request_control=request)
    plugin, event = NS(context=context()), Event()
    assert "未确认" in asyncio.run(toggle(plugin, event, "jiang_weather", False))
    assert "未确认" in asyncio.run(toggle(plugin, event, "jiang_weather", False))
    assert len(calls) == 1


def test_global_list_includes_disabled_plugin_but_rejects_malformed_states():
    async def request(_payload):
        return {"plugins": [{"module": "jiang_weather", "name": "天气", "enabled": False},
                            {"module": "jiang_commands", "name": "常用指令", "enabled": False,
                             "aliases": ["图来", "涩图"]},
                            {"module": "bad", "name": "无效", "enabled": "false"}]}

    listing = runtime_function("global_plugin_list_tool", request_control=request)
    result = asyncio.run(listing(NS(context=context()), Event()))
    assert "天气（jiang_weather）：已停用" in result
    assert "常用指令（jiang_commands；别名：图来、涩图）：已停用" in result
    assert "无效" not in result


class ToolSet:
    def __init__(self, tools):
        self.tools = tools

    def remove_tool(self, name):
        self.tools = [item for item in self.tools if item.name != name]

    def __iter__(self):
        return iter(self.tools)


@pytest.mark.parametrize("inject_prompt", [False, True])
def test_non_admin_request_filters_global_tools_without_mutating_shared_tools(inject_prompt):
    shared = ToolSet([NS(name=name, description="tool") for name in (
        "global_plugin_list", "global_plugin_set_enabled", "normal_tool")])
    remove = runtime_function("_remove_global_plugin_tools_for_non_admin")
    plugin = NS(context=context(), config={"inject_capability_prompt": inject_prompt})
    plugin._remove_global_plugin_tools_for_non_admin = lambda req, event: remove(plugin, req, event)
    inject = runtime_function("inject_capability_context")
    req = NS(func_tool=shared, system_prompt="original")
    event = Event(sender="wxid_other")
    asyncio.run(inject(plugin, event, req))
    assert [tool.name for tool in req.func_tool] == ["normal_tool"]
    assert len(shared.tools) == 3
    assert "管理员全局插件管理" not in req.system_prompt


def test_admin_prompt_and_hidden_tools_do_not_enter_function_catalog():
    tools = ToolSet([NS(name=name, description="tool") for name in (
        "global_plugin_list", "global_plugin_set_enabled", "normal_tool")])
    remove = runtime_function("_remove_global_plugin_tools_for_non_admin")
    plugin = NS(context=context(), config={"inject_capability_prompt": True})
    plugin._remove_global_plugin_tools_for_non_admin = lambda req, event: remove(plugin, req, event)
    inject = runtime_function("inject_capability_context")
    req, event = NS(func_tool=tools, system_prompt="original"), Event()
    asyncio.run(inject(plugin, event, req))
    assert req.func_tool is tools
    assert "管理员全局插件管理" in req.system_prompt
    assert [tool["name"] for tool in event.get_extra("capability_native_tools")] == ["normal_tool"]
    plugin.config["inject_capability_prompt"] = False
    req = NS(func_tool=tools, system_prompt="original")
    asyncio.run(inject(plugin, Event(), req))
    assert "管理员全局插件管理" in req.system_prompt


def test_toggle_docstring_exposes_boolean_schema_parameter():
    parser = pytest.importorskip("docstring_parser")
    toggle = runtime_function("global_plugin_set_enabled_tool")
    params = parser.parse(toggle.__doc__).params
    assert [(param.arg_name, param.type_name) for param in params] == [
        ("module", "string"), ("enabled", "boolean"),
    ]
