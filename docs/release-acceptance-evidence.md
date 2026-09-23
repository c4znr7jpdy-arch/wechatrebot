# 正式发布证据收集规范

`release/acceptance-evidence.json` 是验收证据索引，不保存私钥、密码、客户消息、Cookie 或其他秘密。证据原件应存入受控归档，索引只记录脱敏相对路径、SHA-256、责任人和验收时间。单人维护时由维护者本人记录和验收，不需要第二人审批。该索引不参与构建或安装判定；正式客户发布仍须按 [发布核对清单](../release/RELEASE-CHECKLIST.md) 完成适用的人工门禁。

## 每项批准记录

将 `status` 改为 `approved` 前必须同时填写：

- `reviewer`：承担验收责任的人；单人项目可填写维护者本人；
- `approved_at`：带时区的 ISO 8601 验收时间；
- `evidence[]`：至少一项，包含 `path` 和 64 位 SHA-256；
- 证据内容明确版本、文件哈希、机器环境和结论，不能只写“测试通过”。

完成一项真实验收后，可用下列命令记录审核人、时间、相对路径和哈希：

```powershell
python scripts/record_acceptance_evidence.py <检查项ID> <脱敏证据文件> --reviewer <审核人>
```

该命令拒绝模板、仓库外文件、空文件及明显包含私钥或证书密码的内容；它只记录责任链，不判断审核结论是否专业或真实。

## 外部验证要求

- HOOK 授权：权利人、授权范围、商业再分发、修改权、地域和期限。
- HOOK 安全：与 `hook-binaries.json` 完全相同的哈希、静态/动态分析和审核结论。
- 实机微信：4.1.8.27 真实账号的登录、文本/图片/文件、断线重连、退出和重复启动；证据必须脱敏。
- Authenticode：管理器、注入器、DLL 的 `Valid` 状态、证书指纹、时间戳和签名后 SHA-256。
- 干净 Windows：无开发环境机器上的安装、中文/空格路径、配置、快捷方式、启停、升级、回滚和诊断。
- 杀毒软件：Windows Defender 加另一种主流安全软件；不得关闭防护，记录产品版本、规则版本和误报处理。
- 离线密钥备份：仅记录双人复核、介质位置编号、恢复演练日期和结果，绝不记录私钥内容或密码。

## 建议流程

为避免反复冻结构建和遍历 32,216 个载荷文件，按以下顺序执行：

1. 先核对商业授权、许可证、客户协议和离线密钥备份等不依赖最终二进制的事项。
2. 只运行一次完整自动化测试并只冻结构建一次最终管理器。
3. 签名最终管理器及两项 HOOK 文件，采集签名后哈希并同步元数据。
4. 构建一次 staging，用这份候选完成安全、实机微信、干净 Windows、杀毒、升级和回滚验收。
5. 写入所需的脱敏验收证据和审核责任人。
6. 对 staging 执行一次最终完整哈希验证，随后组装并签名远程更新包。
7. 版本化 ZIP 先上传并校验，`stable.json` 最后原子替换；再验证线上 ZIP、签名清单和立即前一版本升级。

需要核对元数据内部一致性时，可运行：

```powershell
python scripts/validate_release_metadata.py
```

该命令检查版本、文件路径、固定哈希和证据索引格式，不检查审批是否完成，也不会阻止构建。

## 最终签名步骤

最终冻结构建只执行一次。取得可信代码签名证书后，对最终管理器及两项 HOOK 文件执行：

### 第一次办理代码签名

面向普通 Windows 客户发布时，应办理受 Windows 信任链认可的 Authenticode 代码签名证书或签名服务；项目的更新清单私钥只验证远程更新元数据，不能代替 Authenticode。

采购/开通时向供应方明确以下要求：

- 申请主体所在地为中国大陆，并确认供应方支持该主体类型和实名/组织验证；
- 用途为 Windows 桌面程序的 Authenticode 签名，需签署 `.exe` 和 `.dll`；
- 证书链受 Windows 信任，支持 SHA-256 和 RFC 3161 时间戳；
- 私钥保存在合规 USB 硬件令牌或供应方云 HSM，询问本机 `SignTool` 的接入方式；
- 证书主题显示的发布者名称应与客户协议中的销售/许可主体一致；
- 询问签署次数、并发限制、续费、吊销和时间戳服务地址。

不要购买仅用于 SSL/TLS、文档签名、测试签名或企业内网私有信任的证书。公开代码签名证书的私钥通常不能导出成普通 PFX；硬件令牌或云 HSM 是正常交付形态。

在签名电脑安装供应方中间证书/令牌驱动或云签名客户端，并从 Microsoft Windows SDK 安装 `SignTool`。证书准备完成后先执行：

```powershell
Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert |
  Select-Object Subject, Thumbprint, NotAfter, HasPrivateKey

Get-Command signtool.exe -ErrorAction SilentlyContinue
```

如果云签名服务不把证书暴露为本机证书存储中的私钥，现有 `sign_release_binaries.ps1` 不能直接使用；应根据供应方官方 `SignTool` 集成参数新增一个明确适配器，不能导出或复制云端私钥来绕过。

```powershell
powershell -File scripts/sign_release_binaries.ps1 `
  -ManagerExe <最终管理器> `
  -InjectorExe <注入器> `
  -HookDll <HOOK-DLL> `
  -CertificateThumbprint <证书指纹> `
  -TimestampUrl <RFC-3161-时间戳地址>

powershell -File scripts/collect_windows_release_evidence.ps1 `
  -ReleaseVersion 0.8.3 `
  -ManagerExe <最终管理器> `
  -InjectorExe <注入器> `
  -HookDll <HOOK-DLL> `
  -Output release/signing-evidence.json `
  -RunDefenderScan

python scripts/sync_signed_hook_metadata.py `
  --manager-build-manifest <最终管理器构建目录>/manager-build-manifest.json
```

签名会改变 PE 文件哈希。同步脚本只更新签名状态、签名后大小和固定哈希，不会自动给出商业授权、安全审查或实机验证结论。

最终验证时应重新执行一次 `build_legal_inventory.py`，让最终锁定运行时、管理器构建环境、五个交付插件、NOTICE、许可证汇编和对应源码说明的哈希保持一致；日常修改阶段不需要反复生成。

staging 构建后可以直接交给 `build_distribution.py` 或 `build_update_release.py`；审批状态和证据完整度不会阻止这两个脚本运行。
