from __future__ import annotations

import json
from pathlib import Path

import pytest

from wechatrebot_manager import private_policy
from wechatrebot_manager.private_policy import (
    PrivateChatPolicyService,
    PrivatePolicyError,
)


ACCOUNT = "wxid_robot"
CONTACT = "wxid_friend"


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
                        "module": "astrbot_plugin_music",
                        "display_name": "点歌",
                        "configuration": "none",
                        "default_enabled": False,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_new_contact_inherits_default_enabled_features(tmp_path: Path) -> None:
    _feature_set(tmp_path)
    result = PrivateChatPolicyService(tmp_path).overview(ACCOUNT, CONTACT)

    assert result["policy"]["configured"] is False
    assert result["policy"]["allowed_features"] == ["jiang_commands"]
    assert [item["name"] for item in result["features"]] == ["常用指令", "点歌"]
    assert result["features"][0]["allowed"] is True
    assert result["features"][1]["allowed"] is False


def test_globally_disabled_private_feature_retains_its_chat_scope(tmp_path, monkeypatch):
    _feature_set(tmp_path)
    service = PrivateChatPolicyService(tmp_path)
    service.save(ACCOUNT, CONTACT, {"allowed_features": ["jiang_commands"]})
    monkeypatch.setattr(private_policy, "global_plugin_states", lambda _root: {"jiang_commands": False})

    overview = service.overview(ACCOUNT, CONTACT)
    feature = next(item for item in overview["features"] if item["id"] == "jiang_commands")
    assert feature["globally_enabled"] is False
    assert feature["allowed"] is False
    assert "jiang_commands" in overview["policy"]["allowed_features"]

    service.save(ACCOUNT, CONTACT, {"daily_limit": 7})
    monkeypatch.setattr(private_policy, "global_plugin_states", lambda _root: {"jiang_commands": True})
    restored = service.overview(ACCOUNT, CONTACT)
    assert restored["policy"]["daily_limit"] == 7
    assert next(item for item in restored["features"] if item["id"] == "jiang_commands")["allowed"] is True


def test_private_feature_scope_is_atomic_audited_and_account_scoped(
    tmp_path: Path,
) -> None:
    _feature_set(tmp_path)
    service = PrivateChatPolicyService(tmp_path)
    revision = service.overview(ACCOUNT, CONTACT)["policy"]["revision"]

    saved = service.save(
        ACCOUNT,
        CONTACT,
        {"allowed_features": ["astrbot_plugin_music"]},
        expected_revision=revision,
    )

    assert saved["allowed_features"] == ["astrbot_plugin_music"]
    assert service.overview(ACCOUNT, CONTACT)["policy"]["configured"] is True
    assert (
        PrivateChatPolicyService(tmp_path)
        .overview("wxid_other", CONTACT)["policy"]["allowed_features"]
        == ["jiang_commands"]
    )
    payload = json.loads(service.path.read_text(encoding="utf-8"))
    assert payload["audit"][-1]["action"] == "save_private_chat_policy"
    assert "message" not in payload["audit"][-1]


def test_private_scope_rejects_groups_unknown_features_and_stale_revision(
    tmp_path: Path,
) -> None:
    _feature_set(tmp_path)
    service = PrivateChatPolicyService(tmp_path)

    with pytest.raises(PrivatePolicyError, match="联系人标识"):
        service.overview(ACCOUNT, "room@chatroom")
    with pytest.raises(PrivatePolicyError, match="未交付"):
        service.save(ACCOUNT, CONTACT, {"allowed_features": ["missing"]})
    with pytest.raises(PrivatePolicyError, match="其他窗口"):
        service.save(
            ACCOUNT,
            CONTACT,
            {"allowed_features": ["jiang_commands"]},
            expected_revision="stale",
        )
