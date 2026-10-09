# 0.9.9 Bug TODO

本清单对应 `codex/bugfix-0.9.9-20260922`。基线为远端 `master` 的 0.9.8，目标修复版本沿用 0.9.9。本分支只做源码和轻量验证，不启动机器人、不构建载荷、不上传发布包。

## 已完成

- [x] 修正 `astrbot-4.26.6-outbound-policy.patch` 的新文件 hunk 行数。补丁实际包含 70 行，原声明 68 行会截断 `group_plugins()`，导致群聊事件被 WakingCheckStage 拦截。
- [x] 在运行时根校验中比对群聊权限 helper，拒绝使用被截断或过期的运行时文件，并补充无 wheel 缓存时的补丁回归测试。
- [x] 将 `jiang_commands.setu`（“图来”）从常用指令中拆出独立的群聊权限项，运行时、管理器展示和保存逻辑保持一致。
- [x] 历史记录读取、分页、删除和导出改为使用最新确认的微信账号；未登录或账号变化时返回空结果并停止旧账号操作。
- [x] 油价补丁测试夹具复制完整 `jiang_commands` 插件目录，确保 `_conf_schema.json` 等同插件补丁目标存在。
- [x] 旧 `cron_jobs` 管理接口按明确的 payload 账号隔离；支持的每日/星期计划经预览确认、停机核验后迁入统一任务存储的暂停草稿，含中断恢复和启动门禁。无法确认归属的旧数据保持原样。
- [x] 启动引导复用当次状态和模型列表，每份插件配置只读一次；补充离线合成配置基准和数据等价校验。播放器 40 项浏览器回归接入 Windows CI，缺少浏览器或测试 FFmpeg 时明确失败。

## 待处理

- [ ] 在真实安装环境验证启动引导的可见耗时和桥接不可用时的目录恢复；离线基准不代替这项实机验收。
- [ ] 完成群聊分析迁移到统一定时任务编辑器的页面、保存、删除和回读闭环，并同步清理相互矛盾的 UI 契约测试。
- [ ] 对 `jiang_mystool`、`jiang_commands` 直发、TTS 和群分析等直接发送路径补充实际 delivery/wake 元数据验证，确认群聊策略边界不会拦截合法命令。
- [ ] 在下一次完整载荷验收前，重新运行发布元数据、对应源码哈希、补丁应用和干净安装验证；当前基线的 legal review 自校验已通过。

## 本轮验证

- `tests/test_group_feature_runtime.py`
- `tests/test_bittorrent_command_routing.py`
- `tests/test_group_policy.py`
- `tests/test_chat_history.py`
- `tests/test_app_shell.py`
- `tests/test_private_chat_avatar.py`
- `tests/test_oilprice_command_patch.py`
- `tests/test_staging_builder.py`
- `tests/test_release_guard.py`
- `tests/test_release_metadata.py`

