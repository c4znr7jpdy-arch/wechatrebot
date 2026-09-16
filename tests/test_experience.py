import json
from pathlib import Path

import pytest

from wechatrebot_manager.experience import ExperienceError, ExperienceService


def _plugin(root: Path, name: str, config: dict) -> Path:
    (root / "app" / "astrbot" / "data" / "plugins" / name).mkdir(parents=True)
    path = root / "app" / "astrbot" / "data" / "config" / f"{name}_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


def test_snapshot_names_single_memory_backend_without_internal_brand(tmp_path: Path) -> None:
    service = ExperienceService(tmp_path)

    result = service.snapshot()

    assert result["memory_backend"] == "长期记忆"
    assert "astrbot" not in json.dumps(result, ensure_ascii=False).casefold()
    assert result["memory"]["recall_mode"] == "smart"
    assert result["readiness"]["memory"] is False


def test_save_persona_preserves_advanced_plugin_fields_when_offline(tmp_path: Path) -> None:
    path = _plugin(
        tmp_path,
        "astrbot_plugin_angel_heart",
        {"timing": {"waiting_time": 7}, "personality": {"advanced": "keep"}},
    )
    service = ExperienceService(tmp_path)
    service.engine.is_available = lambda: False

    result = service.save_persona(
        {
            "tone": "warm",
            "reply_length": "balanced",
            "mention_only": True,
            "fast_reply": True,
            "alias": "小妹",
            "identity_prompt": "你是小妹。",
            "reply_guide": "被点名时回应。",
        }
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result["apply_state"] == "pending_start"
    assert saved["timing"]["waiting_time"] == 7
    assert saved["personality"]["advanced"] == "keep"
    assert saved["personality"]["reply_strategy_guide"].startswith("语气温柔")
    assert "一次只处理当前最相关的话题" in saved["personality"]["reply_strategy_guide"]
    assert result["reply_guide"] == "被点名或引用时回应；一次只处理当前最相关的话题，不抢话，不连续刷屏。"
    assert saved["reply_length"]["normal_reply_max_chars"] == 80
    assert saved["wake_interaction"]["direct_wake_fast_path"] is True


def test_save_memory_maps_manual_and_explicit_recall(tmp_path: Path) -> None:
    path = _plugin(tmp_path, "astrbot_plugin_livingmemory", {"graph_memory": {"enabled": False}})
    service = ExperienceService(tmp_path)
    service.engine.is_available = lambda: False

    service.save_memory(
        {
            "enabled": True,
            "recall_mode": "manual",
            "explicit_recall_enabled": True,
            "explicit_recall_phrases": "上次说了什么，你还记得吗",
            "casual_query_max_chars": 30,
            "top_k": 4,
            "summary_trigger_rounds": 12,
        }
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["recall_engine"]["auto_recall_mode"] == "manual"
    assert saved["recall_engine"]["explicit_recall_phrases"] == ["上次说了什么", "你还记得吗"]
    assert saved["reflection_engine"]["summary_trigger_rounds"] == 12
    assert saved["graph_memory"] == {"enabled": False}


def test_save_emotion_forces_compact_hot_context(tmp_path: Path) -> None:
    path = _plugin(tmp_path, "astrbot_plugin_self_learning", {"Runtime_Internal_Settings": {"other": 1}})
    service = ExperienceService(tmp_path)
    service.engine.is_available = lambda: False

    service.save_emotion(
        {"enabled": True, "daily_mood": False, "include_affection": True, "include_mood": False}
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["Runtime_Internal_Settings"]["enable_llm_hooks"] is True
    assert saved["Runtime_Internal_Settings"]["llm_hook_profile"] == "compact"
    assert saved["Runtime_Internal_Settings"]["other"] == 1
    assert saved["Social_Context_Settings"]["include_mood_info"] is False
    assert saved["Self_Learning_Basic"]["enable_message_capture"] is False
    assert saved["Self_Learning_Basic"]["enable_auto_learning"] is False
    assert saved["Advanced_Settings"]["save_raw_messages"] is False


def test_snapshot_detects_enabled_embedding_provider(tmp_path: Path) -> None:
    path = tmp_path / "app" / "astrbot" / "data" / "cmd_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "provider": [
                    {"id": "reply-model", "type": "chat_completion", "enable": True},
                    {"id": "text-embedding", "type": "embedding", "enable": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert ExperienceService(tmp_path).snapshot()["readiness"]["memory"] is True


def test_memory_readiness_degrades_safely_when_provider_config_is_invalid(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app" / "astrbot" / "data" / "cmd_config.json"
    path.parent.mkdir(parents=True)
    path.write_text("{invalid json", encoding="utf-8")

    assert ExperienceService(tmp_path).snapshot()["readiness"]["memory"] is False


def test_invalid_memory_limits_do_not_write(tmp_path: Path) -> None:
    path = _plugin(tmp_path, "astrbot_plugin_livingmemory", {})
    service = ExperienceService(tmp_path)
    service.engine.is_available = lambda: False

    with pytest.raises(ExperienceError, match="回忆条数"):
        service.save_memory({"top_k": 99})

    assert json.loads(path.read_text(encoding="utf-8")) == {}
