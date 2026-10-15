# 生图和撤回接口（0.9.9）

## 生图

私聊不再要求消息包含 At；群聊仍要求明确 @ 当前机器人。生图和修图处理器在同一个协程中使用 event.send() 发送进度及最终结果，避免 stop_event() 后 yield 导致调度器提前中断模型调用。

## 撤回

撤回只发送一次 POST /api/revoke_msg，不调用 /api/revoke_any，也不在失败后切换接口。HTTP 404/405、超时、服务端拒绝和不明确结果都按失败处理。

2026-10-15：核对当前固定 DLL（微信 4.1.8.27）的路由表，只注册 `/api/revoke_msg`，没有 `/api/revoke_any`；此前仅调用 `revoke_any` 会让每次撤回都打到不存在的路由而必然失败，现改回真实存在的 `revoke_msg`。字段依据维护者提供的 [Apifox 撤回任何消息](https://s.apifox.cn/1d27f9da-72ec-4981-ae95-deb20a0c1412/410194260e0.md)：发送回执中的 newMsgId、createTime、toUserName；64 位消息 ID 保持 Python 整数精度。

只有明确的 baseResponse.ret == 0 才确认成功；失败日志仅包含固定分类及数值码。历史 DLL 适配记录见 setu-recall-0.9.9.md，该记录不代表真实撤回已成功。

定向测试覆盖唯一 POST 路径、精确参数、成功结果、HTTP 404/405 不回退、超时及失败不重试。测试模拟 HOOK 响应，没有向微信发送或撤回真实消息；真实生图和撤回仍由维护者在更新后验收。

版本保持 0.9.9，采用组件增量更新：本次仅改变桥接 Python 补丁，不修改原生二进制、AstrBot 核心或依赖。代码提交不等于已经更新安装。
