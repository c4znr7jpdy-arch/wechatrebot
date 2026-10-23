"""Exercise background authorization without loading or starting a robot."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_hot_config_runtime import _runtime_function
from test_plugin_portability import _patched
from wechatrebot_manager import group_feature_runtime

ROOT = Path(__file__).parents[1]
SESSION = "wechat:GroupMessage:room@chatroom"


@pytest.fixture
def background(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "apply", "-p2", "--whitespace=error-all", str(
        ROOT / "patches/astrbot-4.26.6-background-permissions.patch")],
        cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setitem(sys.modules, "astrbot.core.platform.wechat_group_policy", group_feature_runtime)
    spec = importlib.util.spec_from_file_location("astrbot.core.platform.wechat_background_policy",
        tmp_path / "astrbot/core/platform/wechat_background_policy.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    disabled = set()
    async def enabled(session, name):
        return name not in disabled
    monkeypatch.setitem(sys.modules, "astrbot.core.star.session_plugin_manager", NS(
        SessionPluginManager=NS(is_plugin_enabled_for_session=enabled)))
    root = tmp_path / "install"
    cwd = root / "app/astrbot"
    cwd.mkdir(parents=True)
    policy = root / "data/manager/group-policies.json"
    policy.parent.mkdir(parents=True)
    names = ["astrbot_plugin_twitter", "astrbot_plugin_proactive_chat", "meme_manager"]
    stars = [NS(root_dir_name=name, name=name, activated=True, reserved=False) for name in names]
    def save(allowed=None):
        policy.write_text(json.dumps({"schema_version": 1, "accounts": {"wxid_bot": {"groups": {
            "room@chatroom": {"allowed_features": names if allowed is None else allowed}
        }}}}), encoding="utf-8")
    save()
    monkeypatch.chdir(cwd)
    account = ["wxid_bot"]
    async def login(**kwargs):
        return {"user_id": account[0]}
    bot = NS(get_login_info=login, send_group_msg=AsyncMock(return_value={"message_id": 1}),
             send_private_msg=AsyncMock(return_value={"message_id": 2}), call_action=AsyncMock())
    platform = NS(bot=bot, meta=lambda: NS(name="aiocqhttp", id="wechat"), status="running")
    config = {"plugin_set": ["*"]}
    context = NS(get_platform_inst=lambda id: platform, get_config=lambda session: config,
                 get_all_stars=lambda: stars, send_message=AsyncMock(return_value=True))
    class Event(dict):
        pass
    monkeypatch.setitem(sys.modules, "aiocqhttp", NS(Event=Event))
    conversion = []
    async def send_message(**kwargs):
        for callback in conversion:
            callback()
        await kwargs["bot"].send_group_msg(group_id=kwargs["session_id"], message=kwargs["message_chain"])
    monkeypatch.setitem(sys.modules, "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event", NS(
        AiocqhttpMessageEvent=NS(send_message=send_message)))
    return NS(module=module, context=context, platform=platform, bot=bot, stars=stars,
              save=save, disabled=disabled, config=config, account=account, conversion=conversion)


def test_background_binds_every_send_to_confirmed_account(background):
    fixture = background
    assert asyncio.run(fixture.module.send_background_message(
        fixture.context, SESSION, ["tweet"], "astrbot_plugin_twitter"))
    assert fixture.bot.send_group_msg.call_args.kwargs["self_id"] == "wxid_bot"
    fixture.context.send_message.assert_not_called()


@pytest.mark.parametrize("change", ["conversation", "global", "session", "account", "inactive"])
def test_revoke_while_preparing_media_prevents_background_send(background, change):
    f = background
    if change == "conversation":
        action = lambda: f.save([])
    elif change == "global":
        action = lambda: f.config.update(plugin_set=[])
    elif change == "session":
        action = lambda: f.disabled.add("astrbot_plugin_twitter")
    elif change == "inactive":
        action = lambda: setattr(f.stars[0], "activated", False)
    else:
        action = lambda: f.account.__setitem__(0, "other")
    f.conversion.append(action)
    with pytest.raises(PermissionError):
        asyncio.run(f.module.send_background_message(f.context, SESSION, [], "astrbot_plugin_twitter"))
    f.bot.send_group_msg.assert_not_called()


@pytest.mark.parametrize("response", [False, {"status": "failed"}, {"retcode": 1403}])
def test_rejected_bridge_send_is_not_success(background, response):
    background.bot.send_group_msg.return_value = response
    with pytest.raises(RuntimeError, match="rejected"):
        asyncio.run(background.module.send_background_message(
            background.context, SESSION, [], "astrbot_plugin_twitter"))


def test_saved_voice_account_cannot_be_replaced_with_current_background_account(background):
    with pytest.raises(PermissionError):
        asyncio.run(background.module.background_event(
            background.context, SESSION, "astrbot_plugin_twitter", account_id="old"))


def test_twitter_send_wrapper_reuses_background_policy(background):
    source = (ROOT / "runtime/astrbot_plugin_twitter/services/tweet_delivery_service.py").read_text("utf-8")
    send = _runtime_function(source, "_send_message_checked", {})
    background.save([])
    with pytest.raises(PermissionError):
        asyncio.run(send(NS(context=background.context), SESSION, []))
    background.bot.send_group_msg.assert_not_called()


def test_proactive_send_checks_permission_before_decorators(background, tmp_path):
    directory = tmp_path / "plugin"
    directory.mkdir()
    source = _patched(directory, "astrbot_plugin_proactive_chat", "core/message_sender.py", all_patches=True)
    function = _runtime_function(source, "_send_chain_with_hooks", {
        "PlatformStatus": NS(RUNNING="running"), "MessageChain": list, "logger": Mock()})
    owner = NS(context=background.context, _trigger_decorating_hooks=AsyncMock(),
               _persist_proactive_message_to_platform_history=AsyncMock())
    background.save([])
    asyncio.run(function(owner, SESSION, ["hello"]))
    owner._trigger_decorating_hooks.assert_not_called()


def test_proactive_rechecks_after_decorators(background, tmp_path):
    directory = tmp_path / "plugin"
    directory.mkdir()
    source = _patched(directory, "astrbot_plugin_proactive_chat", "core/message_sender.py", all_patches=True)
    function = _runtime_function(source, "_send_chain_with_hooks", {
        "PlatformStatus": NS(RUNNING="running"), "MessageChain": list, "logger": Mock()})
    async def decorate(*args, **kwargs):
        background.save([])
        return ["hello"]
    owner = NS(context=background.context, _trigger_decorating_hooks=decorate,
               _persist_proactive_message_to_platform_history=AsyncMock())
    asyncio.run(function(owner, SESSION, ["hello"]))
    background.bot.send_group_msg.assert_not_called()
    owner._persist_proactive_message_to_platform_history.assert_not_called()


def test_mystool_background_summary_obeys_contact_feature_grants(background, tmp_path):
    directory = tmp_path / "mystool"
    directory.mkdir()
    source = _patched(directory, "jiang_mystool", "main.py", all_patches=True)
    function = _runtime_function(source, "_send_mission_private", {
        "MessageChain": lambda: NS(message=lambda text: [text]), "logger": Mock()})
    assert not asyncio.run(function(NS(context=background.context), SESSION, "summary"))
    background.bot.send_group_msg.assert_not_called()


def test_proactive_denial_precedes_tts_and_session_work(background, tmp_path):
    directory = tmp_path / "proactive"
    directory.mkdir()
    source = _patched(directory, "astrbot_plugin_proactive_chat", "core/message_sender.py", all_patches=True)
    function = _runtime_function(source, "_send_proactive_message", {"logger": Mock()})
    owner = NS(context=background.context, _get_session_config=Mock())
    background.save([])
    asyncio.run(function(owner, SESSION, "hello"))
    owner._get_session_config.assert_not_called()


def test_meme_rechecks_permission_after_image_conversion(background, tmp_path):
    directory = tmp_path / "meme_manager"
    directory.mkdir()
    source = _patched(directory, "meme_manager", "mixins/event_handlers.py",
                      all_patches=True, source_root=ROOT / "runtime")
    function = _runtime_function(source, "_send_meme_image", {"MessageChain": list})
    async def convert(image):
        background.save([])
        return image
    event = asyncio.run(background.module.background_event(
        background.context, SESSION, "meme_manager"))[1]
    event.send = AsyncMock()
    owner = NS(context=background.context, _ensure_image_send_format=convert)
    asyncio.run(function(owner, event, "image"))
    event.send.assert_not_called()
    background.context.send_message.assert_not_called()


def test_meme_reply_uses_authorized_original_event(background, tmp_path):
    directory = tmp_path / "meme_manager"
    directory.mkdir()
    source = _patched(directory, "meme_manager", "mixins/event_handlers.py",
                      all_patches=True, source_root=ROOT / "runtime")
    function = _runtime_function(source, "_send_meme_image", {"MessageChain": list})
    event = asyncio.run(background.module.background_event(
        background.context, SESSION, "meme_manager"))[1]
    event.send = AsyncMock()
    owner = NS(context=background.context, _ensure_image_send_format=AsyncMock(return_value="image"))
    asyncio.run(function(owner, event, "input"))
    event.send.assert_awaited_once_with(["image"])
    background.context.send_message.assert_not_called()


def test_office_debug_notification_checks_destination_permissions(background, tmp_path):
    directory = tmp_path / "astrbot_plugin_office_assistant"
    directory.mkdir()
    source = _patched(directory, "astrbot_plugin_office_assistant", "services/error_hook_service.py",
                      all_patches=True, source_root=ROOT / "runtime")
    function = _runtime_function(source, "handle_plugin_error", {
        "MessageChain": lambda: NS(message=lambda text: [text]), "logger": Mock()})
    owner = NS(_context=background.context, _config={"debug_error_hook_target_session": SESSION},
               _plugin_name="office")
    event = NS(unified_msg_origin="other:FriendMessage:user", stop_event=Mock())
    asyncio.run(function(owner, event, "office", "generate", RuntimeError("error"), "trace"))
    background.bot.send_group_msg.assert_not_called()
    event.stop_event.assert_called_once()
