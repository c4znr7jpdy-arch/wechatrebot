"""消息事件监听模块。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)


class EventsMixin:
    """事件监听相关混入类。"""

    DEFAULT_LIVE_INTERJECT_PROMPT = """[系统任务：群聊自然插话]
你被允许在群聊正在进行时自然插一句话。请先理解最近的连续聊天上下文，判断当前大家正在聊什么。

[最近群聊上下文，按时间从旧到新]
{{live_context}}

[最后一条消息]
{{last_message}}

[行动要求]
1. 只参考上面的“最近群聊上下文”，不要把已被时间断点丢弃的旧话题拉回来。
2. 回复时优先承接触发插话前的最后一条消息。
3. 如果最后一条只是附和、表情、短句、图片提示或缺少上下文，则结合当前连续上下文自然接话。
4. 如果上下文里有图片内容描述，要把图片当成真实聊天内容来理解，不要假装没看到。
5. 不要说明你是概率触发或主动插话。
6. 不要总结聊天记录，不要生硬地逐条回应，不要突然切换到无关话题。
7. 像群友自然参与聊天一样，用一句简短、口语化、符合你人格的回复接入当前话题。

[当前时间]
{{current_time}}"""

    data_lock: Any
    session_data: dict
    last_message_times: dict[str, float]
    session_temp_state: dict[str, dict]
    first_message_logged: set[str]
    live_interject_sessions: set[str]
    live_interject_last_times: dict[str, float]
    live_interject_image_caption_cache: dict[str, str]
    scheduler: Any

    @staticmethod
    def _strip_system_identity_prefix(text: str) -> str:
        cleaned = str(text or "").strip()
        if cleaned.startswith("[系统身份提示：") and "]\n" in cleaned:
            cleaned = cleaned.split("]\n", 1)[1].lstrip()
        return cleaned

    @staticmethod
    def _looks_like_plain_command(text: str) -> bool:
        cleaned = EventsMixin._strip_system_identity_prefix(text)
        if not cleaned:
            return False
        patterns = (
            r"^图来$",
            r"^点歌(?:\s+.+)?$",
            r"^.+天气$",
            r"^油价\s*.+$",
            r"^[\w\u4e00-\u9fff]+#\w{3,5}$",
        )
        return any(re.fullmatch(pattern, cleaned) for pattern in patterns)

    def _extract_event_text(self, event: AstrMessageEvent) -> str:
        """尽量从 AstrBot 事件里取出纯文本内容。"""
        for method_name in ("get_message_str", "get_plain_text"):
            method = getattr(event, method_name, None)
            if callable(method):
                try:
                    text = method()
                    if text:
                        return str(text).strip()
                except Exception:
                    pass

        try:
            message_obj = getattr(event, "message_obj", None)
            text = getattr(message_obj, "message_str", None)
            if text:
                return str(text).strip()
        except Exception:
            pass

        parts: list[str] = []
        try:
            for segment in event.get_messages() or []:
                text = getattr(segment, "text", None)
                if text:
                    parts.append(str(text))
                    continue
                data = getattr(segment, "data", None)
                if isinstance(data, dict):
                    value = data.get("text") or data.get("content")
                    if value:
                        parts.append(str(value))
                        continue
                segment_type = str(
                    getattr(segment, "type", None)
                    or getattr(segment, "_type", None)
                    or segment.__class__.__name__
                ).lower()
                if "image" in segment_type:
                    parts.append("[图片]")
                elif any(key in segment_type for key in ("record", "audio", "voice")):
                    parts.append("[语音]")
                elif "video" in segment_type:
                    parts.append("[视频]")
                elif "file" in segment_type:
                    parts.append("[文件]")
        except Exception:
            pass
        return " ".join(parts).strip()

    def _extract_event_timestamp(self, event: AstrMessageEvent) -> float:
        """尽量取消息自身时间，取不到则使用当前处理时间。"""
        candidates: list[Any] = []
        try:
            message_obj = getattr(event, "message_obj", None)
            if message_obj is not None:
                candidates.extend(
                    [
                        getattr(message_obj, "timestamp", None),
                        getattr(message_obj, "time", None),
                        getattr(message_obj, "message_time", None),
                    ]
                )
                raw_message = getattr(message_obj, "raw_message", None)
                if isinstance(raw_message, dict):
                    candidates.extend(
                        [
                            raw_message.get("time"),
                            raw_message.get("timestamp"),
                            raw_message.get("message_time"),
                        ]
                    )
        except Exception:
            pass

        for candidate in candidates:
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            if value > 10_000_000_000:
                value = value / 1000
            return value

        return time.time()

    def _get_live_interject_settings(self, session_config: dict) -> dict[str, Any]:
        settings = session_config.get("live_interject_settings", {})
        if not isinstance(settings, dict):
            settings = {}

        try:
            probability_percent = float(settings.get("probability_percent", 1))
        except (TypeError, ValueError):
            probability_percent = 1.0
        probability = max(0.0, min(probability_percent / 100.0, 1.0))

        try:
            cooldown_seconds = int(settings.get("cooldown_seconds", 300))
        except (TypeError, ValueError):
            cooldown_seconds = 300

        try:
            max_response_delay_seconds = int(
                settings.get("max_response_delay_seconds", 120)
            )
        except (TypeError, ValueError):
            max_response_delay_seconds = 120

        prompt = str(
            settings.get("prompt") or self.DEFAULT_LIVE_INTERJECT_PROMPT
        ).strip()

        def _int_setting(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(settings.get(key, default))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))

        image_caption_provider_id = str(
            settings.get("image_caption_provider_id") or ""
        ).strip()
        image_caption_prompt = str(settings.get("image_caption_prompt") or "").strip()
        if not image_caption_provider_id or not image_caption_prompt:
            try:
                cfg = self.context.get_config()
            except Exception:
                cfg = {}
            provider_settings = {}
            if isinstance(cfg, dict):
                provider_settings = cfg.get("provider_settings") or {}
            if not image_caption_provider_id:
                image_caption_provider_id = str(
                    provider_settings.get("default_image_caption_provider_id")
                    or "openai_2/mimo-v2.5"
                ).strip()
            if not image_caption_prompt:
                image_caption_prompt = str(
                    provider_settings.get("image_caption_prompt")
                    or "请用中文简要描述这张图片里的关键信息，尤其是截图文字。"
                ).strip()

        return {
            "enable": bool(settings.get("enable_live_interject", True)),
            "probability": probability,
            "cooldown_seconds": max(0, cooldown_seconds),
            "max_response_delay_seconds": max(1, max_response_delay_seconds),
            "prompt": prompt or self.DEFAULT_LIVE_INTERJECT_PROMPT,
            "context_messages": _int_setting("context_messages", 20, 1, 80),
            "context_max_age_minutes": _int_setting(
                "context_max_age_minutes", 20, 1, 240
            ),
            "topic_gap_minutes": _int_setting("topic_gap_minutes", 5, 1, 60),
            "context_max_chars": _int_setting("context_max_chars", 3500, 500, 20000),
            "image_caption_limit": _int_setting("image_caption_limit", 3, 0, 10),
            "image_caption_timeout_seconds": _int_setting(
                "image_caption_timeout_seconds", 12, 1, 60
            ),
            "image_caption_provider_id": image_caption_provider_id,
            "image_caption_prompt": image_caption_prompt,
        }

    def _is_live_interject_candidate(self, message_text: str) -> bool:
        if not message_text:
            return False
        stripped = self._strip_system_identity_prefix(message_text)
        if not stripped:
            return False
        if self._looks_like_plain_command(stripped):
            return False
        return not stripped.startswith(("/", "\\", "#"))

    def _get_live_record_timestamp(self, record: Any) -> float:
        for field in ("timestamp", "time", "message_time", "created_at"):
            value = self._get_platform_record_field(record, field, None)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return parsed.timestamp()
                except ValueError:
                    pass
            try:
                ts = float(value)
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            if ts > 10_000_000_000:
                ts = ts / 1000
            return ts
        return 0.0

    def _extract_live_context_image_refs(self, content: Any) -> list[str]:
        refs: list[str] = []

        def _add(value: Any) -> None:
            if not value:
                return
            ref = str(value).strip()
            if ref and ref not in refs:
                refs.append(ref)

        def _walk(value: Any, parent_is_image: bool = False) -> None:
            if isinstance(value, list):
                for item in value:
                    _walk(item, parent_is_image)
                return
            if not isinstance(value, dict):
                return

            part_type = str(value.get("type") or value.get("_type") or "").lower()
            is_image = parent_is_image or part_type in {
                "image",
                "image_url",
                "img",
                "photo",
            }
            if is_image:
                for key in ("url", "file", "path", "src"):
                    _add(value.get(key))
                data = value.get("data")
                if isinstance(data, dict):
                    for key in ("url", "file", "path", "src"):
                        _add(data.get(key))

            for key in ("message", "content", "segments", "data"):
                nested = value.get(key)
                if isinstance(nested, (dict, list)):
                    _walk(nested, is_image)

        _walk(content)
        return refs

    def _extract_live_context_existing_caption(self, text: str) -> str:
        if not text:
            return ""
        match = re.search(r"<image_caption>(.*?)</image_caption>", text, re.DOTALL)
        if match:
            return " ".join(match.group(1).split())
        match = re.search(r"\[图片[:：]\s*(.*?)\]", text)
        if match:
            return " ".join(match.group(1).split())
        match = re.search(r"\[Image[:：]\s*(.*?)\]", text)
        if match:
            return " ".join(match.group(1).split())
        return ""

    async def _caption_live_context_image(
        self,
        image_ref: str,
        settings: dict[str, Any],
    ) -> str:
        if not image_ref or settings["image_caption_limit"] <= 0:
            return ""

        cache = getattr(self, "live_interject_image_caption_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self.live_interject_image_caption_cache = cache
        if image_ref in cache:
            return cache[image_ref]

        provider_id = settings.get("image_caption_provider_id")
        if not provider_id:
            return ""

        prompt = settings.get("image_caption_prompt") or (
            "请用中文简要描述这张图片里的关键信息，尤其是截图文字。"
        )
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=str(provider_id),
                    prompt=str(prompt),
                    image_urls=[image_ref],
                ),
                timeout=settings["image_caption_timeout_seconds"],
            )
            caption = ""
            if response and getattr(response, "completion_text", None):
                caption = " ".join(str(response.completion_text).split())
            if caption:
                if len(cache) >= 200:
                    oldest_key = next(iter(cache))
                    cache.pop(oldest_key, None)
                cache[image_ref] = caption
                return caption
        except Exception as e:
            logger.debug(f"[主动消息] 随机插话图片描述失败: {e}")
        return ""

    def _select_live_context_records(
        self,
        records: list[Any],
        trigger_time: float,
        settings: dict[str, Any],
    ) -> list[Any]:
        max_age_seconds = settings["context_max_age_minutes"] * 60
        topic_gap_seconds = settings["topic_gap_minutes"] * 60

        with_ts: list[tuple[float, int, Any]] = []
        for index, record in enumerate(records):
            ts = self._get_live_record_timestamp(record)
            if ts <= 0:
                continue
            if ts > trigger_time + 2:
                continue
            if trigger_time - ts > max_age_seconds:
                continue
            with_ts.append((ts, index, record))

        with_ts.sort(key=lambda item: (item[0], item[1]))
        if not with_ts:
            return []

        selected: list[tuple[float, int, Any]] = [with_ts[-1]]
        newer_ts = with_ts[-1][0]
        for item in reversed(with_ts[:-1]):
            ts = item[0]
            if newer_ts - ts > topic_gap_seconds:
                break
            selected.append(item)
            newer_ts = ts

        selected.reverse()
        return [item[2] for item in selected[-settings["context_messages"] :]]

    async def _format_live_context_record(
        self,
        record: Any,
        settings: dict[str, Any],
        image_caption_budget: dict[str, int],
    ) -> str:
        content = self._get_platform_record_field(record, "content", None)
        raw_text = self._extract_platform_message_text(content)
        text = self._sanitize_platform_context_text(raw_text)
        image_refs = self._extract_live_context_image_refs(content)

        if image_refs:
            caption = self._extract_live_context_existing_caption(raw_text)
            if not caption and image_caption_budget["remaining"] > 0:
                image_caption_budget["remaining"] -= 1
                caption = await self._caption_live_context_image(image_refs[0], settings)
            if caption and "[图片" in text:
                text = re.sub(r"\[图片\]", f"[图片内容：{caption}]", text, count=1)
            elif caption:
                text = f"{text} [图片内容：{caption}]".strip()
            elif not text:
                text = "[图片]"

        if not text:
            return ""

        sender_name = self._sanitize_platform_context_text(
            self._get_platform_record_field(record, "sender_name", None)
            or self._get_platform_record_field(record, "sender_id", None)
            or "未知用户"
        )
        ts = self._get_live_record_timestamp(record)
        time_label = time.strftime("%H:%M", time.localtime(ts)) if ts > 0 else "--:--"
        return f"{time_label} {sender_name}: {text}"

    async def _build_live_interject_context(
        self,
        session_id: str,
        trigger_time: float,
        last_message_text: str,
        settings: dict[str, Any],
    ) -> str:
        load_limit = max(settings["context_messages"] * 3, settings["context_messages"])
        records, raw_count = await self._load_platform_message_history_records(
            session_id=session_id,
            limit=min(load_limit, 200),
        )
        selected = self._select_live_context_records(records, trigger_time, settings)

        lines: list[str] = []
        image_caption_budget = {"remaining": settings["image_caption_limit"]}
        for record in selected:
            line = await self._format_live_context_record(
                record, settings, image_caption_budget
            )
            if line:
                lines.append(line)

        normalized_last = self._sanitize_platform_context_text(last_message_text)
        if normalized_last and (
            not lines or normalized_last not in lines[-1]
        ):
            time_label = time.strftime("%H:%M", time.localtime(trigger_time))
            lines.append(f"{time_label} 触发消息: {normalized_last}")

        if not lines:
            return normalized_last or "（没有可用的连续聊天上下文）"

        context = "\n".join(lines)
        max_chars = settings["context_max_chars"]
        if len(context) > max_chars:
            trimmed = list(lines)
            while len(trimmed) > 1 and len("\n".join(trimmed)) > max_chars:
                trimmed.pop(0)
            context = "\n".join(trimmed)
            if len(context) > max_chars:
                context = context[-max_chars:]

        logger.info(
            f"[主动消息] 随机插话上下文已构建：平台流水原始 {raw_count} 条，"
            f"连续窗口 {len(lines)} 条，长度 {len(context)} 字，"
            f"话题断点 {settings['topic_gap_minutes']} 分钟。"
        )
        return context

    async def _maybe_schedule_live_interject(
        self,
        normalized_session_id: str,
        session_config: dict,
        message_text: str,
        current_time: float,
        message_time: float,
    ) -> None:
        settings = self._get_live_interject_settings(session_config)
        if not settings["enable"] or settings["probability"] <= 0:
            return
        if not self._is_live_interject_candidate(message_text):
            return
        max_delay = settings["max_response_delay_seconds"]
        if current_time - message_time > max_delay:
            logger.debug(
                f"[主动消息] {self._get_session_log_str(normalized_session_id, session_config)} "
                f"随机插话跳过旧消息：消息已过去 {current_time - message_time:.1f}s。"
            )
            return
        if normalized_session_id in self.live_interject_sessions:
            return

        last_time = self.live_interject_last_times.get(normalized_session_id, 0)
        if current_time - last_time < settings["cooldown_seconds"]:
            return
        if random.random() >= settings["probability"]:
            return

        self.live_interject_last_times[normalized_session_id] = current_time
        task = asyncio.create_task(
            self._run_live_interject(
                normalized_session_id,
                session_config,
                message_text,
                message_time,
                settings,
            )
        )
        task.add_done_callback(self._handle_live_interject_task_done)

    def _handle_live_interject_task_done(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[主动消息] 群聊随机插话任务异常: {e}", exc_info=True)

    async def _run_live_interject(
        self,
        session_id: str,
        session_config: dict,
        last_message_text: str,
        trigger_time: float,
        settings: dict[str, Any],
    ) -> None:
        if session_id in self.live_interject_sessions:
            return
        self.live_interject_sessions.add(session_id)
        try:
            max_response_delay_seconds = settings["max_response_delay_seconds"]
            if time.time() - trigger_time > max_response_delay_seconds:
                logger.debug(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} 随机插话已过期，跳过。"
                )
                return

            allowed, reason = await self._is_chat_allowed(session_id)
            if not allowed:
                logger.debug(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} 随机插话被跳过: {reason}"
                )
                return

            llm_request = await self._prepare_llm_request(session_id)
            if not llm_request:
                return

            live_context = await self._build_live_interject_context(
                llm_request["session_id"],
                trigger_time,
                last_message_text,
                settings,
            )
            prompt_template = settings["prompt"]
            interject_config = dict(session_config)
            rendered_prompt = (
                prompt_template.replace("{{last_message}}", last_message_text)
                .replace("{{live_context}}", live_context)
                .replace("{{context_window}}", live_context)
            )
            if (
                "{{live_context}}" not in prompt_template
                and "{{context_window}}" not in prompt_template
            ):
                rendered_prompt = (
                    "[最近群聊上下文，按时间从旧到新]\n"
                    f"{live_context}\n\n"
                    f"{rendered_prompt}"
                )
            interject_config["proactive_prompt"] = rendered_prompt

            response_text, final_user_prompt = await self._generate_llm_response(
                llm_request["session_id"],
                interject_config,
                llm_request["history"],
                llm_request["system_prompt"],
                0,
            )
            if not response_text:
                return

            if time.time() - trigger_time > max_response_delay_seconds:
                logger.info(
                    f"[主动消息] {self._get_session_log_str(session_id, session_config)} "
                    f"随机插话回复生成耗时超过 {max_response_delay_seconds}s，丢弃旧回复。"
                )
                return

            await self._send_proactive_message(llm_request["session_id"], response_text)
            await self._archive_live_interject_pair(
                llm_request["conv_id"],
                final_user_prompt,
                response_text,
            )
            logger.info(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 已完成一次群聊随机插话。"
            )
        finally:
            self.live_interject_sessions.discard(session_id)

    async def _archive_live_interject_pair(
        self, conv_id: str, user_prompt: str, assistant_response: str
    ) -> None:
        try:
            user_msg_obj = UserMessageSegment(content=[TextPart(text=user_prompt)])
            assistant_msg_obj = AssistantMessageSegment(
                content=[TextPart(text=assistant_response)]
            )
            await self.context.conversation_manager.add_message_pair(
                cid=conv_id,
                user_message=user_msg_obj,
                assistant_message=assistant_msg_obj,
            )
        except Exception as e:
            logger.debug(f"[主动消息] 归档群聊随机插话对话失败: {e}")

    async def on_friend_message(self, event: AstrMessageEvent):
        """监听私聊消息，取消旧任务，并重置计时器和计数器。"""
        # 没有消息内容则无需处理
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        # 统一会话键，避免跨平台前缀变化导致状态分裂
        normalized_session_id = self._normalize_session_id(session_id)

        # 缓存 self_id，便于装饰钩子构造事件
        if event.get_self_id():
            async with self.data_lock:
                self.session_data.setdefault(session_id, {})["self_id"] = (
                    event.get_self_id()
                )

        # 更新消息时间（仅插件启动后用于自动触发）
        current_time = time.time()
        self.last_message_times[normalized_session_id] = current_time

        async with self.data_lock:
            # 合并旧键数据
            # 将旧键数据迁移到规范化键，保持计数与 self_id 连续
            if normalized_session_id != session_id and session_id in self.session_data:
                existing_payload = self.session_data.get(session_id, {})
                self.session_data.setdefault(normalized_session_id, {}).update(
                    existing_payload
                )
                del self.session_data[session_id]

            if current_time >= self.plugin_start_time:
                self.session_data.setdefault(normalized_session_id, {})[
                    "last_message_time"
                ] = current_time

        # 取消自动触发：同时处理原键与规范化键，避免漏取消
        auto_trigger_cancelled = await self._cancel_all_related_auto_triggers(
            session_id
        )
        if normalized_session_id != session_id:
            normalized_cancelled = await self._cancel_all_related_auto_triggers(
                normalized_session_id
            )
            auto_trigger_cancelled = auto_trigger_cancelled or normalized_cancelled

        # 避免重复刷屏日志；仅在确实取消了自动触发计时器时打印
        session_config = self._get_session_config(normalized_session_id)
        if (
            auto_trigger_cancelled
            and session_config
            and session_config.get("enable", False)
            and normalized_session_id not in self.first_message_logged
        ):
            self.first_message_logged.add(normalized_session_id)
            logger.info(
                f"[主动消息] 已记录 {self._get_session_log_str(normalized_session_id, session_config)} 的消息时间并取消自动触发喵。"
            )

        # 未启用或配置无效则跳过
        session_config = self._get_session_config(normalized_session_id)
        if not session_config or not session_config.get("enable", False):
            logger.debug(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        # 取消旧的调度任务并重新安排
        cancelled = False
        try:
            self.scheduler.remove_job(normalized_session_id)
            cancelled = True
        except Exception:
            pass

        if normalized_session_id != session_id:
            try:
                self.scheduler.remove_job(session_id)
                cancelled = True
            except Exception:
                pass

        # 兜底清理同目标任务（处理幽灵任务）
        self._purge_related_jobs(normalized_session_id)

        if cancelled:
            logger.info(
                f"[主动消息] 用户已回复喵，已取消 {self._get_session_log_str(normalized_session_id, session_config)} 的主动消息任务喵。"
            )

        logger.info(
            f"[主动消息] 重置 {self._get_session_log_str(normalized_session_id, session_config)} 的未回复计数器为0喵。"
        )
        await self._schedule_next_chat_and_save(
            normalized_session_id, reset_counter=True
        )

    async def on_group_message(self, event: AstrMessageEvent):
        """监听群聊消息流，重置沉默倒计时，并取消已计划的主动消息任务。"""
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        normalized_session_id = self._normalize_session_id(session_id)

        # 缓存 self_id
        if event.get_self_id():
            async with self.data_lock:
                self.session_data.setdefault(session_id, {})["self_id"] = (
                    event.get_self_id()
                )

        # 过滤 Bot 自身消息，避免把机器人发言误判成“用户活跃”
        sender_id = None
        try:
            if hasattr(event, "message_obj") and event.message_obj:
                sender = getattr(event.message_obj, "sender", None)
                if sender:
                    sender_id = getattr(sender, "id", None) or getattr(
                        sender, "user_id", None
                    )
            if not sender_id:
                sender_id = getattr(event, "user_id", None) or getattr(
                    event, "sender_id", None
                )
        except Exception as e:
            logger.debug(f"[主动消息] 获取群聊发送者ID失败喵: {e}")

        self_id = event.get_self_id() or self.session_data.get(session_id, {}).get(
            "self_id"
        )
        if self_id and sender_id and str(sender_id) == str(self_id):
            logger.debug(
                f"[主动消息] 检测到 {self._get_session_log_str(session_id)} 的 Bot 自身消息，跳过用户逻辑喵。"
            )
            return

        # 记录群聊最近用户活跃时间（用于临时态超时清理）
        current_time = time.time()
        self.session_temp_state[normalized_session_id] = {
            "last_user_time": current_time
        }
        logger.debug(
            f"[主动消息] 记录 {self._get_session_log_str(session_id)} 的消息时间戳喵: {current_time}"
        )

        # 更新消息时间（仅插件启动后用于自动触发）
        self.last_message_times[normalized_session_id] = current_time

        async with self.data_lock:
            if normalized_session_id != session_id and session_id in self.session_data:
                existing_payload = self.session_data.get(session_id, {})
                self.session_data.setdefault(normalized_session_id, {}).update(
                    existing_payload
                )
                del self.session_data[session_id]

            if current_time >= self.plugin_start_time:
                self.session_data.setdefault(normalized_session_id, {})[
                    "last_message_time"
                ] = current_time
                logger.debug(
                    f"[主动消息] 已记录插件启动后 {self._get_session_log_str(session_id)} 的消息时间喵 -> {current_time}"
                )
            else:
                logger.debug(
                    f"[主动消息] 忽略插件启动前 {self._get_session_log_str(session_id)} 的旧消息用于自动主动消息任务喵 -> {current_time}"
                )

        # 取消自动触发
        auto_trigger_cancelled = await self._cancel_all_related_auto_triggers(
            session_id
        )
        if normalized_session_id != session_id:
            normalized_cancelled = await self._cancel_all_related_auto_triggers(
                normalized_session_id
            )
            auto_trigger_cancelled = auto_trigger_cancelled or normalized_cancelled

        # 读取当前会话配置，供日志与启用状态判断复用，避免重复查询。
        session_config = self._get_session_config(normalized_session_id)

        # 避免重复刷屏日志；仅在确实取消了自动触发计时器时打印
        if (
            auto_trigger_cancelled
            and session_config
            and session_config.get("enable", False)
            and normalized_session_id not in self.first_message_logged
        ):
            self.first_message_logged.add(normalized_session_id)
            logger.info(
                f"[主动消息] 已记录 {self._get_session_log_str(normalized_session_id, session_config)} 的消息时间并取消自动触发喵。"
            )

        # 未启用或配置无效则跳过
        if not session_config or not session_config.get("enable", False):
            logger.debug(
                f"[主动消息] {self._get_session_log_str(session_id, session_config)} 未启用或配置无效，跳过处理喵。"
            )
            return

        await self._maybe_schedule_live_interject(
            normalized_session_id,
            session_config,
            self._extract_event_text(event),
            current_time,
            self._extract_event_timestamp(event),
        )

        # 取消群聊中的既有调度任务（含“已持久化但未入调度器”的兜底判断）
        had_scheduled_task = False
        if self.scheduler.get_job(normalized_session_id):
            had_scheduled_task = True
        if normalized_session_id != session_id and self.scheduler.get_job(session_id):
            had_scheduled_task = True
        if (
            not had_scheduled_task
            and normalized_session_id in self.session_data
            and self._is_persisted_task_still_valid(
                normalized_session_id,
                self.session_data.get(normalized_session_id),
                current_time=current_time,
            )
        ):
            had_scheduled_task = True

        cancelled = False
        try:
            self.scheduler.remove_job(normalized_session_id)
            cancelled = True
        except Exception:
            pass

        if normalized_session_id != session_id:
            try:
                self.scheduler.remove_job(session_id)
                cancelled = True
            except Exception:
                pass

        # 兜底清理同目标任务（处理幽灵任务）
        self._purge_related_jobs(normalized_session_id)

        if cancelled:
            logger.info(
                f"[主动消息] 群聊活跃喵，已取消 {self._get_session_log_str(normalized_session_id, session_config)} 的主动消息任务喵。"
            )
        elif had_scheduled_task:
            logger.info(
                f"[主动消息] 群聊活跃喵，{self._get_session_log_str(normalized_session_id, session_config)} 未找到可取消的主动消息任务（可能已被提前清理）喵。"
            )

        # 重置沉默倒计时
        await self._reset_group_silence_timer(normalized_session_id)

        # 清理计数与任务标记：用户发言后将未回复计数归零
        async with self.data_lock:
            changed = False
            if normalized_session_id in self.session_data:
                current_unanswered = self.session_data[normalized_session_id].get(
                    "unanswered_count", 0
                )
                self.session_data[normalized_session_id]["unanswered_count"] = 0
                changed = True
                if current_unanswered > 0:
                    logger.debug(
                        f"[主动消息] {self._get_session_log_str(normalized_session_id, session_config)} 的用户已回复， 未回复计数器已重置喵。"
                    )

                if "group" in normalized_session_id.lower():
                    changed = (
                        self._clear_session_schedule_state(normalized_session_id)
                        or changed
                    )

            if changed:
                await self._save_data_internal()

    async def on_after_message_sent(self, event: AstrMessageEvent):
        """监听 Bot 发送消息后事件，重置群聊沉默倒计时。"""
        session_id = event.unified_msg_origin
        normalized_session_id = self._normalize_session_id(session_id)

        # 只对群聊生效
        if "group" not in normalized_session_id.lower():
            return

        # Bot 自己发言后，清理已计划的调度任务，避免重复触发
        try:
            self.scheduler.remove_job(normalized_session_id)
            if normalized_session_id != session_id:
                self.scheduler.remove_job(session_id)
            logger.debug(
                f"[主动消息] Bot已发言，已取消 {self._get_session_log_str(normalized_session_id)} 的主动消息任务喵。"
            )
        except Exception as e:
            logger.debug(
                f"[主动消息] {self._get_session_log_str(normalized_session_id)} 没有待取消的调度任务喵: {e}"
            )

        # 兜底清理同目标任务
        self._purge_related_jobs(normalized_session_id)

        async with self.data_lock:
            changed = False
            if normalized_session_id != session_id and session_id in self.session_data:
                existing_payload = self.session_data.get(session_id, {})
                self.session_data.setdefault(normalized_session_id, {}).update(
                    existing_payload
                )
                del self.session_data[session_id]
                changed = True

            if self._clear_session_schedule_state(normalized_session_id):
                changed = True

            if changed:
                await self._save_data_internal()

        # 周期性清理临时状态，避免 session_temp_state 长期膨胀
        current_time = time.time()
        self._cleanup_counter += 1

        # 周期性清理过期会话状态
        if self._cleanup_counter % 10 == 0:
            self._cleanup_expired_session_states(current_time)

        try:
            await self._reset_group_silence_timer(normalized_session_id)
            if normalized_session_id in self.session_temp_state:
                del self.session_temp_state[normalized_session_id]
        except Exception as e:
            logger.error(
                f"[主动消息] {self._get_session_log_str(session_id)} 的 after_message_sent 处理异常喵: {e}"
            )
