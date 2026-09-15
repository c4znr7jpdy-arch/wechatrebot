from pathlib import Path
import json
import subprocess
from types import SimpleNamespace
import zipfile
import pytest

from wechatrebot_manager.group_feature_runtime import DEFAULT_GROUP_FEATURES, group_plugins
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
    wheel = ROOT / "artifacts/runtime-cache/wheels-py313-win-amd64/astrbot-4.26.6-py3-none-any.whl"
    if not wheel.is_file():
        pytest.skip("Pinned engine cache is required for patch integration verification")
    relative = "astrbot/core/pipeline/waking_check/stage.py"
    with zipfile.ZipFile(wheel) as archive:
        archive.extract(relative, tmp_path)
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
