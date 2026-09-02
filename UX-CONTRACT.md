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
| 插件配置中心 | `PluginCenterService` + 独立 WebView 窗口 | 已安装目录、配置 Schema、本机回复引擎 API | 离线配置/在线热重载/插件高级页 | unit + customer manual |
| 日志 | `LogCatalog` | 本地日志文件 | 来源/尾部行 | unit + manual |
| 反馈 | `NoticeBar` | 页面级状态 | info/success/warning/error | widget/manual |
| 软件更新 | `UpdateDialog` + 独立更新副本 | 签名远程清单与完整 payload | 自动检查/手动检查/回滚 | unit + customer manual |

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
| 切换插件状态 | 配置中心启停按钮 | 禁止重复提交 | 当前功能 | 在线立即更新；离线提示下次启动 | 恢复原状态并保留错误 | 当前功能 | plugin manifest |
| 保存插件配置 | “保存配置/保存并热重载” | 原子写入或本机 API 提交 | 当前功能 | 在线完成热重载；离线提示下次启动 | 保留输入；版本冲突要求刷新 | 保存按钮/首个错误 | plugin schema |
| 手动热重载 | “热重载”按钮 | 按钮显示“正在重载…” | 当前功能 | “新的设置已经生效” | 保留页面与日志入口 | 热重载按钮 | local engine API |
| 打开高级页面 | 高级页面按钮 | 显示加载状态 | 当前功能内嵌页 | 页面就地打开 | 引导先启动机器人或重试 | 当前页面按钮 | plugin page API |
| 导出诊断 | 文件选择 | 后台生成 | 当前页 | 给出文件路径 | 可重试 | 导出按钮 | release contract |
| 检查更新 | 启动后台检查/顶栏按钮 | “检查中…”且禁止重复 | 当前页/更新弹窗 | 最新版本或发现新版本 | 自动检查静默；手动检查可重试 | 顶栏按钮 | update channel |
| 安装更新 | “立即更新” | 下载/展开/校验进度；必要时停止机器人 | 新版本控制台 | 更新完成弹窗 | 事务回滚旧版本并显示原因 | 更新弹窗 | signed update manifest |

## Navigation and responsive behavior

- Route document title policy: `{机器人名称} · {当前页面}`。
- Sidebar transformation: 桌面端固定侧栏；窗口低于最小尺寸时不再压缩。
- Truncation/full-value access: 路径和模型名允许换行或横向展开；日志保留横向滚动。
- Focus restoration: 页面切换后焦点落到页面标题或首个主要操作。

## Overlays and feedback

- Dialog primitive: 主窗口使用 Tk Toplevel/系统文件选择器；配置中心使用自有 HTML 模态层，插件高级页使用同源内嵌框架。
- Destructive confirmation levels: 恢复备份、运行中退出必须二次确认。
- Alert/banner scope and persistence: 普通保存与错误使用页内横幅；文件选择与破坏性动作使用模态框。
- Unsaved-changes behavior: 配置中心切换功能时提示放弃或继续编辑；窗口退出不自动提交。

## Async and resilience

- Mutation default: 离线配置使用原子写入；在线配置调用本机回环接口并由功能管理器完成热重载；配置版本摘要阻止多窗口覆盖。
- Idempotency and duplicate-submit policy: 忙碌期间禁用按钮；启动监督器只允许一个实例。
- Offline/read-stale/write behavior: 基础配置离线可读写并在下次启动生效；高级页面和手动热重载仅在回复引擎在线时可用。
- Retry/backoff/timeout behavior: 服务按固定就绪超时；失败后允许重新启动并查看日志。
- Remote update behavior: 启动后与每 6 小时检查签名清单；自动检查网络失败静默，手动检查反馈原因；包体 SHA-256、签名、路径和逐文件哈希全部通过后才交给独立更新副本。
- Long-running progress and return path: 启动阶段持续展示在运行中心，用户可以切换到日志页。

## Validation

- Schema/validation layer: Python dataclass/显式 validate 方法；插件配置使用交付 Schema 递归校验与浏览器字段校验。
- Trigger timing: 保存或下一步时验证；微信路径选择后立即验证。
- Error summary/inline policy: 页首摘要 + 具体字段说明，不清空输入。
- Sensitive-value handling: API Key 默认遮罩，诊断导出脱敏，日志页不主动显示配置文件。

## Permission and clipboard

- Permission UI strategy: 未交付或未通过准入的插件不展示为可安装项。
- Clipboard copy policy: 只复制脱敏诊断或日志选段，不复制 API Key。
- Disabled-state explanation: 控件旁显示“运行中不可修改”或“重启后生效”。

## Verification

- Required static commands: `python -m pytest`; `python -m compileall src`; `npx -p @google/design.md designmd lint DESIGN.md`; premium strict audit。
- Browser/device/locale/theme matrix: Windows 10/11、Edge WebView2、100%/125% 缩放、简体中文、亮色；开发自动化不得控制用户电脑，最终图形验收由用户手动完成。
- Accessibility checks: Tab 顺序、焦点可见、密钥遮罩、颜色不作为唯一状态信息。
- Component-state/visual regression coverage: 运行中心离线/启动中/待登录/在线/失败；表单成功/失败；插件空态；日志缺失。
- Failure-path evidence: 服务端口占用、微信路径失效、模型字段无效、日志文件不存在。
