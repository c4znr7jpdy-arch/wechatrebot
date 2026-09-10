# 发行许可门槛

商业发布前必须补齐：

1. 新 HOOK 注入器和 DLL 的来源、商业再分发权、哈希与安全审查记录；
2. AstrBot 的 AGPLv3/EULA 合规方案与对应源码提供方式；
3. 插件、Python/Node 包、字体、图片和其他资产的 SBOM、许可证与 NOTICE；
4. 自有代码的著作权归属、客户许可、支持期和更新权说明。

Python 3.13.9 x64 与 AstrBot 4.26.6 运行时已经通过固定版本、wheelhouse 汇总指纹、逐 wheel SHA-256、离线复建和 staged import 校验。运行时技术可重复性已验证，但该结果不替代 AstrBot、Python 包及其传递依赖的许可证审查。

完整发布入口按实际构建结果重新生成 CycloneDX SBOM、第三方组件声明和许可证文本汇编，当前应包含 24 个交付模块、CPython、FFmpeg/FFprobe、Playwright 自带 Node、全部 wheel 与共享游戏工具库。现有静态清单可能来自上一版，应以本次完整构建生成并随包提供的结果为准。`release/legal-review.json` 仍保留 `review-required`，包括 Rocom 等未确认授权的源码和资产及强 copyleft 组件；技术验证不替代来源权利和许可证审查。

图形管理器的功能配置窗口使用 pywebview 6.2.1（BSD-3-Clause）及其固定传递依赖，并依赖 Windows Edge WebView2 Runtime 和 .NET Framework 4.6.2 或更高版本。正式交付前须将对应许可证文本、SBOM 和干净系统兼容性记录并入发行证据。

当前 `WeChatReBot.exe` 与两项新 HOOK PE 文件均未进行 Authenticode 签名。未签名状态必须在交付前通过可信代码签名、签名后哈希归档和干净 Windows 环境信誉测试关闭，不能用降低客户系统安全设置的方式绕过。

构建会按 `source-manifest.json` 中的固定哈希验证 HOOK 文件；哈希一致不等同于已经取得商业再分发授权。

构建会验证来源路径与固定哈希；审核状态是发行记录，不自动阻断 staging。发布者仍须完成上述实际分发审查。

微信客户端及其安装程序不属于商业项目交付物。客户必须自行从官方渠道取得并安装微信；本项目只保存客户选定的本机 `Weixin.exe` 路径，不复制或再分发微信文件。

最终发行批准项和证据要求见 `RELEASE-CHECKLIST.md` 与 `acceptance-evidence.json`。
