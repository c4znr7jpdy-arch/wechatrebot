---
version: "0.1"
name: "WeChat ReBot Desktop Console"
description: "面向非技术客户的一键运行、白标微信机器人通信控制台。"
colors:
  ink: "#102A49"
  primary: "#1769E8"
  primary-hover: "#0E55C6"
  success: "#16845B"
  warning: "#92400E"
  danger: "#B33A3A"
  canvas: "#F8FAFD"
  surface: "#FFFFFF"
  surface-muted: "#F3F6FA"
  border: "#D9DFE8"
  text-muted: "#59677B"
typography:
  sans:
    fontFamily: "Microsoft YaHei UI, Segoe UI, sans-serif"
  display:
    fontFamily: "Bahnschrift, Segoe UI Semibold, Microsoft YaHei UI, sans-serif"
  mono:
    fontFamily: "Cascadia Mono, Consolas, monospace"
rounded:
  DEFAULT: "8px"
  sm: "6px"
  md: "8px"
  lg: "12px"
spacing:
  unit: "4px"
  sidebar-width: "212px"
  content-gap: "20px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    rounded: "{rounded.sm}"
    padding: "12px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  navigation:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
  page-canvas:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
  surface:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
  surface-muted:
    backgroundColor: "{colors.surface-muted}"
    textColor: "{colors.text-muted}"
  divider:
    backgroundColor: "{colors.border}"
    height: "1px"
  status-success:
    backgroundColor: "{colors.success}"
    textColor: "{colors.surface}"
  status-warning:
    backgroundColor: "{colors.warning}"
    textColor: "{colors.surface}"
  status-danger:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.surface}"
---

# WeChat ReBot Desktop Console Design System

## Overview

### Creative North Star

以用户提供的“姜小妹机器人商业版”桌面稿为直接视觉基准：白色品牌顶栏、深蓝侧栏、中央圆形启动器、右侧运行状态和底部最近运行。状态先于装饰，操作顺序清楚，故障位置可以一眼定位；不使用玻璃拟态或营销式插画。

### Product context and register

- **Audience and primary job:** 中国大陆非技术 Windows 客户；双击程序并点击一次，让机器人拉起微信、等待登录并进入在线状态。
- **Target market(s) and evidence:** 中国大陆微信社群运营者与客服经营者，来自 `docs/requirements/requirements-commercialization.md` 的商业化需求。
- **Locale(s) and language policy:** 首版仅简体中文；界面术语必须是产品自有语言，底层框架名不得出现在日常操作界面、提示或导出诊断中。
- **Usage scene:** Windows 桌面、日常高频启动与低频配置；首页低密度，日志页高密度。
- **Register:** 产品型控制台。法律声明和第三方许可证属于交付材料，不进入日常运行界面。
- **Memorable signature:** 首页中央双环电源启动器与下方三步进度，依次表示启动服务、打开微信、登录完成。
- **Restraint:** 表单、插件列表和日志遵循熟悉的桌面交互，表达性只放在运行中心。
- **Anti-references:** 不做通用后台模板、圆角卡片海、巨型标题、无意义图标、emoji 状态或网页导航栏。
- **Token ownership/runtime mapping:** 本文件是设计意图源；运行时由 `src/wechatrebot_manager/ui_theme.py` 显式映射到 Tk/ttk 样式，测试检查关键颜色与术语漂移。

## Colors

`ink` 用于侧栏与强层级；`primary` 只用于主要动作和当前导航；`success`、`warning`、`danger` 只表达状态。`canvas` 与 `surface` 形成安静的两层背景，`border` 保证浅色环境下的组件边界。首版仅支持亮色主题，键盘焦点使用 `primary` 高对比描边。

## Typography

正文和控件使用 Microsoft YaHei UI；标题/状态数字优先 Bahnschrift 或 Segoe UI Semibold；日志、端口和路径使用 Cascadia Mono/Consolas。标题不用全大写，中文按钮使用动作短语。

## Layout

默认窗口 1254×802、最小尺寸 1120×720；使用 56px 自绘品牌顶栏和固定 212px 左侧导航。在 Windows 125% 缩放下对应参考稿的 1568×1003、70px 顶栏和 265px 侧栏。右侧内容区按 20px 间距组织：首页上部为中央启动器与右侧状态面板，下部为最近运行；表单标签宽度稳定，错误提示保留布局空间。窗口缩放时内容区扩展，侧栏与主操作几何保持稳定。

## Elevation & Depth

以色块和 1px 边框表达层级，窗口内部不叠加重阴影。弹窗使用系统窗口层级；侧栏、页头和内容面板不使用模糊或透明效果。

## Shapes

普通容器 8px、强调面板 12px、输入和按钮 6px；状态节点使用圆形。日志区域与侧栏允许直边，避免所有内容都变成卡片。

## Components

### Foundational visual states

默认、悬停、按下、禁用和忙碌状态由共享 ttk 样式控制。保存与启动期间按钮保持原宽并显示明确动词；错误以内联横幅为主，破坏性恢复操作才使用确认弹窗。

### Buttons and actions

每页最多一个主按钮。运行中心使用宽主按钮“一键启动”；停止为次级危险动作。按钮忙碌时禁用重复提交，文本切换为“正在启动…”或“正在保存…”。

### Navigation and data display

左侧导航固定为运行中心、机器人设置、回复模型、功能扩展、运行日志。当前页使用整行蓝色底与左侧高亮条；每页标题同步更新窗口标题。

“功能扩展”从主控制台打开独立配置窗口。配置窗口沿用白色品牌顶栏与深蓝功能目录，使用 292px 左栏搜索/选择功能，右栏展示标题、启停、热重载、基础配置和高级页面。内部功能标识不得作为列表名称、标题或浏览地址展示。

### Forms and overlays

字段使用稳定标签列，密钥默认遮罩并提供显示/隐藏。验证错误出现在表单顶部且不清空输入。关闭运行中的控制台与恢复备份需要确认。

插件配置由交付 Schema 动态生成：对象为分组，布尔值为带文字状态的复选框，枚举与模型使用 Windows 原生下拉选择，数字使用范围输入，列表/模板列表使用带格式校验的结构编辑区。Windows 原生下拉弹层是本桌面产品允许的系统控件变体。切换功能且存在未保存修改时使用自有模态层，不调用浏览器提示框。

### Iconography

首版不依赖图标字体或外部图片；品牌标记、导航、状态和窗口控件均使用代码绘制的线性图标，确保离线安装一致。所有关键动作必须同时保留文字标签。

### Motion

不使用装饰动画。状态轨道仅在阶段变化时更新颜色和文案；轮询为 1 秒，不造成布局跳动。

配置窗口只在首次载入使用小型进度旋转；遵循系统减少动态效果设置。保存、启停和热重载期间保持按钮宽度并阻止重复提交。

### Content and data visualization

语气直接、可操作：说明当前状态、下一步和恢复方式。客户可见术语固定为“回复引擎”“微信连接”“功能插件”“运行日志”，不暴露底层项目名称。

## Do's and Don'ts

- **Do:** 首屏只让用户做一次明确选择：启动机器人。
- **Do:** 所有页面复用同一套颜色、间距、按钮和反馈组件。
- **Don't:** 在客户界面、提示、诊断键名或日志来源标签中出现底层框架品牌。
- **Don't:** 用大量卡片、渐变和 emoji 掩盖状态与故障恢复信息。
