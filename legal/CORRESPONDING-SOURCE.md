# 对应源码与本地修改说明（待法务审核）

状态：`REVIEW REQUIRED`。本说明描述随交付物可获得的源码要素，不构成对 AGPL/GPL 等许可证义务已经履行的法律结论。

## 随包可获得内容

- 管理器 Python 源码：`manager/src/wechatrebot_manager/`；
- 微信连接桥源代码：`app/bridge/src/`；
- 实际交付插件源代码：`app/astrbot/data/plugins/` 下列入当前功能清单的 24 个运行模块；
- AstrBot 4.26.6 的已安装 Python 源码及资源：`runtime/python/Lib/site-packages/astrbot/`；
- AstrBot 本地修改补丁：`legal/corresponding-source/astrbot-4.26.6-wechat.patch`，以及同目录全部 `astrbot-plugin-*.patch` 插件适配补丁；
- 固定依赖与复建输入：`legal/runtime-requirements.lock`、`legal/runtime-wheelhouse.json` 和 `legal/corresponding-source/build_runtime.py`；
- 第三方许可证文本：`legal/THIRD-PARTY-LICENSES.txt`。

## 媒体与共享工具

- FFmpeg/FFprobe 采用 `legal/ffmpeg-runtime.json` 固定的 BtbN Windows x64 LGPL shared 构建，保留原始 DLL 与 `runtime/ffmpeg/LICENSE.txt`。清单记录二进制来源、SHA-256、FFmpeg 源码提交及构建脚本来源；该清单随完整载荷提供。2026-08-31 月末构建的官方二进制、源码及构建脚本链接均已核验。
- Node 由已固定的 Playwright wheel 提供，位于 `runtime/python/Lib/site-packages/playwright/driver/node.exe`；同目录 `LICENSE` 包含 Node 与捆绑依赖声明。用于网页驱动和 YouTube 解析，不运行独立音乐服务。
- 米游社工具源库及 MIT LICENSE 位于 `app/astrbot/nonebot-plugin-mystool/`；洛克王国工具源库与模板位于 `app/astrbot/ai_plugin/nonebot_plugin_rocom/`，后者未发现独立许可证，SBOM 保留 `NOASSERTION` 并要求来源权利确认。
- 发布者应归档实际分发的 FFmpeg 源码、构建输入和传递依赖源码；仅列出上游链接不代表已满足全部对应源码义务。不得限制接收者替换 LGPL DLL 或行使第三方许可证授予的权利。

## 必须人工关闭的问题

1. 确认上述内容是否构成各强 copyleft 组件所要求的完整对应源码，包括生成、安装和运行所需脚本。
2. 确认 `astrbot_plugin_angel_heart 2.2.2` 的本地修改情况；如有修改，提供可审阅差异和修改日期。
3. 明确网络交互场景下向用户提供对应源码的稳定方式、有效期限和获取说明。
4. 对 SBOM 中其他 AGPL/GPL 组件逐项判断它们是否实际分发、是否有例外条款，以及应提供的源码范围。
5. 在客户协议中保留第三方许可证权利，不以“禁止转售”等自有软件条款限制开源许可证明确授予的权利。

`release/legal-review.json` 保留人工审核记录，但其状态不参与构建或安装判定。
