# 批次 C 交接：知识（KNW-T02 / KNW-T03 / KNW-T04）

> 日期：2026-11-03；基线提交 `49ba9d6`（`main`，已推送）；产品契约 `product-contract/0.4`。
> 本文是**执行指令**，不是设计权威。设计与字段的唯一来源：
> [`02-knowledge-and-collection.md`](./02-knowledge-and-collection.md) §2—§7/§9/§10、
> [`02a-collector-integration.md`](./02a-collector-integration.md) §3—§5/§10、
> [`03-recommendation.md`](./03-recommendation.md) §5（仅消费接口）、
> [`shared/data-contracts.md`](./shared/data-contracts.md) §3/§4/§8/§9、
> [`shared/state-conventions.md`](./shared/state-conventions.md) §2/§3/§6、
> [`shared/error-conventions.md`](./shared/error-conventions.md) §2—§5。
> 与它们冲突时以规格为准，并**先改规格再改代码**。

---

## 0. 现状

- `HEAD 49ba9d6`；全量 `py -3.13 -B -m pytest -q -p no:cacheprovider` → **544 passed, 1 skipped**。
  任何低于 544 的结果都是回归，先查清再继续。
- 批次 A（PERF-T02）与批次 B（PERF-T03、PERF-T04、VAL-T02 框架）已交付并推送。
  **不要重做，也不要改判**批次 B 的接口。批次 B 的原始报告在
  `.superpowers/batch-runs/report-B.md`（git 忽略，但磁盘上应仍在）；任务台账在
  `.superpowers/sdd/batch-b-handoff` 已被清理，B 的裁决以本文件 §0.3 与 report-B 为准。
- 既有采集原型（`game_collector/`）保持原样：`tests/test_*.py` 共 **45 例**必须继续通过，
  原 CLI 行为与导出格式不被本批次改写。

### 0.1 已实现、C 必须复用（不要重写）

| 能力 | 位置 |
|---|---|
| `IdentityService`：`list_games` / `select_game` / `resolve_game_identity` / `declare_import_identity` / `assess_reuse` | `game_app/knowledge.py`（`assess_reuse` 在文件末尾附近） |
| `ReuseDecision`、`OPERATION_SCOPES`、`is_registered_scope` | `game_app/contracts/knowledge.py` §D-P01 段与文件头 |
| `Game` / `GameAlias` / `IdentityMapping` / `GameSelection` / `GameContext` 契约 | `contracts/knowledge.py`、`contracts/device.py` |
| `ImportService`（`knowledge_import` handler）、`ImportPublisher.publish`、`frozen_group_from_file_ids` | `game_app/importer.py` |
| `ImportPublishResult`：`snapshot_id / status / code`（`published`\|`retained_only`）`/ accepted_record_ids / diagnostic_record_ids / declaration_file_id` | `game_app/importer.py` |
| `ImportSnapshot` 三元去重、`ImportedRecord` 四元键、envelope 原状态保留 | `importer.py` + 迁移 `0006`/`0007` + `contracts/knowledge.py` |
| 任务骨架：`register_task` / 幂等 / `Budget` / `invoke_provider` 审计 / 未知调用不重发 / `resume_task` | `game_app/tasks.py` |
| 文件发布：`FileService.publish`（writer 在 `ManagedFile` 行之前）、`store_artifact`、`artifact_file_id` | `game_app/files.py` |
| `ManagedFile.user_scope_id`（null=内部产物）与读取授权 | `files.py`、`performance.py` 的 `_require_file_owner` |
| 夹具：`collector-run-smoke@v1`（`run/sources/records/gaps/extractions.json` + manifest） | `tests/fixtures/product/collector-run-smoke/v1` |
| 测试框架：`FixtureLoader`/`CaseSpec`/`CaseResult`/`RunManifest`、`ScenarioRunner`、`SpyProvider`、`CrashInjector` | `tests/product/support/{fixtures,validation,spies}.py` |
| 性能域已完成的摘要/比较（不作为 C 的输入，仅说明已有代码边界） | `game_app/performance.py`、`analyzer.py` |

### 0.2 已明确**没有**做（属于 C）

- `Application` **没有注册** `knowledge` handler；`register_task` 对 `knowledge` 仍然拒绝。
  `DEFAULT_ROUTES["knowledge"] = "/api/knowledge-tasks"` 已存在。
  `KnowledgeInput` 已在 `contracts/tasks.py`（含 `budget`），无需新加任务类型。
- 没有 `CoverageEvaluator`、`build_package`、`collect_knowledge`、`notify_impacts`，
  没有任何 `KnowledgePackage` 被创建或发布（`assess_reuse` 只读已有包，测试里是手工桩）。
- 没有 `game_app/collection.py`，没有 `FollowupPlanner` / `CollectionSnapshotWriter`。
- 没有 `ImpactNotice` 契约、没有去重表、没有图像启用门禁。
- **没有推荐模块**：`game_app/recommendation.py` 不存在，`RecommendationVersion` /
  `RecommendationReviewEvent` 只有 `ENTITY_TYPES` 里的登记。KNW-T04 的 03 侧消费接口因此缺失，
  见 §4.1。
- 没有 `tests/product/test_packages.py` / `test_collection_tasks.py` / `test_impacts.py`。

### 0.3 A/B 的裁决（C 不得悄悄改判）

| 裁决 | 理由 | 新会话的义务 |
|---|---|---|
| 所有新提交守卫在 `files.publish` **之前**执行；纯冲突不消耗 key | error §4.2 | 组包/通知同样先校验再暂存 |
| 冻结文件逐字节确定：不写实时时间戳、不写带 `checked_at` 的检查 | 崩溃重试/双击复用同一冻结文件 | 包声明 manifest 的登记时刻放不可变对象 `created_at` |
| 文件级归属用 `ManagedFile.user_scope_id`；他人文件 `RESOURCE_NOT_FOUND` | A-4 Critical | 包/通知引用的原件同样先解析归属 |
| 读接口一律要求调用者作用域；他人资源不泄露存在性 | A-2/A-4 | `get_package`、`notify_impacts` 同样处理 |
| 存储对象回读用 `model_validate_json`；`UnitOfWork` 内禁止嵌套 | 严格模式与锁 | 组包/写入内部只用传入的 `uow` |
| `files.publish` 的 writer 在 `ManagedFile` 行插入之前运行 | 事务边界 | writer 内不能 `insert_reference` 指向本次声明的文件 |
| 只有 handler 注册后才接受该任务类型；未注册保持拒绝 | 不把缺失实现伪装成成功 | 注册 `knowledge` 的那一提交要同时有 handler 与测试 |
| **同 key 重放返回原响应，即使世界已变**（B 评审修复） | error §4.2 | `build_package`/通知重放不得重算或改 `created_at` |

### 0.4 迁移与编号

- `0010_sessions.sql`、`0011_capture_leases.sql` 已被占用；**本批次从 `0012` 起，只追加**。
- 预期只有 KNW-T04 需要迁移（通知去重）；KNW-T02/T03 的包/通知对象走 `objects`。
  若实现确需额外列，追加在同一迁移内并同步本文件与 `02-knowledge-and-collection.md`。

### 0.5 门禁

- **Q1 未回答** → 不实现 HTTP/CLI 路由、鉴权与页面（ENG-T04/UX-*）。
- **Q2 与 G-GAME/G-FORMAT/G-MODEL/G-IMAGE 无证据** → 不启用真实采集适配器、
  不联网、不调用真实模型或供应商、不读真实图片。测试只用计数桩与固定文件组。
- 证据等级上限 `offline_fixture`。
- `external_calls` 的行数口径（**必须在会话内落成 Ruling**）：
  - KNW-T02、KNW-T04：**0 行**。
  - KNW-T03：该切片的任务本身就是"持久调用审计"，测试用桩时必须经由
    `TaskContext.invoke_provider` 产生真实审计行；门禁是**没有网络、provider 名为桩、
    行数与 `provider_send_count` 精确相等、未发送的调用不得有 `sent_at`**，而不是行数为 0。
    若选择退化为 0 行，就等于没有实现 KNW-T03 的审计要求，必须在报告中写明未完成。

---

## 1. 切片划分

| 切片 | 内容 | 目标测试 |
|---|---|---|
| C-1 | KNW-T02：`CoverageEvaluator` / `build_package` / `knowledge` handler | `tests/product/test_packages.py`（新） |
| C-2 | KNW-T03：`collection.py`（`FollowupPlanner`、`CollectionSnapshotWriter`）、增益/预算/停止、调用审计 | `tests/product/test_collection_tasks.py`（新） |
| C-3 | KNW-T04：`ImpactNotice` 去重、图像门禁、来源修订影响；03 消费侧受阻记录 | `tests/product/test_impacts.py`（新） |

顺序不能乱：C-2 依赖 C-1 的 `build_package`；C-3 依赖 C-1/C-2 的真实包。

---

## 2. 切片 C-1 — KNW-T02 用途覆盖与包发布

**权威**：02 §4（覆盖归约）、§5（`build_package` 接口）、§6（范围与停止）、§7（降级）、
§9 的 KNW-V02/V03；data §4.1/§4.2（`ImportSnapshot`、`KnowledgeRecordRef`、`KnowledgePackage`、
`CoverageCell`、`KnowledgeGap`）；state §2（`CoverageStatus`、`PublicationStatus`、
`CollectionResult`）；error §3。

### 2.1 产出

- `contracts/knowledge.py`：仅补 C-1 真正需要的本地 DTO（`build_package` 是受信任的内部接口，
  不新增浏览器可传的任意 JSON 入口；`KnowledgeInput` 已存在，尽量不改）。
- `game_app/knowledge.py`：`CoverageEvaluator`、`build_package`、`collect_knowledge`（任务入口）
  与 `get_package`（只读、核验调用者归属）。
- `game_app/application.py`：注册 `knowledge` handler，暴露 `collect_knowledge` / `get_package`。
- `tests/product/test_packages.py`。

### 2.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 输入 | 只消费**已发布**的 `ImportSnapshot`；`retained_only`/`rejected` 不得成为匹配包的成员；快照 `game` 与会话上下文不一致 → `REFERENCE_INVALID` |
| 身份 | 包发布只允许已核验/明确声明的同一 `game_id`；身份未解决时保留诊断与缺口，**不发布"已匹配"包**，领域结果按 partial |
| 覆盖归约 | 每格：无合法依据 = `unknown`；有关键子项未处理 = `partially_covered`；本操作全部必需子项有通过依据 = `covered`；有排除依据且确实不适用 = `not_applicable`。**未执行的检查不得算 covered**；`covered/not_applicable` 必须有 `evidence_refs`（契约已强制） |
| 依赖闭合 | 被选成员引用的 setting/configuration/environment/evidence 必须在同一快照作用域内可解析；缺依赖 → 该成员不进包（或整包拒绝，见下） |
| 隔离记录 | 只进 `diagnostic_record_ids`，绝不进 `record_snapshot_ids` 或 `setting_catalog` 等目录 |
| 私人与公共 | 会话、偏好、性能摘要、Outcome 等私人对象一律不得进入公共包；包内只出现 `KnowledgeRecordRef` 指向的导入记录 |
| collection 三字段 | 只有真实单一执行事实时才填 `collection_result`/`stop_reason`/`collection_execution_ref`；多运行聚合三者**全为 null**，不得从新覆盖反推历史运行事实 |
| 声明范围 | `declared_scope` 只能用 `OPERATION_SCOPES` 或 `diagnosis.<症状>`；`critical_gaps` 的关键性必须带 `operation_scope` |
| 版本 | 新包是新版本；`prior_package_id` 必须同游戏、同 series 且可见；旧包的成员/检查/字节不变 |
| 幂等 | 同 key 同内容 → 原 `ResourceResult`/原包；异内容 `IDEMPOTENCY_CONFLICT`；纯冲突（未发布快照、开放身份）不消耗 key |
| 产物 | 新 `KnowledgePackage` + 声明 manifest（`ManagedFile`），manifest 逐字节确定 |
| 副作用 | 不调用模型、不发网络、不改当前采用指针、不写推荐；`external_calls` 为 0 |

### 2.3 失败测试（至少覆盖）

1. accepted 记录但目标用途未知 → 对应格 `unknown`/`partially_covered`，绝不 `covered`。
2. 隔离记录只出现在 `diagnostic_record_ids`，包成员与目录里都没有它。
3. 成员缺依赖 → 拒绝组包（零包），或按规格只移除该成员；两种处理必须选一种并在测试里固定。
4. 图像关键条件缺失 → 该格未 covered；纯文字部分仍可用于其它格（不得整包判死）。
5. 多快照但无单一执行事实 → `collection_result`/`stop_reason`/`collection_execution_ref` 全为 null。
6. 每格都有状态与依据；`covered` 无 `evidence_refs` 被契约拒绝。
7. 私人性能摘要 id 不能进入任何包字段。
8. 跨游戏快照 → `REFERENCE_INVALID`，零包。
9. 同 key 重放同包；异内容 409；未发布快照先触发一次冲突，再用同 key 成功（key 未被消耗）。
10. 第二个版本发布后，旧包字节与行不变；`assess_reuse` 对旧包仍可复用。
11. `external_calls` 行数为 0；`provider_send_count` 为 0。

### 2.4 完成定义

- `test_packages.py` 与全量通过；提交 `feat(knw-t02): ...`，正文含证据模板。

---

## 3. 切片 C-2 — KNW-T03 自主补查、标准文件组与持久调用审计

**权威**：02 §3（组件）、§6（补查/停止/增益/预算）、§9 的 KNW-V02/V04/V05；
02a §3—§5/§10（标准文件组、冻结、映射、发布）；data §8（任务/调用/幂等）；
state §3（任务状态与领域结果）、§6（调用未知窗口）；error §3/§5。

### 3.1 产出

- `game_app/collection.py`：`FollowupPlanner`、`CollectionSnapshotWriter`（写标准
  `collector-run/v0.1` 兼容文件组）、增益与停止判定。
- `game_app/knowledge.py`：`knowledge` handler 的采集分支（规划 → 补查 → 停止 → 组包）。
- `game_collector/providers.py`：**只在必要时**加最小边界；原 CLI、原导出与 45 个既有测试保持通过。
- `tests/product/test_collection_tasks.py`。

### 3.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 入口 | `KnowledgeInput` 经 `register_task` 成为 `knowledge` 任务；无目标 FPS、无偏好、无设备仍可开始（KNW-V01 已由 A 覆盖，不得回退） |
| 规划 | 按 P0/P1/P2 与受影响操作排序 gap；先重读原表/图注/更正，再查原始出处，再定向独立来源 |
| 增益 | 只算关键字段新增、条件补齐、冲突解决、不同取舍新增；**网页数量不构成增益**；连续两轮无有效增益 → `low_information_gain` 停止 |
| 预算 | 每次调用前经 `TaskContext.invoke_provider` 核验 `Budget` 计数；耗尽 → `budget_exhausted`，保留已有成员/缺口/原因；增预算用 `ResumeTaskInput` 新任务，不复用不合格步骤 |
| 调用审计 | 先保存 `prepared`，再 `in_flight`，再发送；返回原件先落盘；未知结果 → `CALL_OUTCOME_UNKNOWN`，**同 key 重试与恢复都不重发** |
| 模型标识 | `envelope.model` 缺失 → `returned_model=null`；原 CLI 的兼容 model 字段不得回填到产品审计 |
| 一次读取多错误 | 同一 URL 的多个提取/校验错误共享一次读取调用，不得为每条错误重复读取 |
| 标准文件组 | `CollectionSnapshotWriter` 在本次独立受管理目录写出 02a 规定的标准组（`run.json`/`sources.json`/`records.json`/`gaps.json`/`extractions.json`），保存真实 run ID 与 source 作用域、原载荷与提取原件；不改旧 CLI 输出 |
| 组包 | 用 02a 内部冻结/映射能力登记 `origin=collector_run` 的 `ImportSnapshot`，再由 C-1 的 `build_package` 组包；**不递归新建 `knowledge_import` 重任务** |
| 关联 | 产品 `TaskRun`、捕获 run ID、导入快照 ID 分别保留；`collection_execution_ref` 指本次真实 `TaskRun` |
| 检查点 | 只复用已提交快照/原件；未提交输出按 07 恢复日志核对，不假装完成 |
| 幂等 | 同 key 重放原 `TaskAccepted`；异内容 409；纯冲突不消耗 key |

### 3.3 失败测试（至少覆盖）

1. 无目标检索也能开始，FPS/偏好/设备不是必填。
2. 两轮无增益 → `low_information_gain`；计数与轮次可核对。
3. 预算耗尽 → `budget_exhausted` + partial，已得成员与缺口保留。
4. 一次 URL 多错误 → 读取调用计数为 1。
5. 未知调用 → `CALL_OUTCOME_UNKNOWN`；同 key 重试与 `resume_task` 都不增加 `provider_send_count`。
6. `envelope.model` 缺失 → `returned_model=null`，不取兼容字段。
7. 新采集 → 标准组 → `ImportSnapshot(origin=collector_run)` → 包成员可追溯到本次任务；
   任务数只增 1（没有递归 `knowledge_import`）。
8. 同 key 重放同响应；纯冲突（预算为 0 或身份未决）不消耗 key。
9. 45 个既有 `game_collector` 测试仍通过；旧 CLI 输出字节不变。
10. `external_calls` 行数 == `provider_send_count`；没有网络；未发送调用无 `sent_at`。

### 3.4 完成定义

- `test_collection_tasks.py` 与全量通过；提交 `feat(knw-t03): ...`。
- 报告必须写明：真实采集适配器与真实模型未启用，KNW-V04 的图像部分为 `not_run`。

---

## 4. 切片 C-3 — KNW-T04 更新影响与图像启用门禁

**权威**：02 §2（KNW-F05）、§4（覆盖与关键性）、§6（更新）、§7（撤回/损坏）、§9 的 KNW-V04/V05；
03 §5（`consume_impact`）；data §4.2（`KnowledgePackage`）、§8.3（幂等）；
state §2（`CheckStatus`）、§5（推荐复核）。

### 4.1 前置缺口（必须先裁决并写入 progress.md）

KNW-T04 在实现计划里的依赖是 **KNW-T02、KNW-T03、REC-T03**，而 **REC-T01—T03 尚不存在**
（`game_app/recommendation.py` 不存在，也没有 `RecommendationVersion` 契约实现）。
`03 §5` 定义的 `ReviewService.consume_impact(notice_id)->ResourceResult` 因此没有宿主。

本批次只做 KNW-T04 的 02 侧能力，并按下面**推荐裁决**处理 03 侧：

- **Ruling（推荐）**：02 侧完整实现 `notify_impacts`（通知登记、去重、受影响集合、来源修订）；
  03 侧消费接口**不实现**，在报告中记为 `blocked`（原因：REC-T01—T03 未实现），
  `test_impacts.py` 只断言"通知已登记且未被消费"，并断言**没有**写入任何
  `RecommendationReviewEvent`/推荐内容/当前指针。
- 备选（若实现计划要求本批次闭环）：只新增一个最小 `RecommendationService.consume_impact`，
  且它只能消费**已存在**的推荐版本；没有推荐版本时返回 `REFERENCE_INVALID`，
  不得为测试伪造 `RecommendationVersion`。这会把 REC-T03 的一部分提前到本批次，
  必须在 Ruling 里写明"提前实现的范围"与代价。

**不得**把 `consume_impact` 写成"已通过"却没有任何调用者；也不得为审计插入一次从未发生的复核事件。

### 4.2 产出

- `game_app/migrations/0012_impact_notices.sql`（见 §5）。
- `contracts/knowledge.py`：`ImpactNotice`（字段见 02 §5，全 R）。
- `game_app/knowledge.py`：`notify_impacts`、受影响记录定位（按 `old/new` 包成员差异）、
  图像适配器门禁（未验证即禁用，保留缺口）。
- `tests/product/test_impacts.py`。

### 4.3 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 通知字段 | `notice_id / old_package_id / new_package_id / affected_record_ids / reason / evidence_refs`，`created_at` 后台生成 |
| 去重 | `unique(old_package_id, new_package_id, 受影响记录集合 hash, 规则版本)`；同通知重投返回同一 `notice_id`，不产生重复事件 |
| 只通知 | 通知只登记消息；不改推荐内容、不改当前采用指针、不改旧会话/旧 Outcome |
| 受影响集合 | 只包含真正变更/被撤回/依赖变化的记录；无实际受影响依赖时**不新建通知**，或按规格登记"已处理+理由"（二选一并在测试固定） |
| 来源修订 | 新 `SourceRevision` 只影响引用它的成员；其余成员与包不被打为受影响 |
| 损坏/撤回 | 成员文件损坏 → 新使用 `ARTIFACT_UNAVAILABLE`，旧历史保留不可复算原因，通知仍可登记 |
| 图像门禁 | 无 G-IMAGE 证据 → 图像适配器保持禁用；任务保留项目范围与对应缺口；不得声称"已读图片"或给出泛化准确率；该部分 `not_run` |
| 幂等 | 同 key 重放原响应；异内容 409；纯冲突不消耗 key |
| 副作用 | 不发模型、不发网络；`external_calls` 为 0 |

### 4.4 失败测试（至少覆盖）

1. 同一条通知重复投递 → 同一 `notice_id`，通知行数为 1，事件数为 0 或 1（按裁决固定）。
2. 无实际受影响依赖 → 不新建通知（或登记已处理+理由，测试固定所选分支）。
3. superseded 的旧推荐不因通知变成 current；不得新建推荐内容。
4. 成员文件损坏 → `ARTIFACT_UNAVAILABLE`，无新消费。
5. 图像 mapper 未验证 → 禁用，缺口保持，`not_run` 被记录。
6. 来源修订变更 → 只有引用它的成员进入 `affected_record_ids`。
7. 当前采用指针、旧 Outcome、旧推荐字节不变。
8. 03 侧：按 §4.1 裁决，`consume_impact` 为 `blocked` 或最小实现；不得伪造复核事件。
9. `external_calls` 为 0。

### 4.5 完成定义

- `test_impacts.py` 与全量通过；提交 `feat(knw-t04): ...`。
- 报告必须写明：G-IMAGE 无证据，KNW-V04 的图像部分 `not_run`；KNW-T04 的 03 消费侧状态。

---

## 5. 迁移 `0012_impact_notices.sql`（预期形状）

只追加。字段按 02 §5（`ImpactNotice` 仍是含 `EntityHeader` 的对象，写 `objects`；下表是可重建的去重投影）：

```sql
CREATE TABLE IF NOT EXISTS impact_notices (
    notice_id          TEXT PRIMARY KEY,
    old_package_id     TEXT NOT NULL,
    new_package_id     TEXT NOT NULL,
    affected_hash      TEXT NOT NULL,   -- 受影响记录集合 + 规则版本的规范 hash
    rule_version       TEXT NOT NULL,
    affected_record_ids TEXT NOT NULL,  -- 规范 JSON 数组，按 id 排序
    reason             TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_impact_notice_dedupe
    ON impact_notices(old_package_id, new_package_id, affected_hash, rule_version);

CREATE INDEX IF NOT EXISTS ix_impact_notices_new_package
    ON impact_notices(new_package_id, created_at);
```

> 若实现需要额外列，追加在本迁移内并同步更新本节与 `02-knowledge-and-collection.md`。
> `notice_id` 用确定性 id（`old/new/affected_hash/rule_version` 派生），使重投天然同 id。

---

## 6. 评审重点（每切片派新上下文评审员）

沿用批次 C 提示词的固定类别，本仓库已多次出问题：

1. **授权/越权**：`get_package`、`notify_impacts`、任何从输入取 id 的路径是否核验归属；
   他人资源是否 `RESOURCE_NOT_FOUND` 而非泄露存在性。
2. **幂等顺序**：已登记 key 是否先于新提交守卫；纯冲突是否消耗 key；request hash 是否包含
   快照/身份/范围等全部业务字段。
3. **并发**：`BEGIN IMMEDIATE` 假设；版本号是否在写锁内分配；唯一约束冲突是否被误报为校验失败。
4. **诚实性**：`not_run` 是否被写成 pass；缺失能力是否被默认值填补；是否为审计插入未发生的事件；
   `returned_model` 是否取了兼容回填。
5. **存储回读**：`model_validate_json`、枚举值、旧载荷兼容。
6. **测试自证**：测试是否验证真实行为而非 mock 自证；数量断言是否精确（`==`，不用 `>=`）。

Critical/Important 必须在会话内修完（先红后绿）；Minor 记 `deferred`。

---

## 7. 完成定义（本批次）

- 三个切片各自提交、各自评审；`test_packages.py`、`test_collection_tasks.py`、`test_impacts.py`
  全部存在并运行过。
- 全量 `py -3.13 -B -m pytest -q -p no:cacheprovider` 全绿（≥ 544 + 新增）。
- 每个切片的 `external_calls` 口径符合 §0.5，且报告给出实际行数与 `provider_send_count`。
- KNW-T02/T04 的 `external_calls` 为 0；KNW-T03 的审计行数与桩发送数相等，无网络。
- 每个切片评审的 Critical/Important 已在本会话内修完（先红后绿），Minor 记录不修。
- 推送 `origin/main`，工作区干净，本地不落后远端。

## 8. 明确不做

- 不实现 HTTP/CLI/页面（Q1）；不实现 REC/FBK/IMP-T04 模块（KNW-T04 的 03 侧按 §4.1 裁决）。
- 不联网、不调用真实模型/供应商/硬件/游戏；不启用图像适配器；不读玩家库或 `runs/`。
- 不把 `not_run` 写成 `pass`，不补默认值（无 60 秒/3 次/1080p/默认 FPS）。
- 不改写 `game_collector` 的 CLI 行为与导出格式；不改写批次 A/B 已交付的接口。

## 9. 提交与证据模板

按 `batch-b-handoff.md` §9（同 `batch-2d-handoff.md` §5）：

```text
task_id / spec_revision / prerequisite_tasks / decision_and_gate_status
changed_files / fixture_id_and_hash / focused_command / observed_exit_code
assertions_checked / provider_send_count / object_and_event_counts
evidence_level / not_run_or_blocked_cases / remaining_scope / commit_revision
```

- 目标命令：`py -3.13 -B -m pytest tests/product/<本切片文件> -q -p no:cacheprovider`
- 收尾命令：`py -3.13 -B -m pytest -q -p no:cacheprovider`
- 前缀：`feat(knw-t02):`、`feat(knw-t03):`、`feat(knw-t04):`；契约/迁移单独
  `docs(knw-t02):`；修复按 `fix(...)`。

## 10. 报告

写入 `.superpowers/batch-runs/report-C.md`，格式同批次 A/B：

```text
批次：C（KNW-T02、KNW-T03、KNW-T04）
基线：49ba9d6（544 passed, 1 skipped）
提交：<每切片 sha + 一行摘要>
评审：<每切片 Critical/Important 数量与修复状态>
全量：<N> passed, 1 skipped
推送：<range> → origin/main @ <sha>
证据等级：offline_fixture
Rulings：<逐条，含 cost if wrong>
Deferred minors：<逐条>
Blocked：<Q1/Q2/G-* 相关跳过项；KNW-T04 的 03 消费侧状态；图像部分 not_run>
下一步建议：<批次 D>
```

## 11. 新会话第一步

1. `git status --short`、`git log -1 --oneline`：HEAD 必须是 `49ba9d6`（或其后只含文档提交）；
   工作区除未跟踪的 `demo/` 外干净。**不要把 `demo/` 当作正式依据**（其 README 明确说明仅演示）。
2. 跑基线 `py -3.13 -B -m pytest -q -p no:cacheprovider`，确认 544 passed, 1 skipped。
3. 读 `02 §4—§6`、`data §4.2`、`state §2/§3`、`importer.py` 的 `ImportPublisher.publish`
   与 `tests/product/test_import_publish.py`（了解已发布快照的真实形状）。
4. 先做 C-1；C-1 的 `build_package` 是 C-2/C-3 的依赖，不要在 C-2 里另造一套组包逻辑。
5. 每个二选一写成 `Ruling: <决定> — <理由> — <判断错了的代价>`，追加到
   `.superpowers/sdd/batch-c-handoff/progress.md`，然后继续，不要停下来问。
