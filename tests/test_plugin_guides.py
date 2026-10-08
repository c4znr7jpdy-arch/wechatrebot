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
    delivered_modules = {row["module"] for row in features}
    virtual_features = {"jiang_commands.setu"}
    assert set(_GUIDES) == delivered_modules | virtual_features
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


def test_common_commands_guide_exposes_grouped_customer_commands():
    guide = get_plugin_guide("jiang_commands")

    assert [group["title"] for group in guide["commands"]] == [
        "常用查询",
        "AI 互动",
        "趣味与游戏",
        "其他",
    ]
    assert guide["commands"][0]["items"][0] == ("/新闻", "抖音热榜（也支持定时发送）")
    assert guide["commands"][1]["items"] == (
        ("@机器人 帮我画 xxx", "文生图"),
        ("@机器人 P图/改图/图生图", "编辑图片（需引用图片）"),
    )
    rendered = [item[0] for group in guide["commands"] for item in group["items"]]
    assert all("\\" not in item for item in rendered)
    assert all("姜小妹" not in item for item in rendered)
    assert all(not any(word in item for word in ("燕云", "理想", "塔罗", "占卜")) for item in rendered)
    assert all("定时" not in item for item in rendered)
    assert all("订阅动态" not in item for item in rendered)
    assert all("启用" not in item and "禁用" not in item for item in rendered)
    assert all("插件列表" not in item for item in rendered)
    assert all("切换模型" not in item and "图片模型" not in item for item in rendered)
    assert all("语音" not in item for item in rendered)


def test_visible_features_have_quick_reference_except_dedicated_exclusions():
    root = Path(__file__).parents[1]
    features = json.loads((root / "release/feature-set.json").read_text("utf-8"))["features"]
    excluded = {"长期记忆", "语音克隆", "推特推送", "表情管理"}
    modules = [row["module"] for row in features if row.get("visible", True) and row["display_name"] not in excluded]
    for module in [*modules, "jiang_commands.setu"]:
        groups = get_plugin_guide(module).get("commands", ())
        assert groups, module
        assert all(group["title"] and group["items"] for group in groups), module
        assert all(command and description for group in groups for command, description in group["items"]), module


def test_quick_reference_is_detached_from_shared_guide_data():
    guide = get_plugin_guide("astrbot_plugin_bittorrent")
    guide["commands"][0]["title"] = "changed"
    assert get_plugin_guide("astrbot_plugin_bittorrent")["commands"][0]["title"] == "资源检索"


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
                patches = (Path(__file__).parents[1] / "patches").glob(
                    f"astrbot-plugin-{module}*.patch"
                )
                assert any(
                    f'"{parts[-1]}"' in patch.read_text(encoding="utf-8")
                    for patch in patches
                ), (module, path)


def test_projection_preserves_advanced_values_and_masks_secret_defaults():
    schema = {"primary_model": {"type": "string", "default": "image-model"},
              "primary_base_url": {"type": "string", "default": "https://images.example/v1"},
              "private_token": {"type": "string", "default": "never-show"}}
    projected = common_schema("jiang_image", schema)
    assert list(projected) == ["primary_model"]
    assert projected["primary_model"]["default"] == "image-model"
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


def test_short_drama_hides_product_managed_player_settings():
    schema = {
        "episode_player_public_base_url": {"type": "string"},
        "selection_store_api_url": {"type": "string"},
        "selection_store_api_token": {"type": "string", "secret": True},
    }

    assert common_schema("jiang_short_drama", schema) == {}
    assert advanced_schema("jiang_short_drama", schema) == {}


def test_link_parser_uses_defaults_and_only_exposes_the_feature_switch():
    schema = {
        "require_at_in_group": {"type": "bool", "default": False},
        "parsers_template": {"type": "template_list", "default": []},
    }

    assert common_schema("astrbot_plugin_parser", schema) == {}
    assert advanced_schema("astrbot_plugin_parser", schema) == {}


def test_image_generation_uses_unified_model_services_and_keeps_proxy_settings():
    raw_fields = (
        "primary_base_url", "primary_model", "primary_api_key",
        "secondary_enabled", "secondary_base_url", "secondary_model",
        "secondary_api_key", "use_system_proxy", "local_proxy_port",
    )
    visible_fields = (
        "primary_model", "secondary_enabled", "secondary_model",
        "use_system_proxy", "local_proxy_port",
    )
    schema = {key: {"type": "string", "default": ""} for key in raw_fields}

    projected = common_schema("jiang_image", schema)
    assert tuple(projected) == visible_fields
    hidden_credentials = {
        "primary_base_url", "primary_api_key", "secondary_base_url", "secondary_api_key"
    }
    assert not hidden_credentials & set(projected)
    assert projected["primary_model"]["_ui_group"] == "主生图模型"
    assert projected["secondary_enabled"]["_ui_disclosure"] == "添加备用模型"
    assert projected["secondary_model"]["_ui_depends_on"] == "secondary_enabled"
    assert projected["local_proxy_port"]["_ui_depends_on"] == "use_system_proxy"
    assert advanced_schema("jiang_image", schema) == {}
    clean_schema = _customer_schema(schema, {"primary_api_key": "stored-secret"})
    clean_config = _customer_config(schema, {"primary_api_key": "stored-secret"})
    assert clean_schema["primary_api_key"]["secret"] is True
    assert clean_schema["primary_api_key"]["default"] == ""
    assert clean_config["primary_api_key"] == ""


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
