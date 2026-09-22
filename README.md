# WeChat ReBot 商业版

这是面向 Windows 客户交付的独立商业仓库，不是开发机 `E:\Project` 的镜像。

商业版只使用 `hook_41827_astrbot_bridge` 新 HOOK。发布扫描会拒绝旧 `Py/main.py`、`NoveLoader.dll`、`NoveHelper.dll`、旧 6199 端口、运行日志、数据库、敏感配置和微信客户端文件。

## 客户体验

客户收到的目录只有一个明确入口：双击 `WeChatReBot.exe`。程序数据封装在同目录的单个 `payload.wrb` 中，客户解压下载包时不再预先展开数万个运行文件。

安装向导只有两步：选择安装目录，确认安装。推荐目录已自动填写，客户可以直接连续点击“下一步/安装”，也可以自行浏览更改；安装时从数据包一次性解压并同步校验，避免“校验 → 准备”重复读取，同时保留事务落盘和失败回滚。完成后默认打开管理软件。安装阶段不要求客户先安装微信，也不要求理解模型或运行环境。

安装完成后打开同一个 EXE 会进入统一白标控制台。WebView 只加载显式白名单中的客户操作，不扫描或暴露窗口、生命周期和数据服务等内部对象，避免初始化递归造成界面卡死。软件每次打开都检查 `Weixin.exe` 和版本 `4.1.8.27`；未安装或版本不兼容时禁用启动，并提供下载安装、手动选择和重新检测。控制台包含运行中心、功能中心、群聊&私聊管理、消息中心、定时任务、运行记录和设置，所有客户文案都隐藏底层运行架构。群聊&私聊管理可在群聊与私聊标签间切换；私聊页读取当前机器人账号的微信通讯录，在右侧展示接口可用的个人资料并按联系人配置可用功能。品牌名称、简称、人格和欢迎语在设置页一键统一替换；模型服务中心统一管理 DeepSeek、GLM、MiMo、Qwen、火山引擎、混元、Gemini / 中转站、Sub2 以及自定义聊天、向量和图片服务，并标明文本或多模态能力。AstrBot 工厂模型不作为用户已配置服务展示。机器人运行时保存会热更新并回读确认，各功能的模型设置按用途选择同一目录中的服务，不再重复填写接口和密钥。安装阶段不会自动启动任何机器人进程。

点歌默认使用星之阁在线接口，搜索不到时回退到网易云直连源；商业包不再需要 Node 网易云侧车或 PHP Meting 服务，功能中心不再显示无意义的点歌配置项。短剧默认使用 `E:\project` 已配置的服务端播放中间页，客户侧隐藏服务器地址和令牌，只保留短剧推送计划、内容类型与目标群聊等业务设置。

控制台支持远程更新：启动后和每 6 小时后台检查一次签名清单，发现新版本时显示版本和说明；机器人完全停止后才允许下载、校验并交给独立更新程序安装。更新清单使用 Ed25519 签名，包体同时校验大小、SHA-256、解压路径和逐文件哈希，失败会回滚原版本。断网时自动检查不会弹错打扰客户，仍可在设置页手动重试。客户手动下载页默认关闭，签名更新通道继续独立提供服务。

安装目录提供可直接双击的 `卸载机器人.exe`，设置页“维护”也可以打开同一卸载程序。客户确认后，独立卸载进程会停止机器人相关进程，并清理整套安装目录、配置、日志、缓存、备份、更新文件、桌面/开始菜单快捷方式、Windows 卸载注册项，以及 `%LOCALAPPDATA%\WeChatReBot` 下的定位和启动日志残留；聊天记录固定保存到 `%LOCALAPPDATA%\WeChatReBot\data\chat-history.db`，更新和卸载均不会删除，重新安装后自动继续使用；临时卸载程序会在退出后自清理。

商业项目不包含微信客户端、微信安装程序或微信安装压缩包。客户必须自行从官方渠道安装受支持版本；管理器只保存客户选定的本机路径。

## 当前发布状态

0.9.2 将“群聊管理”升级为“聊天管理”，新增群聊/私聊标签。私聊管理从当前登录机器人的微信通讯录拉取联系人，支持搜索、分页、手动刷新和按需查看昵称、备注、微信号、微信标识、头像、性别、地区、联系人类型及个性签名；联系人资料仅在本次运行内存中保留。每位联系人可独立保存可用功能范围，配置按机器人账号隔离并使用修订号与审计记录保护并发修改。

0.9.0 把人格、情感和长期记忆整合进白标设置页：LivingMemory 作为唯一默认长期记忆，明确回忆请求优先召回，普通寒暄跳过检索；Self Learning 只保留精简情感上下文，默认关闭原始消息采集和自学习。群聊页新增按群总开关、回复方式、允许功能、主动推送、安静时段和每日上限；运行记录新增脱敏回复链路；首次使用引导、功能准备检查、长期记忆管理、设置与记忆备份恢复及安全诊断也进入统一控制台。所有运行配置优先通过本机接口热更新，离线保存会明确提示下次启动生效。

0.8.9 首次引入统一模型服务中心；现行界面只展示用户已配置服务，不再把 AstrBot 工厂模型当作客户新增模型。功能中心的模型字段按用途读取已配置服务。模型保存、切换、测试和删除在机器人运行时通过本机接口热更新并回读确认，离线保存会明确提示启动时使用。任何客户模型密钥都不会写入源码或公开发行清单。

功能中心为当前 21 项用户可见功能提供上手指引、前置条件与常用/专业设置；已读取详情会立即复用并在后台静默刷新，分类切换保留输入。在线保存须通过实例重载、配置回读与初始化检查后才显示生效，失败尝试恢复旧设置。停用、断线、缺少外部服务分别提示下一步，不把写入文件等同于可正常使用。详细边界与隔离验证见 [功能热更新验收](docs/plugin-hot-config-acceptance.md)。

Windows 两步安装向导、统一控制台、环境检测、全链路状态机、功能配置、聊天管理、聊天记录、定时任务、结构化事件、签名更新/回滚、冻结 EXE、便携 Python 运行时、固定哈希新 HOOK 接入、客户目录组装器和发布扫描已经实现并通过自动化测试。当前客户包交付 24 个运行模块，其中 21 项在功能中心可见。LivingMemory 是唯一长期记忆，Self Learning 只提供精简情感与关系上下文；Mnemosyne 外部向量记忆、HAPI Connector 及其他已决定删除的功能不进入客户包。回复、上下文压缩和视频理解按用途选择已配置聊天模型，图片生成选择已配置图片模型；AstrBot 工厂条目不进入客户模型列表，客户密钥不会写入源码或发行清单。群聊分析读取新桥接成功接收并按机器人账号、群聊双重隔离的本机消息，安装前或连接离线期间的消息无法补拉，因此仍按需启用。交付清单同时决定功能中心的顺序、默认启用策略与配置入口，诊断会拒绝缺少所声明配置资源的空壳功能。统一控制台使用系统 Edge/WebView2；正式发行前仍需在干净 Windows 10/11 环境完成实机验收。

构建与安装不再依赖审批状态或 `ready` 标记。以下事项仍记录在仓库中，供发布者根据实际分发范围自行核对：

- 新 HOOK 二进制的商业再分发授权、安全审查与真实微信实机验证；
- Windows EXE 代码签名；
- 24 个交付模块的许可证审查、NOTICE/源码义务和最终商业放行；完整发布入口会按最终运行时和插件清单重新生成 SBOM 与许可证汇编；
- 完整载荷内置固定哈希的 FFmpeg/FFprobe（LGPL shared），YouTube 解析复用 Playwright 自带 Node；音乐功能不依赖 Node/PHP 侧车；
- 全新 Windows 虚拟机安装、升级、卸载和杀毒软件兼容验收；
- 客户许可协议、第三方 NOTICE、源码提供方式和售后边界。

安装器不再检查载荷的审批状态，但仍会校验清单格式、路径边界和每个文件的 SHA-256。缺失、篡改、越界或试图覆盖用户数据的载荷仍会被拒绝。

## 开发与测试

```powershell
cd E:\wechatrebot
C:\Program Files\Python313\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

运行源码安装向导：

```powershell
$env:PYTHONPATH = "src"
C:\Program Files\Python313\python.exe -m wechatrebot_manager gui `
  --payload .\artifacts\staging
```

## 可重复构建

构建固定的 Python 3.13.9 x64 / AstrBot 4.26.6 便携运行时。首次联网生成私有 wheelhouse，之后可完全离线重建：

```powershell
& 'C:\Program Files\Python313\python.exe' scripts\build_runtime.py `
  --output .\artifacts\runtime-build `
  --wheelhouse .\artifacts\runtime-cache\wheels-py313-win-amd64

& 'C:\Program Files\Python313\python.exe' scripts\build_runtime.py `
  --offline `
  --output .\artifacts\runtime-build-offline `
  --wheelhouse .\artifacts\runtime-cache\wheels-py313-win-amd64
```

构建图形管理器：

```powershell
& 'C:\Program Files\Python313\python.exe' scripts\build_manager.py `
  --output .\artifacts\manager-build
```

组装完整 staging：

```powershell
& 'C:\Program Files\Python313\python.exe' scripts\build_staging.py `
  --source-root E:\Project `
  --runtime-root .\artifacts\runtime-build-offline `
  --manager-exe .\artifacts\manager-build\dist\WeChatReBot.exe `
  --output .\artifacts\staging
```

从 staging 构建客户包：

```powershell
& 'C:\Program Files\Python313\python.exe' scripts\build_distribution.py `
  --payload .\artifacts\staging `
  --output .\artifacts\WeChatReBot-Commercial `
  --archive
```

仅修改管理器、插件中心或管理器内置页面时，使用开发者一键增量发布。脚本会自动测试、构建管理器、签名、上传、切换稳定清单并从公网做安装冒烟，不会构建或扫描完整 staging：

```powershell
.\scripts\publish-manager-update.ps1 `
  --title '插件配置更新' `
  --note '修复高级配置页面'
```

项目版本必须已经高于线上稳定版。默认在上传前要求输入版本号确认；需要无人值守时追加 `--yes`，只生成本地产物时追加 `--dry-run`。如果管理器已经构建完成，可用 `--manager-build <构建目录>` 跳过重复构建。

运行时、AstrBot 插件本体、桥接程序、hook 有变化，或需要修复旧单文件管理器的更新引导时，使用完整载荷一键发布：

```powershell
.\scripts\publish-full-update.ps1 `
  --source-root 'E:\Project' `
  --title '完整载荷稳定性更新' `
  --note '修复旧版管理器更新回滚' `
  --yes
```

该入口在内部完成测试、元数据校验、运行时和管理器构建、完整 staging、签名打包、上传、稳定清单切换与公网冒烟，不需要手工串联子命令。

完整的更新地址绑定、密钥备份、发布顺序和回滚说明见 `docs/remote-updates.md`。私钥只能保存在发布电脑和离线备份中，禁止提交到 Git。

构建输出只允许位于本仓库 `artifacts/`，不会修改或启动开发环境里的 AstrBot、微信及桥接进程。

## 关键清单

- `release/source-manifest.json`：发行文件白名单、目标路径和固定哈希。
- `release/runtime-requirements.lock`：运行时 Python 包精确锁定。
- `release/runtime-wheelhouse.json`：私有 wheelhouse 汇总指纹。
- `release/build-requirements.lock`：冻结管理器的构建依赖锁定。
- `release/hook-binaries.json`：新 HOOK 架构、大小、哈希与审核状态。
- `release/plugin-manifest.json`：插件归属及许可证信息。
- `release/RELEASE-CHECKLIST.md`：非阻断式的发布核对建议。
- `src/wechatrebot_manager/update-channel.json`：客户内置的更新通道、HTTPS 清单地址和签名公钥。
