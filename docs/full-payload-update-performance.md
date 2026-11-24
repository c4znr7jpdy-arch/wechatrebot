# 完整更新等待时间优化

## 2026-11-24：复用原生安装窗口与减少重复 I/O

下一待发布版本为 1.0.30，基于线上 1.0.29；本次只变更管理器及其构建，不执行正式打包或发布。以下更新取代本文早期记录中的生产 Tk 窗口方案：

- `installer/WeChatReBot.iss` 增加只显示进度的 `UpdateUI` 编译模式，共用原生安装器的样式、语言和品牌资源，随管理器一起嵌入分发。
- `native_update_progress.py` 通过随机任务标识和可见回执确认窗口已就绪，之后才进入既有 ready/commit/committed 交接。UI 不拥有文件事务，退出、错误提示或进度文件写入失败均不能启动第二次安装；窗口启动失败则不进入事务。主更新进程异常退出时，窗口明确显示“尚未确认完成”。
- 完整包、管理器包和组件增量共用可见交接；组件增量继续使用原有文件级安装与精确基线校验，不转成完整包。
- 备份准备、路径检查、安装前复核均报告文件数，界面显示阶段百分比、阶段耗时和总耗时。进度文件每 250 ms 最多更新一次，阶段切换和最终结果立即写入。
- 更新下载日志对同一阶段和百分比去重，36,159 个文件的单次校验只写 11 条进度记录。回滚准备复用当前事务已经校验的清单；目标路径以一次 `lstat` 同时识别符号链接与目录联接，仍逐级检查边界，并保留源文件、目标文件的内容校验和漂移检测。

本机临时夹具对 1,800 个文件重复测量三次，目标路径检查中位耗时由 2.1519 秒降至 1.3777 秒（减少 36.0%）。这只是路径检查阶段的合成对比，不是完整升级提速比例。真实微信、实际完整升级和安全软件影响仍需用户验收。旧管理器发起的首次升级仍使用它已有的更新器，升级到新管理器后才使用内嵌 Inno 窗口。

原生窗口的可见交接、失败提示、完成自动关闭，以及更新中断恢复和配置保留通过临时夹具验证；不启动机器人。自动验证通过定向 pytest 和 Inno 编译完成。

本次本地回归执行 `python -m pytest -q`，范围为 `test_native_update_progress`、`test_build_manager`、`test_build_native_installer`、`test_self_test`、`test_unchanged_update_targets`、`test_update_move_rollback`、`test_automatic_update`、`test_update_progress`、`test_payload`、`test_updates`、`test_app_shell`、`test_update_close_recovery`、`test_update_restart_processes`、`test_installer`、`test_native_setup`、`test_local_incremental_update`、`test_component_update_manifest`：582 passed。随后补充冻结资源缺失检查和真实 Inno 夹具，后者连续三次完成交接、临时文件更新、启动回执及自动关闭，且断言未强制结束窗口：3 passed in 5.21s。Inno 两种编译模式、`python scripts/validate_release_metadata.py`、改动模块 `compileall` 和 `git diff --check` 通过。以上不替代发布、真实完整升级或安全软件验收。

## 早期实现记录

2026-11-19。基线为 `master a59fe2f`，线上版本为 1.0.24；本次开发供下一待发布候选使用（当前为 1.0.25）。修改管理器内的安装/更新代码，可通过管理器增量交付，不涉及 Python、依赖锁或原生文件重建。本记录不代表已打包、安装或上线。

## 目标与实施顺序

用户认可后台准备、独立安装窗口、缩短替换并保留数据、成功后清理旧文件这四点。完整载荷升级仅确认一次，后续自动覆盖原目录；独立窗口只显示进度，无需“下一步”、选择目录或点击“完成”。

| 优先级 | 工作 | 当前状态与验收边界 |
| --- | --- | --- |
| P0 | 合并重复备份，大量旧文件用同盘移动保留 | 本次实现；定向验证单一回滚责任、中断恢复、文件占用、数据漂移及旧客户端兼容 |
| P0 | 新版窗口确认成功后清理旧文件 | 复用已有启动确认与清理提交顺序；新增测试检查确认前旧文件仍在，既有测试检查清理失败不能回滚 |
| P1 | 关闭前完成同盘准备，缩短替换阶段 | 已实现；后台解包的完整载荷直接用于替换，不再复制到第二套安装暂存；替换前后复核内容、路径和准备期间变动 |
| P2 | 完整更新交给可见的独立安装程序 | 已实现；独立更新 EXE 接管缓存载荷和原目录，显示真实阶段，自动覆盖并打开新版，共用现有恢复事务 |
| P3 | 自动化验收与实机耗时 | 自动化已覆盖真实 Tk 窗口连续三次无点击完成、缓存重用、配置保留和故障恢复；实际完整运行时、真实微信/安全软件及耗时比例仍待实机验收 |

## 为什么先改公共事务

现有 Inno 安装器通过 `native_setup.py` → `installer.py` → `payload.py::install_payload` 安装；自动更新也调用 `install_payload`。仅替换安装窗口不会消除底层复制。

完整载荷自动覆盖使用已安装管理器复制出来的独立更新 EXE 作为安装入口，窗口由 `update_progress.py` 提供。它直接共用安装事务和已校验缓存，避免新增安装资产、重复下载以及另一套配置/回滚规则。Inno 继续承担首次安装，自动更新没有向导页或人工安装按钮。

## 自动覆盖与窗口交接

正常流程：后台下载、解包、校验 → 用户确认一次 → 独立进度窗口显示 → 校验计划并取得更新锁 → 通知旧管理器已就绪 → 停止机器人及本次连接的微信 → 发送安装许可并等待接收回执 → 关闭旧管理器 → 自动覆盖 → 打开新版管理器 → 确认窗口可用 → 清理旧文件并自动关闭安装窗口。

`update_handoff.py` 的 ready/commit/committed/cancel 信号使用一次性事务编号绑定。窗口不可见、helper 提前退出、停止失败或许可没有回执时，不安排关闭旧管理器；取消信号在等待旧进程退出期间也有效。取消后确认 helper 及其文件持有者退出才允许再次安装，避免重叠事务。等待旧进程退出失败时不额外启动重复管理器。

独立窗口主线程运行事件循环，安装工作线程只上报阶段和文件数。总量未知时显示活动进度；提交期间关闭按钮和 Esc 不终止文件事务。成功自动关闭，无“完成”按钮；失败展示实际原因及已完成的恢复结果，允许关闭，不重新进入安装向导。打开管理器不自动启动机器人。

## 后台暂存直接用于覆盖

`prepare_update` 已在旧窗口可用时把包展开到安装目录的 `updates/` 中。自动完整更新以该目录作为待提交的新文件集，`install_payload(consume_prepared=True)` 继续验证全部内容、路径、现有文件及配置保留规则，随后用同盘移动逐个替换；不再为整个载荷创建第二份暂存副本。

该模式只允许完整载荷、外层持久事务和安装目录更新区内的非链接目录；普通安装、组件包、ZIP 直接安装以及旧计划保持原契约。源文件在替换前及落盘后重新核对摘要，准备后被修改会触发恢复。

开始消费前保存 `payload-consumed.json`。部分消费或失败后的目录不能再作为完整候选重用；重试从保留的原 ZIP 缓存重新解包和校验。安装后的入口刷新复用内存清单，不再扫描已经被移动的来源。

自动更新原先有两份旧文件副本：`updates.py` 为中断/启动失败保存一份，`install_payload` 为调用期间异常再保存一份。新实现由 `update_rollback.py` 统一负责持久回滚，通过 `before_replace(target, incoming)` 接入底层安装器，取消内部第二份备份。

## 文件替换与恢复规则

1. 验证载荷、基线、路径及占用；按实际摘要跳过未变化文件，并继续保留客户已有配置。
2. 记录每个变化目标是否存在、旧 SHA-256、回滚路径及复制/移动方式，先原子保存 journal，再修改目标。
3. 除下面五类恢复入口/控制文件外，旧程序文件使用同盘 `os.replace` 移到当前事务目录，随后将已校验的新暂存文件替换到目标。跨盘或锁失败直接结束并恢复，不降级为复制后删除。
4. `manager/WeChatReBot.exe`、`启动机器人.exe`、`卸载机器人.exe`、安装标记、`config/installed-payload.json` 保留一份复制快照。它们始终存在到原子替换，避免中断后找不到管理器或无法识别安装。这些文件不再被内部事务重复备份。
5. 新建文件先保存独立创建记录，绑定相对路径和即将安装的摘要。回滚只有在文件内容仍匹配时才删除；内容改变、变成目录/链接或记录缺失均保留并报告恢复失败。安装清单先写入暂存文件，保证记录对应实际写入字节。
6. 移动备份存在时核对旧摘要再恢复；备份不存在时，仅当目标匹配旧摘要，才判定尚未触碰或此前已恢复。恢复可以重试。旧 schema 1 仍要求其复制备份存在。
7. 安装完成后保留旧文件，等待新版窗口发出当前事务的启动成功确认。确认后提交结果、移除 journal、清理当前事务。清理失败不撤销成功结果；已有恢复入口可继续清理。

`app/astrbot/data` 混有客户配置和业务数据，不能整体覆盖或改名。本次仍按既有受管文件清单逐文件处理，不删除客户配置、未知文件或非空目录。源文件哈希、路径边界、未变文件漂移及更新锁继续有效。

独立原生安装没有外部 journal 时，`install_payload` 使用自己的异常回滚清单，也对一般程序文件采用移动。该入口仍没有自动更新的持久中断恢复及窗口确认，不把此能力当作已完成的 Inno 交接。

## 旧客户端兼容

新管理器生成计划时声明 `rollback_journal_schema: 2`。新 helper 仅在字段为整数 2 时启用移动备份。旧 1.0.24 管理器及现有本地增量脚本没有该字段，采用 schema 1 的单层复制；旧管理器即使在升级中断后重新启动，也仍能读懂恢复记录。

因此首次从旧管理器升级先消除一层重复复制；支持新恢复协议的管理器发起后续更新才获得移动备份收益。不能用组件版本或新 helper 版本推断旧管理器能力。旧 helper 未包含本次实现时仍按其原有流程运行。

新管理器的完整更新计划同时声明 `automatic_install` 和 `consume_prepared_payload`，并复制当前管理器作为支持该协议的独立助手。只有明确声明的完整更新使用窗口交接和暂存消费，旧格式计划及组件更新仍可安装。可先以管理器增量部署这些能力，再在后续完整更新中使用，无需为升级流程本身重建运行时。

## 空间与耗时证据

移动模式的预检保留下载、解包、新文件暂存、五类复制快照和 64 MiB 余量；不再额外预留两份全部旧程序文件。旧文件仍占用原有空间，成功提交后才释放。旧客户端首次升级继续采用它原有的保守空间预检；新 helper 对旧格式计划在复制前还会重新核对单份完整备份与暂存空间。

`logs/update.log` 的 `rollback_prepared` 记录准备秒数、复制/移动/未变化文件数；`install_progress` 记录阶段经过时间。十个百分点只记录一次，避免大量文件在同一百分比反复写日志。阶段首条回调作为计时起点，不能将其当作完整端到端耗时。

定向测试覆盖复制字节来源、替换前中断、重复恢复、缺失/损坏备份、新文件漂移、锁失败、跨盘失败、删除项、旧计划兼容、源校验、配置保留和启动失败恢复。测试使用临时夹具与进程替身，不启动机器人。

本次定向命令（`PYTHONPATH=src`）为 `py -3.13 -m pytest -q tests/test_update_move_rollback.py tests/test_payload.py tests/test_updates.py tests/test_unchanged_update_targets.py tests/test_native_setup.py tests/test_update_restart_processes.py tests/test_windows_file_holders.py tests/test_installer.py tests/test_local_incremental_update.py tests/test_component_update_manifest.py`，结果 **345 passed**。改动模块的 `compileall` 与 `git diff --check` 通过；完整 CI 以 PR 检查为准。

剩余实现的自动化验证覆盖：窗口绘制后交接、许可回执、启动前取消、旧进程等待取消、关闭失败、helper 子进程仍存活、直接替换不重复复制、源文件漂移、失败恢复、缓存重建不联网，以及真实 Tk 窗口连续三次自动完成。窗口测试运行在独立子进程，符合冻结更新器的 Tcl 生命周期，不启动机器人。

自动覆盖集成验证命令（`PYTHONPATH=src`）：

```powershell
py -3.13 -m pytest -q tests/test_automatic_update.py tests/test_update_progress.py tests/test_updates.py tests/test_update_move_rollback.py tests/test_payload.py tests/test_update_close_recovery.py tests/test_update_restart_processes.py tests/test_app_shell.py tests/test_unchanged_update_targets.py tests/test_update_cache.py tests/test_local_incremental_update.py tests/test_native_setup.py tests/test_admin_imports.py tests/test_desktop.py tests/test_cli_output.py --tb=short
```

结果 **540 passed in 37.54s**。最后补充“取消信号在末次系统进程等待内到达”的回归后，重跑 `tests/test_automatic_update.py tests/test_update_progress.py`，结果 **30 passed in 9.78s**；改动模块 `compileall` 通过。完整 CI 以 PR #221 最新提交的检查为准，前一次提交的通过结果不替代新增实现的检查。

真实完整载荷升级、干净 Windows、真实微信/安全软件文件占用及具体提速比例尚未实测。当前是代码与临时夹具验收，不代表本机安装已更新或已上线。
