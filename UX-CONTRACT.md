# UX Contract

## Product context

- Audience: 非技术 Windows 客户、社群运营者和微信客服经营者。
- Primary jobs: 一键启动并登录微信；修改机器人资料；配置回复模型；管理已交付功能；查看日志与导出诊断。
- Target market(s): 中国大陆。
- Active locales: 简体中文。
- Language/content register and native-review policy: 产品自有、直接、无底层框架术语；发布前由项目所有者做中文业务复核。
- Timezone/calendar policy: Asia/Shanghai，日志显示本机时间。
- Accessibility target: 键盘可达、高对比焦点、正文不低于 10pt；桌面 Tk 不宣称完整 WCAG 认证。

## Business-context sources

| Domain / scope | Authoritative source | Source type | Reviewed date |
|---|---|---|---|
| 商业模式与交付范围 | `docs/requirements/requirements-commercialization.md` | Requirements | 2026-09-02 |
| 客户包文件边界 | `release/source-manifest.json` | Release contract | 2026-09-02 |
| 插件准入 | `release/plugin-manifest.json` | Release gate | 2026-09-02 |
| 第三方许可 | `legal/` 与发布包第三方声明 | Legal artifacts | 2026-09-02 |

## Visual contract

- Project `DESIGN.md`: `DESIGN.md`
- Token ownership model: DESIGN.md 定义意图，Python 主题文件实现。
- Runtime design-system/token source: `src/wechatrebot_manager/ui_theme.py`
- Mapping/export/adapters: `apply_product_theme()`。
- Token drift gate: 单元测试 + DESIGN lint + 严格项目审计。
- Supported themes: Windows 亮色主题。
- Design-context owner/review policy: 商业版桌面端变更必须同时维护 DESIGN.md 与本契约。

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| 导航 | `ControlCenter` | 页面注册表 | 固定侧栏 | widget test/manual |
| 表单 | `SettingsPage` / `ModelPage` | manager + runtime config | 编辑 | unit + manual |
| 插件列表 | `PluginRegistry` | 已安装目录 + plugin_set | 已启用/已停用 | unit + restart notice |
| 日志 | `LogCatalog` | 本地日志文件 | 来源/尾部行 | unit + manual |
| 反馈 | `NoticeBar` | 页面级状态 | info/success/warning/error | widget/manual |

## Component behavior

| Component | Default | Hover | Focus | Active | Disabled | Busy | Error |
|---|---|---|---|---|---|---|---|
| Button | 清晰动作标签 | 色阶变化 | 蓝色焦点 | 加深 | 降低对比 | 文案切换且防重复 | 顶部横幅 |
| Input | 边框可见 | 不移动 | 焦点描边 | n/a | 只读底色 | 保留宽度 | 保留值并显示原因 |
| Secret input | 默认遮罩 | n/a | 同 Input | 显示按钮独立 | 同 Input | 同 Input | 不在反馈中回显 |
| Table/list | 单行选择 | 轻底色 | 键盘可达 | 选中底色 | n/a | 保留旧数据 | 空态/错误态 |

## Dataset navigation

- Admin tables: 首版没有大数据表；插件列表按已安装功能数量自然滚动。
- Exploratory lists: 日志来源列表保留当前选择。
- Empty/no-results/error/loading treatment: 每个页面都有明确空态和恢复建议。
- Selection scope: 插件逐项启停，不提供批量动作。

## Flow ledger

| Operation | Trigger | Pending | Success destination | Success feedback | Failure recovery | Focus outcome | Source ref |
|---|---|---|---|---|---|---|---|
| 一键启动 | 首页主按钮 | 阶段轨道 + 禁止重复点击 | 运行中心 | “机器人在线” | 保留日志入口并给出错误 | 回到主按钮/错误横幅 | requirements |
| 停止 | 首页次按钮 | “正在停止” | 运行中心 | “已停止” | 显示未停止原因 | 停止按钮 | requirements |
| 保存机器人设置 | 保存按钮 | 禁止重复保存 | 当前页 | 页内成功提示 | 保留全部输入 | 首个错误字段 | requirements |
| 保存模型 | 保存按钮 | 密钥不回显 | 当前页 | 下次启动生效提示 | 保留输入并指出字段 | 首个错误字段 | requirements |
| 切换插件 | 复选/按钮 | 立即写配置 | 当前页 | 提示重启生效 | 恢复原状态 | 当前插件 | plugin manifest |
| 导出诊断 | 文件选择 | 后台生成 | 当前页 | 给出文件路径 | 可重试 | 导出按钮 | release contract |

## Navigation and responsive behavior

- Route document title policy: `{机器人名称} · {当前页面}`。
- Sidebar transformation: 桌面端固定侧栏；窗口低于最小尺寸时不再压缩。
- Truncation/full-value access: 路径和模型名允许换行或横向展开；日志保留横向滚动。
- Focus restoration: 页面切换后焦点落到页面标题或首个主要操作。

## Overlays and feedback

- Dialog primitive: Tk Toplevel/系统文件选择器。
- Destructive confirmation levels: 恢复备份、运行中退出必须二次确认。
- Alert/banner scope and persistence: 普通保存与错误使用页内横幅；文件选择与破坏性动作使用模态框。
- Unsaved-changes behavior: 首版页面切换保留字段值；窗口退出不自动提交。

## Async and resilience

- Mutation default: 本地配置保存使用悲观提交与原子写入。
- Idempotency and duplicate-submit policy: 忙碌期间禁用按钮；启动监督器只允许一个实例。
- Offline/read-stale/write behavior: 核心配置全部本地可用；模型调用失败不影响管理页面打开。
- Retry/backoff/timeout behavior: 服务按固定就绪超时；失败后允许重新启动并查看日志。
- Long-running progress and return path: 启动阶段持续展示在运行中心，用户可以切换到日志页。

## Validation

- Schema/validation layer: Python dataclass/显式 validate 方法。
- Trigger timing: 保存或下一步时验证；微信路径选择后立即验证。
- Error summary/inline policy: 页首摘要 + 具体字段说明，不清空输入。
- Sensitive-value handling: API Key 默认遮罩，诊断导出脱敏，日志页不主动显示配置文件。

## Permission and clipboard

- Permission UI strategy: 未交付或未通过准入的插件不展示为可安装项。
- Clipboard copy policy: 只复制脱敏诊断或日志选段，不复制 API Key。
- Disabled-state explanation: 控件旁显示“运行中不可修改”或“重启后生效”。

## Verification

- Required static commands: `python -m pytest`; `python -m compileall src`; `npx -p @google/design.md designmd lint DESIGN.md`; premium strict audit。
- Browser/device/locale/theme matrix: Windows 10/11、100%/125% 缩放、简体中文、亮色。
- Accessibility checks: Tab 顺序、焦点可见、密钥遮罩、颜色不作为唯一状态信息。
- Component-state/visual regression coverage: 运行中心离线/启动中/待登录/在线/失败；表单成功/失败；插件空态；日志缺失。
- Failure-path evidence: 服务端口占用、微信路径失效、模型字段无效、日志文件不存在。
