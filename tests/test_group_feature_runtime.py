from pathlib import Path
import asyncio
import json
import subprocess
from types import SimpleNamespace
import zipfile
import sys
import pytest

from wechatrebot_manager.group_feature_runtime import (
    DEFAULT_GROUP_FEATURES, conversation_plugins, event_plugins, group_plugins,
    plugin_allowed, session_plugin_allowed,
)
from wechatrebot_manager.group_policy import DEFAULT_GROUP_FEATURES as MANAGER_DEFAULTS


ROOT = Path(__file__).resolve().parents[1]
STARS = [SimpleNamespace(root_dir_name=module, name="friendly_" + module)
         for module in (*DEFAULT_GROUP_FEATURES, "jiang_schedule", "jiang_capability_router", "astrbot_plugin_patch_at_wxid")]


def _save(path, **policy):
    path.write_text(json.dumps({"schema_version": 1, "accounts": {"wxid_bot": {"groups": {"room@chatroom": policy}}}}), encoding="utf-8")


def test_default_feature_list_matches_manager_and_excludes_ungranted_functions(tmp_path):
    assert DEFAULT_GROUP_FEATURES == MANAGER_DEFAULTS
    permitted = group_plugins(tmp_path / "missing.json", "wxid_bot", "room@chatroom", None, STARS)
    assert "jiang_commands" in permitted
    assert "jiang_schedule" not in permitted
    assert "jiang_capability_router" in permitted


def test_feature_selection_takes_effect_on_next_message_without_restart(tmp_path):
    path = tmp_path / "policy.json"
    _save(path, allowed_features=[])
    assert "jiang_schedule" not in group_plugins(path, "wxid_bot", "room@chatroom", None, STARS)
    _save(path, allowed_features=["jiang_schedule"])
    assert "jiang_schedule" in group_plugins(path, "wxid_bot", "room@chatroom", None, STARS)
    assert "jiang_commands" not in group_plugins(path, "wxid_bot", "room@chatroom", None, STARS)


def test_group_permissions_never_reenable_globally_disabled_functions(tmp_path):
    path = tmp_path / "policy.json"
    _save(path, allowed_features=["jiang_schedule", "jiang_commands"])
    permitted = group_plugins(path, "wxid_bot", "room@chatroom", ["jiang_commands"], STARS)
    assert "jiang_schedule" not in permitted
    assert "jiang_commands" in permitted
    assert "astrbot_plugin_angel_heart" not in permitted


def test_conversation_core_survives_optional_grants_but_respects_chat_boundaries(tmp_path):
    path = tmp_path / "policy.json"
    _save(path, allowed_features=[])
    assert "astrbot_plugin_angel_heart" in group_plugins(path, "wxid_bot", "room@chatroom", None, STARS)
    _save(path, allowed_features=[], bot_enabled=False)
    assert group_plugins(path, "wxid_bot", "room@chatroom", None, STARS) is None
    _save(path, allowed_features=[], reply_mode="off")
    assert group_plugins(path, "wxid_bot", "room@chatroom", None, STARS) is None


def test_group_feature_policy_fails_closed_and_is_account_scoped(tmp_path):
    path = tmp_path / "policy.json"
    _save(path, bot_enabled=False)
    assert group_plugins(path, "wxid_bot", "room@chatroom", None, STARS) is None
    assert "jiang_commands" in group_plugins(path, "wxid_other", "room@chatroom", None, STARS)
    _save(path, reply_mode="off")
    assert group_plugins(path, "wxid_bot", "room@chatroom", None, STARS) is None
    path.write_text("corrupt", encoding="utf-8")
    assert group_plugins(path, "wxid_bot", "room@chatroom", None, STARS) is None
    assert group_plugins(path, "", "room@chatroom", None, STARS) is None


def test_pinned_engine_patch_applies_and_contains_identical_group_policy(tmp_path):
    engine = ROOT / "vendor/engine"
    relative = "astrbot/core/pipeline/waking_check/stage.py"
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((engine / relative).read_bytes())
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    result = subprocess.run(["git", "apply", "-p2", "--whitespace=error-all", "--include=" + relative,
                             "--include=astrbot/core/platform/wechat_group_policy.py",
                             str(ROOT / "patches/astrbot-4.26.6-outbound-policy.patch")],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    helper = (tmp_path / "astrbot/core/platform/wechat_group_policy.py").read_text(encoding="utf-8")
    assert helper == (ROOT / "src/wechatrebot_manager/group_feature_runtime.py").read_text(encoding="utf-8")
    stage = (tmp_path / relative).read_text(encoding="utf-8")
    assert "event.plugins_name = allowed" in stage
    assert stage.index("event.plugins_name = allowed") < stage.index("for handler in star_handlers_registry")


def test_packaged_group_filter_handles_commands_without_wheel_cache(tmp_path):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True)
    relative = "astrbot/core/platform/wechat_group_policy.py"
    subprocess.run(["git", "apply", "-p2", "--include=" + relative,
                    str(ROOT / "patches/astrbot-4.26.6-outbound-policy.patch")],
                   cwd=tmp_path, check=True, capture_output=True)
    helper = (tmp_path / relative).read_text(encoding="utf-8")
    assert helper == (ROOT / "src/wechatrebot_manager/group_feature_runtime.py").read_text(encoding="utf-8")
    namespace = {}
    exec(compile(helper, relative, "exec"), namespace)
    policy = tmp_path / "policy.json"
    permitted = namespace["group_plugins"](policy, "wxid_bot", "room@chatroom", None, STARS)
    assert "jiang_commands" in permitted
    assert "astrbot_plugin_music" in permitted
    _save(policy, bot_enabled=False)
    assert namespace["group_plugins"](policy, "wxid_bot", "room@chatroom", None, STARS) is None


@pytest.fixture
def permission_runtime(tmp_path, monkeypatch):
    root = tmp_path
    (root / "app/astrbot").mkdir(parents=True)
    (root / "data/manager").mkdir(parents=True)
    (root / "legal").mkdir()
    monkeypatch.chdir(root / "app/astrbot")
    (root / "legal/feature-set.json").write_text(json.dumps({"features": [
        {"module": "voice", "visible": True, "default_enabled": True},
        {"module": "hidden", "visible": False, "default_enabled": True},
        {"module": "off", "visible": True, "default_enabled": False},
    ]}), encoding="utf-8")
    disabled = set()
    async def enabled(session, name):
        return name not in disabled
    monkeypatch.setitem(sys.modules, "astrbot.core.star.session_plugin_manager",
        SimpleNamespace(SessionPluginManager=SimpleNamespace(is_plugin_enabled_for_session=enabled)))
    stars = [SimpleNamespace(root_dir_name=name, name="friendly_" + name, activated=True,
                              reserved=False) for name in ("voice", "hidden", "off", "jiang_commands")]
    event = SimpleNamespace(unified_msg_origin="wechat:FriendMessage:friend", plugins_name=None,
        get_platform_name=lambda: "aiocqhttp", get_self_id=lambda: "wxid_bot",
        get_group_id=lambda: "", get_sender_id=lambda: "friend")
    config = {"plugin_set": ["*"]}
    async def login(**kwargs):
        return {"user_id": "wxid_bot"}
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"),
                              bot=SimpleNamespace(get_login_info=login))
    context = SimpleNamespace(get_config=lambda session: config, get_all_stars=lambda: stars,
                              get_platform_inst=lambda platform_id: platform)
    return root, disabled, stars, event, config, platform, context


def _private_policy(root, account="wxid_bot", **policy):
    (root / "data/manager/private-chat-policies.json").write_text(json.dumps({
        "schema_version": 1, "accounts": {account: {"contacts": {"friend": policy}}}
    }), encoding="utf-8")


def test_private_defaults_and_explicit_grants_match_saved_contact_contract(permission_runtime):
    root, _, stars, event, _, _, context = permission_runtime
    read = lambda module="voice": asyncio.run(plugin_allowed(context, event, module))
    assert read()
    assert not read("hidden")
    assert not read("off")
    _private_policy(root, allowed_features=[])
    assert not read()
    _private_policy(root, allowed_features=["voice"])
    assert read()
    _private_policy(root, allowed_features=["voice"], bot_enabled=False)
    assert not read()
    event.get_self_id = lambda: "other_bot"
    assert read()


@pytest.mark.parametrize("change", ["chat", "global", "session", "inactive", "event", "corrupt", "account"])
def test_dependency_rechecks_each_permission_before_later_side_effect(permission_runtime, change):
    root, disabled, stars, event, config, _, context = permission_runtime
    _private_policy(root, allowed_features=["voice"])
    assert asyncio.run(plugin_allowed(context, event, "voice"))
    if change == "chat":
        _private_policy(root, allowed_features=[])
    elif change == "global":
        config["plugin_set"] = []
    elif change == "session":
        disabled.add("friendly_voice")
    elif change == "inactive":
        stars[0].activated = False
    elif change == "event":
        event.plugins_name = []
    elif change == "account":
        event.get_self_id = lambda: ""
    else:
        (root / "data/manager/private-chat-policies.json").write_text("corrupt", encoding="utf-8")
    assert not asyncio.run(plugin_allowed(context, event, "voice"))


def test_event_scope_removes_session_disabled_hooks_and_tools_for_other_platforms(permission_runtime):
    _, disabled, stars, event, _, _, _ = permission_runtime
    event.get_platform_name = lambda: "test"
    disabled.add("friendly_voice")
    permitted = asyncio.run(event_plugins(event, ["*"], stars))
    assert "voice" not in permitted
    assert "friendly_voice" not in permitted
    assert "hidden" in permitted


def test_private_commands_preserve_random_image_in_shared_feature(permission_runtime):
    root, _, stars, event, _, _, context = permission_runtime
    _private_policy(root, allowed_features=["jiang_commands"])
    assert asyncio.run(plugin_allowed(context, event, "jiang_commands"))
    assert asyncio.run(plugin_allowed(context, event, "jiang_commands", command_handler="random_image"))
    assert asyncio.run(plugin_allowed(context, event, "jiang_commands", command_handler="news"))
    _private_policy(root, allowed_features=[])
    assert not asyncio.run(plugin_allowed(context, event, "jiang_commands", command_handler="random_image"))


def test_background_task_requires_saved_account_and_rechecks_current_target(permission_runtime):
    root, disabled, _, event, _, platform, context = permission_runtime
    _private_policy(root, allowed_features=["voice"])
    read = lambda **kwargs: asyncio.run(session_plugin_allowed(context, event.unified_msg_origin,
                                                              "voice", **kwargs))
    assert not read()
    assert read(account_id="wxid_bot", user_id="friend")
    assert not read(account_id="wxid_bot", user_id="other")
    disabled.add("friendly_voice")
    assert not read(account_id="wxid_bot")
    disabled.clear()
    async def switched(**kwargs):
        return {"user_id": "another_bot"}
    platform.bot.get_login_info = switched
    assert not read(account_id="wxid_bot")


def test_permission_storage_failure_is_denied(permission_runtime, monkeypatch):
    _, _, _, event, _, _, context = permission_runtime
    async def failed(*args):
        raise OSError("unavailable")
    monkeypatch.setattr(sys.modules["astrbot.core.star.session_plugin_manager"].SessionPluginManager,
                        "is_plugin_enabled_for_session", failed)
    assert not asyncio.run(plugin_allowed(context, event, "voice"))
