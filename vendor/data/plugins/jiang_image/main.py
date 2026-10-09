"""
文生图/图生图插件 — HostCentral 首选后端 + WebUI 自定义模型热更新。

群聊需 @机器人，私聊直接说“画 xxx”即可；
引用图片 + “画 xxx”/“P图”会自动走图生图。
"""
import json as _json
import asyncio
import os
import re
import sys
import logging
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.components import Image, Reply

logger = logging.getLogger("jiang_image")

# ── 引入 ai_plugin 模块（作为包导入，支持相对导入）──
_PLUGINS_DIR = str(Path(__file__).resolve().parent.parent)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import _nonebot_stubs
_nonebot_stubs.setup()

_AI_PLUGIN_PARENT = str(Path(__file__).resolve().parent.parent.parent.parent / "ai_plugin")
if _AI_PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, _AI_PLUGIN_PARENT)

from ai_plugin.image_generator import (
    detect_image_intent,
    detect_image_edit_intent,
)
from .model_manager import ImageModelConfigError, ImageModelManager

# ── 比例解析 ──────────────────────────────────────────────
_ASPECT_PATTERNS = [
    (r"(?:横屏|16\s*[:：]\s*9)", "16:9"),
    (r"(?:竖屏|9\s*[:：]\s*16)", "9:16"),
    (r"4\s*[:：]\s*3", "4:3"),
    (r"3\s*[:：]\s*4", "3:4"),
    (r"3\s*[:：]\s*2", "3:2"),
    (r"2\s*[:：]\s*3", "2:3"),
    (r"1\s*[:：]\s*1", "1:1"),
]


def _parse_aspect(text: str) -> tuple[str, str]:
    """返回 (清理后文本, 比例)"""
    for pattern, ratio in _ASPECT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            text = text[:m.start()] + text[m.end():]
            return text.strip(), ratio
    return text.strip(), "1:1"


# ── 引用消息解析（WeChat JSON 前缀格式）──────────────────

def _extract_quote_info(event: AstrMessageEvent) -> dict | None:
    """从消息文本中解析 WeChat 引用消息的 JSON 前缀

    格式: {"qt":"text|img","qs":"sender","qsw":"wxid","qtxt":"..."/"qp":"path"}
    """
    text = event.get_message_str().strip()
    if not text.startswith("{"):
        return None
    # 找到 JSON 结束位置（第一个换行前）
    nl = text.find("\n")
    json_str = text[:nl] if nl > 0 else text
    try:
        data = _json.loads(json_str)
        if isinstance(data, dict) and "qt" in data:
            return data
    except (_json.JSONDecodeError, ValueError):
        pass
    return None


async def _get_quoted_image_path(event: AstrMessageEvent) -> str | None:
    """从旧版元数据或标准 OneBot Reply 链中提取引用图片路径。"""
    info = _extract_quote_info(event)
    if info and info.get("qt") == "img":
        path = info.get("qp", "")
        if path and not path.startswith("http") and os.path.exists(path):
            return path

    for component in event.get_messages():
        if not isinstance(component, Reply) or not component.chain:
            continue
        for quoted_component in component.chain:
            if not isinstance(quoted_component, Image):
                continue
            path = str(getattr(quoted_component, "path", "") or "")
            if path and os.path.exists(path):
                return path
            try:
                resolved = await quoted_component.convert_to_file_path()
            except Exception:
                continue
            if resolved and os.path.exists(resolved):
                return resolved
    return None


def _get_quoted_text(event: AstrMessageEvent) -> str | None:
    """从引用消息中提取文本"""
    info = _extract_quote_info(event)
    if info and info.get("qt") == "text":
        return info.get("qtxt", "")
    return None


def _get_plain_text(event: AstrMessageEvent) -> str:
    """获取消息中的纯文本（去掉 JSON 前缀、[引用 ...] 标记、@等）"""
    text = event.get_message_str().strip()
    # 去掉 JSON 前缀
    if text.startswith("{"):
        nl = text.find("\n")
        if nl > 0:
            try:
                _json.loads(text[:nl])
                text = text[nl + 1:].strip()
            except (_json.JSONDecodeError, ValueError):
                pass
    # 去掉 [引用 ...] / [转发 ...] 行
    text = re.sub(r"^\[(?:引用|转发)\s.*?\]\s*", "", text)
    # 去掉 @mention
    text = re.sub(r"^@\S+\s*", "", text)
    return text.strip()


# ── AstrBot Star 插件 ─────────────────────────────────────

class Main(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._config_lock = asyncio.Lock()
        self.model_manager = ImageModelManager(
            plugin_dir=Path(__file__).resolve().parent,
            data_dir=StarTools.get_data_dir("jiang_image"),
            logger=logger,
        )
        self._models_initialized = False
        self._register_page_apis()

    async def initialize(self) -> None:
        self.model_manager.initialize()
        self._apply_plugin_config()
        self._models_initialized = True
        active = self.model_manager.active_profile()
        logger.info(
            "[IMAGE] 文生图/图生图插件已加载，默认模型=%s",
            active["id"] if active else "(无)",
        )

    def _apply_plugin_config(self) -> None:
        """把 AstrBot 齿轮配置映射到模型管理器的有序回退链。"""
        if not self.config:
            return
        slots = (
            ("primary_base_url", "primary_model", None),
            ("secondary_base_url", "secondary_model", "secondary_enabled"),
            ("tertiary_base_url", "tertiary_model", "tertiary_enabled"),
        )
        public = self.model_manager.public_config()
        active_id = str(public.get("active_model") or "")
        active = next(
            (profile for profile in public["models"] if profile["id"] == active_id),
            None,
        )
        fallbacks = sorted(
            (
                profile
                for profile in public["models"]
                if profile["id"] != active_id
                and int(profile.get("fallback_priority", 0)) > 0
            ),
            key=lambda profile: (
                int(profile.get("fallback_priority", 0)),
                profile["name"],
            ),
        )
        chain = ([active] if active else []) + fallbacks
        for index, (base_key, model_key, enabled_key) in enumerate(slots):
            if index >= len(chain):
                break
            profile = chain[index]
            base_url = str(self.config.get(base_key) or "").strip()
            model = str(self.config.get(model_key) or "").strip()
            enabled = (
                profile.get("enabled", True)
                if enabled_key is None
                else bool(self.config.get(enabled_key, profile.get("enabled", True)))
            )
            if (
                (not base_url or base_url.rstrip("/") == str(profile.get("base_url") or "").rstrip("/"))
                and (not model or model == profile.get("model"))
                and enabled == profile.get("enabled", True)
                and int(profile.get("fallback_priority", 0)) == index
            ):
                continue
            payload = dict(profile)
            if base_url:
                payload["base_url"] = base_url
            if model:
                payload["model"] = model
            payload["enabled"] = enabled
            payload["fallback_priority"] = index
            self.model_manager.upsert_profile(payload)

    def _register_page_apis(self) -> None:
        """注册 AstrBot Plugin Page 使用的管理 API。"""
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[IMAGE] 当前 AstrBot 不支持 Plugin Pages")
            return
        routes = [
            ("config", self.page_get_config, ["GET"], "Image model config"),
            ("models/save", self.page_save_model, ["POST"], "Save image model"),
            ("models/delete", self.page_delete_model, ["POST"], "Delete image model"),
            ("models/active", self.page_set_active, ["POST"], "Set active image model"),
            ("models/test", self.page_test_model, ["POST"], "Test image model"),
            ("reload", self.page_reload, ["POST"], "Reload image model config"),
        ]
        for path, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/jiang_image/page/{path}", handler, methods, desc
            )

    def _ensure_models_initialized(self) -> None:
        if self._models_initialized:
            return
        self.model_manager.initialize()
        self._apply_plugin_config()
        self._models_initialized = True

    async def page_get_config(self):
        try:
            self._ensure_models_initialized()
            return json_response({"status": "ok", "data": self.model_manager.public_config()})
        except Exception as exc:
            logger.exception("[IMAGE] WebUI 读取模型配置失败")
            return error_response(str(exc), status_code=500)

    async def page_save_model(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                raise ImageModelConfigError("请求体必须是 JSON 对象")
            async with self._config_lock:
                self._ensure_models_initialized()
                config = self.model_manager.upsert_profile(payload.get("model", payload))
            return json_response({"status": "ok", "data": config})
        except ImageModelConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[IMAGE] WebUI 保存模型失败")
            return error_response(str(exc), status_code=500)

    async def page_delete_model(self):
        try:
            payload = await request.json(default={})
            model_id = str((payload or {}).get("id") or "").strip()
            async with self._config_lock:
                self._ensure_models_initialized()
                config = self.model_manager.delete_profile(model_id)
            return json_response({"status": "ok", "data": config})
        except ImageModelConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[IMAGE] WebUI 删除模型失败")
            return error_response(str(exc), status_code=500)

    async def page_set_active(self):
        try:
            payload = await request.json(default={})
            model_id = str((payload or {}).get("id") or "").strip()
            async with self._config_lock:
                self._ensure_models_initialized()
                config = self.model_manager.set_active(model_id)
            return json_response({"status": "ok", "data": config})
        except ImageModelConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[IMAGE] WebUI 切换默认模型失败")
            return error_response(str(exc), status_code=500)

    async def page_test_model(self):
        try:
            payload = await request.json(default={})
            model_id = str((payload or {}).get("id") or "").strip()
            self._ensure_models_initialized()
            result = await self.model_manager.test_profile(model_id)
            return json_response({"status": "ok", "data": result})
        except ImageModelConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.warning("[IMAGE] WebUI 模型连接检查失败: %s", exc)
            return error_response(str(exc), status_code=502)

    async def page_reload(self):
        try:
            async with self._config_lock:
                self._ensure_models_initialized()
                self.model_manager.reload()
                self._apply_plugin_config()
                config = self.model_manager.public_config()
            return json_response({"status": "ok", "data": config})
        except ImageModelConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[IMAGE] WebUI 热重载失败")
            return error_response(str(exc), status_code=500)

    # ── 文生图 / 图生图 ────────────────────────────────────
    # 用 regex 匹配所有画图意图，绕过 LLM 拦截
    @filter.regex(
        r"(?:生成|画|制作|做|创作).*(?:图片|照片|图像|插画|壁纸|头像|图)"
        r"|帮我.*(?:画|生成|做|制作)"
        r"|文生图|(?:帮我|给我|请|想要|来|做|生成)?(?<![图文])生图(?:$|[\s，,：:])|text.*to.*image|t2i"
        r"|画一[个张幅]"
        r"|(?:给我|来|发|找).{0,20}(?:图片|照片|壁纸|头像|的图)"
        r"|P图|改图|修图|图生图|i2i"
        r"|(?:换脸|换背景|抠图)"
        r"|(?:美化|优化|增强|滤镜|风格化).*(?:图片|照片|图)"
        r"|把.*图.*(?:变|改|换|成|弄|P|修|转|优化)"
        r"|把.{0,10}(?:P|p)成"
        r"|(?:P|p)成.{0,20}"
    )
    async def handle_image(self, event: AstrMessageEvent):
        # 跳过机器人自身消息
        if event.get_sender_id() == event.get_self_id():
            return
        # 群聊必须 @机器人 才触发画图，避免其他机器人的回复误触发
        from astrbot.api.message_components import At
        self_id = event.get_self_id()
        message_obj = getattr(event, "message_obj", None)
        if message_obj:
            chain = getattr(message_obj, "message", [])
            has_at = any(
                isinstance(seg, At) and str(getattr(seg, "qq", "")) == self_id
                for seg in chain
            )
            if not has_at:
                return
        text = _get_plain_text(event)
        if not text:
            return

        # 提取引用图片
        quoted_image_path = await _get_quoted_image_path(event)

        # ── 图生图：有引用图片 + 编辑意图 ──
        edit_prompt = detect_image_edit_intent(text)
        if edit_prompt and quoted_image_path:
            async for r in self._do_image_edit(event, edit_prompt, quoted_image_path):
                yield r
            return

        # ── 图生图：有引用图片 + 画图意图（自动转编辑）──
        image_prompt = detect_image_intent(text)
        if image_prompt and quoted_image_path:
            async for r in self._do_image_edit(event, image_prompt, quoted_image_path):
                yield r
            return

        # ── 文生图 ──
        if image_prompt:
            prompt, aspect = _parse_aspect(image_prompt)
            if not prompt:
                prompt = image_prompt
            async for r in self._do_image_gen(event, prompt, aspect):
                yield r
            return

    # ── 切换模型命令 ──────────────────────────────────────
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def switch_model(self, event: AstrMessageEvent):
        text = _get_plain_text(event)
        if text.startswith(("/", "\\", "#")):
            text = text[1:].strip()
        if not re.match(r"^切(?:换|到)*(?:画图|图片|生图).*模型", text):
            return
        event.stop_event()
        compact = re.sub(r"\s+", "", text.lower())
        self._ensure_models_initialized()
        profiles = self.model_manager.public_config()["models"]
        target = next(
            (
                profile
                for profile in profiles
                if profile["id"].lower() in compact
                or profile["name"].lower().replace(" ", "") in compact
            ),
            None,
        )
        if target is None:
            current = self.model_manager.active_profile()
            available = "\n".join(
                f"- {profile['id']}: {profile['name']}"
                for profile in profiles
                if profile.get("enabled", True)
            )
            result = (
                f"当前画图模型: {current['id'] if current else '(无)'}\n"
                f"发送 /切换图片模型 <模型ID>\n可用模型:\n{available}"
            )
            yield event.plain_result(result)
            return
        try:
            async with self._config_lock:
                self.model_manager.set_active(target["id"])
        except ImageModelConfigError as exc:
            yield event.plain_result(str(exc))
            return
        logger.info("[IMAGE] 聊天命令切换图片模型: target=%s", target["id"])
        yield event.plain_result(f"画图模型已切换到 {target['name']} ({target['id']})")

    # ── 内部方法 ──────────────────────────────────────────

    async def _do_image_gen(self, event: AstrMessageEvent, prompt: str, aspect: str):
        """按 WebUI 配置执行文生图和有序回退。"""
        self._ensure_models_initialized()
        chain = self.model_manager.generation_chain()
        if not chain:
            yield event.plain_result("画图后端未配置")
            return

        active = chain[0]
        logger.info("[IMAGE] 开始画图, model=%s, prompt=%s...", active["id"], prompt[:80])
        started = time.monotonic()
        yield event.plain_result(f"正在画「{prompt}」...")

        errors = []
        paths = []
        used_profile = None
        for profile in chain:
            attempt_started = time.monotonic()
            try:
                paths = await self.model_manager.generate(
                    profile, prompt, n=1, aspect_ratio=aspect
                )
                used_profile = profile
                logger.info(
                    "[IMAGE] 画图成功, model=%s, elapsed=%.1fs, paths=%s",
                    profile["id"],
                    time.monotonic() - attempt_started,
                    paths,
                )
                break
            except Exception as exc:
                logger.warning("[IMAGE] 模型 %s 生图失败: %s", profile["id"], exc)
                errors.append(f"{profile['name']}: {exc}")
        if not paths:
            yield event.plain_result(f"画图失败: {' | '.join(errors)}")
            return
        if used_profile and used_profile["id"] != active["id"]:
            logger.info(
                "[IMAGE] 默认模型失败后回退成功: %s -> %s",
                active["id"],
                used_profile["id"],
            )
        logger.info("[IMAGE] 本次画图总耗时 %.1fs", time.monotonic() - started)
        self.model_manager.cleanup_expired()

        for path in paths:
            if os.path.exists(path):
                logger.info(f"[IMAGE] 发送图片: {path}")
                yield event.image_result(path)
            else:
                logger.warning(f"[IMAGE] 图片文件不存在: {path}")

    async def _do_image_edit(self, event: AstrMessageEvent, prompt: str, image_path: str):
        """执行图生图"""
        self._ensure_models_initialized()
        chain = [
            profile
            for profile in self.model_manager.generation_chain()
            if profile.get("supports_edit", False)
        ]
        if not chain:
            yield event.plain_result("图生图后端未配置")
            return

        yield event.plain_result(f"正在编辑图片「{prompt}」...")
        errors = []
        paths = []
        for profile in chain:
            try:
                paths = await self.model_manager.edit(
                    profile, image_path, prompt, n=1, aspect_ratio="1:1"
                )
                logger.info("[IMAGE] 图生图成功, model=%s", profile["id"])
                break
            except Exception as exc:
                logger.warning("[IMAGE] 模型 %s 图生图失败: %s", profile["id"], exc)
                errors.append(f"{profile['name']}: {exc}")
        if not paths:
            yield event.plain_result(f"图片编辑失败: {' | '.join(errors)}")
            return

        for path in paths:
            if os.path.exists(path):
                yield event.image_result(path)
