import json
from pathlib import Path

from wechatrebot_manager.plugin_guides import (
    _ASSESSMENTS,
    _GUIDES,
    advanced_schema,
    common_schema,
    get_plugin_guide,
)
from wechatrebot_manager.plugin_center import _customer_config, _customer_schema


def test_all_delivered_features_have_actionable_guides():
    root = Path(__file__).parents[1]
    features = json.loads((root / "release/feature-set.json").read_text("utf-8"))["features"]
    assert set(_GUIDES) == {row["module"] for row in features}
    assert set(_ASSESSMENTS) == set(_GUIDES)
    for row in features:
        guide = get_plugin_guide(row["module"])
        assert guide["summary"] and guide["setup_steps"] and guide["prerequisites"]
        assert guide["audience"] and guide["readiness"] and guide["recommendation"]


def test_high_risk_and_misnamed_features_are_explained_truthfully():
    analysis = get_plugin_guide("astrbot_plugin_qq_group_daily_analysis")
    iterative = get_plugin_guide("astrbot_plugin_self_iterative_core")

    assert "本机消息记录" in analysis["readiness"]
    assert "当前机器人账号" in analysis["recommendation"]
    assert "不建议默认启用" == iterative["audience"]


def test_curated_paths_match_local_delivered_schemas():
    import pytest
    root = Path(__file__).parents[1] / "artifacts/staging-hot-config-validation-20260907-final/app/astrbot/data/plugins"
    if not root.exists():
        pytest.skip("Delivered source fixture unavailable")
    delivered_modules = [module for module in _GUIDES if (root / module).is_dir()]
    assert delivered_modules, "Delivered source fixture contains no guided modules"
    for module in delivered_modules:
        fields = get_plugin_guide(module)["common_fields"]
        if not fields:
            continue
        schema = json.loads(
            (root / module / "_conf_schema.json").read_text("utf-8-sig")
        )
        for path in fields:
            cursor = schema
            parts = path.split(".")
            for key in parts[:-1]:
                cursor = cursor[key]["items"]
            if parts[-1] not in cursor:
                patch = (
                    Path(__file__).parents[1]
                    / "patches"
                    / f"astrbot-plugin-{module}.patch"
                )
                assert patch.is_file() and f'"{parts[-1]}"' in patch.read_text(
                    encoding="utf-8"
                ), (module, path)


def test_projection_preserves_advanced_values_and_masks_secret_defaults():
    schema = {"require_at_in_group": {"type": "bool", "default": True},
              "private_token": {"type": "string", "default": "never-show"}}
    projected = common_schema("astrbot_plugin_parser", schema)
    assert list(projected) == ["require_at_in_group"]
    full = _customer_schema(schema, {"private_token": "stored"})
    assert full["private_token"]["default"] == ""
    assert _customer_config(schema, {"private_token": "stored"})["private_token"] == ""
    assert schema["private_token"]["default"] == "never-show"


def test_common_commands_hide_internal_service_fields_from_basic_settings():
    schema = {
        "content_api_id": {"type": "string"},
        "content_api_key": {"type": "string"},
        "weather_detail_token": {"type": "string"},
    }

    assert common_schema("jiang_commands", schema) == {}
    assert advanced_schema("jiang_commands", schema) == {}


def test_livingmemory_exposes_required_model_bindings():
    schema = {
        "provider_settings": {
            "type": "object",
            "items": {
                "embedding_provider_id": {"type": "string", "default": ""},
                "llm_provider_id": {
                    "type": "string",
                    "default": "",
                    "_special": "select_provider",
                },
            },
        },
        "recall_engine": {
            "type": "object",
            "items": {
                "auto_recall_mode": {"type": "string", "default": "smart"},
            },
        },
    }

    projected = common_schema("astrbot_plugin_livingmemory", schema)

    assert list(projected["provider_settings"]["items"]) == [
        "embedding_provider_id",
        "llm_provider_id",
    ]
