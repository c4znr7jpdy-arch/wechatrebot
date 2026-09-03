from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from .customer_config import load_plugins, load_robot_settings, set_plugin_enabled
from .json_io import write_json_atomic
from .single_instance import SingleInstanceLock


_PUBLIC_PLUGIN_ID = re.compile(r"^feature-[a-f0-9]{16}$")
_PAGE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_PROXY_PREFIXES = (
    "/api/plugin/page/",
    "/api/plug/",
)
_MAX_BODY_BYTES = 4 * 1024 * 1024


class PluginCenterError(RuntimeError):
    status_code = HTTPStatus.BAD_REQUEST


class EngineUnavailableError(PluginCenterError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE


class ConfigConflictError(PluginCenterError):
    status_code = HTTPStatus.CONFLICT


@dataclass(frozen=True, slots=True)
class PluginCatalogItem:
    module_name: str
    display_name: str
    enabled: bool
    configurable: bool
    field_count: int
    model_field_count: int
    pages: tuple[str, ...]


def _public_plugin_id(module_name: str) -> str:
    digest = hashlib.sha256(module_name.encode("utf-8")).hexdigest()[:16]
    return f"feature-{digest}"


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginCenterError("配置文件无法读取，请检查文件是否完整。") from exc
    if not isinstance(value, dict):
        raise PluginCenterError("配置文件格式不正确，应为 JSON 对象。")
    return value


def _json_revision(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _field_count(schema: dict[str, Any]) -> int:
    count = 0
    for item in schema.values():
        if not isinstance(item, dict):
            continue
        if item.get("type") == "object" and isinstance(item.get("items"), dict):
            count += _field_count(item["items"])
        else:
            count += 1
    return count


def _model_field_kind(key: str, field: dict[str, Any]) -> str | None:
    special = str(field.get("_special") or "").casefold()
    provider_type = str(field.get("_provider_type") or "").casefold()
    normalized = key.casefold()
    if special == "select_persona":
        return "persona"
    if special == "select_embedding_provider" or provider_type == "embedding":
        return "embedding"
    if provider_type == "rerank" or "rerank_provider" in normalized:
        return "rerank"
    if special == "select_provider" or normalized.endswith("provider_id"):
        return "chat"
    if "persona" in normalized and normalized.endswith(("_id", "_name")):
        return "persona"
    return None


def _split_schema(
    schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    basic: dict[str, Any] = {}
    models: dict[str, Any] = {}
    for key, field in schema.items():
        if not isinstance(field, dict):
            continue
        if field.get("type") == "object" and isinstance(field.get("items"), dict):
            basic_items, model_items = _split_schema(field["items"])
            if basic_items:
                basic[key] = {**field, "items": basic_items}
            if model_items:
                models[key] = {**field, "items": model_items}
        elif _model_field_kind(str(key), field):
            models[key] = field
        else:
            basic[key] = field
    return basic, models


def _merge_defaults(schema: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    for key, field in schema.items():
        if not isinstance(field, dict):
            continue
        field_type = field.get("type")
        if field_type == "object" and isinstance(field.get("items"), dict):
            current = result.get(key)
            result[key] = _merge_defaults(
                field["items"], current if isinstance(current, dict) else {}
            )
        elif key not in result and "default" in field:
            result[key] = field["default"]
    return result


def _normalize_value(field: dict[str, Any], value: Any) -> Any:
    if value is None:
        return None
    field_type = str(field.get("type") or "string")
    if field_type in {"string", "text"}:
        if isinstance(value, (str, int, float, bool)):
            return str(value).lower() if isinstance(value, bool) else str(value)
        return value
    if field_type == "int" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    if field_type == "float" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if field_type == "bool" and isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        return value
    if field_type == "object" and isinstance(value, dict):
        items = field.get("items")
        return _normalize_config(items, value) if isinstance(items, dict) else dict(value)
    if field_type == "list" and isinstance(value, list):
        item_schema = field.get("items")
        if isinstance(item_schema, dict) and item_schema.get("type"):
            return [_normalize_value(item_schema, item) for item in value]
        return list(value)
    if field_type == "template_list" and isinstance(value, list):
        templates = field.get("templates")
        result = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(templates, dict):
                result.append(item)
                continue
            template = templates.get(item.get("__template_key"))
            template_items = template.get("items") if isinstance(template, dict) else None
            result.append(
                _normalize_config(template_items, item)
                if isinstance(template_items, dict)
                else dict(item)
            )
        return result
    return value


def _normalize_config(
    schema: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    result = dict(config)
    for key, field in schema.items():
        if isinstance(field, dict) and key in result:
            result[key] = _normalize_value(field, result[key])
    return result


def _white_label_schema(value: Any) -> Any:
    """Clean customer-facing schema copy without changing functional field keys."""
    if isinstance(value, dict):
        return {str(key): _white_label_schema(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_white_label_schema(item) for item in value]
    if isinstance(value, str):
        result = re.sub(r"(?i)astrbot", "回复引擎", value)
        result = re.sub(r"(?i)new[-_ ]?hook", "微信连接", result)
        result = re.sub(r"(?i)hook", "微信连接", result)
        result = re.sub(r"(?i)napcat", "微信连接", result)
        result = re.sub(r"(?i)webui\s*dashboard", "高级管理页", result)
        result = re.sub(r"(?i)webui|dashboard", "管理页", result)
        result = result.replace("QQ群", "微信群").replace("QQ号", "微信账号")
        result = re.sub(r"(?i)qq", "微信", result)
        return re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", result).strip()
    return value


def _validate_config(schema: dict[str, Any], config: dict[str, Any], path: str = "") -> None:
    expected_types: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "text": (str,),
        "int": (int,),
        "float": (int, float),
        "bool": (bool,),
        "list": (list,),
        "template_list": (list,),
        "object": (dict,),
    }
    for key, field in schema.items():
        if not isinstance(field, dict) or key not in config:
            continue
        field_path = f"{path}.{key}" if path else key
        field_type = str(field.get("type") or "string")
        value = config[key]
        if value is None:
            continue
        accepted = expected_types.get(field_type)
        if accepted and (
            not isinstance(value, accepted)
            or (field_type in {"int", "float"} and isinstance(value, bool))
        ):
            raise PluginCenterError(f"“{field.get('description') or field_path}”的值类型不正确。")
        options = field.get("options")
        if isinstance(options, list):
            values = value if field_type == "list" and isinstance(value, list) else [value]
            if any(item not in options for item in values):
                raise PluginCenterError(
                    f"“{field.get('description') or field_path}”包含不支持的选项。"
                )
        slider = field.get("slider")
        if isinstance(slider, dict) and isinstance(value, (int, float)):
            minimum = slider.get("min")
            maximum = slider.get("max")
            if isinstance(minimum, (int, float)) and value < minimum:
                raise PluginCenterError(
                    f"“{field.get('description') or field_path}”不能小于 {minimum}。"
                )
            if isinstance(maximum, (int, float)) and value > maximum:
                raise PluginCenterError(
                    f"“{field.get('description') or field_path}”不能大于 {maximum}。"
                )
        if field_type == "object" and isinstance(value, dict) and isinstance(field.get("items"), dict):
            _validate_config(field["items"], value, field_path)
        elif field_type == "list" and isinstance(value, list):
            item_schema = field.get("items")
            if isinstance(item_schema, dict) and item_schema.get("type"):
                for index, item in enumerate(value):
                    _validate_config(
                        {"item": item_schema},
                        {"item": item},
                        f"{field_path}[{index}]",
                    )
        elif field_type == "template_list" and isinstance(value, list):
            templates = field.get("templates")
            if not isinstance(templates, dict):
                continue
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    raise PluginCenterError(
                        f"“{field.get('description') or field_path}”的第 {index + 1} 项格式不正确。"
                    )
                template_key = item.get("__template_key")
                template = templates.get(template_key)
                if isinstance(template, dict) and isinstance(template.get("items"), dict):
                    _validate_config(
                        template["items"],
                        item,
                        f"{field_path}[{index}]",
                    )


def _dashboard_path(install_root: Path) -> Path:
    return install_root / "app" / "astrbot" / "data" / "cmd_config.json"


def _plugin_root(install_root: Path) -> Path:
    return install_root / "app" / "astrbot" / "data" / "plugins"


def _config_path(install_root: Path, module_name: str) -> Path:
    return install_root / "app" / "astrbot" / "data" / "config" / f"{module_name}_config.json"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class EngineApiClient:
    def __init__(self, install_root: str | Path, *, timeout: float = 5.0) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.timeout = timeout

    def _settings(self) -> tuple[str, str, int]:
        payload = _read_object(_dashboard_path(self.install_root))
        dashboard = payload.get("dashboard")
        if not isinstance(dashboard, dict):
            raise EngineUnavailableError("回复引擎的本地控制接口尚未配置。")
        secret = str(dashboard.get("jwt_secret") or "").strip()
        username = str(dashboard.get("username") or "local-console").strip()
        try:
            port = int(dashboard.get("port") or 6185)
        except (TypeError, ValueError) as exc:
            raise EngineUnavailableError("回复引擎的本地控制接口尚未就绪。") from exc
        if not secret or not 1 <= port <= 65535:
            raise EngineUnavailableError("回复引擎的本地控制接口尚未就绪。")
        return secret, username, port

    def _token(self) -> str:
        secret, username, _ = self._settings()
        header = _base64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        body = _base64url(
            json.dumps(
                {"username": username, "iat": int(time.time()), "exp": int(time.time()) + 300},
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"{header}.{body}".encode("ascii")
        signature = _base64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
        return f"{header}.{body}.{signature}"

    def _url(self, path: str) -> str:
        _, _, port = self._settings()
        if not path.startswith("/"):
            raise ValueError("engine API path must be absolute")
        return f"http://127.0.0.1:{port}{path}"

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        accept_language: str = "zh-CN",
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept-Language": accept_language,
        }
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise EngineUnavailableError("回复引擎尚未在线，请先在运行中心启动机器人。") from exc

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, raw = self.raw_request(method, path, body=body)
        try:
            response = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginCenterError("回复引擎返回了无法识别的数据。") from exc
        if status >= 400 or not isinstance(response, dict) or response.get("status") == "error":
            message = ""
            if isinstance(response, dict):
                message = str(_white_label_schema(response.get("message") or "")).strip()
            raise PluginCenterError(
                message or "回复引擎未接受本次操作，请检查配置后重试。"
            )
        return response.get("data", response)

    def is_available(self) -> bool:
        try:
            self.request("GET", "/api/v1/plugins")
            return True
        except PluginCenterError:
            return False

    def save_config(self, module_name: str, config: dict[str, Any]) -> None:
        encoded = quote(module_name, safe="")
        self.request("PUT", f"/api/v1/plugins/{encoded}/config", {"config": config})

    def set_enabled(self, module_name: str, enabled: bool) -> None:
        encoded = quote(module_name, safe="")
        self.request("PATCH", f"/api/v1/plugins/{encoded}/enabled", {"enabled": enabled})

    def reload(self, module_name: str) -> None:
        encoded = quote(module_name, safe="")
        self.request("POST", f"/api/v1/plugins/{encoded}/reload")

    def page_entry(self, module_name: str, page_name: str) -> str:
        encoded_plugin = quote(module_name, safe="")
        encoded_page = quote(page_name, safe="")
        data = self.request("GET", f"/api/v1/plugins/{encoded_plugin}/pages/{encoded_page}")
        if not isinstance(data, dict) or not isinstance(data.get("content_path"), str):
            raise PluginCenterError("高级配置页暂时无法打开。")
        return data["content_path"]


class PluginCenterService:
    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.engine = EngineApiClient(self.install_root)

    def catalog(self) -> dict[str, Any]:
        entries = []
        root = _plugin_root(self.install_root)
        for plugin in load_plugins(self.install_root):
            plugin_dir = root / plugin.module_name
            schema = _read_object(plugin_dir / "_conf_schema.json")
            _, model_schema = _split_schema(schema)
            pages_root = plugin_dir / "pages"
            pages = tuple(
                path.name
                for path in sorted(pages_root.iterdir(), key=lambda item: item.name.casefold())
                if path.is_dir()
                and _PAGE_ID.fullmatch(path.name)
                and (path / "index.html").is_file()
            ) if pages_root.is_dir() else ()
            entries.append(
                {
                    "id": _public_plugin_id(plugin.module_name),
                    "display_name": plugin.display_name,
                    "enabled": plugin.enabled,
                    "configurable": bool(schema),
                    "field_count": _field_count(schema),
                    "model_field_count": _field_count(model_schema),
                    "pages": pages,
                }
            )
        return {
            "plugins": entries,
            "engine_online": self.engine.is_available(),
            "product_name": load_robot_settings(self.install_root).profile.display_name,
        }

    def _plugin(self, public_id: str) -> PluginCatalogItem:
        if not _PUBLIC_PLUGIN_ID.fullmatch(public_id):
            raise PluginCenterError("功能标识无效。")
        root = _plugin_root(self.install_root)
        for plugin in load_plugins(self.install_root):
            if _public_plugin_id(plugin.module_name) == public_id:
                schema = _read_object(root / plugin.module_name / "_conf_schema.json")
                _, model_schema = _split_schema(schema)
                pages_root = root / plugin.module_name / "pages"
                pages = tuple(
                    path.name
                    for path in sorted(
                        pages_root.iterdir(), key=lambda item: item.name.casefold()
                    )
                    if path.is_dir()
                    and _PAGE_ID.fullmatch(path.name)
                    and (path / "index.html").is_file()
                ) if pages_root.is_dir() else ()
                return PluginCatalogItem(
                    module_name=plugin.module_name,
                    display_name=plugin.display_name,
                    enabled=plugin.enabled,
                    configurable=bool(schema),
                    field_count=_field_count(schema),
                    model_field_count=_field_count(model_schema),
                    pages=pages,
                )
        raise PluginCenterError("所选功能不存在或尚未交付。")

    def detail(self, public_id: str) -> dict[str, Any]:
        item = self._plugin(public_id)
        module_name = item.module_name
        schema = _read_object(_plugin_root(self.install_root) / module_name / "_conf_schema.json")
        basic_schema, model_schema = _split_schema(schema)
        stored = _read_object(_config_path(self.install_root, module_name))
        config = _normalize_config(schema, _merge_defaults(schema, stored))
        engine_config = _read_object(_dashboard_path(self.install_root))
        providers: list[dict[str, str]] = []
        embedding_providers: list[dict[str, str]] = []
        rerank_providers: list[dict[str, str]] = []
        configured_providers = engine_config.get("provider", [])
        if not isinstance(configured_providers, list):
            configured_providers = []
        for provider in configured_providers:
            if not isinstance(provider, dict) or provider.get("enable") is False:
                continue
            provider_id = str(provider.get("id") or "").strip()
            if not provider_id:
                continue
            model_name = str(provider.get("model") or "").strip()
            label = (
                f"{model_name} · {provider_id}"
                if model_name and model_name != provider_id
                else provider_id
            )
            choice = {"value": provider_id, "label": str(_white_label_schema(label))}
            provider_kind = " ".join(
                (
                    str(provider.get("type") or ""),
                    provider_id,
                    model_name,
                )
            ).casefold()
            if "embedding" in provider_kind:
                embedding_providers.append(choice)
            elif "rerank" in provider_kind:
                rerank_providers.append(choice)
            else:
                providers.append(choice)
        configured_personas = engine_config.get("persona", [])
        if not isinstance(configured_personas, list):
            configured_personas = []
        personas = [
            str(value.get("name") or value.get("id") or "").strip()
            for value in configured_personas
            if isinstance(value, dict) and str(value.get("name") or value.get("id") or "").strip()
        ]
        clean_schema = _white_label_schema(schema)
        return {
            "id": public_id,
            "display_name": item.display_name,
            "enabled": item.enabled,
            "configurable": item.configurable,
            "field_count": item.field_count,
            "model_field_count": item.model_field_count,
            "pages": item.pages,
            "schema": clean_schema,
            "basic_schema": _white_label_schema(basic_schema),
            "model_schema": _white_label_schema(model_schema),
            "config": config,
            "revision": _json_revision(stored),
            "providers": providers,
            "embedding_providers": embedding_providers,
            "rerank_providers": rerank_providers,
            "personas": personas,
            "engine_online": self.engine.is_available(),
        }

    def save(self, public_id: str, config: dict[str, Any], base_revision: str | None) -> dict[str, Any]:
        item = self._plugin(public_id)
        module_name = item.module_name
        if not item.configurable:
            raise PluginCenterError("这项功能没有可编辑的基础配置。")
        path = _config_path(self.install_root, module_name)
        stored = _read_object(path)
        if base_revision and base_revision != _json_revision(stored):
            raise ConfigConflictError("配置已被其他窗口修改，请刷新后再保存。")
        schema = _read_object(_plugin_root(self.install_root) / module_name / "_conf_schema.json")
        _validate_config(schema, config)
        online = self.engine.is_available()
        reloaded = False
        if online and item.enabled:
            self.engine.save_config(module_name, config)
            reloaded = True
        else:
            write_json_atomic(path, config)
        saved = _read_object(path)
        return {
            "saved": True,
            "reloaded": reloaded,
            "revision": _json_revision(saved),
            "message": "配置已保存并完成热重载。" if reloaded else "配置已保存，将在下次启动时生效。",
        }

    def set_enabled(self, public_id: str, enabled: bool) -> dict[str, Any]:
        item = self._plugin(public_id)
        module_name = item.module_name
        online = self.engine.is_available()
        if online:
            self.engine.set_enabled(module_name, enabled)
        else:
            set_plugin_enabled(self.install_root, module_name, enabled)
        return {
            "enabled": enabled,
            "applied": online,
            "message": "功能状态已立即更新。" if online else "功能状态已保存，将在下次启动时生效。",
        }

    def reload(self, public_id: str) -> dict[str, Any]:
        item = self._plugin(public_id)
        module_name = item.module_name
        if not item.enabled:
            raise PluginCenterError("请先启用这项功能，再执行热重载。")
        self.engine.reload(module_name)
        return {"reloaded": True, "message": "热重载完成，新的设置已经生效。"}

    def page_entry(self, public_id: str, page_name: str) -> dict[str, str]:
        item = self._plugin(public_id)
        module_name = item.module_name
        if page_name not in item.pages or not _PAGE_ID.fullmatch(page_name):
            raise PluginCenterError("高级配置页不存在。")
        return {"content_path": self.engine.page_entry(module_name, page_name)}


def _asset_root() -> Path:
    candidates = [Path(__file__).with_name("plugin_webui")]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.insert(0, Path(bundle_root) / "wechatrebot_manager" / "plugin_webui")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    raise FileNotFoundError("功能配置中心资源缺失，请重新安装管理器。")


def _handler(service: PluginCenterService):
    assets = _asset_root()

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

        def _serve_asset(self, relative: str, content_type: str) -> None:
            target = assets / relative
            if not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面资源不存在。"})
                return
            raw = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-src 'self'")
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
            self.send_response(status)
            self._security_headers()
            for key in ("Content-Type", "Content-Security-Policy", "Permissions-Policy"):
                if headers.get(key):
                    self.send_header(key, headers[key])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                split = urlsplit(self.path)
                if split.path in {"/", "/index.html"}:
                    self._serve_asset("index.html", "text/html; charset=utf-8")
                    return
                if split.path == "/assets/app.css":
                    self._serve_asset("app.css", "text/css; charset=utf-8")
                    return
                if split.path == "/assets/app.js":
                    self._serve_asset("app.js", "text/javascript; charset=utf-8")
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
                if len(parts) == 6 and parts[:2] == ["control", "plugins"] and parts[3] == "pages" and parts[5] == "entry":
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": service.page_entry(parts[2], parts[4])})
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


def plugin_center_command(install_root: str | Path) -> list[str]:
    root = str(Path(install_root).expanduser().resolve())
    if getattr(sys, "frozen", False):
        return [sys.executable, "plugin-center", root]
    return [sys.executable, "-m", "wechatrebot_manager", "plugin-center", root]


def launch_plugin_center(install_root: str | Path) -> subprocess.Popen[bytes]:
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        plugin_center_command(install_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )


def run_plugin_center(install_root: str | Path) -> int:
    root = Path(install_root).expanduser().resolve()
    lock = SingleInstanceLock(f"plugin-center:{root}")
    if not lock.acquire():
        return 3
    server: ThreadingHTTPServer | None = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(PluginCenterService(root)))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        import webview

        brand = load_robot_settings(root).profile
        url = f"http://127.0.0.1:{server.server_port}/"
        webview.create_window(
            f"{brand.display_name} · 功能配置",
            url,
            width=1180,
            height=760,
            min_size=(960, 640),
            background_color="#F8FAFD",
            text_select=True,
        )
        webview.start(gui="edgechromium", debug=False, private_mode=True)
        return 0
    except ImportError as exc:
        raise PluginCenterError("功能配置窗口组件缺失，请重新安装管理器。") from exc
    except PluginCenterError:
        raise
    except Exception as exc:
        raise PluginCenterError(
            "功能配置窗口无法打开，请检查 Edge WebView2 Runtime 后重试。"
        ) from exc
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        lock.release()


__all__ = [
    "ConfigConflictError",
    "EngineApiClient",
    "EngineUnavailableError",
    "PluginCatalogItem",
    "PluginCenterError",
    "PluginCenterService",
    "launch_plugin_center",
    "plugin_center_command",
    "run_plugin_center",
]
