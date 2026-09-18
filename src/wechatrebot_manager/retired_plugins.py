from __future__ import annotations

import json
from pathlib import Path
import shutil

from .feature_policy import RETIRED_PLUGIN_MODULES
from .json_io import write_json_atomic


def _delete_exact_path(path: Path, *, parent: Path) -> bool:
    if path.parent != parent:
        raise ValueError("退役功能清理路径越界。")
    if path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    ):
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    if path.exists():
        path.unlink()
        return True
    return False


def purge_retired_plugins(install_root: str | Path) -> tuple[str, ...]:
    """Delete retired plugin code, configuration and local data from an install."""
    root = Path(install_root).expanduser().resolve()
    data_root = root / "app" / "astrbot" / "data"
    plugin_root = data_root / "plugins"
    config_root = data_root / "config"
    plugin_data_root = data_root / "plugin_data"
    source_root = root / "legal" / "corresponding-source"
    removed: list[str] = []

    for module in sorted(RETIRED_PLUGIN_MODULES):
        targets = (
            (plugin_root / module, plugin_root),
            (config_root / f"{module}_config.json", config_root),
            (plugin_data_root / module, plugin_data_root),
            (source_root / f"astrbot-plugin-{module}.patch", source_root),
        )
        for path, parent in targets:
            if _delete_exact_path(path, parent=parent):
                removed.append(path.relative_to(root).as_posix())

    engine_path = data_root / "cmd_config.json"
    try:
        engine = json.loads(engine_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        engine = None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("退役功能清理时无法读取运行配置。") from exc
    if isinstance(engine, dict) and isinstance(engine.get("plugin_set"), list):
        configured = engine["plugin_set"]
        filtered = [
            name for name in configured
            if str(name) not in RETIRED_PLUGIN_MODULES
        ]
        if filtered != configured:
            engine["plugin_set"] = filtered
            write_json_atomic(engine_path, engine)
            removed.append("app/astrbot/data/cmd_config.json:plugin_set")

    return tuple(removed)


__all__ = ["purge_retired_plugins"]
