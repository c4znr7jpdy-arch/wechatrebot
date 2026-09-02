# 商业发行放行清单

只有全部阻断项都有可追溯证据并经负责人确认后，才允许将 `source-manifest.json` 的状态改为 `ready`。自动化通过不代表许可或安全审核已经完成。

## 阻断项

- [ ] 新 HOOK 注入器与 DLL 已取得明确的商业再分发授权，来源和修改记录可追溯。
- [ ] 两项新 HOOK 文件已完成恶意代码与供应链安全审查，审核对象哈希与 `hook-binaries.json` 一致。
- [ ] 在微信 `4.1.8.27` 真实账号上完成登录、收发文本/图片/文件、断线重连、退出和重复启动验证。
- [ ] `WeChatReBot.exe`、注入器和 DLL 完成预期的代码签名策略；签名后重新生成并归档哈希。
- [ ] 已确定实际交付的插件集合；每个插件均完成归属、许可证、第三方资源、密钥和客户数据清理。
- [ ] AstrBot、Python 运行时、全部 wheel、可选 ffmpeg/Node 组件完成 SBOM、NOTICE 和许可证履行审查。
- [ ] 客户许可协议明确永久代码授权范围、禁止转售条款、更新权、售后期限、责任边界和第三方开源义务。
- [ ] 在无开发环境的干净 Windows 机器完成安装目录选择、微信手选、中文/空格路径、首次配置、快捷方式、启动、停止、升级回滚和诊断导出。
- [ ] 完成 Windows Defender 与至少一种主流安全软件兼容测试，并记录误报处理流程；不得要求客户关闭安全软件。
- [ ] 发布包通过 `release_guard.py`，完整 payload SHA-256 校验、冻结管理器自测和安装后诊断。
- [ ] 发布归档、顶层 EXE、payload 清单和源代码交付物的 SHA-256 已归档并与订单版本关联。
- [ ] 客户文档不包含开发机路径、内部账号、API Key、Cookie、数据库、日志或微信安装文件。

## 当前未关闭状态

- `release/hook-binaries.json`: `redistribution_approved=false`，安全审查和实机验证均为 `pending`。
- `release/plugin-manifest.json`: `license-review-required`。
- 图形管理器版本 `0.5.0`: 尚未签名。
- ffmpeg 与 Node 侧车: 尚未纳入固定版本发行载荷。
