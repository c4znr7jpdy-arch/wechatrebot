"""Local service port settings shared by the manager and runtime guard."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


def _load_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"现有配置无法读取：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"现有配置根节点必须是 JSON 对象：{path}")
    return payload


@dataclass(frozen=True, slots=True)
class RuntimePorts:
    dashboard: int = 6185
    astrbot_ws: int = 6200
    bridge_callback: int = 5000
    hook_api: int = 19088

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        values = asdict(self)
        for name, value in values.items():
            if type(value) is not int or not 1 <= value <= 65535:
                errors.append(f"{name} 端口超出范围。")
        if not errors and len(set(values.values())) != len(values):
            errors.append("各本地服务端口不能重复。")
        return tuple(errors)


def load_runtime_ports(install_root: str | Path) -> RuntimePorts:
    """Load this installation's ports; invalid saved values must not fall back."""
    root = Path(install_root).expanduser().resolve()
    saved_ports = _load_object(root / "config" / "manager.json").get("ports", {})
    if not isinstance(saved_ports, dict):
        raise ValueError("现有本地服务端口配置格式不正确。")
    try:
        ports = RuntimePorts(**saved_ports)
    except TypeError as exc:
        raise ValueError("现有本地服务端口配置格式不正确。") from exc
    errors = ports.validate()
    if errors:
        raise ValueError("；".join(errors))
    return ports


__all__ = ["RuntimePorts", "load_runtime_ports"]
