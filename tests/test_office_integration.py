"""Office delivery contracts; no robot, Office process or network is started."""
import asyncio
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from test_hot_config_runtime import _runtime_function

ROOT = Path(__file__).parents[1]
MODULE = "astrbot_plugin_office_assistant"


@pytest.fixture(scope="module")
def office_bridge(tmp_path_factory):
    checkout = tmp_path_factory.mktemp("office-bridge")
    source = ROOT / "vendor/hook_41827_astrbot_bridge"
    shutil.copytree(source / "src", checkout / "src")
    shutil.copy2(ROOT / "src/wechatrebot_manager/task_store.py", checkout / "src/hook_bridge/task_store.py")
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    for patch in sorted((ROOT / "patches").glob("hook-bridge-*.patch")):
        subprocess.run(["git", "apply", "-p2", "--whitespace=error-all", str(patch)],
                       cwd=checkout, check=True, capture_output=True)
    package_name = "_office_bridge_test"
    package = checkout / "src/hook_bridge"
    spec = importlib.util.spec_from_file_location(
        package_name, package / "__init__.py", submodule_search_locations=[str(package)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    yield NS(**{name: importlib.import_module(f"{package_name}.{name}")
                for name in ("app", "inbound_file", "message_codec", "models")})
    for key in list(sys.modules):
        if key == package_name or key.startswith(package_name + "."):
            del sys.modules[key]


def attachment_xml(name="销售表.xlsx", size=4, kind=6):
    return (f"<msg><appmsg><title>{name}</title><type>{kind}</type><appattach>"
            f"<totallen>{size}</totallen><attachid>opaque-id</attachid>"
            "<fileext>xlsx</fileext></appattach></appmsg></msg>")


@pytest.mark.parametrize("name,expected", [
    ("销售表.xlsx", "销售表.xlsx"),
    ("../../销售表.xlsx", "销售表.xlsx"),
    (r"C:\private\销售表.xlsx", "销售表.xlsx"),
    ("销售表", "销售表.xlsx"),
])
def test_attachment_keeps_safe_original_name(office_bridge, name, expected):
    metadata = office_bridge.inbound_file.parse_file_attachment({"content": attachment_xml(name)})
    assert metadata.name == expected
    assert metadata.size == 4
    assert metadata.attach_id == "opaque-id"


def test_app_card_is_not_an_office_attachment(office_bridge):
    assert office_bridge.inbound_file.parse_file_attachment({"content": attachment_xml(kind=5)}) is None
    assert office_bridge.inbound_file.parse_file_attachment({"content": "<broken"}) is None


@pytest.mark.parametrize("size,downloaded,expected", [(4, b"data", True), (4, b"da", False),
                                                           (0, b"data", False), (500, b"data", False)])
def test_attachment_download_and_onebot_handoff(office_bridge, tmp_path, size, downloaded, expected):
    application = object.__new__(office_bridge.app.BridgeApplication)
    application._active_native_downloads = 0
    application._native_download_generation = 0
    application.settings = NS(media_root=tmp_path, media_max_bytes=100, request_timeout_seconds=.02, file_download_timeout_seconds=.02)
    calls = []

    async def download(payload):
        calls.append(payload)
        Path(payload["path"]).write_bytes(downloaded)

    application.hook = NS(download_file=download)
    event = office_bridge.models.NormalizedEvent(
        kind="message", message_type=49, message_id="123", sender_id="wxid_user",
        conversation_id="room@chatroom", raw={"data": {
            "content": attachment_xml(size=size), "from_user": "wxid_user", "MsgId": "123",
        }},
    )
    result = asyncio.run(application._prepare_inbound_media(event))
    assert bool(result.media_path) is expected
    if expected:
        assert result.media_path.read_bytes() == downloaded
        assert result.media_path.suffix == ".xlsx"
        assert result.media_path.is_relative_to(tmp_path)
        assert calls[0]["total_len"] == "4"
        assert calls[0]["attachid"] == "opaque-id"
        message = office_bridge.message_codec.encode_inbound(result, "wxid_bot", 123)
        segment = next(item for item in message["message"] if item["type"] == "file")
        assert segment["data"]["name"] == "销售表.xlsx"
        assert segment["data"]["file"] == result.media_path.as_uri()
    else:
        assert not list(tmp_path.iterdir())


def test_local_file_adapter_is_scoped_to_wechat():
    patch = (ROOT / "patches/astrbot-4.26.6-wechat.patch").read_text("utf-8")
    added = "\n".join(line[1:] for line in patch.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    start = added.index("def _wechat_local_file(")
    end = added.index("\n", added.index("    return File(", start))
    helper = added[start:end]
    namespace = {"File": lambda **kwargs: kwargs}
    exec(helper, namespace)
    resolve = namespace["_wechat_local_file"]
    data = {"file": "file:///E:/media/销售表.xlsx", "name": "销售表.xlsx"}
    assert resolve(data, "aiocqhttp_hook41827") == data
    assert resolve(data, "qq") is None
    assert resolve({"file": "https://example.test/file.xlsx"}, "aiocqhttp_hook41827") is None


@pytest.fixture
def office_plugin(tmp_path):
    shutil.copytree(ROOT / "runtime" / MODULE, tmp_path / MODULE)
    plugin = tmp_path / MODULE
    subprocess.run(["git", "init", "--quiet"], cwd=plugin, check=True)
    subprocess.run(["git", "apply", str(ROOT / "patches" / f"astrbot-plugin-{MODULE}.patch")],
                   cwd=plugin, check=True)
    return plugin


def test_office_defaults_and_permission_before_capture(office_plugin):
    plugin = office_plugin
    schema = json.loads((plugin / "_conf_schema.json").read_text("utf-8"))
    assert schema["trigger_settings"]["items"]["enable_features_in_group"]["default"] is True
    assert schema["permission_settings"]["items"]["allow_all_users"]["default"] is False
    assert schema["trigger_settings"]["items"]["allow_local_excel_script"]["default"] is False
    assert schema["file_settings"]["items"]["auto_delete_files"]["default"] is False
    on_file = _runtime_function((plugin / "main.py").read_text("utf-8"), "on_file_message", {})
    handler = AsyncMock()
    owner = NS(_runtime=NS(access_policy_service=NS(check_permission=lambda event: event.allowed),
                         incoming_message_service=NS(handle_file_message=handler)))
    asyncio.run(on_file(owner, NS(allowed=False)))
    handler.assert_not_awaited()
    asyncio.run(on_file(owner, NS(allowed=True)))
    handler.assert_awaited_once()


@pytest.mark.parametrize("mentioned", [False, True])
def test_followup_mention_survives_upload_buffer(office_plugin, mentioned):
    add = _runtime_function((office_plugin / "services/message_buffer.py").read_text("utf-8"),
                            "add_message", {"logger": Mock()})
    original = NS()
    buffer = NS(event=original, files=[], images=[], texts=[])
    event = NS(get_platform_id=lambda: "aiocqhttp_hook41827", is_at_or_wake_command=mentioned)
    owner = NS(_wait_seconds=4, _lock=asyncio.Lock(), _buffers={"session": buffer},
               _get_buffer_key=lambda event: "session", _extract_components=lambda event: ([], [], ["分析"] ))
    assert asyncio.run(add(owner, event)) is True
    assert buffer.event is (event if mentioned else original)

    queue = asyncio.Queue()
    components = NS(Plain=lambda text: NS(text=text), At=lambda qq: NS(qq=qq))
    requeue = _runtime_function(
        (office_plugin / "services/upload_session_service.py").read_text("utf-8"), "_requeue_event",
        {"copy": copy, "Comp": components, "logger": Mock(), "active_event_registry": Mock(),
         "EVENT_UPLOAD_CACHE_ATTR": "_office_uploads"},
    )
    event._extras = {}
    event.message_obj = NS(message=[])
    event.get_group_id = lambda: "room@chatroom"
    event.get_self_id = lambda: "wxid_bot"
    event.unified_msg_origin = "session"
    owner = NS(_context=NS(get_event_queue=lambda: queue))
    asyncio.run(requeue(owner, event, prompt_text="分析表格", upload_infos=[],
                        file_components=[], reentry_count=0, force_command_wake=False))
    result = queue.get_nowait()
    assert hasattr(result.message_obj.message[0], "qq") is mentioned
    assert result._buffered is True


def test_office_guide_projects_usable_settings():
    from wechatrebot_manager.plugin_guides import get_plugin_guide, common_schema
    schema = json.loads((ROOT / "runtime" / MODULE / "_conf_schema.json").read_text("utf-8"))
    guide = get_plugin_guide(MODULE)
    assert guide["commands"]
    visible = common_schema(MODULE, schema)
    assert "permission_settings" in visible
    assert "allow_local_excel_script" not in visible["trigger_settings"]["items"]
