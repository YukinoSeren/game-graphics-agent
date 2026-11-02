# 一次性执行提示词（按批次）

> 日期：2026-11-02；基线 `e86ecd5`（批次 2D 完成并推送）。
> 用途：把剩余实现任务拆成可一键执行的批次，每批次内自动完成编码、测试、评审、修复与推送。

## 为什么不是一个提示词

剩余任务有 21 个，`PERF-T02` 单个体量就抵得上 `DEV-T03` + `DEV-T04`。一次会话写完整个项目，
上下文压缩后后半程会开始伪造通过。**正确用法是一次一个批次**：提示词结构完全一样，
只换 §9 的任务清单。这样每个批次内才是真正的"一次性完成 + 自动评审 + 自动推送"。

每个批次开一个新会话。批次之间先 `git pull --ff-only` 确认基线，再把上一批最终报告里的
`Rulings` 与 `Deferred minors` 贴进新会话开头。

---

## 1. 主提示词模板

复制下面整段，把 `{{批次任务清单}}` 替换为 §2 中对应批次的内容。

````text
你是本仓库的执行代理。目标：把指定批次的产品实现任务一次性完成——包括编码、测试、评审、修复与推送——中途不向我确认。

## 0. 仓库与环境

- 工作目录 E:\code\game，Windows 11 + Git Bash，Python 用 `py -3.13`。
- 当前分支 main。开始前先跑 `git status --short`、`git log -1 --oneline`；工作区必须干净，不整库 reset。若 HEAD 不是上一批次末尾的提交，先停下来报告。
- 基线命令：`py -3.13 -B -m pytest -q -p no:cacheprovider`。开始前记录基线数字，任何与"基线通过数 + 本批次新增数"不符的结果都要查清原因。

## 1. 权威文档与阅读顺序（不读就写代码＝返工）

1. `docs/technical-design/implementation-plan.md` —— 任务依赖与新任务的具体要求（每个任务在"实施任务与 DoD"段落里有完整要求）
2. 本批任务所属模块规格：`01-device-game-configuration.md` / `02-knowledge-and-collection.md` / `02a-collector-integration.md` / `03-recommendation.md` / `04-performance.md` / `05-feedback-optimization.md` / `06-player-experience.md` / `07-data-and-architecture.md` / `08-validation-and-delivery.md`
3. 契约唯一权威：`docs/technical-design/shared/data-contracts.md`、`state-conventions.md`、`error-conventions.md`
4. 最近一个已完成批次的交接文档（例如 `docs/technical-design/batch-2d-handoff.md`）——照抄它的"提交与证据模板""本仓库具体注意事项"
5. 开始编码前，用 `git log --oneline -20` 了解最近 3 个批次的实现风格

## 2. 硬性约束（违反即失败，不能用"时间紧"交换）

- **契约优先**：新字段先写进 `shared/` 三份契约与模块规格，再改代码；规格与实现冲突时先修规格。
- **TDD**：每个行为先写失败测试，运行并确认"因行为缺失而失败"（不是拼写/导入错误），再最小实现。目标测试通过后跑全量。
- **不联网、不调用真实模型/供应商/硬件/游戏**。`external_calls` 行数必须为 0（离线任务）。不读玩家真实库或 `runs/` 金样本。
- **不伪造**：不得为了"让演示通过"手填 ID、补默认值、追填历史、把 `not_run` 写成通过、把缺失 handler 写成 completed、为审计插入从未发生的中间状态。能力没有真实证据时保持 `not_run`/禁用。
- **迁移只追加**，编号从 `0010` 起（`0009` 已被批次 2D 占用）。
- 存储对象回读用 `model_validate_json`；`UnitOfWork` 内禁止嵌套 `unit_of_work()`。
- `FileService.publish` 的 writer 在 `ManagedFile` 行插入之前运行，writer 内不能 `insert_reference` 指向本次声明的文件。
- 同 key 幂等的检查顺序：**先查已登记 key，再做新提交的前置校验**；纯冲突不消耗 key。
- 夹具字节：`.gitattributes` 已把 `tests/fixtures/**/*.csv` 设为 `-text`；新增夹具后核对 manifest 的 `byte_length`/`sha256`。JSON 强制 `eol=lf`。
- 提交信息按 `batch-2d-handoff.md` §5 的证据模板（task_id / spec_revision / prerequisite_tasks / decision_and_gate_status / changed_files / fixture_id_and_hash / focused_command / observed_exit_code / assertions_checked / provider_send_count / object_and_event_counts / evidence_level / not_run_or_blocked_cases / remaining_scope / commit_revision）。

## 3. 不可越过的门禁

- **Q1 产品形态未确认** → 不得实现 HTTP/CLI 路由与鉴权（ENG-T04）、页面（UX-*）。本批次若包含它们，先检查 Q1 是否已在仓库文档中被明确回答；没有就跳过该任务并在报告中标记 `blocked_by_q1`，继续做其余任务。
- **Q2 首游戏/适配范围未确认** → 不得预启用 FH6 等真实适配器。
- **G-WIN / G-GAME / G-FORMAT / G-MODEL / G-WEB / G-IMAGE / G-EFFECT 均无真实证据** → 相关能力保持 `not_run`/禁用，证据等级最多 `offline_fixture`。
- 遇到规格缺口：先修规格文档再改代码，并在报告中列出改了哪些规格段落。

## 4. 执行循环（每个任务严格按此顺序）

1. 读该任务的完整段落（实现计划"实施任务与 DoD"）+ 所属模块规格章节。
2. 若需要新字段/新状态/新错误码：先改 `shared/` 与模块规格，单独提交 `docs(<task-id>): ...`。
3. 写失败测试 → 运行 → 确认失败原因正确 → 最小实现 → 目标测试通过 → 跑全量。
4. 一个任务一个提交（契约与代码可分两个提交），提交前 `git status` 确认没有夹带无关改动。
5. 在任务结束时把结果追加到 `.superpowers/sdd/<batch-name>/progress.md`（一行任务结果 + 所有 `Ruling:` 行）。

任务之间不要停下来问我。遇到的每条二选一，自己按规格裁决，写成
`Ruling: <决定> — <理由> — <判断错了的代价>`，然后继续。

## 5. 每任务后的评审（自动，不用问我）

每个任务提交后，派一个**全新上下文的评审子代理**（general-purpose，最可用的强模型）做只读评审：

- 给出 base..head 的 git range，让它自己跑 `git diff` 与全量测试。
- 评审重点固定为这些**本仓库已多次出事的类别**：
  1. 授权/越权（IDOR）：任何从输入取 id 的路径，是否校验归属；是否泄漏他人资源存在性（应按 `RESOURCE_NOT_FOUND`）。
  2. 幂等顺序：已登记 key 是否先于新提交守卫；纯冲突是否消耗 key；request hash 是否完整。
  3. 并发：`BEGIN IMMEDIATE` 假设、版本号是否在写锁内分配、唯一约束冲突是否被误报为校验失败。
  4. 诚实性：`not_run` 是否被写成 pass；缺失能力是否被默认值填补；审计事件是否记录未发生的状态；归属 id 是否指向真正产生它的记录。
  5. 存储回读：`model_validate_json`、枚举值、旧载荷兼容。
  6. 测试是否验证真实行为而非 mock 自证。
- 让它输出 Strengths / Critical / Important / Minor / Declined-to-judge。
- **Critical 与 Important 必须在本次会话内修完**：每条先写复现测试、看它失败、再修、跑全量。一次修复轮，不做二次评审。
- Minor 记入 progress.md（`minor (deferred): ...`），不修。
- 评审判定不成立时，写成 `Ruling:` 说明为什么代码应保持原样，cost if wrong 一并写。

## 6. 推送（已授权，自动执行）

- 只在**全量测试全绿**且本轮 Critical/Important 已修完时推送。
- 每完成一个批次推一次：`git push origin main`。不 force，不 rebase 远端，不覆盖远端历史。
- 推送前确认 `git status -sb` 显示工作区干净、本地领先远端且没有落后（落后就停下来报告，不要强推）。
- 推送后在报告中写明 pushed range 与远端 HEAD。

## 7. 批次完成定义

全部满足才算完成：

- 本批次每个任务的目标测试存在、运行过、我看到了输出。
- 全量 `py -3.13 -B -m pytest -q -p no:cacheprovider` 全绿，报告通过数（基线 + 新增）。
- 离线任务的 `external_calls` 行数为 0。
- 每个任务一个提交，提交信息含完整证据模板。
- 每个任务的评审已完成，Critical/Important 已修，Minor 已记录。
- 已推送，且远端 HEAD 与本地一致。

## 8. 最终报告格式

```
批次：<name>
基线：<commit> (<N> passed, 1 skipped)
新增任务：<task-id 列表，含跳过项与原因>
提交：<每任务 commit sha + 一行摘要>
评审：<每任务 Critical/Important 数量与修复状态>
全量：<M> passed, 1 skipped
推送：<range> → origin/main @ <sha>
证据等级：<offline_fixture / 无 integration / 无 local_effect>
Rulings：<逐条列出，含 cost if wrong>
Deferred minors：<逐条列出>
Blocked：<Q1/Q2/G-* 相关跳过项>
下一步建议：<下个批次名>
```

## 9. 本批次任务清单

{{批次任务清单}}

现在开始。先执行 §0 与 §1，然后按 §9 逐项执行，全程不要向我确认。
````

---

## 2. 批次任务清单

把下面某一节的正文替换进主提示词的 `{{批次任务清单}}`。
顺序不能打乱：`PERF-T02` 会往 task union 加 `LogImportInput`，`KNW-T02` 依赖批次 2D
之后才可达的 `published`。

### 批次 A — 性能

```text
按此顺序执行，每个任务各自提交、各自评审：

1. PERF-T02 会话/闭合绑定/声明与测试协议（04 §10）
   - 同时把 LogImportInput 加入 contracts/tasks.py 的 task union（批次 2D 唯一遗留缺口）
   - 产物：tests/product/test_sessions.py
2. PERF-T03 分析/比较/比较协议/日志关联（04 §10）
   - 产物：tests/product/test_metrics.py
3. PERF-T04 自有采集租约/收尾/恢复（04 §10）
   - 产物：tests/product/test_capture_lifecycle.py
4. VAL-T02 框架搭建（08 §10）
   - 本批次只建确定性/故障串联框架，不宣称全量结案
   - 产物：tests/product/test_contracts.py、test_faults.py

禁用：任何真实采集适配器、真实游戏进程、真实 GPU 查询。
```

### 批次 B — 知识

```text
前置：批次 A 已完成并推送。

按此顺序执行：

1. KNW-T02 用途覆盖与包发布（02 §10）
   - 消费已发布导入；此线在批次 2D-C 之后才真正可达 published
   - 产物：tests/product/test_packages.py
2. KNW-T03 自主补查/标准文件组/持久调用审计（02 §10）
   - 仍禁用真实采集适配器；provider 用计数桩
   - 产物：tests/product/test_collection_tasks.py
3. KNW-T04 更新影响/图像启用门禁（02 §10）
   - 图像能力无 G-IMAGE 证据，保持禁用并记 not_run
   - 产物：tests/product/test_impacts.py
```

### 批次 C — 推荐

```text
前置：批次 B 已完成并推送。

按此顺序执行：

1. REC-T01 候选/用途/约束（03 §10）→ tests/product/test_recommendation_gate.py
2. REC-T02 主方案/动作/目标/readiness（03 §10）→ tests/product/test_recommendation_actions.py
3. REC-T03 推荐复核与发布门禁（03 §10）

R03 仍是建议而非已确认规则：不得据其宣布 readiness 或 actionable。
```

### 批次 D — 反馈与效果

```text
前置：批次 C 已完成并推送。

按此顺序执行：

1. FBK-T01 反馈与偏好（05 §10）
2. FBK-T02 下一动作/合法 ChangeSet/LoopGuard（05 §10）→ tests/product/test_next_actions.py
3. FBK-T03 效果/接受/Outcome 与 manifest（05 §10）→ tests/product/test_outcomes.py
4. IMP-T04 研究 mapper/独立标注门禁（02a §10）→ tests/product/test_research_import.py
   - 与研究样本无关时不得用夹具顶替真实未见评价

效果结论最多到 offline_fixture；local_effect 必须有真实会话证据才可声明。
```

### 批次 E — 入口与页面（受 Q1 门禁）

```text
先检查 Q1（产品形态：本地网页）是否已在仓库文档中被明确回答。

若已回答，按此顺序执行：
1. ENG-T04 HTTP/CLI 路由/安全/查询/导出 → tests/product/test_http.py
2. UX-T01 固定只读视图
3. UX-T02 草稿/回答/刷新/未知响应 → tests/integration/test_submissions.py
4. UX-T03 全旅程/可访问/协议/日志/历史/导出 → tests/integration/test_journey.py
5. VAL-T02 全量串联结案

若 Q1 仍未回答：跳过 1—4，全部标记 blocked_by_q1，只做 VAL-T02 收尾，
并在最终报告的 Blocked 段中写明。
```

### 批次 F — 验证与交付

```text
前置：批次 A—D 已完成；批次 E 若被 Q1 阻塞则先报告。

按此顺序执行：

1. VAL-T03 兼容矩阵/未见评价（08 §10）→ tests/integration/test_compatibility.py
   - 真服务需明确预算；无预算即记 not_run
2. VAL-T04 浏览器闭环/演示/效果分级证据
   - 无真实浏览器与真实会话证据时只记 not_run，禁止声称 local_effect 完成
3. ENG-T05 最终集成/分发/部署诊断 → tests/integration/test_distribution.py
   - 不反向阻断测试服务
```

---

## 3. 使用要点

1. **每批次开一个新会话**，不要在同一会话里连跑两个批次。
2. 批次之间先 `git pull --ff-only` 确认基线，再把上一批报告中的 `Rulings` 与
   `Deferred minors` 贴进新会话开头，避免重复裁决或悄悄改判。
3. 若某批次评审报出 Critical 但根因在规格，让它标 `blocked` 而不是硬修——
   硬修出来的东西比没修更贵。
4. 安全评审插件目前两次都因模型不可达未完成，所以主提示词把授权/越权评审
   写进了每任务的固定评审维度；这是刻意的冗余，不要删。
5. 每批次结束后核对 `docs/technical-design/implementation-plan.md` 的复选框，
   当前该文档与真实进度已脱节（VAL-T01 已完成但未勾选）。

## 4. 当前剩余工作量速查

| 类别 | 内容 |
|---|---|
| 可立即开工 | PERF-T02、KNW-T02、VAL-T02 框架 |
| 需要前置批次 | PERF-T03/T04、KNW-T03/T04、REC-*、FBK-*、IMP-T04、VAL-T03 |
| 受 Q1 阻塞 | ENG-T04、UX-T01/T02/T03 |
| 受真实证据门禁 | VAL-T04、ENG-T05 的效果分级部分、所有 G-* 能力 |
| 代码债务 | 6 条 review Minor（内联 document 强转、device_scan 上下文归属未校验等），建议在 PERF-T02 前清掉前两条 |
