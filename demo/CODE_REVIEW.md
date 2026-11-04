# 独立演示版代码审计报告

> **仅用于临时演示。正式开发不得以本文件及 `demo/` 下任何设计、代码、数据、接口、测试或结果作为参考、实现依据、验收依据或事实证据。**

## 1. 结论与范围

当前默认正常旅程已有较完整实现，但不能据此认定设计中的全部演示功能通过验收。本次确认 **1 项 P1、26 项 P2、1 项 P3**。优先修复上传分析错读原件，其次是背景保存、案例切换竞态、任务恢复、分步推进和草稿恢复；再处理配置/比较/备份的事实一致性。

严重程度：P1 为应在演示前修复的高影响错误；P2 为确定的功能缺陷或数据关系错误；P3 为较低优先级的资源管理问题。未发现足够证据将问题定为 P0。

审计对象是当前磁盘上的 `demo/`，不是 Git 提交差异：目录仍为未跟踪状态。工作区另外存在正式模块及正式测试的未提交修改；未将其归因于演示开发，也未修改这些内容。

审查范围包含演示后台、前端、fixtures、测试、启动入口与验收声明。仅审查代码和复现问题，本次不修复实现、不提交或推送；新增加的交付物只有本报告。

源码/资源快照：85 个 Python/JavaScript/HTML/CSS/CMD/JSON/SVG 文件，排除 runtime 和缓存，以“路径:SHA256”排序聚合后的 SHA256 为：

```text
f4a072f4b775951a0e44ea443559d3d37846919ce419a745037ae8b55a9c70cb
```

## 2. 实际验证及限制

执行了：

```powershell
py -3.13 -B -m unittest discover -s demo/tests -t . -v
```

结果：**191 tests，73.520 秒，OK，退出码 0**。

另用 `DemoHarness` 和独立临时目录执行领域反例；前端通过 Node 加载当前模块，以最小 DOM/API 替身执行真实事件回调和异步顺序探针。反例未使用或重置现有 `demo/runtime/`。

本次没有重新执行完整真实浏览器和分辨率验收。Node 回调探针能够证明以下接口调用、状态写入和 TypeError，但不替代真实浏览器布局、IndexedDB 环境或下载交互验收。既有 `test_web_flow` 主要验证 HTTP/服务旅程，不执行前端 DOM，因此 191 项通过没有覆盖这些页面断链。

允许固定样例、模拟来源和预设性能。本报告不把未调用真实模型、未探测硬件或未运行真实游戏作为缺陷；问题集中于已承诺的操作功能和演示数据的一致性。

## 3. 演示操作与前端问题

### R01 · P1 · 上传关联后仍分析旧模拟日志

位置：[performance.js:355](E:/code/game/demo/static/js/pages/performance.js:355)、[performance.js:223](E:/code/game/demo/static/js/pages/performance.js:223)。

先完成模拟采集，再上传有效 CSV、预览、关联当前闭合区段，点击“分析本次原件”。关联回复只写 `uploadedArtifactId`；分析使用的仍是 `performanceRun.artifactId`，全前端没有消费前一个字段。

隔离回调探针得到 `remembered=uploaded-artifact`、`analyzed=capture-artifact`。用户会以为分析了上传文件，实际看到旧样例数据；文件上传成功提示不能说明此功能完成。

建议：显式选择当前要分析的 artifact，关联成功后统一更新该引用及区段/绑定信息；分析前展示并核对 file ID/hash。回归必须验证上传文件与旧模拟文件内容不同，最终摘要引用上传原件。

### R02 · P2 · 保存背景按钮必然报 TypeError

位置：[overview.js:39](E:/code/game/demo/static/js/pages/overview.js:39)、[overview.js:177](E:/code/game/demo/static/js/pages/overview.js:177)。

`backgroundForm` 创建的是 `div`，保存按钮调用 `form.form.requestSubmit()`。修改 GPU、分辨率或 CPU 后点击保存，会抛 `TypeError: form.form.requestSubmit is not a function`，保存请求发送数为零。

建议：使用真正的 form 并正确关联提交按钮，或直接调用统一提交函数；用真实按钮事件验证背景修订发生，而非只测 `context.save` API。

### R03 · P2 · 延迟响应可污染另一个案例的前端缓存

位置：[app.js:121](E:/code/game/demo/static/js/app.js:121)。

`refresh()` 发请求时使用当前案例，await 返回后再次读取可变的 `state.scenarioId` 作为写入键。A 的刷新延迟期间首次切到 B，若 A 响应在 B 初始化前返回，B 缓存为空，A 工作区会被接受并写入 B。

探针结果：`activeCase=B`，但 `cachedCaseB.scenario_id=A`。这会造成错误的背景、epoch/revision、对象读取和后续提交冲突。本次没有证明后端可跨案例写入。

建议：请求开始时捕获案例/epoch，按捕获的键写入，并校验响应 scenario_id；切换案例时丢弃不再适用的渲染结果。回归应控制 A/B 两个请求的返回顺序。

### R04 · P2 · 持久化草稿在刷新后无法取回

位置：[app.js:46](E:/code/game/demo/static/js/app.js:46)、[drafts.js:75](E:/code/game/demo/static/js/drafts.js:75)。

草稿虽然写入 IndexedDB，初始化与案例切换却不读取 `drafts.list()`；重试入口只依赖内存 `lastDraft`。断网/丢响应后刷新，原输入和原操作键仍在数据库中，但页面无法恢复或重试。

探针输出为 `listCalls=0`、`lastDraft=null`。建议启动时按案例/epoch恢复 pending/unknown 草稿，明确处理旧 epoch，并保留可核对输入；不能只证明 IndexedDB 中存在记录。

### R05 · P2 · 原键重试成功后丢失业务结果衔接

位置：[app.js:223](E:/code/game/demo/static/js/app.js:223)、[recommendation.js:139](E:/code/game/demo/static/js/pages/recommendation.js:139)。

推荐已提交但响应丢失后，点击“重试同一请求”确实取回原 Reply，却只刷新工作区并重绘。原按钮处理器已经抛异常结束，未再保存推荐 ID；任务、摘要等同样依赖按钮回调保存引用。

探针得到 `sameKey=true`、`recommendationPointer=null`。服务避免了重复创建，但玩家仍无法进入已有成果。

建议把各 action 的结果应用收敛为可重放的统一 reducer，正常回复、重试回复和刷新恢复共用；或由服务返回可恢复的当前流程引用。

### R06 · P2 · 采集中重启服务后，测试页无法继续

位置：[performance.js:70](E:/code/game/demo/static/js/pages/performance.js:70)、[performance.js:166](E:/code/game/demo/static/js/pages/performance.js:166)、[performance.py:263](E:/code/game/demo/app/performance.py:263)。

后台会将未完成任务标 interrupted。页面只有取得 `artifact_id` 才清 taskId，因此 interrupted 无原件时，开始按钮一直禁用；唯一“提前停止”又被后台作为终态拒绝，页面没有 resume。

探针输出 `startDisabled=true`、`offersStop=true`、`resumeButtons=0`。建议按任务状态渲染继续/新建/放弃，继续时保存新 task ID，不把终态作为仍可停止的任务。

### R07 · P2 · 分步模式没有推进按钮

位置：[controls.js](E:/code/game/demo/static/js/controls.js)、[store.py:767](E:/code/game/demo/app/store.py:767)。

控制台可以选 step，后台也会排除该模式的自动调度，但全部前端没有 `task.advance` 调用或“推进下一阶段”操作。选分步后启动采集会停住，只能切回其他模式救场。

建议增加当前任务的显式推进操作及 waiting/terminal 守卫，或在实现前移除该可选模式；本设计承诺了分步模式，最终验收不能仅证明后端 action 存在。

### R08 · P2 · 新知识引用被旧任务结果覆盖

位置：[knowledge.js:56](E:/code/game/demo/static/js/pages/knowledge.js:56)。

资料收集完成后，`knowledgeTaskId` 仍保留。导入新包或“模拟版本更新”保存新 knowledgeId，下一次 render 又从旧任务取出旧知识 ID，覆盖刚保存的引用。

回调探针得到 `knowledge-new → knowledge-old`。建议完成任务消费一次后清除任务关联，或以明确的快照选择状态区分“任务成果”和“当前知识”。

### R09 · P2 · 满意/保持路径仍提出或保留调整

位置：[feedback.js:69](E:/code/game/demo/static/js/pages/feedback.js:69)、[feedback.js:103](E:/code/game/demo/static/js/pages/feedback.js:103)、[feedback.js:123](E:/code/game/demo/static/js/pages/feedback.js:123)。

反馈表每次自动填入卡顿原话。直接选“满意，没有新问题”，虽提交 `satisfied=true, symptoms=[]`，后台仍从默认原话识别 stutter；两个案例的临时服务反例都返回 adjust。

即使清空原话取得 keep，页面也只在非空时更新 changeSetId，不清旧 C1，继续显示可采用的旧调整。探针为 `nextAction=keep`、`changeSetId=old-C1`、`stillOffersApply=true`。

建议样例原话采用用户显式加载或与满意选项联动，next-action 每次覆盖/清除旧建议状态，并阻止保持/停止后继续采用过时建议。

## 4. 背景、配置及历史关系

### R10 · P2 · 已处理问题的更正会覆盖更新背景

位置：[device.py:375](E:/code/game/demo/app/device.py:375)、[device.py:405](E:/code/game/demo/app/device.py:405)。

只有 open 分支检查问题/背景修订；resolved/skipped 分支把旧草稿里的版本替换成当前版本。先完成问题，保留 context_revision=2 草稿，再 `context.save` 将 CPU 更新为 `NEW CPU`（rev3），提交旧 amend 的 `STALE` 仍成功覆盖新答案。

建议创建更正前也核对调用者看到的源问题与背景修订；过时草稿返回 409，并明确新更正是针对哪份历史背景，而不是自动套用当前值。

### R11 · P2 · 配置绑定修订始终为 1，旧修订守卫失效

位置：[sessions.py:135](E:/code/game/demo/app/sessions.py:135)、[sessions.py:312](E:/code/game/demo/app/sessions.py:312)。

`binding.revise` 新建 Binding 没有递增 revision，更新 Segment 也沿用原修订。连续两次使用 `expected_binding_revision=1` 更正，第二次仍返回 201；新旧绑定修订均是 1。

旧对象有保留，不等于并发/历史修订守卫有效。建议追加 binding_revision 并同步推进 Segment 修订，或强制比较预期 binding ID；增加“其他操作刷新工作区后，仍提交旧绑定”的反例。

### R12 · P2 · 配置编辑接受不存在或跨案例的 ChangeSet

位置：[configuration.py:300](E:/code/game/demo/app/configuration.py:300)、[configuration.py:312](E:/code/game/demo/app/configuration.py:312)。

handle_edit 只把 change_set_id 存入配置，不读取或验证。MW2 编辑可引用 `nonexistent`，也可挂上 FH6 的真实 ChangeSet，仍推进 current。

建议校验 ChangeSet 类型/scope、base_applied_id、旧值和新值与实际 changes 完全一致；纯手改可以不带变更集，但不能带一个未被使用的错误引用。

### R13 · P2 · 旧推荐可静默迁移到自定义背景

位置：[configuration.py:270](E:/code/game/demo/app/configuration.py:270)、[configuration.py:200](E:/code/game/demo/app/configuration.py:200)。

预设 GPU 下生成 ready 推荐，随后改 GPU 为 CUSTOM，再采用旧推荐。推荐仍引用旧背景且 ready，但新配置一律绑定当前自定义背景，操作成功。

建议采用时比较推荐固定 context 与当前背景；不匹配时要求显式核对/新推荐或降级声明，不自动迁移旧样例的适用结论。现有测试只覆盖改背景后重新生成推荐。

## 5. 比较、反馈与日志统计

### R14 · P2 · 后测已采完才冻结口径，仍被认作预先计划

位置：[comparison.py:177](E:/code/game/demo/app/comparison.py:177)。

planned 只比较协议和后测“摘要创建”的事件序号，没有比较后测采集/原件产生时间。完成 S2 的 capture，先不分析，再冻结 P1、分析 S2，比较返回 `comparable=true, exploratory=false, notes=[]`。

建议用采集开始/原件发布事件判断先后；晚于采样或无法确定时，仅允许探索性比较。回归覆盖“先有原件、后有协议、最后才分析”。

### R15 · P2 · 不同运行条件仍显示可比较

位置：[comparison.py:168](E:/code/game/demo/app/comparison.py:168)。

仅比较 Binding.context_id 与配置是否存在，未读取 Session.run_conditions 或测试计划。同背景 ID 下，首轮声明 1920×1080/交战、后轮声明 3840×2160/菜单，仍返回 `comparable=true, notes=[]`。

建议对固定的实际运行条件与计划执行情况逐项检查，未知或不同降级；不要用“背景 ID 相同”代替分辨率、场景和采样条件相同。

### R16 · P2 · Outcome 可以挂到未测试的配置

位置：[feedback.py:338](E:/code/game/demo/app/feedback.py:338)。

只检查三个 ID 在当前案例存在，不检查 ChangeSet、采用链与比较绑定关系。完成正常 A1→A2 比较后，提交相同 C1/comparison，但 applied_id 改为原配置 A0，仍返回 201，并记录 improved=true。

建议核对基准等于 C1.base、后测绑定等于被记录采用版本、采用链确实实施 C1；不同对象关系即使同案例也应拒绝。

### R17 · P2 · 历史日志关联产物无法进入分析

位置：[performance.py:358](E:/code/game/demo/app/performance.py:358)、[performance.py:445](E:/code/game/demo/app/performance.py:445)。

historical 分支保存 `segment_id=null`；分析入口则要求原件 segment_id 严格等于闭合 Segment ID。合法样例上传、声明过去时间和依据、关联历史会话后，分析始终报 `artifact_segment_mismatch`。

建议历史关联创建或显式定位真实演示历史区段和绑定，返回可分析引用；仅测试能生成 LogArtifact 不足以验收历史导入闭环。

### R18 · P2 · 时间戳错误被记录后仍计入有效间隔

位置：[traces.py:183](E:/code/game/demo/app/traces.py:183)、[traces.py:201](E:/code/game/demo/app/traces.py:201)。

发现 timebase_break 后调用 break_run，却继续把当前 interval 加入新 run。输入相邻时间戳 0、16、1000、1016ms，各声明间隔 16ms，异常 1000ms 行仍被计入。

反例输出 `issues=[timebase_break]`，有效 runs 却为 `[[16],[16,16]]`。建议丢弃无法验证的当前间隔并明确重新建立时间锚点；断点检测不能只影响 issue 列表。

### R19 · P2 · 最慢 1% 的样本门槛检查错对象

位置：[metrics.py:68](E:/code/game/demo/app/metrics.py:68)。

代码检查总 interval count 是否小于 20，设计要求的是最慢 1% 尾部样本数至少二十个。100 个 16ms 间隔只有一个尾部样本，仍返回 `slowest_1pct_fps=62.5`，应为未知。

建议先计算 ceil(0.01×count)，对 tail count 应用门槛。测试覆盖 20—1900 个总样本，而非只测小于二十帧的 short fixture。

### R20 · P2 · 曲线截断后半段并错误声明保峰

位置：[performance.py:215](E:/code/game/demo/app/performance.py:215)、[performance.py:222](E:/code/game/demo/app/performance.py:222)。

保留抽样点及长帧后，直接截取前 480 点；卡顿多时，整个后半段和尖峰被裁掉，仍标 `preserves_peaks=true`。

反例三千个间隔（每三个一个 80ms，其他 16ms）：原件 112 秒、1000 个长帧，曲线只到 46.56 秒、保留 416 个长帧。建议按全时间范围分桶保峰，保留首尾及时间断点，不用前缀截断冒充降采样。

## 6. 知识更新与复核

### R21 · P2 · 未提取任何证据的资料可被升级为 ready

位置：[knowledge.py:542](E:/code/game/demo/app/knowledge.py:542)、[recommendation.py:80](E:/code/game/demo/app/recommendation.py:80)。

collect 的 max_stages=1 只发现来源，partial 且 accepted_count=0；调用 knowledge.update 后无条件 completed，接受数仍零，生成推荐却 ready。

建议更新沿用实际覆盖和接受记录归约，不以更新操作本身证明提取/校验完成；推荐的依据应关联已接受记录，而不只匹配来源 URL。

### R22 · P2 · 复核读取旧知识，却清除需复核状态

位置：[recommendation.py:173](E:/code/game/demo/app/recommendation.py:173)、[recommendation.py:185](E:/code/game/demo/app/recommendation.py:185)。

K1/R1 后更新得到 K2，R1 已 needs_review；review(R1) 仍复制 K1 及旧设置，但返回 current/ready。建议复核明确指定/定位新知识修订并重新检查依赖；没有使用新依据时保留 needs_review。

### R23 · P2 · 补查改写旧知识快照所引用的 Gap

位置：[knowledge.py:496](E:/code/game/demo/app/knowledge.py:496)。

K1 和补查后的 K2 共享 gap IDs，补查原地把 Gap 从 open 改为 resolved。重新展开 K1，过去缺口已经变化，尽管 K1 顶层 revision 没变。

建议缺口事实追加版本或把当前状态投影与历史快照分开；回归检查旧快照引用闭包，而非只断言顶层 revision 不变。现有 test_knowledge 明确断言共享 Gap 更新，未覆盖历史含义。

## 7. 备份、报告与运行隔离

### R24 · P2 · 损坏对象图的备份仍可覆盖工作区

位置：[exports.py:351](E:/code/game/demo/app/exports.py:351)、[exports.py:359](E:/code/game/demo/app/exports.py:359)。

恢复只特殊检查 file_id，不校验其他实体引用、类型和关系闭包。正常故事备份删除 A1 后，上传并 restore 仍返回 200；当前 A2 存在，但其 parent_id 查询为 not_found。

建议在发布新 epoch 前验证完整对象图、所有固定引用和工作区指针，损坏备份应整体拒绝，原状态保留。文件 hash 正确不代表业务图合法。

### R25 · P2 · 备份递归包含以前的备份

位置：[exports.py:246](E:/code/game/demo/app/exports.py:246)。

新备份嵌入当前所有 File，包括先前 backup 的 JSON。一次正常故事后无新增业务，连续备份体积为 852320→1989534→4643034 字节，第 4 次即 `backup_too_large` / 413。

建议区分业务原件与导出/备份产物，只打包被选业务图依赖的文件，避免把旧备份当原件递归装入；回归要求无业务变化的连续备份保持有界。

### R26 · P2 · 修改背景后导出会给旧结果换硬件/游戏名称

位置：[exports.py:55](E:/code/game/demo/app/exports.py:55)、[exports.py:69](E:/code/game/demo/app/exports.py:69)。

报告总是读 workspace 当前 Context。完成 4060 故事后改为 Custom GPU/Custom Game，再指定旧 comparison 导出，报告标题背景为新硬件/游戏，数值仍来自旧摘要，且没有逐摘要背景来澄清。

建议按所选比较/摘要的固定背景出报告；混合历史则分组列出背景，不能用当前值替换历史标签。

### R27 · P2 · --data-dir 绕过 demo/runtime 隔离边界

位置：[server.py:41](E:/code/game/demo/server.py:41)、[service.py:220](E:/code/game/demo/app/service.py:220)。

命令行路径未约束；传临时目录模拟正式目录，甚至监听失败退出码 4 时，指定目录也已产生 demo.sqlite3 和 uploads/。与 AGENTS 中“运行产物只在 demo/runtime”不符。

本次只证明显式指定路径会越界写入，没有证明会删除正式数据。建议 CLI 限制允许的演示运行根目录；底层服务的临时目录接口仍供隔离测试使用。

### R28 · P3 · 幂等上传和失败恢复遗留未登记文件

位置：[service.py:172](E:/code/game/demo/app/service.py:172)、[files.py:100](E:/code/game/demo/app/files.py:100)、[exports.py:331](E:/code/game/demo/app/exports.py:331)。

上传先 stage 后查幂等，成功重放只返回原结果，暂存文件未释放。同一 upload command 额外重放两次，File 仍一份，但 .staging 多两个 .part。

恢复先移动文件，后插入实体；人为令备份 origin 非法，数据库及 epoch 回滚，但遗留两个未登记受管理文件。建议给文件阶段增加最终清理与事务失败回收；不能只在 stage/register 当场异常时清理。

## 8. 修复顺序与复验建议

1. **现场直接操作**：R01、R02、R03、R06、R07。先确保上传确实分析新文件、背景可保存、切案例不会污染页面、重启/分步可继续。
2. **恢复和流程衔接**：R04、R05、R08、R09。验证断网/丢响应/刷新后的原键重放，能够继续使用已创建成果。
3. **事实关系**：R10—R17、R21—R26。补关系负例，不只检查对象存在或 action 返回 200。
4. **统计及图表**：R18—R20。新增中等短样本、时间错位、密集卡顿的输入，核对指标、全区间曲线和原件。
5. **隔离及清理**：R27、R28。使用临时目录做路径/失败窗口探针，不能用真实正式目录复验。

修复后的最小验收应包括：同一案例从零正常流程、上传不同 CSV 后分析、满意/保持、分步推进、中断继续、丢响应刷新恢复、跨案例延迟响应、过时背景/绑定提交、损坏备份拒绝、无业务变化重复备份。

再运行演示全量测试，并实际浏览器验证上述页面。当前 191 项通过说明已有断言通过，不说明本报告反例已修复；本报告未修改任何实现，所有列出的缺陷在审计快照上仍存在。
