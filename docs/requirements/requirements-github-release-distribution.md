# 分发迁移到 GitHub Release：需求与实施方案

> 2026-11-03 确认。客户安装包、完整载荷、在线更新包和签名稳定清单全部改为放在本仓库的 GitHub Release，不再使用 OSS/CDN 和 `ss.xiuxianjyj.xin/wechatrebot-updates/`。授权服务、激活码和微信群二维码继续运行在 47.242.208.64，不在本文范围。管理后台见 [requirements-admin-console.md](requirements-admin-console.md)。

## 1. 已确认的规则

| 项目 | 规则 |
| --- | --- |
| 托管位置 | 仓库 `c4znr7jpdy-arch/wechatrebot` 的 GitHub Release；仓库为公开仓库，Release 资产任何人可下载，销售依赖激活码而不是下载链接保密 |
| 国内下载 | 直接使用 GitHub 资产地址，不加镜像或加速。国内网络下载慢或失败的风险由维护者承担；以后需要再补加速 |
| 版本 Release | 每个版本一个标签 `v<版本号>`，例如 `v0.9.9`，标题为版本标题，正文为更新说明 |
| 稳定清单 | 固定标签 `stable` 的 Release 只放一个资产 `stable.json`；发布时覆盖上传，地址永远是 `https://github.com/c4znr7jpdy-arch/wechatrebot/releases/download/stable/stable.json` |
| 签名 | `stable.json` 仍由开发机上的 Ed25519 更新私钥签名，公钥内置在客户端不变；私钥不进入 GitHub、服务器或 CI |
| 构建 | 构建、签名仍在 Windows 开发机一键完成；GitHub 只做存储 |
| 退役 | OSS 上传、服务器 `releases/`、`downloads/`、`download.json`、下载页发布脚本全部退役；`release/oss-publish.json` 改为 `enabled:false` 并保留为历史记录 |
| 过渡 | 当前没有客户安装，因此不需要为旧清单地址保留过渡期 |

## 2. Release 布局

每个版本标签 `v<版本>` 上传以下资产，文件名沿用现有带 SHA-256 前缀的命名，避免同版本重试命中缓存：

| 资产 | 来源 | 用途 |
| --- | --- | --- |
| `WeChatReBot-<版本>-Windows-x64-online-<sha12>.zip` 及 `.sha256` | 现有客户包生成逻辑 | 客户下载的外层 ZIP，内含 Inno 在线安装器 |
| `WeChatReBot-<版本>-payload-<sha12>.wrb` 及 `.sha256` | 现有 staging 载荷 | 在线安装器下载的完整载荷 |
| `wechatrebot-<版本>-update.zip` 及 `.sha256` | 现有在线更新包 | 已安装管理器的事务更新 |
| `wechatrebot-<版本>-manager-update.zip` 及 `.sha256` | 现有管理器增量包 | 仅管理器改动时的增量更新（管理器增量发布时才有） |
| `stable-<版本>.json` | 本次签名清单副本 | 回滚来源，内容与当时上传到 `stable` 的清单完全一致 |

`stable` 标签只有 `stable.json`。清单字段不变：`version`、`build`、`mandatory`、`minimum_supported_version`、`title`、`notes`、`package.url/size/sha256/uncompressed_size` 与签名。`package.url` 指向对应版本 Release 的更新包资产地址。

## 3. 发布流程

新增开发机一键入口 `scripts/publish-github-release.ps1`，背后是 `scripts/publish_github_release.py`。参数沿用 `publish-full-update.ps1`：`--title`、`--note`、`--minimum-supported-version`、`--build`、`--mandatory`、`--source-root`、`--dry-run`、`--yes`，另加 `--manager-only` 走管理器增量路径，替代 `publish-manager-update.ps1` 的上传部分。

完整载荷发布顺序：

1. 全量测试、发布元数据校验、冻结管理器、便携运行时、staging、客户安装 ZIP、在线更新 ZIP、`payload.wrb`，与现有完整载荷流程相同的前半段，直接复用现有函数。
2. 在线安装器的 `--payload-url` 指向本版本 Release 的 `payload.wrb` 资产地址；该地址在上传前即可确定，因为文件名由内容哈希决定。
3. 用 `gh release create v<版本>` 创建草稿 Release，上传全部资产和 `.sha256`，再把 Release 从草稿发布。
4. 从公网资产地址逐个验证：HEAD 跟随 302 后状态 200 且 `Content-Length` 等于本地大小；完整下载后 SHA-256 与本地一致。任一项失败则停止，Release 保留供排查，不上传稳定清单。
5. 用私钥签名 `stable.json`，先作为 `stable-<版本>.json` 上传到版本 Release，再覆盖上传为 `stable` 标签的 `stable.json`；从公网读回并验签、比对内容。
6. 公网冒烟：按客户端逻辑读取 `stable.json`、下载更新包并校验，证明一台已安装的管理器会看到并能安装这个版本。

`--dry-run` 只完成第 1、2 步，不创建 Release。`--manager-only` 只构建管理器增量包、上传到版本 Release 并更新稳定清单，不构建 staging，且沿用现有"线上稳定版低于 0.8.7 时拒绝"的兼容检查。

回滚：`scripts/publish-github-release.ps1 -Rollback <版本>` 把该版本 Release 里的 `stable-<版本>.json` 覆盖上传为 `stable/stable.json`，读回验签。不删除任何版本 Release。

## 4. 客户端与安装器改动

| 位置 | 改动 |
| --- | --- |
| `src/wechatrebot_manager/update-channel.json` | `manifest_url` 改为 `stable` 标签的 `stable.json` 地址；公钥、间隔、超时不变 |
| 更新客户端 | 不改下载逻辑：现有实现跟随跳转并校验最终地址为 HTTPS，GitHub 资产的 302 到 `objects.githubusercontent.com` 符合要求。补一个针对 302 的单元测试 |
| 发布端公网校验 | 现有 `_public_head` 对重定向报错，新发布器的校验改为跟随重定向后再核对状态与大小 |
| `installer/WeChatReBot.iss` | `AppUpdatesURL` 改为仓库 Releases 页面；载荷下载使用 Inno 自带下载页，实施时实测 GitHub 302 可被跟随并校验 SHA-256 |
| `release/START-HERE.txt`、README、`docs/remote-updates.md`、AGENTS.md | "0.9.8 起走 OSS/CDN"、服务器 `releases/`、`download.json` 等描述改为 GitHub Release；下载页相关段落删除 |

## 5. 代码结构与复用

| 位置 | 职责 |
| --- | --- |
| `scripts/publish_github_release.py`、`publish-github-release.ps1` | 新入口；复用 `publish_full_update.py` 的构建、客户包、载荷与清单生成函数，复用 `publish_manager_update.py` 的管理器增量构建和兼容检查 |
| `scripts/build_update_release.py`、`build_update_status.py` | 签名清单生成不变 |
| `scripts/publish_full_update.py`、`publish_manager_update.py` | 保留为构建函数库：构建、客户包、载荷、管理器增量与兼容检查继续从这里调用；其中 SSH 上传、服务器清单切换和下载站点函数不再被调用，随退役脚本一并删除 |
| `scripts/oss_release_storage.py`、`publish_native_download.py`、`publish-full-update.ps1`、`publish-manager-update.ps1`、`publish-native-download.ps1` | 退役并删除，连同只覆盖这些路径的测试；README 与 AGENTS.md 指向新入口 |
| `release/oss-publish.json` | `enabled:false`，注释说明 2026-11-03 起由 GitHub Release 承担 |
| `tests/test_publish_github_release.py` | 新增：资产命名、Release 草稿与发布顺序、公网校验跟随重定向、稳定清单覆盖与回滚、`--dry-run` 不调用 `gh`、`--manager-only` 兼容检查 |

`gh` 使用开发机已登录的账号，发布器在开始前检查 `gh auth status` 和对本仓库的写权限；缺失时在构建前就失败。

## 6. 验收场景

| 场景 | 预期 |
| --- | --- |
| `-DryRun` | 本地产物齐全、清单已签名，无 Release 创建，无网络写入 |
| 正式发布 0.9.9 | `v0.9.9` Release 含全部资产和 `.sha256`，`stable/stable.json` 验签通过且 `package.url` 指向 `v0.9.9` 的更新包 |
| 资产公网校验失败 | 流程停止，`stable.json` 不更新，Release 保留 |
| 安装器在线安装 | 从 GitHub 下载 `payload.wrb`，SHA-256 校验通过，安装成功 |
| 已安装管理器检查更新 | 读取 `stable.json` 验签，下载并校验更新包，可安装 |
| 回滚 | `stable.json` 变回上一版签名清单，客户端显示上一版本 |
| 重复发布同版本 | 发布器要求项目版本高于当前 `stable.json` 的版本，否则拒绝 |

## 7. 边界与风险

- 国内下载速度和可达性不受控；本文按维护者决定直接使用 GitHub 地址。客户端 SHA-256 校验保证内容完整，但不能保证下载成功。
- 单个资产上限 2 GB，当前载荷约 520 MB。
- Release 页面公开，任何人可下载安装包；运行仍需激活码。
- 本文不改变完整载荷与管理器增量的分类规则，也不改变"构建、签名、发布须维护者明确授权"的边界。
