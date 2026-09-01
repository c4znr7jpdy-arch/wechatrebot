# WeChat ReBot 商业版

这是商业发行工作区，不是开发机 `E:\Project` 的镜像。

当前阶段已经建立可验证的发行基础设施：

- 首次启动先选择安装根目录；
- 新 HOOK 微信路径自动发现，必要时手动选择 `Weixin.exe`；
- 机器人品牌信息集中配置并原子保存；
- 白名单构建商业 staging；
- SHA-256 清单校验、安全路径检查、事务式载荷展开与失败回滚；
- 固定使用同一套 Python 3.13，按 AstrBot → 新 HOOK 的顺序启动；
- 本地端口就绪等待、逆序停止和脱敏诊断；
- 发布检查拒绝历史桥接、敏感配置、数据库和日志。

商业项目不包含微信客户端、微信安装程序或微信安装压缩包。客户必须自行从官方渠道安装受支持版本；管理器只负责发现或让用户手动选择本机现有的 `Weixin.exe`。

## 当前发布状态

尚不可收费发布。新 HOOK 注入器和 DLL 仍需完成商业再分发权、安全性和实机稳定性审查；AstrBot、插件、运行时与安装器也尚未进入完整发行清单。

## 本地开发

```powershell
cd E:\wechatrebot
C:\Program Files\Python313\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

运行首次配置原型：

```powershell
.\.venv\Scripts\python.exe -m wechatrebot_manager bootstrap
```

由发行载荷运行完整首次配置（当前未完成载荷只允许开发验收）：

```powershell
.\.venv\Scripts\python.exe -m wechatrebot_manager bootstrap `
  --payload .\artifacts\staging `
  --allow-incomplete-payload
```

配置完成后的启动与诊断入口：

```powershell
.\.venv\Scripts\python.exe -m wechatrebot_manager start D:\WeChatReBot
.\.venv\Scripts\python.exe -m wechatrebot_manager diagnose D:\WeChatReBot
```

`start` 会先等待 AstrBot 的 OneBot WebSocket 就绪，再启动新 HOOK 桥接。它不会调用任何历史桥接入口。

构建不含受控二进制的初始 staging：

```powershell
.\.venv\Scripts\python.exe scripts\build_staging.py --source-root E:\Project
```

该命令只写入 `artifacts/`，不会修改或启动 AstrBot、微信和任何桥接进程。
