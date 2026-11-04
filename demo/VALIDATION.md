# 演示版实施与验收记录

> **仅用于临时演示。** 本文件记录的是 `demo/` 独立演示服务的实现与验证结果，不构成正式项目的实现证据、验收依据或性能事实。正式测试数字、正式数据库与正式运行产物未参与本记录。

- 记录日期：2026-11-03
- 运行环境：Windows 11 Pro 26200、Python 3.13（`py -3.13`）、Chrome（chrome-devtools MCP 驱动）
- 数据位置：`demo/runtime/`（SQLite、上传副本、导出文件、截图）；清理该目录即可空白重来
- 服务地址：`http://127.0.0.1:8765/`（仅回环）

## 1. 本轮实际执行的命令与结果

| 命令 | 实际结果 |
|---|---|
| `py -3.13 -B -m unittest discover -s demo/tests -t . -v` | 217 个测试全部通过，退出码 0（含 `test_review_regressions` 的 25 项复查回归） |
| `node demo/tests/frontend_probe.mjs` | 30/30 个前端回调断言通过（最小 DOM 替身，不替代真实浏览器验收） |
| `py -3.13 -B -m unittest demo.tests.test_end_to_end demo.tests.test_isolation -v` | 12 个测试通过 |
| `py -3.13 -B -m unittest demo.tests.test_web_flow -v` | 8 个测试通过 |
| `py -3.13 -B demo/server.py --port 8765` | 启动成功；`GET /api/demo/v1/bootstrap` 返回 200 |
| `cmd /c demo\start-demo.cmd --port 8766` | 脚本执行成功并监听 8766，`bootstrap` 返回 200；随后手动结束进程 |
| `curl "http://127.0.0.1:8765/api/demo/v1/files/{file_id}?…"` | 200、`text/csv`、282695 字节；SHA-256 与页面摘要哈希一致 |
| 浏览器：Chrome，`chrome-devtools` MCP | 六页均可打开；网络记录只出现 `127.0.0.1` 与 `data:`；无未处理控制台错误 |

未运行项集中列在第 5 节，均标注 `not_run`，不冒充通过。

## 2. 验收矩阵（V01–V21）

A = 使命召唤：现代战争 II + RTX 4060；B = 极限竞速：地平线 6 + RTX 5090。
“浏览器”指本轮实际点击/观察；“测试”指 `demo/tests` 中可复现的自动化断言。截图位于 `demo/runtime/shots/`（运行产物，不纳入版本控制）。

| 编号 | 本轮证据 | 结果 |
|---|---|---|
| V01 断网启动并打开六页 | 浏览器打开六页；网络记录 26 条全部为 `127.0.0.1`（另有 1 条 `data:` 图标）；顶栏与页脚演示标识常驻；测试 `test_isolation`、`test_http.test_index_has_no_external_resource_references` | 通过 |
| V02 两案例来回切换 | A：epoch 4、14 项 `mw2.*` 设置、A0/A1/A2、2 份摘要；B：epoch 1、14 项 `fh6.*` 设置、A0/A1/A2、2 份摘要；互不引用；测试 `test_end_to_end.test_each_case_keeps_its_own_settings_and_results` | 通过（A/B 均在本轮浏览器实际切换） |
| V03 用 A 的配置或摘要操作 B | 测试 `test_end_to_end.test_cross_case_references_are_rejected`、`test_store.test_cross_case_reference_is_rejected_without_partial_write`、`test_comparison.test_cross_case_summary_is_rejected` | 通过（测试）；浏览器未专门操作 → 该操作 `not_run` |
| V04 缺字段回答、跳过、更正 | 测试 `test_device`（自定义背景、恢复预设、重复询问）、`test_information`（回答、跳过不重复问、修订冲突、更正生成新修订） | 通过（测试）；浏览器未逐项操作 |
| V05 采集、取消、刷新、继续、重启 | 测试 `test_tasks`（取消、继续、等待输入、重启标记中断、旧 epoch 不发布）、`test_knowledge.test_short_budget_leaves_a_partial_snapshot`；浏览器：采集进行中刷新后任务继续完成 | 通过（取消/继续/重启为测试证据） |
| V06 同包导入两次与错误案例包 | 测试 `test_knowledge_import`（同包复用、跨案例拒绝、格式错误拒绝） | 通过（测试）；浏览器未操作上传导入 |
| V07 推荐与替代项 | 浏览器：A/B 各 14 项设置表，目标 60 fps、参考 58 fps（模拟）、本机结果为空；本轮点击“查看 2”打开证据抽屉，显示演示原名、依赖条件与两条 `demo://` 本地片段；测试 `test_recommendation`（逐项理由/条件/原名、变体不改模板、复核留旧快照） | 通过 |
| V08 基线、采用、手改与回滚 | 浏览器 A/B 均完成 A0→A1→A2（A：revision 13；B：revision 13）；测试 `test_configuration`（父链、回滚追加、过时指针拒绝） | 通过（回滚为测试证据） |
| V09 补录过去配置与补绑历史段 | 测试 `test_configuration.test_history_record_does_not_move_the_current_pointer`、`test_log_import.test_historical_association_needs_an_explicit_time_and_basis` | 通过（测试）；浏览器未操作 |
| V10 采集、提前停止、切段、开放段分析 | 浏览器：两案例各完成两次模拟采集并分析；测试 `test_capture.test_stopping_early_uses_a_short_sample`、`test_sessions.test_open_segment_cannot_be_analyzed`、`test_capture.test_capture_task_has_no_real_process_leases` | 通过；浏览器未点“提前停止” → 该操作 `not_run` |
| V11 上传有效/错误/超大 CSV | 浏览器：上传两行错误列 CSV，预览返回 `unsupported_format`，提示只支持 `demo-frame-csv/1` 七列格式，未创建会话或摘要；测试 `test_files`（大小、哈希、介质类型、路径文件名）、`test_log_import`（预览、关联、坏列、跨案例）、`test_http.test_oversized_json_body_is_rejected` | 通过（错误格式已浏览器验证；有效 CSV 生成摘要为测试证据） |
| V12 图表、指标、CSV、报告一致 | 浏览器：曲线 `data-source-hash` 与摘要原件哈希一致（A 首轮 `37bc1a2a63b3…`）；在 adjusted 摘要上点击“下载演示 CSV”，随后经同一 `GET /files/{id}` 端点核对：200、`text/csv`、282695 字节、3601 行（表头 + 3600 帧）、SHA-256 `77404b07…` 与页面哈希一致；测试 `test_analysis`（同一原件、指标在样例内、短样例尾部未知、篡改拒绝） | 通过（下载端点经 curl 核对；浏览器保存到磁盘的位置未核验） |
| V13 比较协议与事后比较 | 浏览器：A/B 均在第二份样例前冻结 50 ms，比较标记“同口径可比较”；A 最慢 1% 36.956→49.389 fps，P95 17.301→17.299 ms；B 36.067→48.985 fps；测试 `test_comparison`（事后探索性、派生阈值重算、未知配置降级） | 通过 |
| V14 反馈分支 | 浏览器：A/B 均提交“卡顿 + 保护纹理”，变更集只含阴影/反射质量一项；测试 `test_feedback` 覆盖满意保持、缺数据补测、保护目标走替代项、未知反馈、连续两轮无改善停止 | 通过（其余分支为测试证据） |
| V15 接受或拒绝 | 浏览器：A/B 均接受；测试 `test_web_flow.test_rejecting_the_visual_result_keeps_the_comparison_and_adds_rollback`、`test_feedback.test_outcome_cannot_be_told_that_it_improved` | 通过（拒绝为测试证据） |
| V16 双击/丢响应、过时指针 | 测试 `test_web_flow.test_same_request_twice_over_http_creates_one_result`、`..._different_content_conflicts_over_http`、`test_store.test_retry_replays_before_stale_workspace_revision`、`test_configuration.test_stale_current_pointer_is_rejected_without_a_new_version` | 通过（测试）；浏览器未模拟双击 |
| V17 任务进行时重置并刷新旧页 | 测试 `test_end_to_end.test_reset_with_an_open_capture_invalidates_the_old_epoch`、`test_tasks.test_old_epoch_task_cannot_publish_after_reset`、`test_controls.test_reset_changes_epoch_without_touching_other_case`；浏览器：重置当前案例后 epoch 4→…，旧草稿清理 | 通过（浏览器重置时无在途任务，竞态为测试证据） |
| V18 加载、导出、备份恢复 | 浏览器：seed 到“复测结果”后历史显示 A0/A1/A2 与 2 份摘要，导出 Markdown 文件首行为“演示报告，非真实实测”；测试 `test_seed`、`test_exports`、`test_backup`（往返、跨案例拒绝、篡改拒绝） | 通过；备份恢复的浏览器操作为 `not_run`（测试覆盖） |
| V19 自定义 GPU/分辨率后继续 | 测试 `test_device.test_custom_gpu_keeps_sample_unverified`、`test_recommendation.test_custom_background_lowers_readiness` | 通过（测试）；浏览器未改背景 |
| V20 两种讲解尺寸、窄屏与键盘 | 浏览器：1366×768（客户端约 1352 px）、窗口 1920×1080（客户端实测 1710×934）、窄屏（客户端 501 px）六页均无横向溢出；Tab 依次到案例下拉、演示控制、导航；Enter 触发导航；重置确认框默认焦点在“取消”，Esc 取消且状态不变；截图 8 张 | 通过；精确 1920×1080 视口未达成（窗口受限），见第 5 节 |
| V21 原项目与目录依赖 | 测试 `test_isolation`：全部 `demo/**.py`（当前 53 个）不导入 `game_app`/`game_collector`；静态资源无外部 URL；完整故事后 `fixtures/` 与 `runs/`、`game_app/`、`game_collector/`、`tests/` 顶层状态不变；阻断 socket 后完整故事仍通过。另：`test_http.test_path_traversal_cannot_read_outside_static` 与 7 条手工探测路径均返回 404 | 通过 |

## 3. 能力真实性对照

| 能力 | 真实发生 | 模拟发生 | 未发生 |
|---|---|---|---|
| 本地 HTTP 服务与页面 | 是：回环端口、静态资源、JSON 命令 | — | — |
| 状态持久化 | 是：SQLite 于 `demo/runtime/demo.sqlite3` | — | — |
| 上传、导出、备份文件 | 是：仅写入 `demo/runtime/uploads` | 备份内容来自演示对象 | 不读取任意路径 |
| 阶段进度与等待 | 是：真实计时器推进 | 阶段时长是脚本设定 | 不调用采集工具 |
| 来源发现与证据 | 演示数据 | 是：`demo://` 固定样例 | 不联网搜索、不抓取网页 |
| 设备识别与补充 | 玩家声明与演示预设 | 是 | 不探测 CPU/内存/驱动/进程 |
| 帧时间样例与指标 | 指标由本地确定性样例真实计算 | 是：CSV 轨迹由固定配方生成 | 不采集真实游戏帧率 |
| 前后比较 | 同一阈值重算真实执行 | 数据基础是模拟样例 | 不建立因果结论 |
| 游戏设置变更 | 仅记录玩家确认 | 是 | 不启动或修改游戏 |

## 4. 本轮发现并修复的问题

1. `demo/start-demo.cmd` 原为 LF 换行，cmd 解析后 `%DEMO_DIR%` 为空，脚本启动失败（实际报错找不到 `E:\code\game\server.py`）。已改写为 CRLF、纯 ASCII 提示，并加入 `py -3.13` → `py -3` → `python` 回退；实测 8766 端口返回 200。
2. 历史页在 `demo.seed` 之后只显示当前指针（1 个版本、无摘要），因为种子流程不产生逐条采用事件。已新增只读 `GET /api/demo/v1/entities?kind=…`（不推进 revision），历史页回退到按类列举并按父链深度排序；浏览器确认 3 个版本按 A0→A1→A2 排列。
3. 表单 `<label>` 未与控件关联（控制台无障碍提示）。渲染后统一补齐 `for`/`id`，当前页控制台无此类提示。
4. 窄屏（约 500 px）下概览与推荐的 `dd` 长 ID 造成横向溢出。已补 `min-width: 0` 与 `overflow-wrap: anywhere`，六页在窄屏下 `scrollWidth == clientWidth`。
5. 收尾安全复核后加固下载与静态资源读取（不影响功能）：文件响应的 `Content-Type` 只取 `ALLOWED_MEDIA_TYPES`，未知值降级为 `application/octet-stream`；`Content-Disposition` 的文件名经 `_header_text()` 去除引号、反斜杠与控制字符并截断到 120 字符；静态资源改为在解析后的 `static_root` 上用 `Path.is_relative_to()` 判定（与原 `relative_to` 等价，更直观）。新增 `test_http.test_download_header_values_are_sanitized`，并实测 `/%2e%2e/server.py`、`/..%2F..%2Fserver.py`、`/..%5Cserver.py`、`/C:/Windows/win.ini` 等 7 条路径均返回 404。
6. 前端组件 `h()` 支持过未使用的 `html` 键（`innerHTML` 通道），已移除；页面全部文本仍走 `textContent` 或文本节点。复核后六页无脚本错误。

## 5. 未运行项与剩余限制（not_run）

- `not_run`：真人双击 `start-demo.cmd`。本轮以 `cmd /c demo\start-demo.cmd --port 8766` 验证脚本内容与启动路径，未模拟资源管理器双击。
- `not_run`：精确 1920×1080 CSS 视口。窗口设为 1920×1080 后页面客户端实测 1710×934（16:9），未达到 1920×1080 像素；布局在该尺寸下无溢出。
- `not_run`：浏览器内的有效 CSV 上传并生成摘要、知识包导入、补录过去配置、提前停止采集、备份恢复、回滚、拒绝接受、双击重试。以上均有服务端/集成测试覆盖，未在浏览器逐项点击（错误格式 CSV 已在浏览器验证）。
- `not_run`：Firefox/Safari/移动端真机；仅验证 Chrome。
- 固定样例：两案例的帧时间分布刻意把差异放在 1% 低点与长帧簇，平均帧率仅相差约 0.2–0.3 fps；不能据此推断真实游戏表现。
- 服务为单机演示：无鉴权、无并发用户设计；`demo/runtime` 之外不写文件，重置仅清理登记过的对象与上传副本。
- 页面恢复依赖同一浏览器的本机草稿（`localStorage`）。换浏览器或清空站点数据后，历史/摘要仍可从服务读取，但“当前选中的摘要/推荐”需要重新生成或使用一键加载。

## 6. 任务开发记录（T01–T20）

| 任务 | 主要内容 | 验证 | 状态 |
|---|---|---|---|
| T01 | 目录隔离、README/AGENTS、启动边界 | 人工核对 | 完成 |
| T02 | 契约、SQLite 存储、命令重放、epoch/revision | `test_store`、`test_service` | 完成 |
| T03 | 受管理文件、上传限制、路径穿越防护 | `test_files`、`test_http` | 完成 |
| T04 | HTTP 服务、静态资源、草稿与前端骨架 | `test_http`、`test_web_flow` | 完成 |
| T05 | 任务阶段、取消/继续/推进、失败剧本 | `test_tasks`、`test_controls` | 完成 |
| T06 | 背景快照、模拟设备识别、补充问答 | `test_device`、`test_information` | 完成 |
| T07 | 知识采集六阶段、缺口、导入 | `test_knowledge`、`test_knowledge_import` | 完成 |
| T08 | 推荐、变体、复核、准备状态 | `test_recommendation` | 完成 |
| T09 | 配置采用、编辑、回滚、补录 | `test_configuration` | 完成 |
| T10 | 计划、会话、区段、绑定修订 | `test_sessions` | 完成 |
| T11 | 指标口径、长帧、阈值、确定性轨迹 | `test_metrics`、`test_traces` | 完成 |
| T12 | 模拟采集、上传预览/关联、摘要与曲线 | `test_capture`、`test_log_import`、`test_analysis` | 完成 |
| T13 | 比较协议冻结与前后比较 | `test_comparison` | 完成 |
| T14 | 反馈、偏好、目标、诊断、结果接受 | `test_feedback` | 完成 |
| T15 | 导出、备份、恢复、seed | `test_exports`、`test_backup`、`test_seed` | 完成 |
| T16–T19 | 六页界面、控制台、图表、历史/反馈/报告交互 | 浏览器（本轮）与 `test_web_flow` | 完成；个别交互见第 5 节 |
| T20 | 双案例端到端、隔离、离线、启动与文档 | `test_end_to_end`、`test_isolation`、全量 217 测试、浏览器 | 完成；not_run 已披露 |

## 7. 最终自检

- [x] T01–T20 的成果与行为验证已完成，A/B 两案例分别走通（浏览器 + 测试）。
- [x] 全量自动测试实际退出码 0（217 tests）。本轮为 `demo/CODE_REVIEW.md` 的修复复验，新增 25 项后端回归；前端用 `frontend_probe.mjs` 执行模块回调，未重新执行完整真实浏览器与分辨率验收。
- [x] 4060 主线呈现 A0→A1→S1→P1→C1→A2→S2→比较→接受；5090 使用独立内容。
- [x] 离线可用：无外网请求；刷新、重启、重置、导出可演示；原件、曲线与导出一致。
- [x] 六页无假按钮；样例、声明、模拟数据边界清楚。
- [x] `demo/` 声明保留；正式源码、测试、依赖、数据库、`runs/` 未被修改或消费。
- [x] 启动命令、讲解脚本、验证记录与剩余限制随目录交付。
