# 项目优化实施档案

日期：2026-11-18。面向项目维护者，记录代码审查发现、设计、执行顺序及验证结果。审查基线为 `51b3aac`；线上版本为 `1.0.23`，本轮待发布目标统一为 `1.0.24`。本任务只实施、验证和提交代码，不构建安装包、不发布、不启动机器人。

后续记录（2026-11-19）：本文版本、授权及验证范围描述的是 11 月 18 日开发阶段。OPT-01～09 已由 PR #220 合入，随后另行授权的 [1.0.24 发布](https://github.com/c4znr7jpdy-arch/wechatrebot/releases/tag/v1.0.24)已完成。完整更新等待、独立窗口自动覆盖和旧文件清理的后续工作统一记录在[完整更新等待时间优化](full-payload-update-performance.md)，供下一待发布候选 1.0.25 使用；本文历史测试记录不作为该候选的新验证结果。

### OPT-08 后续：群聊和私聊配置重复刷新（2026-11-19）

1.0.25 发布后收到配置页持续刷新反馈。本次修复基于 `cd701fa`，属于下一候选 **1.0.26 管理器增量**；不改变核心补丁、运行时或安装基线，当前只提交修复，不打包发布。

- **原因**：15 秒目录轮询恰好跨过后端缓存期限；主导航、子标签和窗口恢复还会强制读取目录。状态探测暂时未知时，空 `history_account` 被当成换号并清除草稿；后端也把目录接口的所有 409 当作换号。后台补齐其他群人数会重建正在编辑的表单。上述是已复现的重复请求和界面刷新，尚无证据证明实际微信消息连接被反复断开。
- **方案**：复用现有 loaded 状态、账号隔离、请求代号、目录缓存和表单保留机制。取消配置目录周期轮询；首次读取、真实换号及用户手动刷新才读目录，已加载页面切换和恢复直接展示。首次失败保留有限重试。连接未知时保留列表/草稿并暂时禁用账号操作，包括已打开确认框的后续提交；确认退出、换号或授权失效仍清理旧账号数据。409 复核账号后决定是否失效缓存。后台人数只更新列表及人数，不重建规则表单，取消读取显示“待刷新”。
- **保留行为**：全局连接生命周期监控和当前群功能权限同步继续运行；新增/退出的群、联系人通过刷新按钮更新。未修改接口或配置格式。
- **变更文件**：`app.js`、`chat-management.js`、`index.html`、`group_directory.py`；新增 `tests/test_chat_refresh_webui.py`，更新目录、导航、队列和 shell 回归；本节记录设计与验收。
- **已执行验证**：目录/私聊/配置/shell/连接相关 269 项通过；刷新、恢复及 tab 保留 24 项通过；绑定、规则校验、群保存回执、私聊保存和启动页面 67 项通过（各批次存在重叠，不累加）。最终刷新/确认框/队列回归 20 项、取消人数读取 1 项、退出及授权失效清理 2 项通过。`node tests/chat_management_webui.cjs`、JavaScript 语法、修改的 Python 文件编译及 `git diff --check` 通过。浏览器使用现有 Playwright 离线预览（Browser plugin not available），验证页面 URL/标题、非空内容、无错误覆盖和控制台错误，并检查截图；未启动机器人。
- **验收边界**：本机真实微信长时间连接和已安装客户端体验待维护者验证；GitHub 完整 CI、合并及交付以本次 PR/发布记录为准，离线预览通过不代表已更新本机或线上。
- **CI 跟进**：PR #223 首轮 run `44475984374` 为 6,133 passed、3 failed、2 skipped；冻结管理器和播放器通过。失败为旧刷新次数约定、旧人数模板的静态断言，以及订阅测试抽取 `renderStatus` 后缺失新的 controller 替身。同步三个夹具，保留草稿、保存及授权断言；shell/订阅定向 222 项通过，配置切换单例通过后重新提交 CI，完整结果见 PR 检查。

## 实施顺序与状态

先修正确性和资源问题，再改善数据规模、诊断及开发效率。独立项可以并行准备和审查，按下表顺序集成；依赖项须在前项验证后实施。

| 顺序 | 编号 | 事项 | 优先级 | 状态 | 验收重点 |
| --- | --- | --- | --- | --- | --- |
| 1 | OPT-01 | 聊天历史数据库连接确定性关闭 | 高 | 代码及定向验证完成 | 成功及异常均关闭，事务语义保持 |
| 2 | OPT-02 | 会话目录分页与准确统计 | 高 | 代码及定向验证完成 | 超过 200 个会话可访问，分类/筛选/账号切换正确 |
| 3 | OPT-03 | 调度数据库操作移出事件循环 | 高 | 代码及定向验证完成 | 写锁不阻塞心跳，取消/停机无悬挂任务 |
| 4 | OPT-04 | 历史查询缓存与分批同步 | 高 | 代码及定向验证完成 | 无变化不重复聚合，积压有界，同步删除不复活 |
| 5 | OPT-05 | 并发回复链路关联 | 中 | 代码及定向验证完成 | 并发不串链路，未知耗时不伪造，不记录正文 |
| 6 | OPT-06 | 补齐插件测试入口 | 中 | 代码及定向验证完成 | 既有测试实际被收集，修正过期夹具及清理 |
| 7 | OPT-07 | CI 去重、缓存与性能回归证据 | 中 | 代码及定向验证完成 | 保留完整检查，旧运行取消，确定性预算可检查 |
| 8 | OPT-08 | 隐藏窗口停止非必要目录轮询 | 中 | 代码及定向验证完成 | 隐藏不请求，恢复刷新，生命周期监控保持 |
| 9 | OPT-09 | 更新引擎按职责渐进拆分 | 低 | 代码及定向验证完成 | 导出接口、校验、安装事务及回滚行为不变 |

## 证据与设计

### OPT-01：连接释放

`chat_history.py` 的 `_connect()` 返回裸 SQLite Connection，`with connection` 仅提交/回滚事务，不关闭连接。禁用循环 GC 的临时实验中，20 次目录读取留下 20 个连接对象。这是释放时机依赖 GC，不能表述为已证实的永久泄漏。

复用 `TaskStore._connect()` 的上下文管理模式，在 `finally` 中关闭；保留连接内事务。文件边界为 `chat_history.py` 与对应测试。验证正常提交、异常回滚、成功/失败关闭及原有历史测试。无需更改数据库格式。

### OPT-02：目录完整性

历史目录默认只返回 200 个会话，API 未传分页参数；前端先取全部类型再本地分类。合成 250 会话只返回 200 个；未进入结果但仍在群目录的群会补成 0 条消息。消息没有从数据库删除。

给目录增加明确分页信息，先按账号和类型筛选，再分页。复用现有消息分页和过期响应隔离；目录与群资料的合并必须在最终分页之前完成，防止把未加载摘要当作空记录。服务端总量独立于当前页。刷新已展开目录时保留选择与阅读位置，筛选/账号切换重置分页。保持旧调用可用，新增返回字段不删除原字段。

主要文件：`chat_history.py`、`chat_api.py`、`app_webui/message-history.js`、必要的共享状态及测试。新增 UI 控件使用现有按钮样式。验收覆盖 250+ 会话、同时间排序、群补全、搜索/日期、分页中账号切换、错误重试与真实浏览器预览；不启动机器人。

### OPT-03：异步调度

`unified_runtime.py` 在异步主循环直接调用 SQLite，`TaskStore` 等待写锁最长 10 秒。临时持锁 0.4 秒时，50 毫秒心跳实测 413 毫秒；后台线程实验约 55 毫秒。

复用现有 Store 的完整事务和 `asyncio.to_thread`。同一运行实例限制在途数据库工作，等待线程真正结束后才完成取消及释放运行租约。特别覆盖取消领取任务、创建即时测试任务、投递结果落盘和停机；禁止仅套 `to_thread` 后留下已提交但未登记的 running 记录。主要文件为调度 runtime 及相关测试，不换数据库、不加异步数据库依赖。

### OPT-04：历史数据规模

当前列表重复执行分组和总量统计，正文小页也统计发送者历史；每次读之前先同步，积压使用无上限 `fetchall()` 并持写锁逐条转换。

基线为本机合成 250 会话、150 字符左右正文，绕过同步的三次热查询中位数：1 万条约 20 毫秒、10 万条约 482 毫秒、30 万条约 4.6 秒；它不是实际客户环境 SLA。

先复用导入游标、去重键及 SQLite 修改版本建立有界查询缓存：无数据变化不重新聚合，导入/删除/外部连接写入均使缓存失效，查询键含账号、类型、搜索和日期。同步按批处理并合并并发请求，页面可读取已索引数据；导出/删除保持严格同步语义。持写锁后再读游标的删除一致性保护不得移除。仅在测量仍有必要时引入可重建摘要索引，避免无依据增加数据库迁移。

将导入、查询/缓存按职责提取到必要模块，保留 `ChatHistoryService` 接口。验收包含有界批量、并发导入、删除后不复活、外部写入失效、账号隔离、分页排序及确定性扫描/读取计数。性能报告记录数据规模和环境，不以单次时间值作硬门禁。

### OPT-05：回复链路

`ResponseTraceService.recent()` 按日志相邻位置识别回复。交错的群聊/私聊合成日志中，两条原本各 11 秒的链路被识别成私聊 6 秒和 5 秒。

优先利用现有消息/事件标识，让结构化事件按同一 trace 标识记录阶段、结果和单调时钟耗时；复用脱敏和日志保留机制，不输出正文、账号、密钥。不能可靠关联的旧日志必须显示未知或不完整，不能拼接其他请求的阶段。先检查现有 runtime/patch 接入点，保持固定 vendor 输入；核心补丁变更应明确记录未来发布需要完整载荷的边界，不擅自发布。

验收：两会话交错、同会话并发、失败/取消、跨日、缺阶段、旧日志、敏感字段不返回。具体文件随调用链调查补充，禁止为展示效果伪造阶段耗时。

### OPT-06：已有插件测试接入

根 pytest 仅收集 `tests`，Twitter 隔离入口只调 4 个文件，另 6 个 Python 文件和 2 个 JS 文件未进入 CI。90 个静态测试函数展开为 170 个用例；初次定向执行为 125 passed、17 failed、28 errors，其中缺 `bs4` 以及过期 provider 夹具需要先处理。JS 9 项通过。不能把未收集等同于实现有 Bug，也不能只接入口就宣称完成。

扩展已有隔离子进程入口，补足测试依赖，修正与真实接口不一致的夹具；真实行为差异单独核对，不为通过测试放宽正确性断言。清理后台任务、保证子进程按时退出，显示实际收集数量。复用现有 stub 隔离，不把插件测试污染到管理器进程。

### OPT-07：CI 与基准

工作流对 push/PR 均运行，无过时任务取消、依赖缓存；检查和冻结构建串行。已有 `benchmark_manager_bootstrap.py` 为离线合成启动基准，但未接入 CI。

主干 push 与 PR 各保留完整检查，取消同 PR 过时运行；依赖缓存按锁文件失效，设置合理超时。按测试隔离需求拆分快速检查、浏览器和冻结构建，不重复执行、不遗漏原检查。固定测试依赖版本；启动基准存档，以配置读取/探测次数作为确定性预算，时间先报告观察。复用已有脚本并增加历史规模基准，CI 不运行机器人。

### OPT-08：后台轮询

消息目录和群目录轮询在隐藏窗口仍发请求；其他页面已有隐藏保护，恢复可见也已有刷新入口。

复用这些保护，只暂停非必要 UI 数据读取。不得停止承担待启动恢复的 `get_status` 生命周期刷新。测试隐藏零读取、恢复一次刷新、忙碌请求不重叠。界面外观无需重做。

### OPT-09：更新模块边界

`updates.py` 基线约 1,994 行，`apply_update_plan` 约 210 行，同时承担清单验证、下载、解压、安装事务和恢复。

先提取职责稳定的清单/包验证或事务辅助逻辑，保留已有导出和测试替换点，复用 `update_cache`、`UpdateFailure`、`UpdateLogger`、原子写入和 `OwnedProcess`。不机械按行数拆分，不新增另一套规则。按更新、签名篡改、路径边界、中断、回滚、进程退出定向测试验证，安装器/真实 Windows 升级验收仍由用户执行。

## 验证、兼容与交付

- 每项记录变更文件、复用实现、实际命令和结果；未执行检查明确标注。
- 只执行相关定向测试、离线预览和轻量基准，本地不运行全量测试及机器人；完整 CI 在 GitHub 执行。
- 使用独立工作树和 `codex/` 分支，保留已有工作区修改；提交按事项分步留痕。
- 仅管理器/UI 改动属于管理器增量；插件 Python 变化可用兼容基线组件更新；若需核心补丁则单独说明完整载荷要求。本任务无发布操作。
- 数据改动须兼容已有安装，新增缓存可丢弃、可重建；旧记录和用户配置不自动清理。
- 每步失败先修复或记录具体阻碍，不能把“已有代码”“测试收集到”“已提交”“CI 通过”“已合并”“已发布”混为一谈。

## 执行记录

- 2026-11-18：建立方案及基线证据；开始 OPT-01。
- OPT-01：修改 `chat_history.py`、`tests/test_chat_history.py`，复用 TaskStore 的上下文管理方式；初始化异常也进入 finally 关闭。定向验证：`$env:PYTHONPATH='src'; python -m pytest -q tests/test_chat_history.py`，14 passed。首次未设置源码搜索路径导致收集失败，修正执行环境后通过；未安装/启动机器人。
- OPT-02：完整合并目录后按 `(timestamp, type, id)` 游标分页，新增加载更多和服务端分类，刷新保留已展开条数与选择，总消息数使用服务端汇总。变更 `chat_history.py`、`chat_api.py`、`message-history.js`，新增 `test_history_directory.py`，扩展 `test_history_webui.py`；边界测试的连接观察器适配 OPT-01 上下文接口，四个失败已修复。`PYTHONPATH=src` 下历史/目录/边界 53 passed；历史浏览器及恢复测试 42 passed（60.19 秒）；`node tests/history_webui.cjs` 通过。使用现有 Playwright 离线预览：Browser plugin not available；1380×860 截图、控制台、251 会话加载更多/分类/刷新均通过，不启动机器人。
- OPT-03：修改 `runtime/jiang_schedule/unified_runtime.py`，新增 `test_unified_schedule_store_async.py`。复用同步 TaskStore 事务，串行后台数据库操作、取消与停机收尾后释放租约；即时测试、已领取任务和发送回执均覆盖。定向调度/Store/迁移/内容测试 113 passed（12.12 秒），两文件编译及 diff 检查通过；不更改数据库/协议，无真实微信验证。
- OPT-04：新增 `history_cache.py`、`history_summary.py`、`benchmark_history_queries.py`、`test_history_performance.py`；修改历史服务、API 和补录状态文案。使用最多16项/30秒有界缓存，DB/WAL修订失效并复制返回；新消息触发增量摘要，删除/账号回填仅重建受影响会话，汇总读取使用同一事务。首次旧库一次建立可重建摘要；新增元数据表/触发器及回填游标，不修改消息内容。读请求每批500条且不排队重复补录，页面后续轮询继续补录；导出/删除等待并补齐开始时已提交源记录。历史相关111 passed、性能回归6 passed（有重叠），Node通过。30万消息/250会话合成基准：首次3.5毫秒、缓存中位1.09毫秒、消息全量聚合0次；旧基线约4.6秒，仅为本机离线数据对比。搜索/日期首次仍查询原始消息，首次摘要迁移和真实大库体验待用户验证。
- OPT-06：扩展 `test_twitter_integration.py` 为10个Python文件和2个JS文件的隔离入口；`pyproject.toml`复用运行时已锁版本补BeautifulSoup及HTTP/2测试依赖；修正provider测试夹具以覆盖微信禁合并转发和后台初始化的当前契约，保留底层发送失败/原子落盘断言。12个入口通过（22.65秒），插件Python收集234项，JS 9项，不改产品行为。
- OPT-07：CI仅主干push/PR触发、取消同PR旧运行，manager/player/frozen-manager并行且保留`test`汇总门禁；按依赖输入缓存pip、明确超时，固定测试依赖版本。启动及历史基准JSON存档，约束读取/探测次数和消息聚合次数；时间只报告。修改工作流、CONTRIBUTING、pyproject、build_manager和启动基准，新增预算夹具及测试。13项定向通过，预算拒绝及汇总success/failure/cancelled/skipped验证通过，未执行正式构建。
- OPT-08：消息/群目录增加隐藏窗口保护，复用恢复可见刷新，不变更生命周期监控。修改app.js/message-history.js及Node/浏览器回归；隐藏零读取、恢复读取的离线浏览器测试1 passed，Node通过。
- 独立复查：摘要200步增删改、跨账号/会话共600次对照原始SQL全部一致，查询计划确认按dirty会话索引重建；补齐旧库迁移在目标连接创建失败时关闭源连接。
- OPT-05：新增随机事件trace结构化写入及严格投影，修改核心/发送权限补丁并新增Angel决策补丁；沿用现有日志轮转，实际provider/action await记录结果，流式只计provider等待，旧日志未知耗时。新增writer、2个runtime/patch回归文件，更新原trace测试及诊断说明。相关回归97 passed，追加决策覆盖后重跑38 passed（有重叠），固定vendor哈希通过。**本项改变核心BASE_INPUTS，整体1.0.24未来部署需要完整运行时基线，不能对旧基线发布组件包；本任务没有构建或发布授权。** 其余独立管理器/插件改动仍可按对应基线选择增量；无兼容全包自动回退。
- OPT-09：新增 `update_manifest.py`，从updates移动13个纯定义并显式重导出；56个原有类/函数AST对照不变，签名先验和安装基线替换点保持。下载、事务、进程及回滚流程未改；README更新边界，新增类型/替换点回归。定向99/198/84项分批通过（存在重叠，不累加），发布元数据通过。
- 整合复查：修复忙碌追加请求提前递增请求代号导致loading卡住；保留各分类已加载目录和第二页选择，统一分类统计口径；严格分批同步中源消失拒绝删除/导出。新增相应Node、浏览器、SQLite回归；性能/源消失8项与Node通过。版本元数据统一准备为1.0.24，签名/实机验收仍保持pending。
- 整合定向验证：历史浏览器/恢复43 passed（60.43秒）；trace/组件清单/启动预算67 passed；元数据校验通过；启动预算满足engine_config=4、plugin_config=40、lifecycle_refresh=1。所有测试命令在独立工作树执行，Python源码路径设为src。PR的CI与合并状态以GitHub为准，代码/本地验证状态不代表已经上线。

- CI首轮发现SBOM在Windows写入CRLF后被Git规范化为LF导致登记哈希失配；已改用固定LF计算，工作树/提交字节哈希一致，元数据复检通过。PR：https://github.com/c4znr7jpdy-arch/wechatrebot/pull/220；完整CI与合并结果由该PR留痕。
- trace最终复查修复：日志路径每次读取解析，支持管理器先于运行日志启动；底层provider流确定性关闭；正常最终响应在yield前结束模型计时，fallback/step提前退出也关闭迭代器。使用真实runner方法和真实tenacity（test extras固定运行时9.1.4）覆盖正常/错误回退/关闭/取消/停止，最终6个定向文件97 passed。合并依赖变更时保留已有pytest/Jinja2/pytest-asyncio精确版本。
- CI整合回归（run 44254030189）：player和frozen-manager通过；manager为6050 passed、1 failed、2 skipped，唯一失败为授权服务最小镜像未复制OPT-09新提取的模块。已在 `deploy/activation-service/Dockerfile` 补齐 `update_manifest.py`，复用已有最小镜像导入测试验证，`python -m pytest -q tests/test_admin_imports.py`：1 passed（1.12秒）；完整CI重新运行，最终状态以PR检查为准。

## 已运行的主要定向命令

PowerShell执行前设置 `$env:PYTHONPATH=(Resolve-Path src).Path`。下面命令在本任务独立工作树或子任务独立工作树运行；插件/更新批次使用子任务 `.venv/Scripts/python.exe`。各批次有重复，不累加唯一测试总数。

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 历史正确性 | `python -m pytest -q tests/test_chat_history.py tests/test_history_boundaries.py tests/test_history_directory.py` | 53通过 |
| 历史性能与源中断 | `python -m pytest -q tests/test_history_performance.py` | 8通过 |
| 历史浏览器 | `python -m pytest -q tests/test_history_webui.py tests/test_history_resume_webui.py` | 最终43通过 |
| JS历史状态 | `node tests/history_webui.cjs` | 通过 |
| 调度 | `python -B -m pytest -q tests/test_unified_schedule_store_async.py tests/test_unified_schedule_runtime.py tests/test_task_store.py tests/test_scheduled_delivery_policy.py` | 113通过 |
| 插件隔离入口 | `python -B -m pytest -q -ra --tb=short -p no:cacheprovider tests/test_twitter_integration.py` | 12通过 |
| 插件收集 | `python -B -m pytest --collect-only -q -p no:cacheprovider runtime/astrbot_plugin_twitter/tests` | 234项 |
| 更新事务与兼容 | `python -B -m pytest -q --tb=short -p no:cacheprovider tests/test_updates.py tests/test_update_cache.py tests/test_update_restart_processes.py tests/test_unchanged_update_targets.py tests/test_component_update_manifest.py` | 198通过 |
| 发行组装单测 | `python -B -m pytest -q --tb=short -p no:cacheprovider tests/test_update_release_builder.py tests/test_component_release_builder.py tests/test_update_message.py tests/test_publish_manager_update.py tests/test_local_incremental_update.py` | 84通过；只测试，不发布 |
| trace/清单/预算整合 | `python -m pytest -q tests/test_response_trace.py tests/test_reply_trace_runtime.py tests/test_component_update_manifest.py tests/test_bootstrap_benchmark.py` | 67通过（流式收尾追加测试另记） |
| 历史基准 | `python scripts/benchmark_history_queries.py --rows 300000` | 3.5ms首次/1.09ms缓存、聚合0次 |
| 启动预算 | `python scripts/benchmark_manager_bootstrap.py --budget tests/fixtures/manager_bootstrap_budget.json` | 预算通过 |
| 元数据 | `python scripts/validate_release_metadata.py` | ok=true |
| 格式 | `git diff --check origin/master` | 通过 |

以上开发阶段没有执行本地全量测试、真实微信、正式安装器/载荷构建、发布或安装。GitHub CI包含冻结管理器自测，不等于已生成并发布客户安装包；后续交付状态见文首记录。
