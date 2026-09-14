"""Explicit content adapters: never replay a command or fabricate a chat event."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .task_store import ACTION_FEATURES, ACTION_NAMES

MAX_IMAGE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class Content:
    text: str = ""
    image: bytes = b""
    mime: str = ""
    card: dict | None = None

    def __post_init__(self):
        if sum((bool(self.text), bool(self.image), self.card is not None)) != 1:
            raise ValueError("内容必须是一段文字、一张图片或一张链接卡片")
        if self.image and (self.mime not in {"image/png", "image/jpeg", "image/webp"}
                           or len(self.image) > MAX_IMAGE_BYTES):
            raise ValueError("天气卡片格式或大小不支持")
        if self.card is not None:
            if not isinstance(self.card, dict):
                raise ValueError("影视推荐卡片格式不正确")
            title = self.card.get("title")
            description = self.card.get("description")
            url = self.card.get("url")
            image_url = self.card.get("image_url")
            if (not isinstance(title, str) or not title.strip() or len(title) > 48
                    or not isinstance(description, str) or len(description) > 80
                    or not _public_http_url(url) or not _public_http_url(image_url)):
                raise ValueError("影视推荐卡片内容或地址不正确")

    def preview(self) -> dict:
        if self.image:
            encoded = base64.b64encode(self.image).decode("ascii")
            return {"kind": "image", "type": "image", "image_data_url": f"data:{self.mime};base64,{encoded}"}
        if self.card is not None:
            return {"kind": "link_card", "type": "link_card", **self.card}
        return {"kind": "text", "type": "text", "text": self.text}

    def segments(self) -> list[dict]:
        if self.image:
            return [{"type": "image", "data": {
                "file": "base64://" + base64.b64encode(self.image).decode("ascii"),
            }}]
        if self.card is not None:
            return [{"type": "wechat_link_card", "data": {
                "title": self.card["title"],
                "desc": self.card["description"],
                "url": self.card["url"],
                "image_url": self.card["image_url"],
            }}]
        return [{"type": "text", "data": {"text": self.text}}]


def _public_http_url(value) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 32 for char in value):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _hosted_player_base(value) -> bool:
    raw_value = str(value or "").strip()
    if (
        not raw_value
        or len(raw_value) > 2048
        or any(ord(character) < 32 for character in raw_value)
        or not _public_http_url(raw_value)
    ):
        return False
    parsed = urlsplit(raw_value)
    try:
        parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if len(labels) < 2 or not re.search(r"[a-z]", labels[-1]) or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            return False
    else:
        if not address.is_global:
            return False
    return not parsed.query and not parsed.fragment


def _hosted_player_url(value, base_url) -> bool:
    raw_value = str(value or "").strip()
    if (
        not raw_value
        or len(raw_value) > 2048
        or any(ord(character) < 32 for character in raw_value)
        or not _public_http_url(raw_value)
        or not _hosted_player_base(base_url)
    ):
        return False
    candidate = urlsplit(raw_value)
    base = urlsplit(base_url)
    try:
        same_endpoint = (
            candidate.hostname == base.hostname
            and candidate.port == base.port
            and candidate.path == base.path
            and bool(candidate.query or candidate.fragment)
            and not (candidate.query and candidate.fragment)
        )
        if not same_endpoint:
            return False
        if candidate.query:
            return bool(re.fullmatch(r"s=[A-Za-z0-9_-]{16,96}", candidate.query))
        return bool(re.fullmatch(
            r"(?:r=[A-Za-z0-9_-]{1,1800}|"
            r"p=[A-Za-z0-9_-]{1,1800}&ep=[1-9]\d{0,4}&force=[01])",
            candidate.fragment,
        ))
    except ValueError:
        return False


def _image_content(path: str) -> Content:
    # Only a trusted renderer returns this path; never accept a path from a task.
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_IMAGE_BYTES + 1)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("天气服务未生成有效图片卡片")
    return Content(image=data, mime=mime)


class ContentService:
    def __init__(self, context, install_root: Path):
        self.context = context
        self.root = Path(install_root)

    def _configuration(self) -> dict:
        path = self.root / "app/astrbot/data/config/jiang_commands_config.json"
        try:
            config = json.loads(path.read_text("utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ValueError("内容服务配置无法读取，请在功能中心重新保存") from None
        if not isinstance(config, dict):
            raise ValueError("内容服务配置格式不正确")
        return config

    def _short_drama_configuration(self) -> dict:
        path = self.root / "app/astrbot/data/config/jiang_short_drama_config.json"
        try:
            config = json.loads(path.read_text("utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ValueError("影视播放服务配置无法读取，请重新安装或更新") from None
        if not isinstance(config, dict):
            raise ValueError("影视播放服务配置格式不正确")
        return config

    def _active_plugin(self, module: str):
        for item in self.context.get_all_stars():
            if (getattr(item, "root_dir_name", None) == module
                    and getattr(item, "activated", False)
                    and getattr(item, "star_cls", None) is not None):
                return item.star_cls
        return None

    def readiness(self, action: str) -> dict:
        if action not in ACTION_NAMES:
            return {"ready": False, "message": "该内容尚不支持定时发送"}
        feature = ACTION_FEATURES[action]
        if not feature:
            return {"ready": True, "message": ""}
        try:
            config = json.loads((self.root / "app/astrbot/data/cmd_config.json").read_text("utf-8-sig"))
            enabled = config.get("plugin_set", ["*"])
            label = "短剧" if feature == "jiang_short_drama" else "常用命令"
            if not isinstance(enabled, list) or not ("*" in enabled or feature in enabled):
                return {"ready": False, "message": f"请先在功能中心启用{label}"}
            plugin = self._active_plugin(feature)
            if plugin is None:
                return {"ready": False, "message": f"{label}尚未加载，请先在功能中心启用"}
            if action == "short_drama" and not callable(getattr(plugin, "build_unified_recommendation", None)):
                return {"ready": False, "message": "短剧功能版本过旧，请更新后使用统一任务"}
            if action == "short_drama" and not _hosted_player_base(
                self._short_drama_configuration().get("stateless_player_url")
            ):
                return {"ready": False, "message": "影视播放服务尚未配置，请更新或重新安装"}
            content_config = self._configuration() if feature == "jiang_commands" else {}
            if action == "news" and not all(str(content_config.get(key) or "").strip()
                                            for key in ("content_api_id", "content_api_key")):
                return {"ready": False, "message": "请先在常用命令中填写热点服务账号和密钥"}
        except (OSError, ValueError, AttributeError):
            return {"ready": False, "message": "无法确认内容功能是否就绪，请检查功能中心"}
        return {"ready": True, "message": ""}

    def require_ready(self, action: str) -> None:
        ready = self.readiness(action)
        if not ready["ready"]:
            raise ValueError(ready["message"])

    async def generate(self, task: dict) -> Content:
        action = task["action"]
        self.require_ready(action)
        args = task["arguments"]
        if action == "text":
            return Content(text=args["text"])
        if action != "short_drama":
            config = self._configuration()
            for key, env in (("content_api_id", "JIANG_CONTENT_API_ID"),
                             ("content_api_key", "JIANG_CONTENT_API_KEY"),
                             ("weather_detail_token", "JIANG_WEATHER_DETAIL_TOKEN")):
                os.environ[env] = str(config.get(key) or "").strip()
        if action == "weather":
            from ai_plugin.scheduled_weather import generate_weather
            day = args.get("day", "today")
            if day not in {"today", "tomorrow"}:
                raise ValueError("请选择今天或明天天气")
            return _image_content(await generate_weather(args["city"], 0 if day == "today" else 1))
        if action == "news":
            from ai_plugin.news import fetch_hot_news, format_news
            return Content(text=format_news(await fetch_hot_news(int(args.get("count", 20)))))
        if action == "oilprice":
            from ai_plugin.oilprice import fetch_oilprice, format_oilprice
            return Content(text=format_oilprice(await fetch_oilprice(args["province"])))
        if action == "kfc":
            from ai_plugin.kfc import fetch_kfc_text
            return Content(text=await fetch_kfc_text())
        if action == "short_drama":
            plugin = self._active_plugin("jiang_short_drama")
            if plugin is None:
                raise ValueError("短剧功能尚未加载")
            result = await plugin.build_unified_recommendation(
                args.get("media_type", "短剧"), int(args.get("limit", 12))
            )
            if not isinstance(result, dict):
                raise ValueError("短剧功能未返回可发送内容")
            if result.get("type") == "text":
                return Content(text=str(result.get("text") or ""))
            if result.get("type") != "link_card":
                raise ValueError("短剧功能返回了不支持的消息类型")
            configuration = self._short_drama_configuration()
            if not _hosted_player_url(
                result.get("url"), configuration.get("stateless_player_url")
            ):
                fallback = str(result.get("fallback_text") or "").strip()
                if fallback:
                    return Content(text=fallback)
                raise ValueError("影视播放地址未通过服务校验")
            return Content(card={
                "title": result.get("title"),
                "description": result.get("description"),
                "url": result.get("url"),
                "image_url": result.get("image_url"),
            })
        raise ValueError("该内容尚不支持定时发送")
