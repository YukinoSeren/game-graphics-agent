# 批次三切片交接：会话、不可变绑定与测试协议

> 日期：2026-11-03；基线 `be16c88`（`main`，已推送）；产品契约 `product-contract/0.4`。
> 本文是**执行指令**，不是设计权威。设计与字段的唯一来源：
> [`04-performance.md`](./04-performance.md) §4/§4.1/§4.4/§10、
> [`shared/data-contracts.md`](./shared/data-contracts.md) §5.1/§7/§9、
> [`shared/state-conventions.md`](./shared/state-conventions.md) §4、
> [`shared/error-conventions.md`](./shared/error-conventions.md)。
> 与它们冲突时以规格为准，并先修规格再改代码。

---

## 0. 现状

- HEAD `be16c88`；全量 `py -3.13 -B -m pytest -q -p no:cacheprovider` → **404 passed, 1 skipped**。
- 已实现并可复用：`Application`（2D-A 接线）、`AdoptionService`、`AnswerService`、`DeviceService`、
  `ImportPublisher`、`FileService.publish`、`TaskService.append_input_revision_and_wake`。
- 迁移编号已用到 `0009`；**本批次从 `0010` 起，只追加**。
- `game_app/performance.py` **尚不存在**；`tests/product/test_sessions.py`、`test_test_protocols.py` 尚不存在。

**已知缺口（本批次必须一并补）**：`LogImportInput` 不在 `contracts/tasks.py` 的 task union 中。
2D 刻意留到本轮（handoff §0 第 8 项）。补它时同时补 `TaskType.log_import` 的路由映射，
但**不要**注册 handler（PERF-T02 不做日志导入执行）。

---

## 1. 切片划分

| 切片 | 内容 | 目标测试 |
|---|---|---|
| A-1 | `LogImportInput` 进入 task union | `test_tasks.py`（扩） |
| A-2 | 会话与不可变绑定：`create_session` / `end_segment` / `split_segment` / `end_session` / `bind_configuration` | `test_sessions.py`（新） |
| A-3 | 测试协议与指标目录：`list_metric_definitions` / `create_test_protocol` | `test_test_protocols.py`（新） |
| A-4 | 历史日志关联入口：`inspect_log` / `associate_log` | `test_log_association.py`（新） |
| A-5 | 指标定义惰性登记与 `MetricDefinition` 唯一键 | 合入 `test_test_protocols.py` |

A-1 先做（最小、解除长期缺口）；A-2 与 A-3 可并行；A-4 依赖 A-2（需要段/绑定修订）。

---

## 2. 切片 A-1 — `LogImportInput`

**权威**：data §9（TaskInput 联合）、04 §4.3。

### 2.1 规则

| 规则 | 实现要求 |
|---|---|
| 形状 | `kind="log_import"`、`file_id: ID`、`source_schema: str`、`session_id: ID`、`segment_ref: BindingRef`、`target: ...`（按 04 §4.3 的 `ExistingLogTarget` / `HistoricalLogTarget` 判别） |
| 校验 | `file_id` 为 32 位 hex；`source_schema` 非空白；`segment_ref` 的两修订 >= 1 |
| 路由 | `DEFAULT_ROUTES["log_import"] = "/api/log-imports"` |
| 不执行 | **不注册 handler**；`Application.register_task` 对 `log_import` 仍然拒绝（因为无 handler），这是预期行为，写进测试 |

> 若 04 §4.3 对本切片要求的字段与上表不同，以规格为准并先改规格。

### 2.2 失败测试

1. `TaskInputAdapter.validate_python` 接受合法 `log_import` 输入。
2. 缺字段 / 空白 `source_schema` / 非 hex `file_id` → `INPUT_VALIDATION_FAILED`。
3. `Application` 未注册 handler 时注册 `log_import` 被拒（不创建任务行）。

---

## 3. 切片 A-2 — 会话与不可变绑定

**权威**：04 §4、§4.1；data §5.1（PlaySession/SessionSegment/ConfigurationBinding）；state §4。

### 3.1 产出

- `game_app/contracts/performance.py`（已存在则扩）：`CreateSessionRequest`、`EndSegmentRequest`、
  `SplitSegmentRequest`、`EndSessionRequest`、`BindConfigurationRequest`、
  `RunDeclarationInput`、`PlaySession`、`SessionSegment`、`ConfigurationBinding`、`RunConditions`。
- `game_app/performance.py`：`PerformanceService`，方法
  `create_session` / `end_segment` / `split_segment` / `end_session` / `bind_configuration`。
- `game_app/migrations/0010_sessions.sql`：会话/段/绑定投影与事件表（见 §6）。

### 3.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 创建 | `applied_id=null` 时 `configuration_state` 必须是 `unverified`，或 `mixed` 且有明确依据；否则 `INPUT_VALIDATION_FAILED` |
| 初绑定 | 创建会话同时建 `SessionSegment`（`start_s=0`、`end_s=null`、`lifecycle=active`）与 `ConfigurationBinding`（`binding_revision=1`、`effective_range.end=null`） |
| 来源 | `run_declaration` 先经 01 的声明路径保存，再构造 `RunConditions`；`provenance` 为 `user_provided` + artifact `EvidenceRef`，未知值保持 unknown |
| 安装一致 | `run_declaration.installation_id` 必须等于会话安装，否则 `INPUT_VALIDATION_FAILED` |
| 结束段 | 双修订校验：`expected_segment_revision` 与 `expected_binding_revision` 任一不符 → `BINDING_REVISION_CONFLICT`，`field_errors` 标明 `segment_revision` / `binding_revision`，并给出当前定位 |
| 结束段 | 结束与闭合绑定**同一事务**；旧绑定行不改，追加 `binding_revision+1` 且 `effective_range.end` 闭合；旧段快照保留 |
| 切段 | 在**相同边界**结束旧段并创建新活动段与初绑定；不得跨未知时点假切 |
| 结束会话 | 验证 `active_segments` 集合无遗漏（既不多也不少），全部闭合与 `lifecycle=ended` 同事务 |
| 时间 | `end_s > start_s`；不使用浏览器当前时间猜过去；不可靠计时 → `PRECONDITION_UNVERIFIED` 并保留原件 |
| 补绑 | `bind_configuration` 按当时区间依据追加新绑定与段快照；**不推进 `current_applied`**，不改旧摘要 |
| 开放修订 | 活动段（`end=null`）**不可分析**；分析入口必须拒绝，错误码 `PRECONDITION_UNVERIFIED` |
| 事件 | 每次状态变化写 `task`/领域事件或专用表行；聚合修改用 `expected_revision` 比较更新 |
| 幂等 | 先查已登记 key，再做新提交守卫；纯冲突不消耗 key |

### 3.3 失败测试

1. 无配置创建会话：段 `end=null`、绑定 `revision=1` 且 `end=null`，会话 `revision=1`。
2. `applied_id=null` + `configuration_state=active_confirmed` → 拒绝（不得把未知配置说成已生效）。
3. 结束段：旧绑定保留，新绑定 `revision=2` 且闭合；段 `end_s` 等于提交值。
4. 双修订竞态：段修订过期 → `BINDING_REVISION_CONFLICT` 且 `field_errors` 指向 `segment_revision`；绑定修订过期指向 `binding_revision`；两种情况都**零写入**。
5. 切段：边界相同（旧 `end_s` == 新 `start_s`），新段为新 revision，旧段不可再改。
6. 结束会话漏掉活动段 → 拒绝且不改任何段。
7. 结束会话后两个段都闭合、会话 `ended`，且只产生一条会话结束事件。
8. 补绑：产生新绑定 revision 与段快照，`current_applied` 指针不变，旧摘要文件 hash 不变。
9. 开放段分析被拒：`PRECONDITION_UNVERIFIED`。
10. 同一 key 重放返回同一批 ID 与同一响应；不同内容 → `IDEMPOTENCY_CONFLICT`。
11. `run_declaration.installation_id` 与会话不符 → 拒绝，无半写入。
12. 未知运行条件值保持 `unknown/not_provided`，不补默认分辨率或帧生成方式。

---

## 4. 切片 A-3 — 测试协议与指标目录

**权威**：04 §4.4；data §7（TestProtocol、MetricDefinition）。

### 4.1 产出

- `performance.py`：`list_metric_definitions(MetricCatalogQuery) -> MetricChoicePage`、
  `create_test_protocol(CreateTestProtocolRequest, MutationContext) -> ResourceResult`。
- `game_app/metrics.py`（或 `performance.py` 内）：打包规则目录 `project-metrics/1`，
  登记四个规则 key：`average_present_rate/v1`、`interval_p95_nearest_rank/v1`、
  `interval_p99_nearest_rank/v1`、`slowest_1pct_inverse_mean/v1`，
  全部 `frame_stream_kind=application_present`。

### 4.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 只读目录 | `list_metric_definitions` **零写入**，不联网；空库时 `definition_id=null` |
| 规则版本 | `metric_registry_version` 必须定位后台保留的具体 manifest；不隐式取最新 |
| 条件词表 | `condition_fields` 只允许 output/render 宽高、`frame_generation`、`upscaling`、已登记 `scene.*`；`scenario` 单列不重复 |
| 类型严格 | `frame_generation` 必须是 bool（字符串 `"false"` 拒绝）；`upscaling` 非空白字符串；分辨率必须成对且正整数 |
| 受控变量 | `controlled_variables` 无重复，只能引用 `scenario` 或本次 `condition_fields` 中**完整、已知**的变量；成对尺寸写 `output_resolution` / `render_resolution` |
| 采样参数 | `warmup_seconds` 为 null 或有限且 `>=0`；`sample_duration_seconds` 为 null 或有限且 `>0`；`repeat_count` 为 null 或严格 int `>=1`（bool 不算） |
| 不补默认 | null 保留未规定，**不**默认 60 秒/3 次 |
| 不自报 | schema 不接受 `controlled`、`executed`、`created_at`、`FieldValue`、`provenance` |
| 父协议 | `parent=null` → 新 series、`version=1`；`parent` 非空须同 `Game.series_id` 且可见，新版本 `version=parent.version+1`，保存 `parent_protocol_id`，不覆盖父 |
| 跨游戏父 | `REFERENCE_INVALID` |
| 指标 key | 非空、无重复、全部来自目录；未知 key → `INPUT_VALIDATION_FAILED`；空目录 → `PRECONDITION_UNVERIFIED` |
| 惰性登记 | 首次创建在同一事务登记所需 `MetricDefinition`，唯一键 `(metric_key, definition_version, rule_hash)`；并发复用同一 ID；GET 不写入 |
| 声明文件 | `TestProtocol.declaration_file_id` 必须非空；manifest 固定 GameContext、完整输入、规则 key→definition ID 映射、规则 manifest hash、登记时刻 |
| 幂等 | 同 key 回原协议/声明/响应；异内容 `IDEMPOTENCY_CONFLICT`；纯冲突不消耗 key |
| 错型引用 | 用 `ComparisonProtocol` id 传给 `CreateSessionRequest.test_protocol_id` 或 `CaptureInput.protocol_id` → `REFERENCE_INVALID`；用 `TestProtocol` id 传给 `CompareInput.protocol_id` 同理 |

### 4.3 失败测试

1. 空库目录：`choices` 含四个 key，`definition_id` 为 null，`registry_version == "project-metrics/1"`；库无新对象。
2. 创建协议：返回 `TestProtocol` + declaration `ManagedFile` ref；声明文件可读且含输入与规则映射。
3. 同 key 重放 → 同 ID 与同响应；异内容 → `IDEMPOTENCY_CONFLICT`，协议数不变。
4. 四个 `MetricDefinition` 只登记一次；两个不同 key 并发创建共享同一 definition ID。
5. `repeat_count=true`（bool）拒绝；`repeat_count=0` 拒绝；`sample_duration_seconds=0` 拒绝；`warmup_seconds=-1` 拒绝。
6. 只给 `output_width` 不给 `output_height` → 拒绝。
7. `controlled_variables=["scene.weather"]` 但 `condition_fields` 无该键 → 拒绝；完整给出 → 接受。
8. `frame_generation="false"`（字符串）→ 拒绝。
9. 未知 metric key → `INPUT_VALIDATION_FAILED`；不存在 `metric_registry_version` → `PRECONDITION_UNVERIFIED`。
10. 子版本：同父创建后 `version=2`、`parent_protocol_id` 正确、父对象与旧会话引用不变。
11. 跨游戏父 → `REFERENCE_INVALID`。
12. 协议类型错用 → `REFERENCE_INVALID`。
13. 创建不写 `controlled`/`executed`：协议 `checks` 中不存在自报通过项。

---

## 5. 切片 A-4 — 历史日志关联

**权威**：04 §4.3；PERF-V08。

产出：`inspect_log(file_id, source_schema) -> LogInspection`、`associate_log(LogAssociationRequest, MutationContext) -> ResourceResult`。

必须落实：

- 预览只读、有界，不建会话、不调模型；未知格式 `UNSUPPORTED_ARTIFACT_FORMAT` 且原件保留。
- `HistoricalLogTarget`：`source_anchor == source_range.start` 且 `target_anchor_s == 0`；
  `applied_id=null`、`unverified`，**不读当前指针**；`started_at` 必须有依据，不得填导入时间。
- `ExistingLogTarget`：闭合段 `close_at_s` 必须 null；开放段必须给有限结束点及依据，按段/绑定双修订原子闭合。
- 同 key 重放同 ID；过时修订或坏 key → 零半声明。
- 旧 artifact/摘要不改；`current_applied` 不变。

失败测试：PERF-V08 的六条断言各一条。

---

## 6. 迁移 `0010_sessions.sql`

只追加。按 07 §4 的表族建：

```sql
CREATE TABLE IF NOT EXISTS session_segments (
    session_id        TEXT NOT NULL,
    segment_id        TEXT NOT NULL,
    revision          INTEGER NOT NULL CHECK (revision >= 1),
    start_s           REAL NOT NULL,
    end_s             REAL,
    time_basis        TEXT NOT NULL,
    lifecycle         TEXT NOT NULL,
    applied_id        TEXT,
    configuration_state TEXT NOT NULL,
    binding_revision  INTEGER NOT NULL CHECK (binding_revision >= 1),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (session_id, segment_id)
);

CREATE TABLE IF NOT EXISTS configuration_bindings (
    segment_id        TEXT NOT NULL,
    binding_revision  INTEGER NOT NULL CHECK (binding_revision >= 1),
    applied_id        TEXT,
    configuration_state TEXT NOT NULL,
    start_s           REAL NOT NULL,
    end_s             REAL,
    supersedes_revision INTEGER,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (segment_id, binding_revision)
);
```

> 会话、协议等版本对象仍走 `objects`/`aggregate_snapshots`；上表只存段/绑定的可重建投影。
> 若实现需要额外列，追加在本迁移内并在此文档同步说明。

---

## 7. 完成定义

- `test_sessions.py`、`test_test_protocols.py`、`test_log_association.py` 与全量通过。
- `external_calls` 行数为 0。
- 提交前缀：`feat(perf-t02):`；契约改动另起 `docs(perf-t02):`。
- 每个切片一个提交，正文按 `batch-2d-handoff.md` §5 证据模板。

## 8. 明确不做

- 不做 PERF-T03（分析/比较）、PERF-T04（采集租约）；它们是后续切片。
- 不实现 `log_import` 的 handler，不启动真实采集工具。
- 不启用任何真实游戏/硬件/PresentMon 适配器。
