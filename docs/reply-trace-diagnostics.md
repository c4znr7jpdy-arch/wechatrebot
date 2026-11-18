# 回复链路诊断

本功能按每条实际引擎事件的随机标识关联日志，不依据时间接近、聊天名称或相邻行推断。管理器的 `get_response_traces` 保留原接口和 `time/title/duration_seconds/duration_text/stages` 字段，补充 `trace_id/status/complete`。运行记录中的手动发送测试仍使用原有测试链路。

## 阶段与可信边界

- 收到消息：事件总线收到本机微信适配器事件时记录；复用 `event.trace.span_id` 的随机 UUID，不使用微信账号或消息 ID。
- 回复判断：智能回复模块执行实际决策时记录，包含明确点名快路径、不回复、失败；没有经过该模块时不补造阶段。
- 模型请求：在主回复 runner 的实际 provider 调用边界记录，包括重试/回退的不同尝试和参数修复请求。流式只累计等待 provider 的时间，不把下游处理或发送耗时计入模型时间。其他插件自行发起的模型调用不由相邻日志猜测。
- 提交微信接口：在实际 OneBot 发送 action 的 await 边界记录，包括分段、文件和转发。成功仅证明接口接受，不证明对方收到或已读。`after_message_sent` 在发送异常被上层捕获后也可能触发，因此不能作为成功证据。
- 回复处理：包装原 scheduler 的完整执行，保留授权、异常、取消及临时文件清理行为；直接返回且无发送记录显示跳过。

同一事件的多个调用带独立 operation 序号。同会话同时处理的消息也使用不同 trace。耗时来自单调时钟，日期来自 UTC 墙钟；系统时间回拨不改变耗时。缺失接收、结束或中间记录时，总耗时为未知；缺失某阶段起点时，该阶段耗时也为未知。旧日志仅能说明进入准备发送，不能关联其他行，也不能把“AI 回复完成”当成投递成功。

## 数据和资源

权威 writer 位于 `src/wechatrebot_manager/reply_trace_runtime.py`，按项目现有 runtime helper 方式原样包含在核心补丁中，测试验证两者一致。事件仅允许随机 trace、固定阶段/结果、序号、群聊/私聊类型及时间，不接受正文、异常文字、模型地址、密钥、账号、联系人或群标识。管理器仅投影固定字段；额外字段不会返回。

writer 复用引擎日志及轮转，不新增数据库、后台任务或独立日志文件。每事件最多 512 条结构化记录；读端最多读取末尾 1 MiB / 3,000 行并最多返回 50 条链路。截断保守标记不完整。引擎运行日志位置复用 `customer_logs.log_sources`。

## 构建与兼容

本改动修改 `astrbot-4.26.6-wechat.patch` 和 `astrbot-4.26.6-outbound-policy.patch`，并追加智能回复决策补丁。固定 `vendor/` 和它的哈希清单没有变化。

这两个核心补丁属于 `update_local_incremental.BASE_INPUTS`；旧基线必须拒绝组件更新，不能只投递依赖新 writer 的插件。后续实际部署需要包含新 writer 的完整运行时基线。本次仅修改代码和离线测试，不构建、不安装、不发布、不启动机器人。

## 验证

定向测试覆盖交错群聊/私聊、同类会话并发、实际 provider 和发送 await、发送失败、取消、空上下文、流式消费者暂停、跨日/时钟回拨、缺阶段、旧日志、固定字段输出及 writer 条数上限。补丁应用测试按正式顺序应用微信、发送权限、后台权限和授权补丁，确认语法与辅助模块交付；已有空提及、工具回复、权限和 provider 测试继续运行。

这些检查不等同于真实微信发送验收或客户完整升级验收。

2026-11-18 执行结果：`PYTHONPATH=src python -m pytest -q tests/test_reply_trace_runtime.py tests/test_response_trace.py tests/test_angel_empty_mention.py tests/test_provider_runtime_patch.py tests/test_license_runtime_patches.py tests/test_tool_reply_lifecycle.py tests/test_background_plugin_permissions.py` 为 97 passed；追加不回复决策用例后，前三个文件定向重跑为 38 passed。`python scripts/source_inputs.py` 通过，`git diff --check` 无差异格式错误。

独立审查后补齐 provider 流的确定性关闭：外层关闭返回前等待底层可用的 `aclose()`；保留已有请求异常或取消，以固定 `cleanup_status` 标记二次清理失败，不输出异常文字。无 `aclose()` 的异步迭代器保持兼容。管理器每次读取动态选择实际日志路径，支持管理器先启动、引擎稍后首次创建日志。新增关闭顺序、关闭失败/取消、无关闭方法及日志延迟创建回归；两个 trace 测试文件为 31 passed。

流式最终响应（`is_chunk=False`）在交给 runner 前完成模型阶段并关闭 provider，避免上层取到最终响应后直接返回，使正常请求被延后记录为取消。fallback 和 step 的消费边界也使用确定性关闭，覆盖候选切换、最终返回和分块中止。测试提取实际补丁后的三个 runner 方法并使用运行时相同版本的 tenacity，验证正常完成、错误响应/异常回退、等待中取消和分块后停止，确保模型结束记录先于整个回复处理结束；新增 tenacity 仅为测试依赖。

上述流式边界修正后执行 `PYTHONPATH=src python -m pytest -q tests/test_reply_trace_runtime.py tests/test_response_trace.py tests/test_provider_runtime_patch.py tests/test_license_runtime_patches.py tests/test_tool_reply_lifecycle.py tests/test_background_plugin_permissions.py`，结果 97 passed；`git diff --check` 通过。
