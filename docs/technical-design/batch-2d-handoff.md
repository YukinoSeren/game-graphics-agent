# 批次二切片 2D 交接：接线、回答接续与契约补全

> 日期：2026-11-02；基线提交 `0f5f77c`（`main`，领先 `origin/main` 10 个提交，未推送）；
> 产品契约 `product-contract/0.4`。
> 本文是**执行指令**，不是设计权威。设计与裁决的唯一来源是
> [`2026-11-02-batch-2d-wiring-and-contract-completion-design.md`](../superpowers/specs/2026-11-02-batch-2d-wiring-and-contract-completion-design.md)；
> 字段/状态/错误的唯一权威仍是 [`coding-handoff.md`](./coding-handoff.md)、
> [`implementation-plan.md`](./implementation-plan.md)、[`shared/`](./shared/) 三份契约
> 与 `01`/`02a`/`07` 模块规格。本文只写"从哪继续、按什么顺序、每步如何证明"，
> 与设计或规格冲突时以它们为准并先修规格。

---

## 0. 现状快照

**已完成并提交（自 `acd104d` 之后新增 6 个提交）**

| 任务 | 提交 | 交付 |
|---|---|---|
| IMP-T02 作用域与导入记录 | `3aae47d` | `ScopedReferenceMapper`/`ImportValidator`、`Source`/`SourceRevision`/`ImportedRecord`/`RecordExtension`/`BoundarySemantics`、迁移 `0006`（四元键对 usable 行唯一） |
| IMP-T03 发布与故障恢复 | `c060184` | `ImportPublisher`/`ImportService`、三元指纹去重、声明文件、改名/提交故障窗口、迁移 `0007` |
| DEV-T03 采用与历史事实 | `5edfbc2` | `AdoptionService`、origin 守卫、指针 compare-and-set、ChangeSet 合并、显式引用替换、回滚、历史登记、迁移 `0008` |
| DEV-T04 回答归一与接续 | `d7c1fb3` | `AnswerService`/`AmendmentService`、两版本校验、冲突选择、派生快照、唤醒路径、facts_only 更正 |
| 安全修复 | `d8a026f` | 导入 context 归属、回答/更正的任务归属（均在写事务内） |
| 评审修复 | `0f5f77c` | 适配器按 `probe_index` 定位、resolved/skipped 拒绝直答、series 唯一、attempts 保留、分辨率双轴、证据 hash、段 `revision`、坏组变 rejected、冻结原件可回读、知识引用逐字段核对 |

**基线命令与期望**

```powershell
py -3.13 -B -m pytest -q -p no:cacheprovider
# 期望：361 passed, 1 skipped（跳过项为 Windows 上不生效的 POSIX 权限位断言）
```

**已完成、可直接复用的能力**

| 能力 | 位置 |
|---|---|
| 冻结组读取 | `importer.FrozenInputReader.freeze(...)` → `FrozenGroup(format_id, operation_id, file_ids, manifest, inspection)` |
| 从受管理 id 重开冻结组 | `importer.frozen_group_from_file_ids(files, file_ids, registry)` |
| 作用域映射 | `importer.ScopedReferenceMapper(files).map(frozen, import_snapshot_id=, identity_mappings=)` → `ScopedImport` |
| 导入发布 | `importer.ImportPublisher(repository, files).publish(...)`；`ImportService(...).handle(ctx)` |
| 身份/可见性 | `knowledge.IdentityService.select_game / resolve_game_identity / assess_reuse`、`_visible` |
| 采用/历史 | `adoption.AdoptionService.adopt_configuration / record_historical_configuration / get_current_applied` |
| 回答/更正 | `answers.AnswerService.answer_information`、`answers.AmendmentService.create_information_amendment` |
| 任务追加输入并唤醒 | `tasks.TaskService.append_input_revision_and_wake(uow, task_id, cause_ref=, related=, cause=)` |
| 指针推进 | `UnitOfWork.advance_current_applied(...)`（事务内 compare-and-set + 事件） |
| 设备探测 | `device.DeviceService(repository, files, probe).handle(ctx)`；`ProbeAdapter.probe(paths, timeout=)` |

**已知跨任务未落实项（不要误以为已完成）**

1. `game_app/application.py` 只构造 `TaskService`，**没有任何 handler**；而
   `tasks.py` 对未知任务类型回落到 `lambda ctx: HandlerOutcome.completed()`——
   今天任何任务都会"空跑成功且不写事实"。
2. 唤醒后的任务**不消费回答**：`append_input_revision_and_wake` 复制原输入字节，
   全仓无 handler 读 `InformationResponse`。
3. 导入**无法产出 `published`**：结果码要求可用记录 + 已解析 `game_id`，而
   `IdentityMapping` 只能由 `resolve_game_identity`（需 verified 证据）写入。
4. `DeviceSnapshot` 无 `user_scope_id`；采用/回答无法校验快照归属。
5. `InformationResponse` 无 `answered_fields`/`unresolved_fields`/`answered_values`。
6. 无 `domain_events` 表，导入发布没有创建事件。
7. `TaskProgress.phase` 仍按状态粗归约，未按 `phase:<键>` 事件归约。
8. `LogImportInput` 仍不在 task union（随 PERF-T02 加，不在 2D）。
9. 每次设备扫描都新建 `series_id`/`device_series_id`，设备序列只有一版。

---

## 1. 新会话启动流程（每个切片都照做）

1. `git status --short`、`git log -1 --oneline`、`git branch --show-current`；确认 HEAD
   为上一任务提交。**保护已有改动，不整库 reset。** 工作区应干净。
2. 读设计文档 §4 的对应决策（D1—D7）与 §5/§6/§7，再读本切片引用的规格段落。
   不读就写代码＝返工。
3. 运行基线，记录 `361 passed, 1 skipped` 之外的任何变化。
4. **先写失败测试**（`superpowers:test-driven-development`），运行并确认"因行为缺失而
   失败"，不是因拼写/导入错误。
5. 最小实现；目标测试通过后跑**全量**测试。
6. 按 §5 模板记录证据，提交（一个切片一个提交），不 force、不推送。

**不要做的事**：不新建平行计划结构；不联网安装依赖；不调用真实模型/供应商/硬件/游戏；
不把未运行写成通过；不在测试中读取玩家真实库或 `runs/` 金样本；不为"让演示通过"
手填 ID、补默认值、追填历史或自动重发外部请求。

---

## 2. 切片 2D-A — 接线、发布事件与 phase 归约

**权威**：设计 D1、D5、D6；07 §4/§5；state §8.1；02a §6。

**依赖**：2C 全部完成。**必须先做**，2D-B/2D-C 的测试经由 `Application` 或复用其守卫。

### 2.1 目标与边界

让应用层真正执行已实现的任务、对未实现类型明确失败、给导入发布留下诚实的创建事件、
让 `TaskProgress.phase` 反映实际发出的阶段。不新增任何领域功能。

产出：

- `game_app/application.py`：注册 `knowledge_import`（总是）与 `device_scan`（仅当传入
  `probe`）；新增 `adopt_configuration`、`record_historical_configuration`、
  `answer_information`、`create_information_amendment`、`declare_import_identity`
  五个薄包装。
- `game_app/tasks.py`：`run_once` 缺失 handler 时 `failed` + `INTERNAL_ERROR`；
  `TaskContext.phase(key)`。
- `game_app/repository.py`：`UnitOfWork.record_domain_event(...)`。
- `game_app/migrations/0009_domain_events.sql`：`domain_events`、`task_responses`、
  `device_series_slots` 三张表（后两张供 2D-B 使用，本切片一并建好）。
- `tests/product/test_application.py`（新）、扩 `test_import_publish.py`、`test_tasks.py`。

### 2.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 缺失 handler | `Application.register_task` 在 `task_input.kind not in self.tasks.handlers` 时抛 `INPUT_VALIDATION_FAILED`（`field_errors=[path="kind", code="enum"]`），**不创建任务行**；`TaskService.run_once` 遇缺失 handler 直接 `failed` + `INTERNAL_ERROR` + `stop_reason=execution/validation_failed`，绝不 `completed` |
| 无适配器 | 不传 `probe` 时不注册 `device_scan`，因此该类型注册被拒（而不是跑空） |
| 创建事件 | 发布事务内写**一条** `domain_events`：`previous_state=null`、`next_state` 与最终状态一致、`cause=import_published`/`import_rejected`、`related_refs` 含声明文件与全部冻结原件 |
| 不做 staging | 不得插入 `staging` 记录再改状态；`previous_state=null` 精确表达"以该状态诞生" |
| phase | `TaskContext.phase(key)` 追加 `from_state==to_state` 的事件，`cause=f"phase:{key}"`，`key` 限 `acquire/collect/extract/validate/decide/capture/analyze/finalize`；`_phase_for` 优先取最近一条 `phase:` 事件，否则回落现状 |
| 幂等 | 已登记 key 回放优先于新提交守卫；纯冲突不消耗 key |

### 2.3 先写这些失败测试

1. `Application(probe=stub)` 构造后 `device_scan` 与 `knowledge_import` 都可注册并执行。
2. 未注册类型（`analysis`）注册被拒：`INPUT_VALIDATION_FAILED`，任务表无新行。
3. `TaskService` 直接 `run_once` 遇缺失 handler → `failed` + `INTERNAL_ERROR`。
4. 不传 `probe` 时 `device_scan` 注册被拒。
5. 发布写一条 `domain_events`，`previous_state is None`，`next_state` 与状态一致，
   `related_refs` 含声明文件与冻结原件。
6. rejected 导入同样有一条创建事件，`cause=import_rejected`。
7. `phase:<key>` 事件改变 `TaskProgress.phase`；无 phase 事件时回落不变。
8. 并发同 tripe 导入落败方的声明文件可经 `file_operations` 定位（Minor 15 的裁决依据）。

### 2.4 完成定义

- `tests/product/test_application.py`、`test_import_publish.py`、`test_tasks.py` 与全量通过。
- `external_calls` 行数为 0。
- 提交信息按 `feat(eng-t02): ...`（若同时含 IMP-T03 事件，正文说明分属）。

---

## 3. 切片 2D-B — 回答接续、快照归属与激活归约

**权威**：设计 D2、D4、D7（Minor 18/20/21）；01 §2/§6；state §7.2；data §2.1/§3.2/§8.2。

**依赖**：2D-A。

### 3.1 目标与边界

让玩家回答真正被后续运行消费；让设备快照有归属与稳定序列；修正激活归约与
`get_current_applied` 的返回值。不实现新的问题类型，不改唤醒规则。

产出：

- `contracts/information.py`：`InformationResponse` 增 `answered_fields`、
  `unresolved_fields`、`answered_values`。
- `contracts/device.py`：`DeviceSnapshot.user_scope_id`。
- `answers.py`：写 `task_responses`（仅 `source_task`）；`_FIELD_NORMALIZERS` 注册表。
- `tasks.py`：`TaskContext.answered_values`。
- `device.py`：消费回答、按槽投影定序列、写 `user_scope_id`、循环守卫。
- `adoption.py`：快照归属校验、激活归约、`get_current_applied` 返回 `(None, None)`。
- 测试：扩 `test_information_answers.py`、`test_device.py`、`test_adoption.py`。

### 3.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 响应自述 | 三个新字段描述**本次响应**的结果，不是请求投影的累积；`answered_values[path]` 是归一后的值（冲突选择给被选中候选，不是原始提交值）；未归一原话只进 `unresolved_fields` |
| 持久关联 | `AnswerService` 在同一事务内写 `task_responses(task_id=request.source_task_id, sequence=MAX+1, response_id, request_id)`；**仅 `continuation_mode=source_task`**，`facts_only` 不写 |
| 合并语义 | `answered_values` 按 `sequence` 升序；后到覆盖先到；某路径在更晚响应中 `unresolved` 则**删除**该路径 |
| 出处 | 派生快照该值的 `AcquisitionAttempt.information_response_id` 指向产生它的响应；基线原有 attempts/provenance 全部保留 |
| 不重问 | 只探测 `paths - answered.keys()`；已回答路径既不探测也不提问；全已知时不 `suspend` |
| 循环守卫 | 新请求字段集合与"本任务最新一条请求"完全相同且 `answered` 无增删 → 不建问题，`failed` + `stop_reason=execution/input_unresolved` + `last_error_code=PRECONDITION_UNVERIFIED` |
| 设备序列槽 | `device_series_slots(user_scope_id, scope_key)`；`scope_key` 依次取 `installation:<id>` / `context:<id>` / `user`；首次建槽，后续沿用，`version` 递增 |
| 快照归属 | `DeviceSnapshot.user_scope_id` 由 `context.user_scope_id`（持久任务所有者）写入；采用/回答只接受本人快照，否则 `RESOURCE_NOT_FOUND` |
| 激活归约 | 本切片无核验来源，**每条** `active_confirmed` 在聚合层归约 `unknown`，追加 `applied.activation` 的 `unknown` 检查；单项 `ConfirmedSetting.activation_state` 保留玩家声明 |
| 指针返回值 | `get_current_applied` 无指针返回 `(None, None)`，不再返回 `revision=0` |
| 归一注册表 | `_FIELD_NORMALIZERS: dict[str, Callable[[Scalar], Scalar | None]]`；未知路径仍判 `unresolved`，绝不猜 |

**实现陷阱**

- `AnswerService._should_wake` 只在**所有关键字段**闭合时才写 `cause_ref`；部分回答
  没有 `cause_ref`，这正是必须新增 `task_responses` 的原因，不要试图从 `cause_ref` 推导。
- `_apply_device_values` 已按 `probe_index` 定位适配器；新增逻辑不得退回列表下标。
- 派生快照必须链自**同一序列的最新版本**，否则第二次部分回答会撞
  `objects(entity_type, series_id, version)` 唯一约束。

### 3.3 先写这些失败测试

1. 回答关键项后重跑同一任务：不再探测已答路径，不产生第二条问题，`completed`，
   快照值等于回答值且带新 attempt。
2. **部分回答（关键字段未闭合、无 `cause_ref`）后重跑**：仍读到该回答，只问剩余字段。
3. 无进展重跑：不建问题，`failed` / `input_unresolved`。
4. `answered_fields`/`unresolved_fields`/`answered_values` 自洽；未归一原话不在
   `answered_values`。
5. 冲突选择：`answered_values[path]` 是被选中的候选值。
6. 先答后判 `unresolved`：该路径从 `answered_values` 消失，字段回到待问。
7. 派生快照的 `information_response_id` 指向产生它的响应。
8. `facts_only` 回答不写 `task_responses`；源任务 `task_inputs`/`task_events`/
   `tasks.revision` 计数不变。
9. 同一 `(user, installation)` 两次扫描同 `series_id`/`device_series_id` 且 `version`
   递增；无安装按 `context:`，两者皆无按 `user`。
10. `DeviceSnapshot.user_scope_id` 等于任务所有者。
11. 他人快照不能用于采用：`RESOURCE_NOT_FOUND`。
12. 声称 `active_confirmed`：单项保留、聚合 `unknown`、有 `applied.activation` 检查。
13. `get_current_applied` 无指针返回 `(None, None)`。

### 3.4 完成定义

- 三个测试文件与全量通过。
- 提交信息 `feat(dev-t04): ...`；正文写明 `task_responses` 与 `device_series_slots`
  的用途和"facts_only 零写入"的计数断言。

---

## 4. 切片 2D-C — 导入身份映射与来源事实

**权威**：设计 D3、D7（Minor 18/19）；02 §3.1；02a §4/§5；data §3.1/§4.1。

**依赖**：2D-A（薄包装）。**可与 2D-B 并行。**

### 4.1 目标与边界

给"本次采集运行属于哪个游戏"一条受控、可审计、受所有权约束的写入路径，使
`published` 可达；同时把 `sources.json` 里已有但被丢弃的文档事实写进 `SourceRevision`。

产出：

- `contracts/knowledge.py`：`IdentityMapping.declared_by_user_scope_id`、
  `ImportIdentityMappingRequest`。
- `knowledge.py`：`IdentityService.declare_import_identity(...)`。
- `importer.py`：`_identity_mapping` 所有权校验；`_map_sources` 读取 `document` 事实。
- 测试：`tests/product/test_import_identity.py`（新）、扩 `test_scoped_import.py`。

### 4.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 命名空间 | `legacy_namespace = f"collector_run:{run_id}"`，与 `_resolve_game` 查找键完全一致，不得改写 |
| 映射形状 | `legacy_value = legacy_game_id`；`target_game_id = Game.series_id`；`declared_by_user_scope_id = context.user_scope_id`；`checks` 一条 `pass` 的 `identity.declared`，note 说明"玩家为该导入指定的游戏，非外部验证" |
| 可见性 | `provisional` 必须本人声明；`verified` 公开；`legacy_unverified` **拒绝**（`REFERENCE_INVALID`） |
| 同游戏重复声明 | 不同 key、同 `(namespace, legacy_value, game)` → 返回既有映射 id，**不新建**，因此不产生新 `mapping_context_hash` 与新快照 |
| 指向另一游戏 | `REFERENCE_INVALID`，`field_errors=[path="legacy_game_id", code="conflict"]`，**不覆盖** |
| 使用侧所有权 | `ImportPublisher._identity_mapping` 校验 `declared_by_user_scope_id`：非 null 且不等于提交者 → `RESOURCE_NOT_FOUND`；null 表示公共映射，任何用户可用 |
| 幂等 | 同 key 同内容回原响应；异内容 `IDEMPOTENCY_CONFLICT`；纯冲突不消耗 key |
| 来源事实 | `document.content_hash → SourceRevision.content_hash`、`document.truncated → truncated`、`document.retrieval_method → capture_method`；`document_file_id` 保持 `null` 并追加 `source.document` 的 `not_run` 检查 |
| 不新增文件 | **不得**为内联 document 另写 `ManagedFile`（rejected 时会留无主文件） |

### 4.3 先写这些失败测试

1. 声明映射后导入得到 `published`，`game_id` 等于声明的系列。
2. 映射改指另一游戏被拒，旧映射不变。
3. 同 key 重放返回同 ID；不同 key 重复同一 `(namespace, value, game)` 返回既有映射 id，
   且不产生新快照。
4. 未声明映射的导入仍为 `retained_only`（现状保持）。
5. 使用他人声明的映射导入 → `RESOURCE_NOT_FOUND`，不产生快照。
6. `legacy_unverified` 游戏不能用于新声明。
7. `sources.json` 的 `document.*` 映射到 `SourceRevision`；缺失时为 `null` 且检查
   `not_run`；导入不新增任何受管理文件。

### 4.4 完成定义

- `tests/product/test_import_identity.py`、`test_scoped_import.py` 与全量通过。
- `external_calls` 行数为 0；导入不新增 `ManagedFile`（除声明文件）。
- 提交信息 `feat(knw-t01): ...`。

---

## 5. 提交与证据模板

每个切片一个提交，正文含：

```text
task_id / spec_revision / prerequisite_tasks / decision_and_gate_status
changed_files / fixture_id_and_hash / focused_command / observed_exit_code
assertions_checked / provider_send_count / object_and_event_counts
evidence_level / not_run_or_blocked_cases / remaining_scope / commit_revision
```

- 目标命令：`py -3.13 -B -m pytest tests/product/<本切片文件> -q -p no:cacheprovider`
- 收尾命令：`py -3.13 -B -m pytest -q -p no:cacheprovider`（必须全绿）
- 提交前缀：`feat(eng-t02):`、`feat(dev-t04):`、`feat(knw-t01):`；修复按 `fix(...)`。
- 推送与否则由用户决定；不 force、不覆盖远端。

**本仓库的具体注意事项**

- Windows + `py -3.13`；命令统一加 `-B -p no:cacheprovider`。
- 夹具字节：`.gitattributes` 已把 `tests/fixtures/**/*.csv` 设为 `-text`；新增夹具后
  核对 manifest 的 `byte_length`/`sha256`。JSON 强制 `eol=lf`。
- 迁移编号从 `0009` 起，只追加。
- 存储对象回读用 `model_validate_json`；直接 `model_validate(stored.payload)` 会因
  枚举/严格模式失败。
- `UnitOfWork` 内禁止嵌套 `unit_of_work()`；需要"事务内读"时用传入的 `uow`。
- `FileService.publish` 的 `writer` 在 `ManagedFile` 行插入之前运行，因此 writer 内
  不能 `insert_reference` 指向本次声明的文件；把文件 id 存进对象字段或投影表。
- 同 key 幂等的检查顺序：先查已登记 key，再做新提交的前置校验。
- 契约改动先写进 `shared/` 与模块规格，再改代码（设计 §5 已列出全部条目）。

---

## 6. 仍然阻塞 / 不在本轮

| 项 | 状态 |
|---|---|
| Q1 产品形态（本地网页） | 未确认；阻塞 ENG-T04/UX 全量 |
| Q2 首游戏/适配范围 | 未确认；不得预启用 FH6 等真实适配器 |
| Q3 确认方式/玩家测试 | 未确认；不影响离线实现 |
| G-WIN / G-GAME / G-FORMAT / G-MODEL / G-WEB / G-IMAGE / G-EFFECT | 均无真实证据；相关能力保持 `not_run`/禁用 |
| R03 | 仍为建议 |
| 2D 明确不做 | HTTP/CLI 路由与鉴权（ENG-T04）、staging 两段式、`LogImportInput`（随 PERF-T02）、内联 document 的独立文件登记 |
| PERF-T02/T03/T04、KNW-T02/T03、REC-*、FBK-*、IMP-T04、ENG-T04+、UX-*、VAL-* | 2D 之后才开始；2D 正是它们的前置 |

**判定规则**：规格缺口 → 先修规格文档再改代码；实现偏差 → 先写失败测试再修；
不得为了"让演示通过"手填 ID、补默认值、追填历史或自动重发外部请求。
