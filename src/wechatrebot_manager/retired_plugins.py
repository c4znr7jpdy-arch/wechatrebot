from __future__ import annotations

import json
from pathlib import Path
import shutil

from .feature_policy import RETIRED_PLUGIN_MODULES
from .json_io import write_json_atomic
from .payload import PayloadError, _checked_target


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

    targets = [
        target
        for module in sorted(RETIRED_PLUGIN_MODULES)
        for target in (
            (plugin_root / module, plugin_root),
            (config_root / f"{module}_config.json", config_root),
            (plugin_data_root / module, plugin_data_root),
            (source_root / f"astrbot-plugin-{module}.patch", source_root),
        )
    ]
    engine_path = data_root / "cmd_config.json"
    # Validate every parent before deleting anything, including ancestors that
    # could redirect a lexically correct child into another installation.
    try:
        for parent in {parent for _, parent in targets}:
            _checked_target(root, parent.relative_to(root), allow_directory=True)
        _checked_target(root, engine_path.relative_to(root))
    except PayloadError as exc:
        raise ValueError(str(exc)) from exc
    try:
        engine = json.loads(engine_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        engine = {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("退役功能清理时无法读取运行配置。") from exc
    if not isinstance(engine, dict):
        raise ValueError("退役功能清理时运行配置格式不正确。")
    if isinstance(engine.get("plugin_set"), list):
        configured = engine["plugin_set"]
        filtered = [
            name for name in configured
            if str(name) not in RETIRED_PLUGIN_MODULES
        ]
        if filtered != configured:
            engine["plugin_set"] = filtered
            write_json_atomic(engine_path, engine)
            removed.append("app/astrbot/data/cmd_config.json:plugin_set")

    for path, parent in targets:
        if _delete_exact_path(path, parent=parent):
            removed.append(path.relative_to(root).as_posix())

    return tuple(removed)


__all__ = ["purge_retired_plugins"]
