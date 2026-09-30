# 推特卡片的托管页面

微信卡片使用 `jiang_short_drama` 已配置的 HTTPS 内容服务。推特插件复用
`RemoteSelectionStore.store_payload` 的鉴权、地址校验、超时及请求大小限制，
不另设上传密钥，也不再回退为跳转 x.com 的卡片。

发文人的头像通过 Twitter 插件现有代理和持久化头像缓存下载，缩为 JPEG，
与原有 `_tweet_page_html` 生成的正文、译文、原文一起上传。页面内嵌头像，
卡片缩略图指向同服务的 JPEG 地址。页面没有脚本，服务响应使用 sandbox CSP。
推文中的图片和视频仍保留原始媒体地址；本次没有增加视频中转服务。

## 服务端兼容

服务器基线是已有短剧托管服务。按已有 hosted-only、controls、cinema 补丁之后，
应用 `patches/short-drama-player-twitter-pages.patch` 到 `state_server.py`；
Nginx 在已有 cinema-nginx 补丁之后应用
`patches/short-drama-player-twitter-nginx.patch`。

- 已有鉴权 `POST /short-drama/api/selections` 增加 `kind=tweet`，返回原有短令牌。
- `GET /short-drama/twitter/{token}` 返回推文快照。
- `GET /short-drama/twitter/{token}/avatar` 返回作者 JPEG 头像。
- 沿用已有存储目录、TTL 和容量清理策略；过期或被清理的快照返回 404。
- 已有短剧查询、推荐和片单接口保持兼容。

插件使用现有内容服务配置，读取不到有效服务、头像下载失败或上传失败时，
保留可读文字推送并记录失败类型。用户配置的短剧插件必须启用。
发布部署时先更新服务端并验证页面和头像，再更新本地插件；服务端保留原文件备份，
Nginx 检查成功后才重新加载。此服务更新不涉及客户安装包或线上版本清单。

## 翻译

专属订阅页提供开关、目标语言、模型 ID 和总等待时间，复用原 translation 配置。
模型 ID 留空跟随会话模型；保存即更新消息服务，保留自定义翻译提示词。
文字、卡片、截图共用翻译调用，失败或超时保留原文。

目标版本继续使用 0.9.9；插件与资源采用组件增量更新，无需完整载荷。
