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


def test_086_delivery_includes_group_analysis_plugin() -> None:
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

    module = "astrbot_plugin_qq_group_daily_analysis"
    feature = next(item for item in features if item["module"] == module)
    plugin = next(item for item in plugins if item["name"] == module)

    assert feature["display_name"] == "群聊分析"
    assert feature["configuration"] == "schema"
    assert plugin["version"] == "4.10.9.3"
    assert plugin["license"] == "MIT"
    assert any(
        item.get("source") == f"data/plugins/{module}"
        and item.get("target") == f"app/astrbot/data/plugins/{module}"
        for item in sources
    )
