# Twitter 插件来源与本地适配

- 上游：https://github.com/Ars1027/astrbot_plugin_twitter
- 锁定提交：`d29fd1f436d122312792183ff016a932b9dd55f1`（v1.12.0）
- 导入日期：2026-09-24
- `LICENSE` 保留上游许可证原文。
- 微信卡片沿用 `E:/Project/data/plugins/astrbot_plugin_twitter` 的 Share 标题、摘要、北京时间、头像代理和推文页面桥接。

本目录是交付源码；构建不再复制 E:/Project 的旧 Twitter 插件，也不再应用旧 Twitter 补丁。配置和 `twitter_subs` KV 键继续沿用，替换代码不删除订阅、游标、用户配置。

本地适配提供 iframe 内的网络配置和检查结果、按订阅选择文字/卡片/截图、聊天命令设置格式。截图继续使用上游渲染服务；失败回退文字。微信推文直接发送，不包装成 QQ 合并转发。
