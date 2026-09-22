from __future__ import annotations

import json
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
                json.dumps({"session": f"source:GroupMessage:{GROUP}"}),
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
                json.dumps({"session": "source:GroupMessage:other@chatroom"}),
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
                    "session_list": [f"source:GroupMessage:{GROUP}"],
                }
            }
        ),
        encoding="utf-8",
    )
    service = GroupPolicyService(tmp_path, schedules=ScheduleService(tmp_path))

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


def test_schedule_items_expose_exact_machine_readable_target(tmp_path: Path) -> None:
    _schedule_database(tmp_path)

    item = ScheduleService(tmp_path).list()["items"][0]

    assert item["target_type"] == "group"
    assert item["target_id"] == GROUP
