from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path
import zoneinfo

import pytest

from wechatrebot_manager.group_policy import (
    DEFAULT_GROUP_FEATURES,
    GroupPolicyError,
    GroupPolicyService,
)
from wechatrebot_manager.schedules import ScheduleService
from wechatrebot_manager import group_policy


ACCOUNT_A = "wxid_robot_a"
ACCOUNT_B = "wxid_robot_b"
GROUP = "51632940287@chatroom"


@pytest.fixture(autouse=True)
def current_schedule_account(monkeypatch):
    monkeypatch.setattr(ScheduleService, "_runtime_account", lambda self: ACCOUNT_A)


def test_setu_has_independent_group_checkbox_and_persists(tmp_path):
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    features = service.overview(ACCOUNT_A, GROUP)["features"]
    assert any(item["id"] == "jiang_commands.setu" and item["name"] == "图来" for item in features)
    service.save(ACCOUNT_A, GROUP, {"allowed_features": ["jiang_commands.setu"]})
    policy = GroupPolicyService(tmp_path).get(ACCOUNT_A, GROUP)
    assert "jiang_commands.setu" in policy["allowed_features"]
    assert "jiang_commands" not in policy["allowed_features"]
    service.save(ACCOUNT_A, GROUP, {"allowed_features": ["jiang_commands"]})
    assert "jiang_commands.setu" not in service.get(ACCOUNT_A, GROUP)["allowed_features"]


def test_feature_permissions_reads_only_grants_without_schedule_access_or_writes(tmp_path, monkeypatch):
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)

    def unexpected_schedules(**_kwargs):
        pytest.fail("Feature permission queries must not reconcile scheduled tasks")

    monkeypatch.setattr(service.schedules, "list", unexpected_schedules)
    initial_files = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    defaults = service.feature_permissions(ACCOUNT_A, GROUP)
    assert set(defaults) == {"policy", "features"}
    assert defaults["policy"]["configured"] is False
    assert any(item["id"] == "jiang_commands.setu" for item in defaults["features"])
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == initial_files

    service.save(ACCOUNT_A, GROUP, {"allowed_features": ["jiang_commands.setu"]})
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = service.feature_permissions(ACCOUNT_A, GROUP)
    allowed = {item["id"] for item in result["features"] if item["allowed"]}
    assert allowed == {"jiang_commands.setu"}
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_single_feature_toggle_preserves_group_options_and_other_scopes(tmp_path):
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    service.save(ACCOUNT_A, GROUP, {
        "allowed_features": ["jiang_commands", "jiang_commands.setu"],
        "daily_limit": 7, "reply_mode": "natural", "scheduled_enabled": False,
    })
    other_group = "other@chatroom"
    before_other_group = service.get(ACCOUNT_A, other_group)
    before_other_account = service.get(ACCOUNT_B, GROUP)
    changed = service.set_feature_enabled(ACCOUNT_A, GROUP, "jiang_commands.setu", False, actor_id="wxid_second_admin")
    assert changed["saved"] is True and changed["enabled"] is False
    assert changed["module"] == "jiang_commands.setu"
    assert changed["allowed_features"] == ["jiang_commands"]
    assert changed["daily_limit"] == 7 and changed["reply_mode"] == "natural"
    assert changed["scheduled_enabled"] is False
    assert changed["updated_by"] == "wxid_second_admin"
    assert service.get(ACCOUNT_A, other_group) == before_other_group
    assert service.get(ACCOUNT_B, GROUP) == before_other_account
    service.set_feature_enabled(ACCOUNT_A, GROUP, "jiang_commands.setu", True)
    service.set_feature_enabled(ACCOUNT_A, GROUP, "jiang_commands", False)
    assert service.get(ACCOUNT_A, GROUP)["allowed_features"] == ["jiang_commands.setu"]


def test_single_feature_toggle_preserves_implicit_defaults_and_rejects_stale_revision(tmp_path):
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    initial = service.get(ACCOUNT_A, GROUP)
    # Complete-list UI saves still reject undelivered grants. The one-feature
    # transaction alone preserves unrelated inherited defaults atomically.
    with pytest.raises(GroupPolicyError, match="未交付"):
        service.save(ACCOUNT_A, GROUP, {"allowed_features": initial["allowed_features"]})
    changed = service.set_feature_enabled(ACCOUNT_A, GROUP, "jiang_commands", False)
    assert changed["allowed_features"] == [module for module in DEFAULT_GROUP_FEATURES if module != "jiang_commands"]
    before = service.path.read_bytes()
    with pytest.raises(GroupPolicyError, match="其他窗口"):
        service.set_feature_enabled(ACCOUNT_A, GROUP, "jiang_commands", True, expected_revision=initial["revision"])
    assert service.path.read_bytes() == before


def test_single_feature_toggle_cannot_add_unknown_even_when_other_legacy_grants_exist(tmp_path):
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    service.path.parent.mkdir(parents=True, exist_ok=True)
    service.path.write_text(json.dumps({"schema_version": 1, "accounts": {
        ACCOUNT_A: {"groups": {GROUP: {"allowed_features": ["legacy_unknown", "jiang_commands"]}}},
    }}), encoding="utf-8")
    before = service.path.read_bytes()
    with pytest.raises(GroupPolicyError, match="正式功能"):
        service.set_feature_enabled(ACCOUNT_A, GROUP, "new_unknown", True)
    with pytest.raises(GroupPolicyError, match="未交付"):
        service.save(ACCOUNT_A, GROUP, {"allowed_features": ["legacy_unknown"]})
    assert service.path.read_bytes() == before
    changed = service.set_feature_enabled(ACCOUNT_A, GROUP, "jiang_commands", False)
    assert changed["allowed_features"] == ["legacy_unknown"]


def test_concurrent_single_feature_toggles_do_not_lose_other_admin_changes(tmp_path):
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    service.save(ACCOUNT_A, GROUP, {"allowed_features": []})
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(service.set_feature_enabled, ACCOUNT_A, GROUP, module, True, actor_id=f"wxid_admin_{index}")
                   for index, module in enumerate(("jiang_commands", "jiang_commands.setu"))]
        for future in futures:
            assert future.result()["saved"] is True
    assert set(service.get(ACCOUNT_A, GROUP)["allowed_features"]) == {"jiang_commands", "jiang_commands.setu"}
    audit = json.loads(service.path.read_text(encoding="utf-8"))["audit"]
    assert {item["actor_id"] for item in audit[-2:]} == {"wxid_admin_0", "wxid_admin_1"}


@pytest.mark.parametrize("module,enabled", [
    ("missing_feature", True), ("astrbot_plugin_angel_heart", False),
    ("jiang_commands", "false"), ("jiang_commands", 0), (None, False),
])
def test_invalid_single_feature_toggle_does_not_create_policy(tmp_path, module, enabled):
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    with pytest.raises(GroupPolicyError):
        service.set_feature_enabled(ACCOUNT_A, GROUP, module, enabled)
    assert not service.path.exists()


def _feature_set(root: Path) -> None:
    path = root / "legal" / "feature-set.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "features": [
                    {
                        "module": "jiang_commands",
                        "display_name": "常用指令",
                        "configuration": "schema",
                        "default_enabled": True,
                    },
                    {
                        "module": "astrbot_plugin_proactive_chat",
                        "display_name": "主动聊天",
                        "configuration": "schema",
                        "default_enabled": False,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _schedule_database(root: Path) -> None:
    data = root / "app" / "astrbot" / "data"
    data.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data / "data_v4.db") as connection:
        connection.execute(
            """
            CREATE TABLE cron_jobs (
                job_id TEXT PRIMARY KEY, name TEXT, description TEXT,
                cron_expression TEXT, payload TEXT, enabled INTEGER,
                status TEXT, last_run_at TEXT, next_run_time TEXT, last_error TEXT
            )
            """
        )
        rows = [
            (
                "for-group",
                f"weather_source:GroupMessage:{GROUP}",
                "天气预报",
                "0 8 * * *",
                json.dumps({"account_id": "wxid_robot_a", "session": f"source:GroupMessage:{GROUP}"}),
                1,
                "scheduled",
                "",
                "2026-09-10 08:00:00",
                "",
            ),
            (
                "other-group",
                "news_source:GroupMessage:other@chatroom",
                "新闻",
                "0 9 * * *",
                json.dumps({"account_id": "wxid_robot_a", "session": "source:GroupMessage:other@chatroom"}),
                1,
                "scheduled",
                "",
                "2026-09-10 09:00:00",
                "",
            ),
        ]
        connection.executemany("INSERT INTO cron_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def test_new_group_has_conservative_account_scoped_defaults(tmp_path: Path) -> None:
    service = GroupPolicyService(tmp_path)

    result = service.get(ACCOUNT_A, GROUP)

    assert result["configured"] is False
    assert result["bot_enabled"] is True
    assert result["reply_mode"] == "mention"
    assert "proactive_enabled" not in result
    assert result["allowed_features"] == list(DEFAULT_GROUP_FEATURES)
    assert result["permissions"]["group_role"] == "unknown"
    assert result["permissions"]["can_perform_group_admin_actions"] is False


def test_save_is_atomic_audited_and_isolated_by_account(tmp_path: Path) -> None:
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    initial_revision = service.get(ACCOUNT_A, GROUP)["revision"]

    saved = service.save(
        ACCOUNT_A,
        GROUP,
        {
            "reply_mode": "natural",
            "allowed_features": ["jiang_commands"],
            "quiet_hours": {"enabled": True, "start": "23:30", "end": "07:00"},
            "daily_limit": 5,
        },
        expected_revision=initial_revision,
        actor_id="wxid_admin",
    )

    assert saved["saved"] is True
    assert saved["message"].endswith("未向群内发送消息。")
    assert service.get(ACCOUNT_A, GROUP)["reply_mode"] == "natural"
    assert service.get(ACCOUNT_B, GROUP)["reply_mode"] == "mention"
    assert service.list_for_account(ACCOUNT_A)["total"] == 1
    assert service.list_for_account(ACCOUNT_B)["total"] == 0
    payload = json.loads(service.path.read_text(encoding="utf-8"))
    assert payload["audit"][-1]["changed_fields"] == [
        "allowed_features",
        "daily_limit",
        "quiet_hours",
        "reply_mode",
    ]
    assert "message" not in payload["audit"][-1]


def test_revision_conflict_and_invalid_values_leave_original_unchanged(tmp_path: Path) -> None:
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    service.save(ACCOUNT_A, GROUP, {"allowed_features": ["jiang_commands"]})
    before = service.path.read_bytes()

    with pytest.raises(GroupPolicyError, match="其他窗口"):
        service.save(ACCOUNT_A, GROUP, {"reply_mode": "off"}, expected_revision="stale")
    assert service.path.read_bytes() == before

    with pytest.raises(GroupPolicyError, match="未交付"):
        service.save(ACCOUNT_A, GROUP, {"allowed_features": ["missing_feature"]})
    assert service.path.read_bytes() == before

    with pytest.raises(GroupPolicyError, match="0 到 100"):
        service.save(ACCOUNT_A, GROUP, {"daily_limit": 101})
    assert service.path.read_bytes() == before


def test_preview_describes_impact_without_writing_and_save_rereads(tmp_path: Path) -> None:
    _feature_set(tmp_path)
    service = GroupPolicyService(tmp_path)
    revision = service.get(ACCOUNT_A, GROUP)["revision"]
    before = service.path.read_bytes() if service.path.exists() else b""

    preview = service.preview(
        ACCOUNT_A,
        GROUP,
        {"reply_mode": "natural", "scheduled_enabled": False},
        expected_revision=revision,
    )

    assert preview["will_send_messages"] is False
    assert "reply_mode" in preview["changed_fields"]
    assert "定时任务" in " ".join(preview["impact"])
    assert (service.path.read_bytes() if service.path.exists() else b"") == before

    saved = service.save(
        ACCOUNT_A,
        GROUP,
        {"reply_mode": "natural", "scheduled_enabled": False},
        expected_revision=revision,
    )
    assert saved["apply_state"] == "applied"
    assert service.get(ACCOUNT_A, GROUP)["reply_mode"] == "natural"

@pytest.mark.parametrize("quiet_enabled", [False, True])
def test_feature_save_uses_packaged_timezone_without_system_database(
    monkeypatch, tmp_path: Path, quiet_enabled: bool,
) -> None:
    _feature_set(tmp_path)
    feature_path = tmp_path / "legal" / "feature-set.json"
    features = json.loads(feature_path.read_text(encoding="utf-8"))
    features["features"].append({
        "module": "jiang_short_drama",
        "display_name": "短剧",
        "configuration": "schema",
        "default_enabled": True,
    })
    feature_path.write_text(json.dumps(features), encoding="utf-8")
    # Force the Windows/frozen fallback and bypass any prior cached zone.
    original_path = zoneinfo.TZPATH
    zoneinfo.reset_tzpath([])
    monkeypatch.setattr(group_policy, "ZoneInfo", zoneinfo.ZoneInfo.no_cache)
    try:
        service = GroupPolicyService(tmp_path)
        service.save(ACCOUNT_A, GROUP, {
            "allowed_features": ["jiang_short_drama"],
            "quiet_hours": {"enabled": quiet_enabled},
        })
        saved = service.get(ACCOUNT_A, GROUP)
        assert "jiang_short_drama" in saved["allowed_features"]
        assert saved["quiet_hours"]["enabled"] is quiet_enabled
        assert saved["quiet_hours"]["timezone"] == "Asia/Shanghai"
        before = service.path.read_bytes()
        with pytest.raises(GroupPolicyError, match="时区不可用"):
            service.save(ACCOUNT_A, GROUP, {
                "quiet_hours": {"timezone": "Invalid/Timezone"},
            })
        assert service.path.read_bytes() == before
    finally:
        zoneinfo.reset_tzpath(original_path)


@pytest.mark.parametrize(
    ("account_id", "group_id"),
    [("../account", GROUP), (ACCOUNT_A, "../group@chatroom"), ("", GROUP)],
)
def test_scope_identifiers_are_rejected(account_id: str, group_id: str, tmp_path: Path) -> None:
    with pytest.raises(GroupPolicyError):
        GroupPolicyService(tmp_path).get(account_id, group_id)


def test_corrupt_store_is_never_overwritten(tmp_path: Path) -> None:
    service = GroupPolicyService(tmp_path)
    service.path.parent.mkdir(parents=True)
    service.path.write_text("not-json", encoding="utf-8")
    before = service.path.read_bytes()

    with pytest.raises(GroupPolicyError, match="原配置未被修改"):
        service.save(ACCOUNT_A, GROUP, {})

    assert service.path.read_bytes() == before


def test_group_overview_exactly_summarizes_schedules_and_proactive_sources(tmp_path: Path) -> None:
    _feature_set(tmp_path)
    _schedule_database(tmp_path)
    config = (
        tmp_path
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "astrbot_plugin_proactive_chat_config.json"
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    (config.parents[1] / "cmd_config.json").write_text(
        json.dumps({"plugin_set": ["astrbot_plugin_proactive_chat"]}),
        encoding="utf-8",
    )
    config.write_text(
        json.dumps(
            {
                "group_settings": {
                    "enable": True,
                    "session_list": [],
                }
            }
        ),
        encoding="utf-8",
    )
    service = GroupPolicyService(tmp_path, schedules=ScheduleService(tmp_path))
    service.save(ACCOUNT_A, GROUP, {"allowed_features": ["astrbot_plugin_proactive_chat"]})

    # Manager-only upgrades must not claim an old, empty-list plugin can send.
    old_runtime = service.proactive_summary(ACCOUNT_A, GROUP)
    assert not any(item["id"].startswith("feature:") for item in old_runtime["items"])
    schema_path = config.parents[1] / "plugins" / "astrbot_plugin_proactive_chat" / "_conf_schema.json"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text(json.dumps({"group_settings": {"_wechatrebot_managed_scope": True}}), encoding="utf-8")

    overview = service.overview(ACCOUNT_A, GROUP)
    proactive = overview["proactive"]

    assert {item["id"] for item in proactive["items"]} == {
        "for-group",
        "feature:astrbot_plugin_proactive_chat",
    }
    assert proactive["running"] == 2
    assert "effective_enabled" not in proactive
    assert proactive["message"] == "主动聊天由各功能自行配置；定时任务按本群设置执行。"
    assert overview["features"][0]["name"] == "常用指令"

    # The same group under another account never inherits this account's grant.
    other = service.proactive_summary(ACCOUNT_B, GROUP)
    assert not any(item["id"] == "feature:astrbot_plugin_proactive_chat" for item in other["items"])
    service.save(ACCOUNT_A, GROUP, {"bot_enabled": False})
    paused = service.proactive_summary(ACCOUNT_A, GROUP)
    feature = next(item for item in paused["items"] if item["id"].startswith("feature:"))
    assert feature["enabled"] is False
    assert feature["status"] == "本群已停用"
    service.save(ACCOUNT_A, GROUP, {"allowed_features": []})
    revoked = service.proactive_summary(ACCOUNT_A, GROUP)
    assert not any(item["id"].startswith("feature:") for item in revoked["items"])


def test_schedule_items_expose_exact_machine_readable_target(tmp_path: Path) -> None:
    _schedule_database(tmp_path)

    item = ScheduleService(tmp_path).list()["items"][0]

    assert item["target_type"] == "group"
    assert item["target_id"] == GROUP
