from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any

from .install_paths import INSTALL_MARKER
from .launcher import port_accepting


_SENSITIVE_KEY = re.compile(
    r"(?:api.?key|authorization|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    ok: bool
    severity: str
    message: str


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {
            str(child_key): redact_sensitive(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("***REDACTED***", value)
    return value


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, "文件不存在"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"无法读取：{exc}"
    if not isinstance(payload, dict):
        return None, "JSON 根节点不是对象"
    return payload, None


def collect_diagnostics(install_root: str | Path) -> dict[str, Any]:
    root = Path(install_root).expanduser().resolve()
    checks: list[DiagnosticCheck] = []
    captured: dict[str, Any] = {}

    marker, error = _load_json(root / INSTALL_MARKER)
    marker_ready = error is None and marker is not None and marker.get("state") == "ready"
    checks.append(
        DiagnosticCheck(
            "install-marker",
            marker_ready,
            "error",
            error or f"安装状态：{marker.get('state')!r}",
        )
    )
    if marker is not None:
        captured["install_marker"] = marker

    for name, path in (
        ("manager_config", root / "config" / "manager.json"),
        ("brand_config", root / "config" / "brand.json"),
        ("bridge_config", root / "app" / "bridge" / "config.json"),
        ("installed_payload", root / "config" / "installed-payload.json"),
    ):
        payload, config_error = _load_json(path)
        checks.append(
            DiagnosticCheck(
                name.replace("_", "-"),
                config_error is None,
                "error",
                config_error or "配置可读取",
            )
        )
        if payload is not None:
            captured[name] = redact_sensitive(payload)

    required_files = (
        ("python-runtime", root / "runtime" / "python" / "python.exe"),
        ("astrbot-root", root / "app" / "astrbot" / ".astrbot"),
        ("new-hook-injector", root / "app" / "bridge" / "vendor" / "hook" / "x64 inject.exe"),
        (
            "new-hook-library",
            root
            / "app"
            / "bridge"
            / "vendor"
            / "hook"
            / "libGLESv1-c2c-video-v2.dll",
        ),
    )
    for name, path in required_files:
        exists = path.is_file()
        checks.append(
            DiagnosticCheck(name, exists, "error", "文件存在" if exists else "文件缺失")
        )

    optional_media_tools = (
        ("ffmpeg", root / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe"),
        ("ffprobe", root / "runtime" / "ffmpeg" / "bin" / "ffprobe.exe"),
    )
    for name, path in optional_media_tools:
        exists = path.is_file()
        checks.append(
            DiagnosticCheck(
                name,
                exists,
                "warning",
                "文件存在" if exists else "未随包提供；视频转码功能可能不可用",
            )
        )

    manager = captured.get("manager_config", {})
    ports = manager.get("ports", {}) if isinstance(manager, dict) else {}
    port_state: dict[str, Any] = {}
    if isinstance(ports, dict):
        for name, value in ports.items():
            try:
                port = int(value)
                accepting = port_accepting("127.0.0.1", port)
                port_state[str(name)] = {"port": port, "accepting": accepting}
            except (TypeError, ValueError):
                port_state[str(name)] = {"port": value, "error": "端口格式错误"}
    captured["local_ports"] = port_state

    error_count = sum(not item.ok and item.severity == "error" for item in checks)
    warning_count = sum(not item.ok and item.severity == "warning" for item in checks)
    return {
        "schema_version": 1,
        "ok": error_count == 0,
        "install_root": str(root),
        "summary": {
            "error_count": error_count,
            "warning_count": warning_count,
            "check_count": len(checks),
        },
        "checks": [asdict(item) for item in checks],
        "configuration": captured,
    }
