from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import json
from pathlib import Path
import re
import threading

from .json_io import write_json_atomic

FEATURE_CONFIGURATION_MODES = frozenset(
    {
        "none",
        "schema",
        "page",
        "schema-and-page",
        "page-and-global-schedule-center",
    }
)
_MODULE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

# These modules shipped in older full payloads but are no longer part of the
# customer product.  Manager-only updates cannot replace the installed legal
# manifest, so filter them here as well as removing them from future payloads.
RETIRED_PLUGIN_MODULES = frozenset({
    "astrbot_plugin_mnemosyne",
    "jiang_yanyun_checkin",
})

# The manager owns durable global policy. AstrBot removes unknown command
# configuration keys on startup, so the cmd key is only a live mirror.
GLOBAL_DISABLED_PLUGINS_KEY = "wechatrebot_disabled_plugins"
GLOBAL_PLUGIN_STATE_PATH = "data/manager/global-plugin-state.json"
_GLOBAL_STATE_LOCK = threading.RLock()

# These modules are part of the product's message and control plane. They must
# remain loaded so an administrator can recover a disabled customer feature.
PROTECTED_PLUGIN_MODULES = frozenset({
    "astrbot_plugin_angel_heart",
    "astrbot_plugin_patch_at_wxid",
    "jiang_capability_router",
})


def globally_disabled_plugins(config: Mapping[str, object]) -> frozenset[str]:
    """Read explicit customer feature disables, excluding protected modules."""
    values = config.get(GLOBAL_DISABLED_PLUGINS_KEY, [])
    if not isinstance(values, list):
        raise ValueError("全局功能开关配置格式不正确。")
    if any(not isinstance(item, str) or not _MODULE_NAME.fullmatch(item) for item in values):
        raise ValueError("全局功能开关包含无效的功能名称。")
    return frozenset(values) - PROTECTED_PLUGIN_MODULES - RETIRED_PLUGIN_MODULES


def global_plugin_state_path(install_root: str | Path) -> Path:
    return Path(install_root).expanduser().resolve() / GLOBAL_PLUGIN_STATE_PATH


def save_global_disabled_plugins(install_root: str | Path, modules: list[str]) -> None:
    disabled = globally_disabled_plugins({GLOBAL_DISABLED_PLUGINS_KEY: modules})
    with _GLOBAL_STATE_LOCK:
        write_json_atomic(global_plugin_state_path(install_root), {"disabled_plugins": sorted(disabled)})


def load_global_disabled_plugins(
    install_root: str | Path, legacy_config: Mapping[str, object] | None = None,
) -> frozenset[str]:
    """Read durable policy, migrating a legacy command marker once if present."""
    path = global_plugin_state_path(install_root)
    with _GLOBAL_STATE_LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            if legacy_config is None or GLOBAL_DISABLED_PLUGINS_KEY not in legacy_config:
                return frozenset()
            disabled = globally_disabled_plugins(legacy_config)
            save_global_disabled_plugins(install_root, sorted(disabled))
            return disabled
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("全局功能开关配置无法读取，请检查管理器数据。") from exc
        if not isinstance(payload, dict) or "disabled_plugins" not in payload:
            raise ValueError("全局功能开关配置格式不正确。")
        return globally_disabled_plugins({GLOBAL_DISABLED_PLUGINS_KEY: payload["disabled_plugins"]})


@dataclass(frozen=True, slots=True)
class DeliveryFeature:
    module: str
    display_name: str
    configuration: str
    description: str = ""
    default_enabled: bool = True
    visible: bool = True

    @property
    def exposes_schema(self) -> bool:
        return self.configuration in {"schema", "schema-and-page"}

    @property
    def exposes_pages(self) -> bool:
        return self.configuration in {
            "page",
            "schema-and-page",
            "page-and-global-schedule-center",
        }


def load_delivery_features(
    install_root: str | Path,
) -> tuple[DeliveryFeature, ...] | None:
    """Load the installed, signed delivery allowlist when one is present."""
    path = Path(install_root).expanduser().resolve() / "legal" / "feature-set.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("交付功能清单无法读取，请重新安装管理器。") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise ValueError("交付功能清单格式不正确，请重新安装管理器。")

    result: list[DeliveryFeature] = []
    seen: set[str] = set()
    for raw in payload["features"]:
        if not isinstance(raw, dict):
            raise ValueError("交付功能清单包含无效条目。")
        module = str(raw.get("module") or "").strip()
        display_name = str(raw.get("display_name") or "").strip()
        configuration = str(raw.get("configuration") or "").strip()
        description = str(raw.get("description") or "").strip()
        default_enabled = raw.get("default_enabled", True)
        visible = raw.get("visible", True)
        if not _MODULE_NAME.fullmatch(module) or not display_name:
            raise ValueError("交付功能清单包含无效的功能名称。")
        if module in RETIRED_PLUGIN_MODULES:
            continue
        if configuration not in FEATURE_CONFIGURATION_MODES:
            raise ValueError(f"{display_name} 的配置方式无效。")
        if not isinstance(default_enabled, bool):
            raise ValueError(f"{display_name} 的默认启用策略无效。")
        if not isinstance(visible, bool):
            raise ValueError(f"{display_name} 的可见性策略无效。")
        if module in seen:
            raise ValueError(f"交付功能清单重复登记了 {display_name}。")
        seen.add(module)
        result.append(
            DeliveryFeature(
                module=module,
                display_name=display_name,
                configuration=configuration,
                description=description,
                default_enabled=default_enabled,
                visible=visible,
            )
        )
    return tuple(result)


__all__ = [
    "DeliveryFeature",
    "FEATURE_CONFIGURATION_MODES",
    "GLOBAL_DISABLED_PLUGINS_KEY",
    "GLOBAL_PLUGIN_STATE_PATH",
    "PROTECTED_PLUGIN_MODULES",
    "RETIRED_PLUGIN_MODULES",
    "globally_disabled_plugins",
    "global_plugin_state_path",
    "load_global_disabled_plugins",
    "save_global_disabled_plugins",
    "load_delivery_features",
]
