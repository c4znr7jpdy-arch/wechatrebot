from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re


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


@dataclass(frozen=True, slots=True)
class DeliveryFeature:
    module: str
    display_name: str
    configuration: str
    description: str = ""

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
        if not _MODULE_NAME.fullmatch(module) or not display_name:
            raise ValueError("交付功能清单包含无效的功能名称。")
        if configuration not in FEATURE_CONFIGURATION_MODES:
            raise ValueError(f"{display_name} 的配置方式无效。")
        if module in seen:
            raise ValueError(f"交付功能清单重复登记了 {display_name}。")
        seen.add(module)
        result.append(
            DeliveryFeature(
                module=module,
                display_name=display_name,
                configuration=configuration,
                description=description,
            )
        )
    return tuple(result)


__all__ = [
    "DeliveryFeature",
    "FEATURE_CONFIGURATION_MODES",
    "load_delivery_features",
]
