async def process(
    self,
    event: AstrMessageEvent,
) -> None | AsyncGenerator[None, None]:
    """处理事件"""
    activated_handlers: list[StarHandlerMetadata] = event.get_extra(
        "activated_handlers",
    )
    # 有插件 Handler 被激活
    if activated_handlers:
        async for resp in self.star_request_sub_stage.process(event):
            # 生成器返回值处理
            if isinstance(resp, ProviderRequest):
                # Handler 的 LLM 请求
                event.set_extra("provider_request", resp)
                _t = False
                async for _ in self.agent_sub_stage.process(event):
                    _t = True
                    yield
                if not _t:
                    yield
            else:
                yield

    # 调用 LLM 相关请求
    if not self.ctx.astrbot_config["provider_settings"].get("enable", True):
        return

    if (
        not event._has_send_oper
        and event.is_at_or_wake_command
        and not event.call_llm
    ):
        # 是否有过发送操作 and 是否是被 @ 或者通过唤醒前缀
        if (
            event.get_result() and not event.is_stopped()
        ) or not event.get_result():
            async for _ in self.agent_sub_stage.process(event):
                yield
