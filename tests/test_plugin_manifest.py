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
    assert all(item["gate"] for item in payload["plugins"])


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
