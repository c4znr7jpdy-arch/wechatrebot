# 商业版远程更新发布

远程更新只发布客户二进制载荷和签名清单，不要求客户访问私有 GitHub 仓库，也不会把源码仓库地址或访问令牌写进客户端。

## 首次绑定

项目已在 `config/local/update-signing-private.pem` 生成本机 Ed25519 私钥，并把对应公钥写入 `src/wechatrebot_manager/update-channel.json`。私钥已被 `.gitignore` 排除。

正式首发前必须完成：

1. 为更新文件准备独立 HTTPS 静态地址或对象存储；清单建议固定为 `https://更新域名/stable.json`。
2. 把该地址写入 `src/wechatrebot_manager/update-channel.json` 的 `manifest_url`，重新构建首发管理器。
3. 把私钥复制到至少一份离线加密备份。私钥丢失后，已售客户端无法验证用新密钥签发的更新。
4. 不要把对象存储管理密钥、GitHub PAT 或更新签名私钥放进客户程序、配置模板、日志或仓库。

客户安装目录可以用 `config/update-channel.json` 覆盖 `manifest_url`、检查间隔和超时，签名公钥仍固定使用程序内置值，不能被覆盖。

## 每次发布

1. 修改代码和资源后提升语义版本，同时更新 `pyproject.toml`、包版本与 Windows 版本资源。
2. 构建新的冻结管理器和完整 staging；更新载荷必须是 `ready`，研发未完成载荷不能被远程安装。
3. 执行 `scripts/build_update_release.py`。脚本会重新校验全部 payload 文件、生成全量 ZIP、计算 SHA-256，并用本机私钥签署 `stable.json`。
4. 先上传版本 ZIP，确认 HTTPS 下载地址可用；最后原子上传或覆盖 `stable.json`。清单最后发布可避免客户拿到尚未上传完成的包。
5. 保留 ZIP、清单、SHA-256、版本号、发布时间和订单范围的发布记录。

示例：

```powershell
python scripts\build_update_release.py `
  --payload artifacts\staging `
  --output artifacts\update-0.8.0 `
  --private-key config\local\update-signing-private.pem `
  --package-url https://updates.example.com/releases/wechatrebot-0.8.0-update.zip `
  --title "功能与稳定性更新" `
  --note "新增功能配置与热重载" `
  --note "修复启动提示"
```

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
- 不允许跳过 `ready` 门禁、SHA-256 校验、代码签名或干净机升级/回滚测试。
- 签名私钥轮换必须先通过旧密钥签发一个内置新公钥的过渡版本，再切换服务端签名；不能直接替换公钥。
