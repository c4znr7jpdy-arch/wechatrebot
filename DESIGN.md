---
version: "0.2"
name: "WeChat ReBot Customer Desktop"
description: "面向非技术客户的一键安装、微信环境检测、运行控制与业务功能管理软件。"
colors:
  navy-950: "#071D32"
  navy-900: "#102A49"
  blue-600: "#0877E8"
  blue-700: "#0867CC"
  green-600: "#149A68"
  amber-700: "#B66818"
  red-700: "#B33A4A"
  canvas: "#F3F6FA"
  surface: "#FFFFFF"
  line: "#D8E1EC"
  text: "#0A1D33"
  text-muted: "#617087"
typography:
  sans: "Microsoft YaHei UI, Segoe UI, sans-serif"
  display: "Bahnschrift, Segoe UI Semibold, Microsoft YaHei UI, sans-serif"
  mono: "Cascadia Mono, Consolas, monospace"
rounded:
  control: "8px"
  panel: "12px"
spacing:
  unit: "4px"
  sidebar: "244px"
  page-gap: "18px"
---

# WeChat ReBot Customer Desktop Design System

## Product direction

这是普通用户使用的微信机器人管理软件，不是底层运行框架的管理壳。用户只需要理解四件事：微信是否准备好、机器人是否可用、有哪些功能、自己的数据在哪里。

- 用户：非技术 Windows 客户、社群运营者、客服经营者。
- 首要任务：安装后打开软件，确认微信环境，点击一次启动。
- 语言：只使用“机器人、微信连接、功能、聊天记录、定时任务、运行记录”。客户界面不得出现底层框架名、组件名或开发术语。
- 视觉基调：可靠、安静、偏桌面工具；状态比装饰重要。
- 识别特征：深海军蓝侧栏、中央圆形运行状态、三阶段连接轨道。

## Runtime ownership

`DESIGN.md` 定义视觉与交互意图；实际桌面界面由 `src/wechatrebot_manager/app_webui/` 实现并通过 Edge WebView2 承载。Python `ApplicationApi` 只提供业务数据与动作，页面不得直接启动、终止或探测进程。

## Layout

- 正式窗口默认 1380×860，最小 1080×680。
- 侧栏 244px，顶部状态栏 70px，内容区最大宽度 1480px。
- 1220px 以下将运行中心、聊天详情等复杂区域逐步重排；900px 以下侧栏缩为 78px 图标栏，功能、聊天、设置改为单列。
- 表格在窄窗口内局部滚动，不让整个页面产生横向滚动。
- 100% 与 125% Windows 缩放下都必须保留主按钮、错误处理入口与当前状态。

## Color and typography

海军蓝用于结构，亮蓝只用于当前导航和主要动作，绿色/琥珀/红色只表达成功、等待、失败。状态必须同时有文字或形状，不得只靠颜色。

正文使用 Microsoft YaHei UI/Segoe UI；状态标题和数字使用 Bahnschrift/Segoe UI Semibold；路径与错误码使用等宽字体。正文不小于 12px，主要正文以 13–14px 为主。

## Components

### Navigation

固定六项：运行中心、功能中心、聊天记录、定时任务、运行记录、设置。当前项使用整行高亮与左侧蓝条。品牌名称与简称来自统一品牌配置。

### Run center

首页必须同时回答：当前是否可用、停在哪一步、用户下一步做什么。

- 主操作只有启动或停止一个语义。
- 只有回复能力、微信连接、身份和心跳全部健康时才显示“机器人可用”。
- 环境未通过时主按钮禁用，并展示下载安装、重新选择、重新检测。
- 停止时如微信仍运行，进入等待状态并明确要求用户先关闭微信。
- 错误块固定包含用户语言标题、错误码、说明和解决步骤。

### Feature center

左侧搜索和选择功能，右侧展示启停、基础设置与模型设置。功能 Schema 动态生成字段；用户界面统一称“功能”，禁止出现“插件”。保存时保留输入，失败不清空表单。

### Chat history

宽屏为聊天列表、消息内容、聊天信息三栏；窄屏按列表→消息→信息顺序排列。支持个人/群聊分类、昵称/微信号/内容搜索、日期筛选、导出、按会话删除和按日期清理。本地副本删除不得伤害原始微信记录或机器人记忆源。

### Schedules

只展示业务定时任务，内部维护任务隐藏。表格包含任务、来源、发送对象、执行时间、下次运行、状态和操作。暂停、修改时间、删除都使用产品自有反馈和确认层。

### Settings and updates

设置分为品牌身份、回复模型、微信位置、维护四块。品牌保存是“一键替换”的唯一入口。API Key 不回显。更新只在机器人完全停止时安装；远程清单与包体校验全部通过后交给独立更新程序，失败自动回滚。

### Dialogs and feedback

确认、删除、修改时间和更新确认使用应用自有模态层；文件/目录选择使用 Windows 系统选择器。禁止 `alert`、`confirm`、`prompt`。普通成功使用短时 toast，持久故障使用页面内错误块。

## Interaction states

按钮和输入必须覆盖默认、悬停、键盘焦点、禁用、忙碌、错误。忙碌期间禁止重复提交且保持原宽。搜索支持中文输入法合成事件、280ms 防抖和清除按钮。尊重 `prefers-reduced-motion`。

## Do / Don't

- Do：先告诉用户结果和下一步，再提供细节。
- Do：始终让“能否启动/停止”与后台真实状态一致。
- Do：把日志、错误和更新反馈写成普通用户能理解的中文。
- Don't：暴露底层框架、连接组件、模块加载器、堆栈或内部端口。
- Don't：使用 emoji 充当操作图标、浏览器原生确认框或无意义装饰动画。
- Don't：在商业包中携带微信安装程序、旧连接实现、本地 Node/PHP 音乐服务或未获准发布的内容。
