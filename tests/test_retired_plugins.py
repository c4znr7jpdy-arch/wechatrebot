import json
import pytest

from wechatrebot_manager.retired_plugins import purge_retired_plugins


MODULE = "astrbot_plugin_mnemosyne"


@pytest.mark.parametrize("corrupt", ["{broken", "[]", "null"])
def test_retired_cleanup_validates_config_before_removing_files(tmp_path, corrupt):
    data = tmp_path / "app/astrbot/data"
    plugin = data / "plugins" / MODULE
    plugin.mkdir(parents=True)
    (plugin / "main.py").write_text("old plugin")
    (data / "cmd_config.json").write_text(corrupt, encoding="utf-8")
    with pytest.raises(ValueError, match="运行配置"):
        purge_retired_plugins(tmp_path)
    assert (plugin / "main.py").read_text() == "old plugin"


def test_retired_cleanup_write_failure_keeps_plugin_files(tmp_path, monkeypatch):
    from wechatrebot_manager import retired_plugins

    data = tmp_path / "app/astrbot/data"
    plugin = data / "plugins" / MODULE
    plugin.mkdir(parents=True)
    (plugin / "main.py").write_text("old plugin")
    engine = data / "cmd_config.json"
    engine.write_text(json.dumps({"plugin_set": [MODULE]}))
    original = engine.read_bytes()
    def fail(*args):
        raise PermissionError("config is locked")
    monkeypatch.setattr(retired_plugins, "write_json_atomic", fail)
    with pytest.raises(PermissionError):
        purge_retired_plugins(tmp_path)
    assert (plugin / "main.py").read_text() == "old plugin"
    assert engine.read_bytes() == original


def test_retired_cleanup_rejects_redirected_parent_before_any_deletion(tmp_path):
    root = tmp_path / "install"
    data = root / "app/astrbot/data"
    plugin = data / "plugins" / MODULE
    plugin.mkdir(parents=True)
    (plugin / "main.py").write_text("old plugin")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / f"{MODULE}_config.json"
    secret.write_text("external data")
    try:
        (data / "config").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    with pytest.raises(ValueError, match="符号链接|目录联接"):
        purge_retired_plugins(root)
    assert secret.read_text() == "external data"
    assert (plugin / "main.py").read_text() == "old plugin"


def test_retired_cleanup_removes_junction_only_and_keeps_active_plugins(tmp_path):
    winapi = pytest.importorskip("_winapi")
    data = tmp_path / "install/app/astrbot/data"
    plugins = data / "plugins"
    plugins.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.py").write_text("external plugin")
    winapi.CreateJunction(str(outside), str(plugins / MODULE))
    active = plugins / "astrbot_plugin_livingmemory"
    active.mkdir()
    engine = data / "cmd_config.json"
    engine.write_text(json.dumps({"plugin_set": [MODULE, active.name]}))
    result = purge_retired_plugins(tmp_path / "install")
    assert f"app/astrbot/data/plugins/{MODULE}" in result
    assert not (plugins / MODULE).exists()
    assert (outside / "main.py").read_text() == "external plugin"
    assert active.is_dir()
    assert json.loads(engine.read_text())["plugin_set"] == [active.name]
    assert purge_retired_plugins(tmp_path / "install") == ()
