"""Generate account-bound report content without sending through plugin callbacks."""
from __future__ import annotations

from pathlib import Path

from ....task_store import normalize_task
from ...shared.trace_context import TraceContext
from ...utils.logger import logger
from .task_guard import DuplicateGroupTaskError


_SKIPS = {
    "no_messages": "指定时间内没有可分析的群聊记录，本次已跳过。",
    "below_threshold": "群聊有效消息未达到自动分析阈值，本次已跳过。",
    "muted": "群聊当前禁言，本次已跳过。",
}


class UnifiedReportService:
    def __init__(self, plugin):
        self.plugin = plugin

    def readiness(self) -> dict:
        if not self.plugin._initialized or self.plugin._terminating:
            return {"ready": False, "message": "群聊分析尚未完成初始化。"}
        if not self.plugin.context.get_all_providers():
            return {"ready": False, "message": "请先配置群聊分析使用的文本模型。"}
        return {"ready": True, "message": ""}

    def _adapter(self, account: str):
        matches = [(platform, adapter) for platform, adapter in self.plugin.bot_manager.get_all_adapters().items()
                   if callable(getattr(adapter, "_current_wechat_account", None))
                   and adapter._current_wechat_account() == account]
        if len(matches) != 1:
            raise ValueError("无法确认群分析所属微信账号，请刷新连接后重试。")
        return matches[0]

    async def generate(self, account: str, group: str, days: int) -> dict:
        normalize_task({"account_id": account, "action": "group_analysis", "arguments": {"days": days},
                        "targets": [{"type": "group", "id": group}],
                        "schedule": {"kind": "daily", "time": "00:00"}})
        ready = self.readiness()
        if not ready["ready"]:
            raise ValueError(ready["message"])
        platform, adapter = self._adapter(account)
        if not self.plugin.config_manager.is_group_allowed(f"{platform}:GroupMessage:{group}"):
            raise ValueError("所选群聊不在群聊分析的允许名单内。")
        with TraceContext(group_id=group, platform=platform, operation="unified_schedule") as trace:
            trace.metadata["delivery_owner"] = "unified_schedule"
            try:
                result = await self.plugin.analysis_service.execute_daily_analysis(
                    group_id=group, platform_id=platform, manual=False, days=days,
                )
            except DuplicateGroupTaskError:
                trace.finish(status="skipped", error_message="该群已有分析任务执行中。")
                return {"type": "skipped", "message": "该群已有分析任务执行中，本次已跳过。"}
            if self._adapter(account) != (platform, adapter):
                raise ValueError("群分析期间微信连接发生变化，本次报告不会发送。")
            if not result.get("success"):
                reason = str(result.get("reason") or "")
                if reason in _SKIPS:
                    trace.finish(status="skipped", error_message=_SKIPS[reason])
                    return {"type": "skipped", "message": _SKIPS[reason]}
                raise ValueError("群聊分析未生成有效报告，请检查模型和消息记录。")
            analysis = result.get("analysis_result")
            if not isinstance(analysis, dict):
                raise ValueError("群聊分析结果格式不正确。")
            return await self._render(analysis, group, platform, adapter)

    async def _render(self, analysis: dict, group: str, platform: str, adapter) -> dict:
        formats = self.plugin.config_manager.get_output_format()
        preferred = formats[0] if formats else "image"
        # Scheduled reports use a single gated image or text message. HTML files,
        # comics and album uploads remain explicit plugin actions.
        if preferred != "text":
            async def avatar(user_id, size=None):
                return await adapter.get_user_avatar_url(user_id, size=size or 40)

            try:
                image, _html = await self.plugin.report_generator.generate_image_report(
                    analysis, group, self.plugin.html_render, avatar_url_getter=avatar,
                    avatar_cache_namespace=f"{platform}:{adapter._current_wechat_account()}",
                    allow_alphanumeric_user_ids=True,
                    template_theme=self.plugin.config_manager.get_report_template(),
                )
                if isinstance(image, bytes) or (isinstance(image, str) and
                        (image.startswith("base64://") or not image.startswith(("http://", "https://")) and Path(image).is_file())):
                    return {"type": "image", "image": image}
            except Exception as exc:
                logger.warning("统一任务图片报告生成失败，改用文字报告 (%s)", type(exc).__name__)
        text = self.plugin.report_generator.generate_text_report(analysis)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("群聊分析没有可发送的报告内容。")
        return {"type": "text", "text": text}
