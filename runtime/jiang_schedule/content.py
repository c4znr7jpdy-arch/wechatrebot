"""Explicit content adapters: never replay a command or fabricate a chat event."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path


ACTION_NAMES = {
    "weather": "天气预报", "news": "热点资讯", "oilprice": "油价提醒",
    "kfc": "星期四文案", "text": "文字提醒",
}
MAX_IMAGE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class Content:
    text: str = ""
    image: bytes = b""
    mime: str = ""

    def __post_init__(self):
        if bool(self.text) == bool(self.image):
            raise ValueError("内容必须是一段文字或一张图片")
        if self.image and (self.mime not in {"image/png", "image/jpeg", "image/webp"}
                           or len(self.image) > MAX_IMAGE_BYTES):
            raise ValueError("天气卡片格式或大小不支持")

    def preview(self) -> dict:
        if self.image:
            encoded = base64.b64encode(self.image).decode("ascii")
            return {"kind": "image", "type": "image", "image_data_url": f"data:{self.mime};base64,{encoded}"}
        return {"kind": "text", "type": "text", "text": self.text}

    def segments(self) -> list[dict]:
        if self.image:
            return [{"type": "image", "data": {
                "file": "base64://" + base64.b64encode(self.image).decode("ascii"),
            }}]
        return [{"type": "text", "data": {"text": self.text}}]


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

    def readiness(self, action: str) -> dict:
        if action not in ACTION_NAMES:
            return {"ready": False, "message": "该内容尚不支持定时发送"}
        if action == "text":
            return {"ready": True, "message": ""}
        try:
            config = json.loads((self.root / "app/astrbot/data/cmd_config.json").read_text("utf-8-sig"))
            enabled = config.get("plugin_set", ["*"])
            if not isinstance(enabled, list) or not ("*" in enabled or "jiang_commands" in enabled):
                return {"ready": False, "message": "请先在功能中心启用常用命令"}
            stars = self.context.get_all_stars()
            if not any(getattr(item, "root_dir_name", None) == "jiang_commands"
                       and getattr(item, "activated", False)
                       and getattr(item, "star_cls", None) is not None for item in stars):
                return {"ready": False, "message": "常用命令尚未加载，请先在功能中心启用"}
            content_config = self._configuration()
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
        raise ValueError("该内容尚不支持定时发送")
