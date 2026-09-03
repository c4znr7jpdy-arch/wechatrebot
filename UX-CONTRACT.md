# UX Contract

## Product context

- Audience: 非技术 Windows 客户、社群运营者、微信客服经营者。
- Primary jobs: 一键安装；检查微信；启动/停止机器人；管理功能；查看聊天记录和定时任务；更新软件。
- Locale: 简体中文，时间使用本机时区（首发目标 Asia/Shanghai）。
- Accessibility: 键盘可达、焦点可见、状态不只靠颜色、支持减少动态效果。
- Terminology: 客户可见区域禁止底层框架名、“插件”和“桥”；统一使用“功能、微信连接、机器人功能、运行记录”。

## Sources of truth

| 范围 | 权威来源 | 验证 |
|---|---|---|
| 页面与交互 | `src/wechatrebot_manager/app_webui/` | Playwright + 人工截图 |
| 生命周期 | `lifecycle.py`、`runtime_status.py` | 单元测试 + 真实机器验收 |
| 环境检测 | `environment.py` | 单元测试 + 各微信安装路径验收 |
| 功能配置 | `PluginCenterService` + 交付 Schema | 单元测试 |
| 聊天记录 | `chat_history.py` 的产品数据库 | 单元测试 + 大数据量验收 |
| 定时任务 | `schedules.py` + 功能原始配置 | 单元测试 + 联调 |
| 更新 | `updates.py` + 签名发布清单 | 单元测试 + 冻结程序验收 |
| 发布边界 | `release/source-manifest.json` | release guard |

## Canonical UI map

| 页面 | 用户可见能力 | 数据/动作所有者 |
|---|---|---|
| 运行中心 | 环境、状态、启动、停止、错误恢复 | `LifecycleController` |
| 功能中心 | 搜索、选择、启停、动态配置 | `PluginCenterService` |
| 聊天记录 | 分类、搜索、筛选、消息、导出、清理 | `ChatHistoryService` |
| 定时任务 | 汇总、启停、改时间、删除 | `ScheduleService` |
| 运行记录 | 脱敏的结构化生命周期事件 | `EventStore` |
| 设置 | 品牌、回复模型、微信路径、更新与维护 | `ApplicationApi` |

UI 只能调用 `ApplicationApi`，不得直接终止进程、改数据库或读取底层日志。

## Flow ledger

| 操作 | Pending | 成功 | 失败与恢复 |
|---|---|---|---|
| 首次打开 | 自动检查微信 | 环境通过，允许启动 | 缺失/版本错误时禁用启动，提供下载、选择、重检 |
| 启动 | 核心准备→微信连接→身份/心跳确认 | 仅全链路健康显示“机器人可用” | 进入 ERROR，展示错误码、用户说明、解决步骤 |
| 停止 | 若微信运行则等待用户关闭 | 自动依序停止微信连接和机器人功能 | 保持等待页，可重新检测，不强杀微信 |
| 保存功能 | 禁用重复提交 | 在线尽量立即应用，离线提示下次启动 | 保留字段值；版本冲突要求刷新 |
| 查询聊天 | 加载保留旧列表 | 显示会话摘要和消息 | 空态或 toast，可修改筛选重试 |
| 删除聊天 | 自有确认弹窗 | 只删产品本机副本 | 不改微信原记录和记忆源 |
| 修改定时任务 | 当前行操作暂不可重复 | 同步原功能配置并刷新汇总 | 保留任务并给错误码 |
| 保存品牌 | 单次提交 | 窗口、导航、回复身份统一更新 | 保留输入并聚焦缺失字段 |
| 检查更新 | 按钮“检查中…” | 最新版本提示或版本确认弹窗 | 自动检查静默；手动检查展示 14001 |
| 安装更新 | 必须先完全停止；下载/验证 | 独立更新程序替换后重启 | 14002/14003；事务回滚并在下次打开提示 |

## Lifecycle contract

状态固定为 `STOPPED → CHECKING → CORE_STARTING → CORE_READY → CONNECTION_STARTING → WAITING_WECHAT/VERIFYING_CONNECTION → RUNNING`。任一阶段失败进入 `ERROR`。停止为 `STOP_WAIT_WECHAT → STOPPING → STOPPED`。

`RUNNING` 必须同时满足：机器人功能端口就绪、微信连接服务就绪、注入状态就绪、上游通信已连接、已取得微信身份、心跳新鲜。仅端口存在不能宣告可用。

## Validation and sensitive data

- 表单使用 `novalidate` 与产品自有中文校验，错误不清空输入。
- API Key、Cookie、令牌、完整堆栈和底层名称不得进入普通日志、错误详情或导出。
- 错误码来自独立 JSON，可由安装目录配置覆盖；未知错误回落 19000。
- 结构化事件使用 JSONL，至少覆盖打开、检查微信、准备功能、连接微信、功能完成、微信关闭、停止和错误。

## Responsive and overlay contract

- 正式最小窗口 1080×680；900px 仍需可用，作为布局压力测试。
- 宽屏聊天三栏；1220px 隐藏信息侧栏；900px 单列。
- 数据表在面板内滚动；路径和值可换行或截断，但需可选择复制。
- 所有应用确认框使用自有模态层；系统文件选择器是唯一允许的原生弹层。
- Esc、点击遮罩和取消按钮关闭非强制模态；确认期间禁止重复提交。

## Async and resilience

- 生命周期动作由服务管理层串行化；重复点击返回“正在处理”。
- 基础设置使用原子写入；聊天导入使用增量游标，删除的产品副本不被重复导入。
- 自动状态轮询 1.5 秒，不造成布局跳动。
- 更新清单只接受 HTTPS 与 Ed25519 签名；包体限制大小、SHA-256、解压路径和逐文件清单；替换失败自动回滚。

## Verification matrix

- Static: `node --check app.js`、`python -m compileall src`、JSON 解析。
- Unit: 使用 `PYTHONPATH=src python -m pytest -q`。
- Browser: Edge/Chromium，1380×860 与 900×700；六个页面、搜索、模态、空态、错误态、控制台零 error/warning。
- Windows: Windows 10/11，100%/125% 缩放；微信未安装、错误版本、未登录、连接中断、正常运行。
- Release: 仅新连接实现；无旧目录、微信安装包、本地 Node/PHP 音乐服务；签名更新与回滚使用冻结 EXE 实测。
- Completion gate: 单元测试通过不等于正式可发布；真实机器、代码签名、Hook 授权、第三方许可证和正式安装包验收仍是独立发布门槛。
