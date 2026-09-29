from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _policy_module() -> ModuleType:
    path = ROOT / "runtime" / "meme_manager" / "wechatrebot_group_policy.py"
    spec = importlib.util.spec_from_file_location("wechatrebot_group_policy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Event:
    def __init__(
        self,
        *,
        group_id: str = "group@chatroom",
        account_id: str = "wxid_bot",
        platform: str = "aiocqhttp",
    ) -> None:
        self.group_id = group_id
        self.account_id = account_id
        self.platform = platform

    def get_group_id(self) -> str:
        return self.group_id

    def get_self_id(self) -> str:
        return self.account_id

    def get_platform_name(self) -> str:
        return self.platform


def _write_policy(path: Path, **values: object) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "accounts": {
                    "wxid_bot": {
                        "groups": {
                            "group@chatroom": {
                                "bot_enabled": True,
                                "reply_mode": "mention",
                                "allowed_features": ["meme_manager"],
                                **values,
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("overrides", "allowed"),
    [
        ({}, True),
        ({"allowed_features": []}, False),
        ({"bot_enabled": False}, False),
        ({"reply_mode": "off"}, False),
    ],
)
def test_hidden_meme_prompt_follows_group_feature_policy(
    tmp_path: Path, overrides: dict[str, object], allowed: bool
) -> None:
    policy_path = tmp_path / "group-policies.json"
    _write_policy(policy_path, **overrides)

    assert _policy_module().group_allows_meme_prompt(policy_path, Event()) is allowed


def test_hidden_meme_prompt_fails_closed_for_missing_or_corrupt_group_policy(
    tmp_path: Path,
) -> None:
    module = _policy_module()
    policy_path = tmp_path / "group-policies.json"
    assert module.group_allows_meme_prompt(policy_path, Event()) is False
    policy_path.write_text("{", encoding="utf-8")
    assert module.group_allows_meme_prompt(policy_path, Event()) is False


def test_non_group_conversation_keeps_existing_meme_behavior(tmp_path: Path) -> None:
    assert _policy_module().group_allows_meme_prompt(
        tmp_path / "missing.json", Event(group_id="")
    ) is True


def test_delivery_patch_gates_native_prompt_at_request_time() -> None:
    patch = (ROOT / "patches" / "astrbot-plugin-meme_manager.patch").read_text(
        encoding="utf-8"
    )
    assert "HIDDEN_MEME_PROMPT_HEAD" not in patch
    assert "group_allows_meme_prompt" in patch
    assert "req.system_prompt = self._strip_meme_prompt(req.system_prompt)" in patch
    assert "persona[\"prompt\"] = self.persona_base_prompts[key]" not in patch
