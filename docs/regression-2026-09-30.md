# 2026-09-30 项目回归与遗留清理

基线：远端 `master` 的 `b35c661`。目标版本沿用 0.9.9；本次只提交源码，不构建、不安装、不上传、不启动机器人。变更均在管理器侧，交付方式为管理器增量更新。

## 文件变更

| 文件 | 变更 |
| --- | --- |
| `src/wechatrebot_manager/backup.py` | 回滚失败保留恢复点与原文件，检查备份和恢复目录的完整路径 |
| `src/wechatrebot_manager/payload.py` | 扩展现有 `_checked_target`，供目录检查复用；原文件校验默认不变 |
| `src/wechatrebot_manager/retired_plugins.py` | 删除前检查所有路径并读取、保存运行配置，避免部分清理和越界删除 |
| `src/wechatrebot_manager/schedules.py` | 保护旧任务来源配置、有效签到计划，修复未初始化数据库及旧执行时间显示 |
| `src/wechatrebot_manager/app_shell.py` | 旧任务 API 返回标准服务/数据库错误；移除未使用导入 |
| `tests/test_backup.py`、`tests/test_schedules.py`、`tests/test_app_shell.py` | 增补故障注入和真实临时文件/数据库回归 |
| `tests/test_retired_plugins.py` | 新增退役功能清理的边界、重试及保留有效功能测试 |
| `scripts/publish-manager-update.cmd` | 删除已停用、无引用且只会报错退出的历史入口；现有 PowerShell 入口继续使用 |
| 本文件 | 记录覆盖范围、修复证据和实机验收边界 |

## 已复现并修复

| 问题 | 触发与修复后行为 |
| --- | --- |
| 恢复失败后丢失恢复材料 | 恢复中途失败且原文件回滚再次失败时，保留恢复点 ZIP 与未恢复原文件，明确返回失败及位置；普通回滚成功时仍清理暂存 |
| 路径父级联接绕过边界检查 | 备份嵌套数据、恢复暂存/备份目录、退役清理的父目录出现符号链接或目录联接时，在写入/删除前拒绝操作；退役插件自身的联接仅移除联接 |
| 退役清理先删文件后检查配置 | 配置损坏或保存失败时不删除插件文件；配置成功移除退役启用项后才删除精确匹配的遗留文件 |
| 旧任务配置损坏被空列表覆盖 | 非法 JSON、错误根类型、错误 tasks 类型/条目均报错，在调用在线调度器或修改数据库前停止；缺失文件仍保留兼容行为 |
| 清理旧签到误停有效签到 | 清理无目标旧 cron 行时，若当前签到配置已经属于其他有效会话，保持该配置原文不变 |
| 引擎库尚无 cron 表导致页面失败 | 引擎数据库存在但调度器尚未建表时返回空任务列表，不创建旧表、不阻断群聊策略读取 |
| 离线改时间仍显示旧的下次执行时间 | 保存新 cron 表达式时同时清除过期 next_run_time，等待运行时重新计算 |
| 旧任务 API 的异常未返回标准响应 | 列表、开关、改时、删除遇到调度服务失败或 SQLite 锁定时返回现有 Web UI 错误结构 |

复用 `write_json_atomic`、`_checked_target`、恢复事务的 `replaced` 记录、`RETIRED_PLUGIN_MODULES`、`ScheduleService` 和 `ApplicationApi._failed`。未引入新依赖，未改变公开 API 参数或数据库结构。新增故障测试在修复前曾出现 30 项失败，第二批签到/错误响应测试出现 8 项失败；修复后均通过。

## 实际执行的检查

使用 Python 3.13，在工作区设置 `PYTHONPATH=src` 后执行以下定向测试（非本地全量测试）：

```text
python -m pytest -q tests/test_backup.py tests/test_schedules.py tests/test_retired_plugins.py tests/test_app_shell.py tests/test_payload.py tests/test_runtime_config.py tests/test_customer_maintenance.py tests/test_group_policy.py tests/test_private_policy.py tests/test_scheduled_task_service.py tests/test_chat_history.py tests/test_updates.py tests/test_plugin_center.py tests/test_plugin_manifest.py tests/test_provider.py tests/test_installed_entries.py tests/test_update_notification.py tests/test_task_store.py tests/test_publish_manager_update.py --tb=short
```

结果：**383 passed in 18.61s**。覆盖管理器 API、配置、模型服务、功能交付清单、群聊/私聊权限、消息历史、统一及旧版定时任务、安装入口、载荷事务、更新通知、备份恢复和退役清理。测试仅使用临时安装结构、数据库和替身服务。

其他检查：

- `python -m compileall -q src scripts tests`
- `node --check src/wechatrebot_manager/app_webui/app.js`
- `node --check src/wechatrebot_manager/app_webui/core-settings.js`
- `node --check src/wechatrebot_manager/plugin_webui/app.js`
- `python scripts/validate_release_metadata.py`：`ok: true`
- `git diff --check`

完整 Windows CI、全量 pytest 和冻结管理器自测由 PR 的 GitHub Actions 执行，具体结果以 PR 检查记录为准。

## 清理边界与待实机验证

- 不按“隐藏”或“默认关闭”删除产品功能。24 个已登记交付模块保持不变；旧任务兼容层仍被任务页及群聊策略调用，不能直接删除。退役清理仍只针对项目已经明确退役的 Mnemosyne。
- 原工作区的未提交 AGENTS.md 和历史修复文件保持原状；本次使用独立工作区。
- 损坏配置现在会明确阻止旧任务修改，需要修复源配置后重试；不会再自动丢弃内容。这是有意的兼容行为收紧。
- 未启动管理器或机器人，未验证真实微信收发、换号、图来撤回、第三方服务可用性、在线任务实际执行，以及 Windows 安装升级流程；单元测试通过不代表这些实机验收通过。
- 历史清单中的旧 cron 账号归属/迁移仍需要独立设计与实机验证，本次未自动认领或迁移旧任务。统一任务已有账号隔离，不能用它的测试结果替代旧任务换号验收。
- `app_shell.py` 仍是历史大型模块，本次仅修改错误处理和无用导入，未做无关架构重写。
