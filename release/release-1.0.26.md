# 1.0.26 聊天页面状态保留修复

## 范围与来源

线上基线为 1.0.25，本次发布管理器增量，包含 PR #223 群聊私聊配置刷新修复及 PR #224 消息中心连接确认修复。发布候选必须从两项修复均已合入的最新 master 构建，提交和产物哈希以发布回执为准。

用户已明确授权“合并所有pr 发布线上”，沿用本会话直接发布要求。此包不重建或分发 Python、AstrBot、HOOK、插件和 FFmpeg；不自动更新本机安装，不启动机器人。GitHub Latest 保留完整安装版 1.0.24。

## 客户可见变化

- 群聊私聊配置停止定时重复读取目录；切回已加载页面保留配置草稿和选择，成员补齐不重建表单。
- 消息中心短暂显示“正在确认连接”时保留当前群、消息、筛选和阅读位置；同账号恢复不重新加载页面。
- 暂停不确定账号下的读取和敏感操作，丢弃断连前迟到的请求结果；真实退出、换号或授权失效仍清理对应账号数据。

该修复解决页面重置，不宣称消除 HOOK TCP 探测超时；无配置或数据库迁移。

## 文件与实现

复用现有聊天缓存、账号隔离、请求代号及发布入口。集成冲突涉及 `app_webui/app.js`、`app_webui/index.html`、`tests/subscription_webui.cjs`、`tests/test_app_shell.py`，同时保留两项修复。版本同步涉及 `pyproject.toml`、管理器 `__init__.py`、Windows 版本资源、SBOM、验收及签名元数据；`legal-review.json` 仅同步 SBOM 哈希，不将 pending 改为通过。

## 验证与证据

本地集成验证命令：

```powershell
py -3.13 -m pytest -q tests/test_chat_refresh_webui.py tests/test_history_connection_retention_webui.py tests/test_history_account_recovery.py tests/test_group_directory.py tests/test_app_shell.py tests/test_subscription_webui.py tests/test_history_resume_webui.py --tb=short --show-capture=no
node tests/history_webui.cjs
node tests/chat_management_webui.cjs
node tests/subscription_webui.cjs
py -3.13 scripts/validate_release_metadata.py --root .
```

设置 `PYTHONPATH=src` 后，定向 pytest 为 310 passed；三组 Node 检查通过。完整测试由 PR CI 执行，通过后才能合并。版本元数据校验、最终冻结管理器自测、线上资产大小及 SHA-256、稳定清单验签和下载冒烟结果保存到 `artifacts/r26/`。复用完整 CI，不在本地重复全量 pytest。

发布器先验证版本化资产，再切换 stable；切换后的冒烟失败恢复原签名稳定清单。回滚目标为 1.0.25，原清单随候选保存。使用短构建及发布路径，规避旧发布缓存的 Windows 深路径错误。

## 本次检查适用性

| 检查 | 本次结论 |
| --- | --- |
| HOOK、插件、运行时许可证及二进制变更 | 本增量不包含这些资产，沿用安装基线，不重新声明审核通过 |
| Authenticode | 沿用现有策略；pending 保持原状态，不与 Ed25519 清单签名混同 |
| 自动更新、数据保留、回滚 | 复用现有交付机制及完整 CI；最终包验证结果以回执为准 |
| 真实微信、干净 Windows、安全软件 | 本次未执行，按用户直接发布要求如实记录，不视为通过 |
| 客户配置、数据与私钥 | 不进入管理器增量包 |
| 公网验证及稳定切换 | 保留 HEAD、完整下载、大小、哈希、验签及发布后冒烟 |

线上发布是否完成以 `publication-receipt.json` 和公网回读证据为准；本记录不代表已更新用户本机。
