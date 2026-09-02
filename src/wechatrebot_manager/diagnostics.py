from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any

from .install_paths import INSTALL_MARKER
from .launcher import port_accepting
from .payload import sha256_file
from .wechat_path import validate_wechat_executable


_SENSITIVE_KEY = re.compile(
    r"(?:api.?key|authorization|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_PRIVATE_IDENTIFIER_KEY = re.compile(
    r"(?:admins?_id|wxid|group.?id|user.?id)",
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
    if _SENSITIVE_KEY.search(key) or _PRIVATE_IDENTIFIER_KEY.search(key):
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


def _replace_case_insensitive(value: str, source: str, replacement: str) -> str:
    if not source:
        return value
    result = value
    while True:
        index = result.casefold().find(source.casefold())
        if index < 0:
            return result
        result = result[:index] + replacement + result[index + len(source) :]


def redact_local_paths(value: Any, replacements: dict[str, str]) -> Any:
    normalized = sorted(
        ((str(source), target) for source, target in replacements.items() if source),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    if isinstance(value, dict):
        return {
            str(key): redact_local_paths(child, replacements)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_local_paths(item, replacements) for item in value]
    if isinstance(value, str):
        result = value
        for source, replacement in normalized:
            result = _replace_case_insensitive(result, source, replacement)
        return result
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


def _installed_payload_summary(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    files = payload.get("files")
    return {
        "schema_version": payload.get("schema_version"),
        "source_manifest_status": payload.get("source_manifest_status"),
        "file_count": len(files) if isinstance(files, list) else None,
        "file_list_valid": isinstance(files, list),
        "manifest_size": path.stat().st_size,
        "manifest_sha256": sha256_file(path),
    }


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
        ("astrbot_config", root / "app" / "astrbot" / "data" / "cmd_config.json"),
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
            captured[name] = (
                _installed_payload_summary(path, payload)
                if name == "installed_payload"
                else redact_sensitive(payload)
            )

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
        ("new-hook-integrity-manifest", root / "legal" / "hook-binaries.json"),
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
    wechat_exe = manager.get("wechat_exe_path") if isinstance(manager, dict) else None
    if isinstance(wechat_exe, str) and wechat_exe.strip():
        item = validate_wechat_executable(wechat_exe)
        checks.append(
            DiagnosticCheck(
                "wechat-executable",
                item.supported,
                "error",
                "微信路径、版本与架构受支持"
                if item.supported
                else "；".join(item.errors),
            )
        )
    else:
        checks.append(
            DiagnosticCheck("wechat-executable", False, "error", "未配置微信主程序路径")
        )
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
    report = {
        "schema_version": 1,
        "ok": error_count == 0,
        "install_root": "<INSTALL_ROOT>",
        "summary": {
            "error_count": error_count,
            "warning_count": warning_count,
            "check_count": len(checks),
        },
        "checks": [asdict(item) for item in checks],
        "configuration": captured,
    }
    replacements = {
        str(root): "<INSTALL_ROOT>",
        str(Path.home()): "<USER_HOME>",
    }
    if isinstance(wechat_exe, str):
        replacements[wechat_exe] = "<WECHAT_EXE>"
    return redact_local_paths(report, replacements)


def diagnostic_error_messages(report: dict[str, Any]) -> tuple[str, ...]:
    checks = report.get("checks")
    if not isinstance(checks, list):
        return ("诊断报告格式错误。",)
    return tuple(
        f"{item.get('name', 'unknown')}：{item.get('message', '检查失败')}"
        for item in checks
        if isinstance(item, dict)
        and not item.get("ok")
        and item.get("severity") == "error"
    )
