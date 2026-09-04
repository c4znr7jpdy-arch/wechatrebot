# 商业版远程更新发布

远程更新只发布客户二进制载荷和签名清单，不要求客户访问私有 GitHub 仓库，也不会把源码仓库地址或访问令牌写进客户端。

## 首次绑定

项目已在 `config/local/update-signing-private.pem` 生成本机 Ed25519 私钥，并把对应公钥写入 `src/wechatrebot_manager/update-channel.json`。私钥已被 `.gitignore` 排除。

正式首发已绑定签名清单地址：

`https://ss.xiuxianjyj.xin/wechatrebot-updates/stable.json`

服务器发布目录为 `/srv/wechatrebot-updates`，内部在线更新 ZIP 放在 `releases/`，稳定清单为根目录的 `stable.json`。客户手动下载页为 `https://ss.xiuxianjyj.xin/wechatrebot-updates/`，面向客户的完整安装 ZIP 放在 `downloads/`，页面读取根目录的 `download.json` 展示版本、实际文件大小、更新说明和 SHA-256。

正式首发前还必须完成：

1. 把签名私钥复制到至少一份离线加密备份。私钥丢失后，已售客户端无法验证用新密钥签发的更新。
2. 不要把服务器凭据、GitHub PAT 或更新签名私钥放进客户程序、配置模板、日志或仓库。

客户安装目录可以用 `config/update-channel.json` 覆盖 `manifest_url`、检查间隔和超时，签名公钥仍固定使用程序内置值，不能被覆盖。

没有可发布的新包时，用 `scripts/build_update_status.py` 发布当前版本的签名状态清单。它不携带下载包，当前版本客户端会显示“已是最新版本”：

```powershell
python scripts\build_update_status.py `
  --output artifacts\update-status-0.8.0 `
  --private-key config\local\update-signing-private.pem `
  --note "远程更新通道已就绪"
```

## 每次发布

### 管理器一键增量发布（常用）

仅修改 `src/wechatrebot_manager/`、插件中心或管理器内置 Web UI 时，先把项目版本提升到高于线上稳定版，然后执行：

```powershell
.\scripts\publish-manager-update.ps1 `
  --title '插件配置更新' `
  --note '修复高级配置页面'
```

该命令自动完成测试、冻结管理器、自检、生成仅含管理器 EXE 的 Ed25519 签名增量包、SSH 上传、服务器 SHA-256 校验、稳定清单原子切换和公网下载/解压冒烟。线上验收失败时会自动恢复上一份稳定清单。

> 兼容边界：`0.8.7` 之前的 PyInstaller 单文件客户端只等待应用子进程退出，没有等待仍占用管理器 EXE 的引导父进程。从这些旧版升级时必须使用完整载荷更新；线上稳定版低于 `0.8.7` 时，一键增量发布器会直接拒绝发布。完整载荷的事务预处理为旧引导进程留出退出时间，`0.8.7` 起的更新器还会显式等待两个进程并重试短暂文件占用。

后续恢复管理器增量发布前，应确认所有仍受支持的客户安装都已至少升级到 `0.8.7`；只提升线上稳定版本并不能证明离线客户已经完成过渡。

- `--dry-run`：只在本地构建和签名，不上传。
- `--yes`：跳过发布前输入版本号确认，适合无人值守执行。
- `--manager-build artifacts\...`：复用已经完成且清单校验通过的管理器构建。
- `--skip-tests`、`--skip-remote-smoke`：仅用于明确知道风险的故障排查，不建议正式发布使用。

增量包不会重建或遍历完整 staging，适合管理器及其内置静态资源更新。只要运行时、`app/astrbot/data/plugins/`、桥接程序或 hook 文件发生变化，就必须使用下面的完整载荷流程。

### 完整载荷发布

运行时、AstrBot 插件、桥接程序、hook，或旧单文件管理器的引导修复必须使用完整载荷一键入口。该入口内部依次完成全量测试、发布元数据校验、管理器冻结、便携运行时构建、staging 组装、面向客户的完整安装 ZIP 与校验文件生成、在线更新载荷扫描、Ed25519 签名、上传、Nginx 配置校验、稳定清单与下载页元数据原子切换，以及公网下载/解压冒烟；下载页元数据验收失败时会同时恢复上一份下载信息和稳定清单。

```powershell
.\scripts\publish-full-update.ps1 `
  --source-root 'E:\Project' `
  --title '完整载荷稳定性更新' `
  --note '修复旧版管理器更新回滚' `
  --yes
```

`--source-root` 必须指向包含桥接程序和所交付 AstrBot 插件的项目源码根目录。正式发布前项目版本必须高于线上稳定版本，签名私钥与 SSH 环境必须可用。只需本地完整构建时可追加 `--dry-run`；正式发布不要跳过测试或远程冒烟。

## OSS/CDN 大文件分发

`release/oss-publish.json` 保存不含密钥的 OSS 目标配置。当前目标为北京地域的私有 Bucket `jiangxiaomei-releases-bj`，对象前缀为 `wechatrebot-updates`。配置中的 `enabled` 默认保持 `false`，因此现有一键发布仍使用服务器存储，不会意外上传 OSS。

接入 CDN 并完成公网下载验证后，将 `public_base_url` 设置为 CDN HTTPS 地址，再显式使用 `--artifact-storage oss` 试运行。`--dry-run` 只生成和校验本地产物，不查找 `ossutil`、不上传 OSS、也不修改服务器状态。同版本允许用于本地试构建，但正式发布仍要求项目版本高于线上稳定版本。

正式 OSS 发布按以下顺序执行：

1. 本地测试、构建、签名并校验交付包。
2. 通过已配置 RAM 凭证的 `ossutil` 将在线更新包、客户完整包和校验文件上传为私有 Object。
3. 使用 `stat` 核对 Object 的 `Content-Length` 和发布时写入的 SHA-256 元数据；一致时可跳过重复上传。
4. 从 CDN 公网地址执行 HEAD 和完整更新冒烟测试。
5. 最后才原子切换服务器上的 `stable.json` 与 `download.json`。

OSS Object 始终使用私有 ACL。CDN 应开启“OSS 私有 Bucket 回源”；发布脚本不会关闭 Bucket 的“阻止公共访问”。上传使用 ossutil 自带的 CRC64 校验和大文件断点续传能力。

客户下载包与内部更新包用途不同：客户包是外层 ZIP，完整解压后包含 `WeChatReBot.exe`、`payload.wrb`、安装说明和分发清单；内部更新包仅供已安装管理器验证并事务更新，不能把 `releases/` 中的 ZIP 当作客户安装包发送。客户下载文件名带 SHA-256 前缀，避免同版本重试发布时命中旧的不可变缓存。

发布脚本会在包内载荷清单保留 `source_manifest_status: ready` 兼容标记。该字段仅用于让 0.8.3 及更早版本通过旧版更新入口升级；新版本不再用它判断载荷是否完整或可信，仍以签名、SHA-256、路径边界和逐文件清单为准。不要手工删除该字段，否则旧版会在下载完成后拒绝更新包。这个标记只解决旧版的载荷准入，不解决上述单文件 EXE 占用问题；从 0.8.2/0.8.3 升级到 0.8.7 仍应发布完整载荷。

客户控制台会在启动约 2 秒后检查一次，之后每 6 小时检查。发现更高版本后弹窗；客户点击“立即更新”才会下载和安装，不做静默强制覆盖。手动点击顶栏“检查更新”可立即检查。

## 客户端安全与回滚

- 清单先验证 Ed25519 签名，再读取版本、下载地址和包摘要。
- 下载只接受 HTTPS，限制清单、压缩包、文件数量和展开体积。
- ZIP 中所有路径必须位于 `payload/`，拒绝绝对路径、路径穿越、Windows 保留名和符号链接。
- 下载完成后校验包体 SHA-256，展开后再次按 payload 清单逐文件校验。
- 当前管理器复制为独立更新程序；主窗口退出后，独立副本事务替换安装文件，因此可以安全更新管理器本身。
- 安装失败时恢复被替换的旧文件并重新打开原管理器；下次打开会显示失败原因。
- 更新不会自动启动机器人，避免在客户未确认微信账号时直接运行。

## 发布纪律

- 更新 ZIP 和清单可以放到 CDN/对象存储，私有源码仓库继续保持 private。
- 不允许使用可被客户提取的共享密钥或 HMAC 代替数字签名。
- 不允许跳过 SHA-256 和更新清单签名校验；发布前仍建议完成代码签名及干净机升级/回滚测试。
- 签名私钥轮换必须先通过旧密钥签发一个内置新公钥的过渡版本，再切换服务端签名；不能直接替换公钥。
