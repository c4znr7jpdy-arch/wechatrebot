# 发布核对清单

本清单用于人工核对，不参与构建、打包、安装或远程更新判定。自动化通过不代表许可或安全审核已经完成。

## 建议检查项

- [ ] 新 HOOK 注入器与 DLL 已取得明确的商业再分发授权，来源和修改记录可追溯。
- [ ] 两项新 HOOK 文件已完成恶意代码与供应链安全审查，审核对象哈希与 `hook-binaries.json` 一致。
- [ ] 在微信 `4.1.8.27` 真实账号上完成登录、收发文本/图片/文件、断线重连、退出和重复启动验证。
- [ ] `WeChatReBot.exe`、注入器和 DLL 完成预期的代码签名策略；签名后重新生成并归档哈希。
- [ ] 已确定实际交付的 24 个运行模块（23 项用户可见功能）；仍需逐项完成许可证、第三方资源和 NOTICE 审查。`plugin-manifest.json` 中标记为 `excluded` 的模块、硬编码密钥、客户数据与 `.env.prod` 不进入审计载荷。
- [ ] AstrBot、Python 运行时、全部 wheel、内置 FFmpeg/FFprobe、Playwright 自带 Node 以及共享游戏工具库完成 SBOM、NOTICE 和许可证履行审查。
- [ ] 客户许可协议明确永久代码授权范围、禁止转售条款、更新权、售后期限、责任边界和第三方开源义务。
- [ ] 在无开发环境的干净 Windows 机器完成安装目录选择、微信手选、中文/空格路径、首次配置、快捷方式、启动、停止、升级回滚和诊断导出。
- [ ] 完成 Windows Defender 与至少一种主流安全软件兼容测试，并记录误报处理流程；不得要求客户关闭安全软件。
- [ ] 发布包通过 `release_guard.py`，完整 payload SHA-256 校验、冻结管理器自测和安装后诊断。
- [ ] 发布归档、顶层 EXE、payload 清单和源代码交付物的 SHA-256 已归档并与订单版本关联。
- [ ] 客户文档不包含开发机路径、内部账号、API Key、Cookie、数据库、日志或微信安装文件。
- [ ] 已轮换源仓库曾硬编码的 3 组签到 Cookie/Authorization；构建清空与发布扫描只是防止继续分发，不能撤销旧凭据。

## 当前记录

- `release/hook-binaries.json`: `redistribution_approved=false`，安全审查和实机验证均为 `pending`。
- `release/plugin-manifest.json` 记录各插件的许可证和审核信息。
- 完整载荷一键发布会在冻结运行时和管理器后重新生成 CycloneDX SBOM、NOTICE、许可证文本汇编与 `release/legal-review.json`；最终生成结果仍须完成 `review-required` 中列出的人工审批。
- `astrbot_plugin_angel_heart 2.2.2` 检测为 AGPL-3.0-only；对应源码、修改说明和网络交互场景的源码提供方式尚未批准。
- 客户协议审核稿已准备，正式签署版、交易主体、部署范围、售后期限、责任限制和争议解决条款仍待法务及业务确认。
- 图形管理器版本 `0.9.0`: 尚未签名。
- 远程更新公钥已内置，正式首发前仍须配置独立 HTTPS 发布地址、离线备份私钥并完成升级/回滚实机验证。
- Node/PHP 音乐侧车已经从商业运行链路移除；YouTube 解析复用已固定 Playwright wheel 内的 Node。FFmpeg/FFprobe 使用 `ffmpeg-runtime.json` 中固定的 BtbN 月末 LGPL shared 包，构建时校验 SHA-256 并实跑两项工具。
- Mnemosyne 向量记忆需要独立 Milvus 服务与 Embedding 模型。安装清单不代表这些外部服务已经开通。Rocom 共享源码和资产缺少独立许可证，仍须确认来源权利。
