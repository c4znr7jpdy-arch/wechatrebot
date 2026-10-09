# 授权服务部署记录

2026-10-09，授权服务已部署到 `47.242.208.64`，公网入口为 <https://ss.xiuxianjyj.xin/wechatrebot-license>。月付 69 元、年付 699 元、首次兑换试用码后试用 1 小时。管理员核实交易或资格后人工发码，没有在线付款或自动扣款。

| 项目 | 当前配置 |
| --- | --- |
| 镜像 | `wechatrebot-activation:acf59eb8e6c5`，仅包含服务及复用的公共协议 |
| 代码 | `/opt/wechatrebot-activation/current` 指向 `releases/acf59eb8e6c5` |
| 托管 | `wechatrebot-activation.service`，开机启动、异常退出重启；容器内健康检查每 30 秒一次 |
| 服务用户 | `wechatrebot-activation`，无交互登录 |
| 数据 | `/var/lib/wechatrebot-activation`，目录 0700；私钥文件 0600 |
| 宿主机监听 | `127.0.0.1:8091`，由现有 nginx HTTPS 转发 |
| 资源限制 | 192 MiB 内存、0.5 CPU、64 PID，根文件系统只读，丢弃 Linux capabilities |
| 公钥编号 | `b2c3cac3613bdfcb`；公钥已填入客户端配置，私钥只在服务器生成和保管 |
| 备份 | `/var/backups/wechatrebot-activation`，专用账号 0700 目录；北京时间每日 04:20 起随机延迟至多 30 分钟，保留 14 份完整快照 |
| 微信群 | WXRebot；当前二维码 2026-10-16 00:00（北京时间）截止 |
| 客户端状态 | `enabled:false`，未构建、发布或更新客户安装包 |

本目录 Dockerfile 固定 Python 镜像摘要，安装现有锁定版本的依赖。部署传输只包含服务、共享协议、原子 JSON 写入及部署配置，不上传机器人运行时、vendor、客户资料或设备私钥。容器不运行微信或机器人。

## 管理入口

服务器已安装 `/usr/local/bin/wechatrebot-activation-admin`，将子命令转发到受限容器。管理员通过 SSH 调用该入口；完整业务参数见 [服务端说明](../../services/activation_service/README.md)。例如核实某客户的试用资格后，可调用：

```powershell
ssh root@47.242.208.64 wechatrebot-activation-admin issue --owner-ref customer-001 --reference trial-20261009-001 --plan trial
```

客户编号必须稳定，业务编号必须唯一。明文码仅生成时出现一次，应通过与客户的私密渠道交付。月付和年付分别使用 `month`、`year`；已有授权增加时长使用 `renew`，不要重复发新开通码。查询使用 `inspect`，停用使用 `revoke`。命令不会代替管理员核实收款。

## 微信二维码更新

使用 `set-contact --image <容器可读图片路径> --group-name <群名称> --expires-at <有效期>` 原子替换；`clear-contact` 清空。当前私有数据目录映射为容器 `/data`。PNG/JPEG 图片最多 512 KiB，日期 `2026-10-16` 表示北京时间当天零点截止，精确时刻必须附时区。

更新后无需重启服务，客户端在下次刷新（通常 60 秒内）读取新资料。管理器的刷新按钮不能替微信群生成新二维码，管理员仍需从微信获取新图。过期二维码隐藏；远端明确清空后，同一管理器进程不会因网络失败回退旧内置图。仍有效的内置回退图会明确显示刷新失败提示。

二维码读取是匿名接口，不消耗激活码、不创建设备、不改变授权和微信状态。二维码图片和到期时间属于公共联系资料，不是授权凭证。

## 备份与恢复

`wechatrebot-activation-backup.timer` 已启用，调用独立的标准库脚本 `backup.py`。脚本与数据库使用同一服务账号执行，拒绝跨 UID、符号链接、空数据库和越界清理；不依赖容器或服务器的 Python 第三方库。快照通过 SQLite 在线备份产生，连同私钥和可选二维码资料原子完成；只有完整且哈希吻合的本工具快照参与 14 份轮转，失败不会留下标为成功的新快照。业务容器没有挂载备份目录。

首次备份已完成并在独立、无网络的容器内恢复到临时目录，数据库完整性、私钥与客户端公钥匹配、二维码内容和权限均通过。备份是本机恢复点，独立存储复制及异机灾备仍待安排。恢复旧数据库可能回滚兑换或撤销记录，须先停止独立授权服务并核对恢复点之后的业务审计，不能直接覆盖正在运行的数据库。

## 反向代理与回滚

`../nginx/wechatrebot-license-limits.conf` 放在 nginx `http` 级配置目录，`../nginx/wechatrebot-license.conf` 只包含在 ss.xiuxianjyj.xin 的 HTTPS server 中。仅新增 `/wechatrebot-license/` 路由，原网站和 `/wechatrebot-updates/` 继续使用原配置。代理按实际客户端 IP 限流；业务服务只信任 socket 来源，不信任转发头。

加入路由前保存的 nginx 配置位于 `/opt/wechatrebot-activation/nginx-before-license/shengshi`。更改后必须通过 `nginx -t`、平滑 reload 及确切的健康 JSON 验证；reload 短暂期间旧 worker 可能仍返回原网站 HTML，HTTP 200 本身不代表成功。验证失败恢复旧配置并再次校验、reload。

服务升级应使用新的内容标识镜像，保留旧镜像和一致数据库备份，再更新 `/etc/wechatrebot-activation.env` 中的镜像并重启独立服务。未知数据库版本拒绝打开；不得在未知迁移状态下直接回退数据库。任何操作都不能覆盖或重新生成现有签名私钥。

## 现场验证

- 容器 `running/healthy`，私有目录和私钥权限正确；端口仅绑定宿主机回环地址。
- 公网 `GET /health` 返回约定 JSON，`GET /v1/contact` 返回 WXRebot 当前图片、截止时间及服务器时间，均为 `Cache-Control: no-store`。
- 二维码解码后 SHA-256：`392860823d883358d4239567b5a3d13dac0b277343ee1255c74101b26ac9ca03`，与管理器内置图相同。
- 客户端实际匿名读取为 `remote/ready`，临时安装目录未生成设备或授权文件。
- 签名请求携带不存在的码得到 `code_unavailable`（409），重复 nonce 得到 `request_replayed`（401）；没有生成或兑换生产客户激活码。
- 切换前后原网站及稳定更新清单 HTTP 状态与正文 SHA-256 一致。

真实微信登录、绑定、到期停用、续期后手动恢复和干净客户安装仍需验收；服务器上线不表示客户收费开关已经启用。
