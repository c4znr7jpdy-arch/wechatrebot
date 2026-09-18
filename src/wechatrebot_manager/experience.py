from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable

from .json_io import write_json_atomic
from .plugin_center import EngineApiClient, EngineUnavailableError, PluginCenterError


class ExperienceError(ValueError):
    """Raised when a friendly assistant setting cannot be applied safely."""


ANGEL_HEART_MODULE = "astrbot_plugin_angel_heart"
LIVING_MEMORY_MODULE = "astrbot_plugin_livingmemory"
EMOTION_MODULE = "astrbot_plugin_self_learning"

_DEFAULT_STATE: dict[str, Any] = {
    "schema_version": 1,
    "onboarding_complete": False,
    "persona": {
        "tone": "natural",
        "reply_length": "brief",
        "mention_only": False,
        "fast_reply": True,
        "alias": "AI|助手",
        "identity_prompt": "你是群聊里的熟人型助手，说话自然、直接，不使用机械的客服措辞。",
        "reply_guide": "被点名或引用时回应；一次只处理当前最相关的话题，不抢话，不连续刷屏。",
    },
    "memory": {
        "enabled": True,
        "recall_mode": "smart",
        "casual_query_max_chars": 24,
        "top_k": 3,
        "summary_trigger_rounds": 10,
    },
    "emotion": {
        "enabled": True,
        "daily_mood": True,
        "include_affection": True,
        "include_mood": True,
    },
}

_REPLY_LENGTHS = {
    "brief": (28, 180),
    "balanced": (80, 300),
    "detailed": (180, 600),
}

_TONE_GUIDES = {
    "natural": "语气自然直接，像熟悉的群友，不使用机械客服话术。",
    "warm": "语气温柔、有耐心，但不过度撒娇，不替用户做情感判断。",
    "professional": "语气清楚可靠，先给结论，再补充必要信息。",
    "lively": "语气轻松活泼，可以适度幽默，但不抢话、不连续刷屏。",
}


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperienceError(f"设置文件无法读取：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ExperienceError(f"设置文件格式不正确：{path.name}")
    return payload


def _nested(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, dict):
        value = {}
        config[key] = value
    return value


def _bounded_int(value: object, *, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ExperienceError(f"{label}需要填写整数。") from exc
    if not minimum <= parsed <= maximum:
        raise ExperienceError(f"{label}需要在 {minimum} 到 {maximum} 之间。")
    return parsed


class ExperienceService:
    """Friendly controls for persona, emotion and the single memory backend."""

    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.path = self.install_root / "config" / "assistant-experience.json"
        self.engine = EngineApiClient(self.install_root)

    def _plugin_path(self, module_name: str) -> Path:
        return (
            self.install_root
            / "app"
            / "astrbot"
            / "data"
            / "config"
            / f"{module_name}_config.json"
        )

    def _plugin_exists(self, module_name: str) -> bool:
        return (
            self.install_root / "app" / "astrbot" / "data" / "plugins" / module_name
        ).is_dir() or self._plugin_path(module_name).is_file()

    def _state(self) -> dict[str, Any]:
        saved = _read_object(self.path)
        state = deepcopy(_DEFAULT_STATE)
        for key in ("persona", "memory", "emotion"):
            value = saved.get(key)
            if isinstance(value, dict):
                state[key].update(value)
        # Remove fields from the retired custom trigger-phrase control when
        # reading state written by an older manager version.
        state["memory"].pop("explicit_recall_enabled", None)
        state["memory"].pop("explicit_recall_phrases", None)
        state["onboarding_complete"] = bool(saved.get("onboarding_complete", False))
        return state

    def _memory_ready(self) -> bool:
        try:
            dashboard = _read_object(
                self.install_root / "app" / "astrbot" / "data" / "cmd_config.json"
            )
        except ExperienceError:
            return False
        providers = dashboard.get("provider")
        if not isinstance(providers, list):
            return False
        return any(
            isinstance(item, dict)
            and item.get("enable") is not False
            and "embedding" in " ".join(
                str(item.get(key) or "")
                for key in ("type", "provider_type", "id", "model")
            ).casefold()
            for item in providers
        )

    def _write_state(self, state: dict[str, Any]) -> None:
        write_json_atomic(self.path, state)

    def _runtime_config(self, module_name: str) -> tuple[dict[str, Any], str | None, bool]:
        try:
            if self.engine.is_available():
                snapshot = self.engine.get_config(module_name)
                return deepcopy(snapshot["config"]), str(snapshot["revision"]), True
        except EngineUnavailableError:
            pass
        path = self._plugin_path(module_name)
        if not self._plugin_exists(module_name):
            raise ExperienceError("当前安装版本还没有这项能力，请先安装完整软件更新。")
        return _read_object(path), None, False

    def _apply_plugin(
        self,
        module_name: str,
        update: Callable[[dict[str, Any]], None],
    ) -> str:
        config, revision, online = self._runtime_config(module_name)
        update(config)
        if online:
            receipt = self.engine.save_config(module_name, config, revision)
            return str(receipt.get("state") or "applied")
        write_json_atomic(self._plugin_path(module_name), config)
        return "pending_start"

    def snapshot(self) -> dict[str, Any]:
        state = self._state()
        state["capabilities"] = {
            "persona": self._plugin_exists(ANGEL_HEART_MODULE),
            "memory": self._plugin_exists(LIVING_MEMORY_MODULE),
            "emotion": self._plugin_exists(EMOTION_MODULE),
        }
        state["readiness"] = {"memory": self._memory_ready()}
        state["memory_backend"] = "长期记忆"
        state["memory_explanation"] = (
            "聊天摘要和相关往事由长期记忆保存与召回；明确问起上次内容时优先回忆，普通寒暄不查询。"
        )
        return state

    def save_persona(self, values: dict[str, Any]) -> dict[str, Any]:
        state = self._state()
        tone = str(values.get("tone") or "natural").strip()
        if tone not in {"natural", "warm", "professional", "lively"}:
            raise ExperienceError("请选择有效的说话风格。")
        reply_length = str(values.get("reply_length") or "brief").strip()
        if reply_length not in _REPLY_LENGTHS:
            raise ExperienceError("请选择有效的回复长度。")
        alias = str(values.get("alias") or "").strip()
        identity = str(values.get("identity_prompt") or "").strip()
        guide = str(_DEFAULT_STATE["persona"]["reply_guide"])
        if not alias:
            raise ExperienceError("请至少填写一个机器人昵称。")
        if not identity:
            raise ExperienceError("请填写机器人的身份定位。")
        if len(identity) > 3000:
            raise ExperienceError("身份定位不能超过 3000 字。")
        persona = {
            "tone": tone,
            "reply_length": reply_length,
            # Per-group participation is owned by Message Management. Keep the
            # underlying coordinator receptive, then let the outbound group
            # policy enforce mention-only, natural, or off for each group.
            "mention_only": False,
            "fast_reply": True,
            "alias": alias,
            "identity_prompt": identity,
            "reply_guide": guide,
        }

        def update(config: dict[str, Any]) -> None:
            personality = _nested(config, "personality")
            personality["ai_self_identity"] = identity
            personality["reply_strategy_guide"] = f"{_TONE_GUIDES[tone]}\n{guide}".strip()
            wake = _nested(config, "wake_interaction")
            wake["alias"] = alias
            wake["enter_on_mention_only"] = persona["mention_only"]
            wake["direct_wake_fast_path"] = persona["fast_reply"]
            normal, focus = _REPLY_LENGTHS[reply_length]
            lengths = _nested(config, "reply_length")
            lengths["normal_reply_max_chars"] = normal
            lengths["focus_reply_max_chars"] = max(focus, normal)

        apply_state = self._apply_plugin(ANGEL_HEART_MODULE, update)
        state["persona"] = persona
        self._write_state(state)
        return {**persona, "apply_state": apply_state}

    def save_memory(self, values: dict[str, Any]) -> dict[str, Any]:
        state = self._state()
        enabled = bool(values.get("enabled", True))
        recall_mode = str(values.get("recall_mode") or "smart").strip()
        if recall_mode not in {"smart", "always", "manual"}:
            raise ExperienceError("请选择有效的回忆方式。")
        memory = {
            "enabled": enabled,
            "recall_mode": recall_mode,
            "casual_query_max_chars": _bounded_int(
                values.get("casual_query_max_chars", 24),
                minimum=4,
                maximum=100,
                label="短寒暄长度",
            ),
            "top_k": _bounded_int(values.get("top_k", 3), minimum=1, maximum=10, label="回忆条数"),
            "summary_trigger_rounds": _bounded_int(
                values.get("summary_trigger_rounds", 10),
                minimum=2,
                maximum=100,
                label="记忆整理轮次",
            ),
        }

        def update(config: dict[str, Any]) -> None:
            recall = _nested(config, "recall_engine")
            recall["auto_recall_mode"] = "off" if not enabled else recall_mode
            recall.pop("explicit_recall_phrases", None)
            recall["casual_query_max_chars"] = memory["casual_query_max_chars"]
            recall["top_k"] = memory["top_k"]
            reflection = _nested(config, "reflection_engine")
            reflection["summary_trigger_rounds"] = memory["summary_trigger_rounds"]

        apply_state = self._apply_plugin(LIVING_MEMORY_MODULE, update)
        state["memory"] = memory
        self._write_state(state)
        return {**memory, "apply_state": apply_state}

    def save_emotion(self, values: dict[str, Any]) -> dict[str, Any]:
        state = self._state()
        emotion = {
            "enabled": bool(values.get("enabled", True)),
            "daily_mood": bool(values.get("daily_mood", True)),
            "include_affection": bool(values.get("include_affection", True)),
            "include_mood": bool(values.get("include_mood", True)),
        }

        def update(config: dict[str, Any]) -> None:
            learning = _nested(config, "Self_Learning_Basic")
            for field in (
                "enable_message_capture",
                "enable_auto_learning",
                "enable_realtime_learning",
                "enable_jargon_learning",
                "enable_style_learning",
                "enable_realtime_llm_filter",
            ):
                learning[field] = False
            advanced = _nested(config, "Advanced_Settings")
            advanced["save_raw_messages"] = False
            advanced["auto_apply_approved_persona"] = False
            affection = _nested(config, "Affection_System_Settings")
            affection["enable_affection_system"] = emotion["enabled"]
            affection["daily_mood_change"] = emotion["daily_mood"]
            mood = _nested(config, "Mood_System_Settings")
            mood["enable_daily_mood"] = emotion["enabled"] and emotion["daily_mood"]
            social = _nested(config, "Social_Context_Settings")
            social["enable_social_context_injection"] = emotion["enabled"]
            social["include_affection_info"] = emotion["enabled"] and emotion["include_affection"]
            social["include_mood_info"] = emotion["enabled"] and emotion["include_mood"]
            runtime = _nested(config, "Runtime_Internal_Settings")
            runtime["enable_llm_hooks"] = emotion["enabled"]
            runtime["llm_hook_profile"] = "compact"

        apply_state = self._apply_plugin(EMOTION_MODULE, update)
        state["emotion"] = emotion
        self._write_state(state)
        return {**emotion, "apply_state": apply_state}

    def finish_onboarding(self) -> dict[str, Any]:
        state = self._state()
        state["onboarding_complete"] = True
        self._write_state(state)
        return {"onboarding_complete": True}


__all__ = [
    "ANGEL_HEART_MODULE",
    "EMOTION_MODULE",
    "ExperienceError",
    "ExperienceService",
    "LIVING_MEMORY_MODULE",
]
