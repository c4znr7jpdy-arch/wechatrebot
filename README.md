# WeChat ReBot 商业版

这是面向 Windows 客户交付的独立商业仓库，不是开发机 `E:\Project` 的镜像。

商业版只使用 `hook_41827_astrbot_bridge` 新 HOOK。发布门禁会拒绝旧 `Py/main.py`、`NoveLoader.dll`、`NoveHelper.dll`、旧 6199 端口、运行日志、数据库、敏感配置和微信客户端文件。

## 客户体验

客户收到的目录只有一个明确入口：双击 `WeChatReBot.exe`。

首次运行在同一个窗口内完成：

1. 首先由客户选择机器人安装目录；确认安装前不会写入程序文件。
2. 自动发现本机 `Weixin.exe`，发现失败或存在多个版本时引导客户手动选择。
3. 校验当前新 HOOK 支持的微信版本 `4.1.8.27`。
4. 一次填写机器人名称、简称、人格、欢迎语和可选头像。
5. 选择 DeepSeek 或自定义 OpenAI 兼容模型，也可以稍后在自有控制台的“回复模型”中配置。
6. 校验全部 SHA-256、事务式安装、生成配置并创建桌面/开始菜单快捷方式。

安装完成后打开同一个 EXE 会进入白标控制台，可一键拉起微信并启动机器人、修改机器人资料、配置回复模型、管理已交付功能、查看脱敏日志，以及备份/恢复客户配置与数据。“功能扩展”中的独立配置中心复用交付功能的配置 Schema 和专属页面，提供参数编辑、模型绑定、启停与热重载，但不开放底层管理后台。诊断只保留载荷计数与清单哈希，不携带数万条文件路径；恢复前会自动创建本地回滚点。安装阶段不会自动启动服务。

商业项目不包含微信客户端、微信安装程序或微信安装压缩包。客户必须自行从官方渠道安装受支持版本；管理器只保存客户选定的本机路径。

## 当前发布状态

Windows 安装向导、控制中心、功能配置中心、冻结 EXE、便携 Python/AstrBot 运行时、固定哈希新 HOOK 接入、客户目录组装器、备份恢复和发布扫描已经实现并通过自动化及端到端离线安装演练。功能配置窗口要求系统具备 Edge WebView2 Runtime；正式发行前仍需在干净 Windows 10/11 环境验证并确定缺失时的一键补齐策略。

仓库仍不可直接收费发布，`release/source-manifest.json` 保持 `phase-1-incomplete`。尚未关闭的硬门槛包括：

- 新 HOOK 二进制的商业再分发授权、安全审查与真实微信实机验证；
- Windows EXE 代码签名；
- 实际销售插件的筛选、许可证审查、源码净化与打包；
- 需要视频转码时的 ffmpeg 合规发行，以及需要音乐能力时的 Node 侧车；
- 全新 Windows 虚拟机安装、升级、卸载和杀毒软件兼容验收；
- 客户许可协议、第三方 NOTICE、源码提供方式和售后边界。

正式安装器默认拒绝不完整载荷。任何 `--allow-incomplete` 参数仅用于研发验收，不能用于交付客户。

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

构建输出只允许位于本仓库 `artifacts/`，不会修改或启动开发环境里的 AstrBot、微信及桥接进程。

## 关键清单

- `release/source-manifest.json`：商业发行白名单与总体状态。
- `release/runtime-requirements.lock`：运行时 Python 包精确锁定。
- `release/runtime-wheelhouse.json`：私有 wheelhouse 汇总指纹。
- `release/build-requirements.lock`：冻结管理器的构建依赖锁定。
- `release/hook-binaries.json`：新 HOOK 架构、大小、哈希与审核状态。
- `release/plugin-manifest.json`：插件归属及许可门禁。
- `release/RELEASE-CHECKLIST.md`：由 `phase-1-incomplete` 升级为 `ready` 前的放行条件。
