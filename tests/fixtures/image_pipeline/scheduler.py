async def _process_stages(self, event: AstrMessageEvent, from_stage=0) -> None:
    """依次执行各个阶段

    Args:
        event (AstrMessageEvent): 事件对象
        from_stage (int): 从第几个阶段开始执行, 默认从0开始

    """
    for i in range(from_stage, len(self.stages)):
        stage = self.stages[i]  # 获取当前要执行的阶段
        # logger.debug(f"执行阶段 {stage.__class__.__name__}")
        coroutine = stage.process(
            event,
        )  # 调用阶段的process方法, 返回协程或者异步生成器

        if isinstance(coroutine, AsyncGenerator):
            # 如果返回的是异步生成器, 实现洋葱模型的核心
            async for _ in coroutine:
                # 此处是前置处理完成后的暂停点(yield), 下面开始执行后续阶段
                if event.is_stopped():
                    logger.debug(
                        f"阶段 {stage.__class__.__name__} 已终止事件传播。",
                    )
                    break

                # 递归调用, 处理所有后续阶段
                await self._process_stages(event, i + 1)

                # 此处是后续所有阶段处理完毕后返回的点, 执行后置处理
                if event.is_stopped():
                    logger.debug(
                        f"阶段 {stage.__class__.__name__} 已终止事件传播。",
                    )
                    break
        else:
            # 如果返回的是普通协程(不含yield的async函数), 则不进入下一层(基线条件)
            # 简单地等待它执行完成, 然后继续执行下一个阶段
            await coroutine

            if event.is_stopped():
                logger.debug(f"阶段 {stage.__class__.__name__} 已终止事件传播。")
                break
