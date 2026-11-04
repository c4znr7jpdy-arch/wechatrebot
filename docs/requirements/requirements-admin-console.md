# 维护者管理后台：需求与实施方案

> 2026-11-03 确认。在现有授权服务器上为维护者提供一个网页后台，覆盖微信群二维码、激活码（卡密）和发布版本切换。后台公网可达，登录使用长密码加手机验证器动态码。分发本身迁到 GitHub Release，见 [requirements-github-release-distribution.md](requirements-github-release-distribution.md)。

本文改变了 [订阅与授权方案](requirements-subscription-authorization.md) 中"没有公网管理员接口"的边界：管理操作可以经公网执行，但必须通过本文的登录、限流、审计和权限约束。

## 1. 已确认的规则

| 项目 | 规则 |
| --- | --- |
| 使用者 | 只有维护者一人，不做多用户、角色或注册 |
| 访问 | `https://ss.xiuxianjyj.xin/wechatrebot-admin/`，浏览器直接打开 |
| 登录 | 密码 + 6 位 TOTP 动态码；密码只存 PBKDF2-SHA256 哈希；连续失败 5 次锁定 15 分钟 |
| 会话 | 服务端会话，12 小时过期，退出即失效；Cookie `Secure`、`HttpOnly`、`SameSite=Strict`、路径限定在后台前缀 |
| 表单 | 每个会话一个 CSRF 令牌，所有写操作必须携带 |
| 功能 | 二维码查看/上传/清空；激活码发码、续期、补领、迁移、撤销、查询；发布版本查看、设为稳定版、回滚 |
| 不做 | 不接收安装包上传，不构建，不签名，不碰更新签名私钥、授权私钥和数据库文件，不代替管理员核实收款 |

## 2. 架构

不新起服务。授权服务进程在公网接口之外再挂一个管理应用，监听容器内第二个端口，由 systemd 单元同样只发布到宿主机回环地址；nginx 新增 `/wechatrebot-admin/` 路由。管理应用复用同一个 `LicenseStore`、二维码模块、协议和审计表。

```text
浏览器 --HTTPS--> nginx /wechatrebot-admin/ --> 127.0.0.1:8092 管理应用
客户端 --HTTPS--> nginx /wechatrebot-license/ --> 127.0.0.1:8091 公网接口
                                                    └── 同一进程、同一 /data
```

| 组成 | 说明 |
| --- | --- |
| `serve --admin-port 8092` | 同一 aiohttp 进程运行两个应用；未配置管理员凭据时管理端口返回"未初始化"，不提供登录 |
| `admin-init` | 服务器本机命令：设置密码、生成 TOTP 密钥，打印验证器可扫的 `otpauth://` 二维码和手输密钥，写 `/data/admin-auth.json`（0600）；已存在时拒绝覆盖，重置用 `admin-reset` |
| `admin-set-github-token` | 服务器本机命令：写入只对本仓库 Release 有写权限的细粒度 GitHub 令牌到 `/data/admin-github.json`（0600）；页面不回显 |
| `/data/admin-state.sqlite3` | 会话、登录失败计数、CSRF 令牌；与授权数据库分离，丢失只导致重新登录 |
| 审计 | 复用授权数据库 `audit` 表，`actor` 为 `admin-web`，记录登录成功/失败/锁定、每次发码、撤销、二维码替换、稳定版切换 |
| 备份 | `backup.py` 增加 `admin-auth.json` 和 `admin-github.json`；`admin-state.sqlite3` 不备份 |

TOTP 按 RFC 6238：HMAC-SHA1、30 秒步长、6 位、允许前后各一步；用标准库实现，不加依赖。密码哈希用 PBKDF2-SHA256 60 万次，与仓库现有做法一致。

## 3. 页面

服务端渲染 HTML，内置极简样式，不引入前端框架；表单提交后重定向，避免刷新重复提交。

| 路径 | 内容 | 操作 |
| --- | --- | --- |
| `/login` | 密码、动态码 | 登录；锁定期间显示剩余时间 |
| `/` | 服务健康、授权数/码数/试用数、最近备份快照时间、当前稳定版本 | 无 |
| `/contact` | 群名、截止时间、预览图、是否已过期 | 上传新图（PNG/JPEG ≤ 512 KiB）+ 群名 + 截止日期；清空 |
| `/codes` | 发码表单（新开通/续期/补领/迁移，字段同 CLI）、撤销表单、查询表单 | 发码成功后明文码只显示一次并提示复制；查询结果不含明文码 |
| `/releases` | GitHub Releases 列表（版本、发布时间、资产与大小）、当前 `stable.json` 的版本与验签结果 | 设为稳定版、回滚到某版本 |
| `/logout` | 无 | 注销 |

发码表单复用 `LicenseStore.issue_code` 的全部校验；错误信息沿用 `LicenseError` 文案。二维码上传复用 `create_contact` 与原子写入。

## 4. 发布页与 GitHub

发布页只在已签名清单之间切换，不签名、不构建：

- 读取 Release 列表和各版本的 `stable-<版本>.json`，用内置公钥验签后展示；验签失败的版本标记为不可选。
- "设为稳定版"把所选版本的 `stable-<版本>.json` 内容覆盖上传为 `stable` 标签的 `stable.json`，随后从公网读回验签比对；"回滚"是同一动作选上一版本。
- 操作前要求再次输入当前动态码，操作写审计。
- 令牌权限上限为本仓库 Release 读写；即使泄露也无法伪造能通过客户端验签的清单或通过 SHA-256 校验的包，最坏是把稳定版切到旧版本或删除 Release。

## 5. nginx 与部署

```text
location ^~ /wechatrebot-admin/ {
    client_max_body_size 1m;          # 二维码 ≤ 512 KiB
    limit_req zone=wechatrebot_admin burst=20 nodelay;
    proxy_pass http://127.0.0.1:8092/;
}
location = /wechatrebot-admin/login {
    limit_req zone=wechatrebot_admin_login burst=5 nodelay;
    proxy_pass http://127.0.0.1:8092/login;
}
```

`wechatrebot_admin` 10r/s，`wechatrebot_admin_login` 1r/s，均按真实客户端 IP。响应带 `Cache-Control: no-store`、`X-Frame-Options: DENY`、内容安全策略只允许同源。容器 Dockerfile 的 COPY 列表加入管理模块和模板；systemd 单元增加 `--publish 127.0.0.1:8092:8092`。升级通过现有 `deploy-activation-service.ps1` 入口完成，入口的内容标识计算随 Dockerfile COPY 列表自动覆盖新文件；入口验证项增加管理端口健康。

## 6. 代码结构与复用

| 位置 | 职责 |
| --- | --- |
| `services/activation_service/admin/` | `auth.py`（密码、TOTP、会话、锁定）、`app.py`（路由与渲染）、`github.py`（Release 读取与清单切换）、`templates/` |
| `services/activation_service/cli.py` | `serve --admin-port`、`admin-init`、`admin-reset`、`admin-set-github-token` |
| `services/activation_service/store.py` | 新增只读计数查询供概览使用；不改表结构 |
| `deploy/activation-service/` | Dockerfile COPY、systemd 单元端口、README 新增"管理后台"一节 |
| `deploy/nginx/wechatrebot-admin.conf`、`wechatrebot-license-limits.conf` | 新路由与限流 zone |
| `scripts/deploy_activation_service.py` | 验证项加管理端口健康 |
| `tests/test_admin_auth.py`、`test_admin_app.py`、`test_admin_github.py` | 登录矩阵、锁定、会话过期、CSRF、二维码上传、发码各模式、撤销、查询不回显、发布切换与回滚、令牌不回显 |

## 7. 验收场景

| 场景 | 预期 |
| --- | --- |
| 未执行 `admin-init` | 后台任何路径返回"未初始化"，无登录表单 |
| 错误密码或错误动态码 | 拒绝，不区分提示哪项错误；第 5 次起锁定 15 分钟并写审计 |
| 正确密码 + 动态码 | 登录成功，12 小时后或退出后会话失效 |
| 无 CSRF 令牌或令牌不匹配的写请求 | 403，不执行 |
| 上传 600 KiB 图片或 GIF | 拒绝，原二维码不变 |
| 上传合法二维码 | 客户端下一次刷新显示新图；审计有记录 |
| 发新开通码 | 页面显示一次明文码；再次查询只显示码编号和状态 |
| 重复的业务编号 | 拒绝，与 CLI 一致 |
| 设为稳定版 | `stable.json` 变为所选版本，验签通过；客户端检查更新看到该版本 |
| 令牌被撤销 | 发布页显示 GitHub 不可用，其他页面不受影响 |
| 直接访问 8092 端口 | 只在宿主机回环可达，公网不可达 |

## 8. 边界与风险

- 公网登录的安全上限由密码强度和手机验证器决定；密码要求至少 16 位。
- 后台与公网接口同进程，后台异常不能影响客户接口：管理应用的异常边界独立，所有写操作走现有事务。
- 本文不把发码从人工核实变成自动售卖，也不接入支付。
- 后台上线属于服务器升级，不要求更新客户端；GitHub 令牌只在服务器，不进入仓库和备份之外的任何地方。
