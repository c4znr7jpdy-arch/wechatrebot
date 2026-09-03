# WeChat ReBot 商业版

这是面向 Windows 客户交付的独立商业仓库，不是开发机 `E:\Project` 的镜像。

商业版只使用 `hook_41827_astrbot_bridge` 新 HOOK。发布门禁会拒绝旧 `Py/main.py`、`NoveLoader.dll`、`NoveHelper.dll`、旧 6199 端口、运行日志、数据库、敏感配置和微信客户端文件。

## 客户体验

客户收到的目录只有一个明确入口：双击 `WeChatReBot.exe`。

安装向导只有两步：选择安装目录，确认安装。安装阶段不要求客户先安装微信，也不要求理解模型或运行环境；载荷会逐文件校验、事务式落盘并创建桌面/开始菜单快捷方式。

安装完成后打开同一个 EXE 会进入统一白标控制台。软件每次打开都检查 `Weixin.exe` 和版本 `4.1.8.27`；未安装或版本不兼容时禁用启动，并提供下载安装、手动选择和重新检测。控制台包含运行中心、功能中心、聊天记录、定时任务、运行记录和设置，所有客户文案都隐藏底层运行架构。品牌名称、简称、人格和欢迎语在设置页一键统一替换；回复模型也在同一界面配置。安装阶段不会自动启动任何机器人进程。

点歌默认使用星之阁在线接口，搜索不到时回退到网易云直连源；商业包不再需要 Node 网易云侧车或 PHP Meting 服务。

控制台支持远程更新：启动后和每 6 小时后台检查一次签名清单，发现新版本时显示版本和说明；机器人完全停止后才允许下载、校验并交给独立更新程序安装。更新清单使用 Ed25519 签名，包体同时校验大小、SHA-256、解压路径和逐文件哈希，失败会回滚原版本。断网时自动检查不会弹错打扰客户，仍可在设置页手动重试。

商业项目不包含微信客户端、微信安装程序或微信安装压缩包。客户必须自行从官方渠道安装受支持版本；管理器只保存客户选定的本机路径。

## 当前发布状态

Windows 两步安装向导、统一控制台、环境检测、全链路状态机、功能配置、聊天记录、定时任务、结构化事件、签名更新/回滚、冻结 EXE、便携 Python 运行时、固定哈希新 HOOK 接入、客户目录组装器和发布扫描已经实现并通过自动化测试。统一控制台要求系统具备 Edge WebView2 Runtime；正式发行前仍需在干净 Windows 10/11 环境验证并确定缺失时的一键补齐策略。

仓库仍不可直接收费发布，`release/source-manifest.json` 保持 `phase-1-incomplete`。尚未关闭的硬门槛包括：

- 新 HOOK 二进制的商业再分发授权、安全审查与真实微信实机验证；
- Windows EXE 代码签名；
- 实际销售插件的筛选、许可证审查、源码净化与打包；
- 需要视频转码时的 ffmpeg 合规发行；音乐功能不再依赖本地 Node/PHP 服务；
- 全新 Windows 虚拟机安装、升级、卸载和杀毒软件兼容验收；
- 客户许可协议、第三方 NOTICE、源码提供方式和售后边界。

正式安装器默认拒绝不完整载荷。任何 `--allow-incomplete` 参数仅用于研发验收，不能用于交付客户。研发客户目录会写入可校验的 `development_preview` 标记，因此必须双击完整目录顶层的 `WeChatReBot.exe`；单独复制构建中间 EXE 会弹出“启动文件不完整”。

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
  --payload .\artifacts\staging `
  --allow-incomplete-payload
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

组装 staging。受控新 HOOK 只有在对应审核门槛关闭后才应放行：

```powershell
& 'C:\Program Files\Python313\python.exe' scripts\build_staging.py `
  --source-root E:\Project `
  --runtime-root .\artifacts\runtime-build-offline `
  --manager-exe .\artifacts\manager-build\dist\WeChatReBot.exe `
  --allow-gate hook-binary-redistribution `
  --output .\artifacts\staging
```

正式客户包只能从状态为 `ready` 的 staging 构建：

```powershell
& 'C:\Program Files\Python313\python.exe' scripts\build_distribution.py `
  --payload .\artifacts\staging `
  --output .\artifacts\WeChatReBot-Commercial `
  --archive
```

远程更新发布使用同一份 `ready` staging。先上传 ZIP，再最后上传签名清单，客户下一次检查即可收到弹窗：

```powershell
& 'C:\Program Files\Python313\python.exe' scripts\build_update_release.py `
  --payload .\artifacts\staging `
  --output .\artifacts\update-0.8.0 `
  --private-key .\config\local\update-signing-private.pem `
  --package-url https://你的更新域名/releases/wechatrebot-0.8.0-update.zip `
  --note "新增功能配置与热重载" `
  --note "修复已知问题"
```

完整的更新地址绑定、密钥备份、发布顺序和回滚说明见 `docs/remote-updates.md`。私钥只能保存在发布电脑和离线备份中，禁止提交到 Git。

构建输出只允许位于本仓库 `artifacts/`，不会修改或启动开发环境里的 AstrBot、微信及桥接进程。

## 关键清单

- `release/source-manifest.json`：商业发行白名单与总体状态。
- `release/runtime-requirements.lock`：运行时 Python 包精确锁定。
- `release/runtime-wheelhouse.json`：私有 wheelhouse 汇总指纹。
- `release/build-requirements.lock`：冻结管理器的构建依赖锁定。
- `release/hook-binaries.json`：新 HOOK 架构、大小、哈希与审核状态。
- `release/plugin-manifest.json`：插件归属及许可门禁。
- `release/RELEASE-CHECKLIST.md`：由 `phase-1-incomplete` 升级为 `ready` 前的放行条件。
- `src/wechatrebot_manager/update-channel.json`：客户内置的更新通道、HTTPS 清单地址和签名公钥。
