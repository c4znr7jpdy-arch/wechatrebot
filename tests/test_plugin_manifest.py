import json
from pathlib import Path


def test_plugin_manifest_accounts_for_every_project_plugin() -> None:
    commercial_root = Path(__file__).parents[1]
    project_plugins = Path("E:/Project/data/plugins")
    if not project_plugins.is_dir():
        return
    payload = json.loads(
        (commercial_root / "release" / "plugin-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    ignored = set(payload["ignored_directories"])
    actual = {
        path.name for path in project_plugins.iterdir() if path.is_dir()
    } - ignored
    declared = {item["name"] for item in payload["plugins"]}
    assert actual == declared
    assert all("gate" not in item for item in payload["plugins"])


def test_commercial_astrbot_template_has_one_loopback_bridge() -> None:
    commercial_root = Path(__file__).parents[1]
    payload = json.loads(
        (commercial_root / "config" / "astrbot.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(payload["platform"]) == 1
    platform = payload["platform"][0]
    assert platform["id"] == "aiocqhttp_hook41827"
    assert platform["ws_reverse_host"] == "127.0.0.1"
    assert platform["ws_reverse_port"] == 6200
    assert payload["dashboard"]["host"] == "127.0.0.1"
    assert payload["dashboard"]["enable"] is True
    assert payload["dashboard"]["username"] == "local-console"


def test_delivery_manifests_install_every_non_excluded_plugin() -> None:
    commercial_root = Path(__file__).parents[1]
    features = json.loads(
        (commercial_root / "release" / "feature-set.json").read_text(
            encoding="utf-8"
        )
    )["features"]
    plugins = json.loads(
        (commercial_root / "release" / "plugin-manifest.json").read_text(
            encoding="utf-8"
        )
    )["plugins"]
    sources = json.loads(
        (commercial_root / "release" / "source-manifest.json").read_text(
            encoding="utf-8"
        )
    )["entries"]

    delivered = {
        item["name"] for item in plugins if item["ownership"] != "excluded"
    }
    excluded = {
        item["name"] for item in plugins if item["ownership"] == "excluded"
    }
    feature_modules = {item["module"] for item in features}
    source_modules = {
        Path(item["target"]).name
        for item in sources
        if str(item.get("target", "")).startswith("app/astrbot/data/plugins/")
        and Path(item["target"]).name != "_nonebot_stubs.py"
    }

    assert len(delivered) == 24
    assert delivered == feature_modules == source_modules
    assert excluded == {
        "astrbot_plugin_apis",
        "astrbot_plugin_hapi_connector_disabled",
        "astrbot_plugin_kimi_web_search",
        "astrbot_plugin_twitter",
        "astrbot_plugin_zhenxunribao",
        "jiang_12123_notify",
        "jiang_lol_match",
        "jiang_raid_signup",
        "jiang_server_status",
        "jiang_tide",
        "jiang_yanyun_checkin",
    }
    assert excluded.isdisjoint(feature_modules)
    assert excluded.isdisjoint(source_modules)

    template = json.loads(
        (commercial_root / "config" / "astrbot.example.json").read_text(
            encoding="utf-8"
        )
    )
    safe_defaults = {
        item["module"] for item in features if item.get("default_enabled", True)
    }
    assert set(template["plugin_set"]) == safe_defaults
    assert safe_defaults < delivered


def test_every_delivered_plugin_has_auditable_legal_metadata() -> None:
    commercial_root = Path(__file__).parents[1]
    plugins = json.loads(
        (commercial_root / "release" / "plugin-manifest.json").read_text(
            encoding="utf-8"
        )
    )["plugins"]

    delivered = [item for item in plugins if item["ownership"] != "excluded"]
    assert all(item.get("version") for item in delivered)
    assert all(item.get("license") for item in delivered)
    assert all(item.get("review_status") for item in delivered)
    assert all(
        item.get("license_file") == "LICENSE"
        for item in delivered
        if item["ownership"] == "third-party"
    )


def test_source_manifest_excludes_development_and_local_config_artifacts() -> None:
    commercial_root = Path(__file__).parents[1]
    entries = json.loads(
        (commercial_root / "release" / "source-manifest.json").read_text(
            encoding="utf-8"
        )
    )["entries"]
    by_source = {item["source"]: item for item in entries}

    assert "WINDOWS_TUTORIAL.md" in by_source[
        "data/plugins/astrbot_plugin_mnemosyne"
    ]["exclude"]
    assert "example.py" in by_source[
        "data/plugins/astrbot_plugin_parser"
    ]["exclude"]
    assert "config.json" in by_source[
        "data/plugins/meme_manager"
    ]["exclude"]


def test_project_video_plugin_follows_grok() -> None:
    project_plugins = Path("E:/Project/data/plugins")
    if not project_plugins.is_dir():
        return

    video_main = (
        project_plugins / "jiang_video_understanding" / "main.py"
    ).read_text(encoding="utf-8")
    assert '_DEFAULT_FALLBACK_PROVIDER_ID = "jyjapi/grok-4.6"' in video_main


def test_group_analysis_keeps_customer_facing_policy() -> None:
    commercial_root = Path(__file__).parents[1]
    features = json.loads(
        (commercial_root / "release" / "feature-set.json").read_text(
            encoding="utf-8"
        )
    )["features"]
    feature = next(
        item
        for item in features
        if item["module"] == "astrbot_plugin_qq_group_daily_analysis"
    )

    assert feature["display_name"] == "群聊分析"
    assert feature["configuration"] == "schema"
    assert feature["default_enabled"] is False


def test_new_install_enables_only_low_setup_group_basics() -> None:
    commercial_root = Path(__file__).parents[1]
    features = json.loads(
        (commercial_root / "release" / "feature-set.json").read_text(
            encoding="utf-8"
        )
    )["features"]
    enabled = {
        item["module"] for item in features if item.get("default_enabled", True)
    }

    assert enabled == {
        "astrbot_plugin_angel_heart",
        "astrbot_plugin_bittorrent",
        "astrbot_plugin_livingmemory",
        "astrbot_plugin_music",
        "astrbot_plugin_parser",
        "astrbot_plugin_patch_at_wxid",
        "astrbot_plugin_self_learning",
        "jiang_capability_router",
        "jiang_commands",
        "jiang_video_understanding",
        "meme_manager",
    }
