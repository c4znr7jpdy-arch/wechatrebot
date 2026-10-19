"""Local plugin pages, manager assets and same-origin engine proxy handling."""
from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import sys
from typing import Any, TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from .customer_config import load_ui_theme
from .plugin_errors import PluginCenterError
from .plugin_schema_profiles import _ANGEL_HEART_MODULE

if TYPE_CHECKING:
    from .plugin_center import PluginCenterService


_PAGE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


_PLUGIN_EXTENSION_SEGMENT = re.compile(r"^[A-Za-z0-9_.~-]+$")


_PROXY_PREFIXES = (
    "/api/plugin/page/",
    "/api/plug/",
)


_MAX_BODY_BYTES = 4 * 1024 * 1024


_MAX_UPLOAD_BYTES = 12 * 1024 * 1024


_MAX_MEME_PACK_UPLOAD_BYTES = 120 * 1024 * 1024


_LOCAL_UI_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-src 'self'"
)


_MANAGER_UI_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob: https://wx.qlogo.cn; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; frame-src 'self'"
)


def _normalize_plugin_extension_endpoint(endpoint: str) -> str:
    value = str(endpoint or "").strip().strip("/")
    parts = value.split("/") if value else []
    if not parts or any(
        part in {"", ".", ".."} or not _PLUGIN_EXTENSION_SEGMENT.fullmatch(part)
        for part in parts
    ):
        raise PluginCenterError("高级配置页请求无效。")
    return "/".join(parts)


def _extension_body_limit(upstream_path: str, content_type: str) -> int:
    if not content_type.casefold().startswith("multipart/form-data;"):
        return _MAX_BODY_BYTES
    if urlsplit(upstream_path).path == "/api/plug/meme_manager/packs/import":
        return _MAX_MEME_PACK_UPLOAD_BYTES
    return _MAX_UPLOAD_BYTES


def _white_label_core_page_content(
    path: str,
    content_type: str,
    raw: bytes,
) -> bytes:
    """Rebrand the bundled core page without changing its internal API contract."""
    decoded_path = unquote(path)
    media_type = content_type.partition(";")[0].strip().casefold()
    if (
        _ANGEL_HEART_MODULE not in decoded_path
        or "/content/" not in decoded_path
        or media_type
        not in {
            "text/html",
            "text/css",
            "text/javascript",
            "application/javascript",
        }
    ):
        return raw
    for original, replacement in (
        ("天使之心", "机器人核心"),
        ("天使心", "机器人核心"),
        ("AngelHeart", "机器人核心"),
        ("kawayiYokami", "核心维护者"),
        ("插件设置", "核心设置"),
        ("助理", "机器人"),
        ("助手", "机器人"),
    ):
        raw = raw.replace(original.encode("utf-8"), replacement.encode("utf-8"))
    return raw


def _asset_root() -> Path:
    candidates = [Path(__file__).with_name("plugin_webui")]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.insert(0, Path(bundle_root) / "wechatrebot_manager" / "plugin_webui")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    raise FileNotFoundError("功能配置中心资源缺失，请重新安装管理器。")


def _handler(service: PluginCenterService, *, manager_assets: Path | None = None):
    assets = manager_assets if manager_assets is not None else _asset_root()
    manager_mode = manager_assets is not None
    shared_assets = assets if manager_mode else assets.with_name("app_webui")

    class Handler(BaseHTTPRequestHandler):
        server_version = "WeChatReBot"
        sys_version = ""

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_error(self, exc: Exception) -> None:
            status = int(getattr(exc, "status_code", HTTPStatus.INTERNAL_SERVER_ERROR))
            message = str(exc) if isinstance(exc, PluginCenterError) else "操作未完成，请稍后重试。"
            self._send_json(status, {"ok": False, "message": message})

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise PluginCenterError("请求长度无效。") from exc
            if length <= 0 or length > _MAX_BODY_BYTES:
                raise PluginCenterError("请求内容为空或过大。")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PluginCenterError("请求内容不是有效的 JSON。") from exc
            if not isinstance(value, dict):
                raise PluginCenterError("请求内容格式不正确。")
            return value

        def _require_same_origin(self) -> None:
            origin = self.headers.get("Origin")
            if not origin:
                return
            parsed = urlsplit(origin)
            host, port = self.server.server_address[:2]
            if (
                parsed.scheme != "http"
                or parsed.hostname != str(host)
                or parsed.port != int(port)
            ):
                raise PluginCenterError("请求来源无效。")

        def _parts(self) -> list[str]:
            return [unquote(part) for part in urlsplit(self.path).path.strip("/").split("/") if part]

        def _serve_asset(self, relative: str, content_type: str, *, root: Path | None = None) -> None:
            target = (root if root is not None else assets) / relative
            if not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面资源不存在。"})
                return
            raw = target.read_bytes()
            if manager_mode and root is None and relative == "index.html":
                theme = load_ui_theme(service.install_root)
                if theme:
                    raw = raw.replace(b'<html lang="zh-CN">', (
                        f'<html lang="zh-CN" data-saved-theme="{theme}" data-theme="{theme}">'
                    ).encode("utf-8"), 1)
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header(
                "Content-Security-Policy",
                _MANAGER_UI_CONTENT_SECURITY_POLICY
                if manager_mode
                else _LOCAL_UI_CONTENT_SECURITY_POLICY,
            )
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _proxy(self) -> None:
            self._require_same_origin()
            split = urlsplit(self.path)
            if not any(split.path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY_BYTES:
                raise PluginCenterError("请求内容过大。")
            body = self.rfile.read(length) if length else None
            upstream_path = split.path + (f"?{split.query}" if split.query else "")
            status, headers, raw = service.engine.raw_request(
                self.command,
                upstream_path,
                body=body,
                content_type=self.headers.get("Content-Type", "application/json"),
                accept_language=self.headers.get("Accept-Language", "zh-CN"),
            )
            normalized_headers = {key.casefold(): value for key, value in headers.items()}
            raw = _white_label_core_page_content(
                split.path,
                normalized_headers.get("content-type", ""),
                raw,
            )
            self.send_response(status)
            self._security_headers()
            for key in ("Content-Type", "Content-Security-Policy", "Permissions-Policy"):
                value = normalized_headers.get(key.casefold())
                if value:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _proxy_extension(self, public_id: str, endpoint: str) -> None:
            self._require_same_origin()
            split = urlsplit(self.path)
            upstream_path = service.extension_path(public_id, endpoint)
            if split.query:
                upstream_path = f"{upstream_path}?{split.query}"
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise PluginCenterError("请求长度无效。") from exc
            content_type = self.headers.get("Content-Type", "application/json")
            limit = _extension_body_limit(upstream_path, content_type)
            if length > limit:
                raise PluginCenterError("请求内容过大。")
            body = self.rfile.read(length) if length else None
            status, headers, raw = service.engine.raw_request(
                self.command,
                upstream_path,
                body=body,
                content_type=content_type,
                accept_language=self.headers.get("Accept-Language", "zh-CN"),
            )
            normalized_headers = {key.casefold(): value for key, value in headers.items()}
            self.send_response(status)
            self._security_headers()
            content_type = normalized_headers.get("content-type")
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                split = urlsplit(self.path)
                if split.path == "/plugin-center":
                    self._serve_asset("index.html", "text/html; charset=utf-8", root=_asset_root())
                    return
                if split.path in {"/", "/index.html"}:
                    self._serve_asset("index.html", "text/html; charset=utf-8")
                    return
                if split.path in {
                    "/theme-tokens.css",
                    "/feature-page-theme.js",
                    "/feature-page-overrides.css",
                    "/design-overrides.css",
                    "/provider-settings.css",
                    "/assets/AlimamaShuHeiTi-Bold.woff2",
                }:
                    suffix = Path(split.path).suffix
                    content_type = {
                        ".css": "text/css; charset=utf-8",
                        ".js": "text/javascript; charset=utf-8",
                        ".woff2": "font/woff2",
                    }[suffix]
                    self._serve_asset(split.path.lstrip("/"), content_type, root=shared_assets)
                    return
                if manager_mode and split.path in {
                    "/app.css",
                    "/app.js",
                    "/scheduled-tasks.js",
                    "/model-services.js",
                    "/chat-management.js",
                    "/chat-profiles.js",
                    "/settings-layout.css",
                    "/message-history.js",
                    "/features.js",
                    "/preview.js",
                    "/updates.js",
                    "/settings.js",
                    "/activity.js",
                    "/activity-errors.js",
                    "/activity.css",
                    "/core-settings.js",
                    "/date-range.js",
                    "/report-preview.js",
                    "/panel-layout.js",
                    "/subscription.js",
                    "/subscription.css",
                    # The opt-in soft skin, referenced unconditionally by index.html.
                    "/soft-theme.css",
                    "/soft-theme.js",
                }:
                    content_type = "text/css" if split.path.endswith(".css") else "text/javascript"
                    self._serve_asset(split.path.lstrip("/"), f"{content_type}; charset=utf-8")
                    return
                if split.path == "/assets/app.css":
                    self._serve_asset("app.css", "text/css; charset=utf-8", root=_asset_root())
                    return
                if split.path == "/assets/app.js":
                    self._serve_asset("app.js", "text/javascript; charset=utf-8", root=_asset_root())
                    return
                if manager_mode and split.path.startswith("/assets/"):
                    asset_name = split.path.removeprefix("/assets/")
                    if _PAGE_ID.fullmatch(asset_name):
                        asset_types = {
                            ".png": "image/png",
                            ".webp": "image/webp",
                            ".ico": "image/x-icon",
                            ".woff2": "font/woff2",
                        }
                        content_type = asset_types.get(Path(asset_name).suffix.lower())
                        if content_type:
                            self._serve_asset(f"assets/{asset_name}", content_type)
                            return
                if split.path in {
                    "/assets/AlibabaPuHuiTi2-Regular.woff2",
                    "/assets/AlibabaPuHuiTi2-Bold.woff2",
                }:
                    self._serve_asset(split.path.lstrip("/"), "font/woff2")
                    return
                if any(split.path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                parts = self._parts()
                if parts == ["control", "catalog"]:
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": service.catalog()})
                    return
                if len(parts) == 3 and parts[:2] == ["control", "plugins"]:
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": service.detail(parts[2])})
                    return
                if len(parts) == 4 and parts[:2] == ["control", "plugins"] and parts[3] == "pack-storage":
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": service.meme_pack_storage(parts[2])})
                    return
                if len(parts) == 6 and parts[:2] == ["control", "plugins"] and parts[3] == "pages" and parts[5] == "entry":
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": service.page_entry(parts[2], parts[4])})
                    return
                if len(parts) >= 5 and parts[:2] == ["control", "plugins"] and parts[3] == "extension":
                    self._proxy_extension(parts[2], "/".join(parts[4:]))
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
            except Exception as exc:
                self._send_error(exc)

        def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                self._require_same_origin()
                if any(urlsplit(self.path).path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                parts = self._parts()
                if len(parts) != 4 or parts[:2] != ["control", "plugins"] or parts[3] != "config":
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
                    return
                body = self._body()
                config = body.get("config")
                if not isinstance(config, dict):
                    raise PluginCenterError("配置内容格式不正确。")
                data = service.save(parts[2], config, str(body.get("revision") or "") or None)
                self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
            except Exception as exc:
                self._send_error(exc)

        def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                self._require_same_origin()
                if any(urlsplit(self.path).path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                parts = self._parts()
                if len(parts) != 4 or parts[:2] != ["control", "plugins"] or parts[3] != "enabled":
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
                    return
                enabled = self._body().get("enabled")
                if not isinstance(enabled, bool):
                    raise PluginCenterError("启用状态无效。")
                data = service.set_enabled(parts[2], enabled)
                self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                self._require_same_origin()
                if any(urlsplit(self.path).path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                parts = self._parts()
                if len(parts) >= 5 and parts[:2] == ["control", "plugins"] and parts[3] == "extension":
                    self._proxy_extension(parts[2], "/".join(parts[4:]))
                    return
                if len(parts) != 4 or parts[:2] != ["control", "plugins"] or parts[3] != "reload":
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
                    return
                data = service.reload(parts[2])
                self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
            except Exception as exc:
                self._send_error(exc)

        def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                self._require_same_origin()
                if any(urlsplit(self.path).path.startswith(prefix) for prefix in _PROXY_PREFIXES):
                    self._proxy()
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在。"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
