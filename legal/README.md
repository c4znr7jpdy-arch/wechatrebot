# 发行许可门槛

商业发布前必须补齐：

1. 新 HOOK 注入器和 DLL 的来源、商业再分发权、哈希与安全审查记录；
2. AstrBot 的 AGPLv3/EULA 合规方案与对应源码提供方式；
3. 插件、Python/Node 包、字体、图片和其他资产的 SBOM、许可证与 NOTICE；
4. 自有代码的著作权归属、客户许可、支持期和更新权说明。

Python 3.13.9 x64 与 AstrBot 4.26.6 运行时已经通过固定版本、wheelhouse 汇总指纹、逐 wheel SHA-256、离线复建和 staged import 校验。运行时技术可重复性已验证，但该结果不替代 AstrBot、Python 包及其传递依赖的许可证审查。

当前 `WeChatReBot.exe` 与两项新 HOOK PE 文件均未进行 Authenticode 签名。未签名状态必须在交付前通过可信代码签名、签名后哈希归档和干净 Windows 环境信誉测试关闭，不能用降低客户系统安全设置的方式绕过。

开发审计可使用 `hook-binary-redistribution` 门禁验证固定哈希文件的集成结果；该参数不是商业再分发授权，也不能改变 `release/hook-binaries.json` 中的待审核状态。

未关闭这些门槛前，`release/source-manifest.json` 中的受控二进制默认不会进入 staging。

微信客户端及其安装程序不属于商业项目交付物。客户必须自行从官方渠道取得并安装微信；本项目只保存客户选定的本机 `Weixin.exe` 路径，不复制或再分发微信文件。

最终发行批准项和证据要求见 `RELEASE-CHECKLIST.md`。
