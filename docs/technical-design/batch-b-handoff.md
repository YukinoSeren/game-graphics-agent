# 批次 B 交接：分析与采集生命周期

> 日期：2026-11-03；基线提交 `186cbb8`（`main`，已推送）；产品契约 `product-contract/0.4`。
> 本文是**执行指令**，不是设计权威。设计与字段的唯一来源：
> [`04-performance.md`](./04-performance.md) §4.2/§4.3/§5/§6/§7/§9/§10、
> [`08-validation-and-delivery.md`](./08-validation-and-delivery.md) §2—§5/§9/§10、
> [`shared/data-contracts.md`](./shared/data-contracts.md) §6.1/§6.2/§8/§9、
> [`shared/state-conventions.md`](./shared/state-conventions.md) §2/§3/§6、
> [`shared/error-conventions.md`](./shared/error-conventions.md) §2—§5。
> 与它们冲突时以规格为准，并**先改规格再改代码**。

---

## 0. 现状

- `HEAD 186cbb8`；全量 `py -3.13 -B -m pytest -q -p no:cacheprovider` → **490 passed, 1 skipped**。
  任何低于 490 的结果都是回归，先查清再继续。
- 批次 A 已交付并推送（PERF-T02 全部切片，含评审修复）。**不要重做，也不要改判**下面这些接口。

### 0.1 A 已经实现、B 必须复用

| 能力 | 位置 |
|---|---|
| `PerformanceService`（会话/段/绑定/协议/目录/日志关联） | `game_app/performance.py` |
| `require_analyzeable(binding_ref, user_scope_id)` | 同上；只接受**具名**已闭合段修订 + 已闭合绑定 |
| `_build_run_conditions` / `_validate_condition_fields` | 同上；未知条件保持 `unknown/not_provided/not_attempted` |
| `_metric_definition_id` / `metrics.RULES` / `metrics.catalog` | `game_app/metrics.py` |
| `ManagedFile.user_scope_id`（`null` = 内部产物） | `game_app/files.py`；`FileService.publish` 自动写入发布者 |
| 幂等骨架：`_has_registered` / `_replay_resource` / `_record_resource` | `performance.py`；先回放，再守卫 |
| 段/绑定投影与事件 | 迁移 `0010_sessions.sql`；`session_segments`、`configuration_bindings` |
| `Application` 薄包装 | `game_app/application.py`（`create_session`…`associate_log` 已在） |
| 指标规则唯一键 | 迁移 `0002` 的 `metric_definition_rules(metric_key, registry_version, rule_hash)` |

### 0.2 A 明确**没有**做（属于 B）

- 没有 `analyze`、`compare`、`create_comparison_protocol`，也没有 `analysis` 任务 handler
  （`Application.register_task` 对 `analysis` 仍然拒绝，这是当前预期行为）。
- 没有 `capture` / `stop_capture` / `CaptureLease`，`adapters/presentmon.py` 只有解析器，**不能启动任何工具**。
- 没有 `tests/product/test_metrics.py`、`test_comparisons.py`、`test_capture_lifecycle.py`、
  `test_contracts.py`、`test_faults.py`、`tests/product/support/validation.py`。
- `inspect_log` / `associate_log` 已由 A-4 完成并有 `test_log_association.py`（23 例）。
  **B 不再实现这两个入口**，只在需要时补「关联后的显式分析」用例。

### 0.3 批次 A 的裁决（B 不得悄悄改判）

| 裁决 | 理由 | 新会话的义务 |
|---|---|---|
| 文件级归属用 `ManagedFile.user_scope_id`；`null` 不得经用户入口消费 | A-4 的 Critical 是文件 IDOR；契约 data §6.1 已补该字段 | 新发布的用户文件必须带 scope；内部产物（供应商响应等）用 `null` |
| 读接口一律要求调用者作用域；他人资源 `RESOURCE_NOT_FOUND` | 同类越权反复出现在 A-2/A-4 | `analyze`/`compare` 也必须先解析 artifact/summary 的归属 |
| 冻结文件（`FileService.publish` 的 `files`）必须逐字节确定：**不写实时时间戳、不写带 `checked_at` 的检查** | 崩溃重试/双击在同 key 下必须复用同一冻结文件 | 分析/比较/采集的 manifest 同样规则；登记时刻放不可变对象 `created_at` |
| 所有新提交守卫在 `files.publish` **之前**执行；纯冲突不消耗 key | error-conventions §4.2 | 先校验再暂存；评审会逐条核对 |
| 段结束点只来自后台时钟差；结束点不得落在未来 | 04 §4.1 | 分析/采集闭合段沿用同一规则 |
| 历史段/绑定必须经 `0010` 投影解析并核验调用者归属 | A-2 的 Critical | B 的一切 segment/artifact 解析同样处理 |
| 规则文件进 hash；`project-metrics/1` 的四个 key 固定 | A-3 | 比较协议复用 `metrics.rule_for`，不自造第二套目录 |

### 0.4 迁移与编号

- `0010_sessions.sql` 已被 A-2 占用；**本批次从 `0011` 起，只追加**。B-3 预期需要
  `0011_capture_leases.sql`；B-1/B-2 不需要新迁移（摘要/协议走 `objects`）。
- 存储对象回读用 `model_validate_json`；`UnitOfWork` 内禁止嵌套 `unit_of_work()`。
- `FileService.publish` 的 writer 在 `ManagedFile` 行插入之前运行：writer 内不能
  `insert_reference` 指向本次声明的文件。

### 0.5 门禁

- **Q1 未回答** → 不实现 HTTP/CLI 路由、鉴权与页面（ENG-T04/UX-*）。
- **Q2 与 G-GAME/G-FORMAT 无证据** → 不启用真实适配器、不启动真实游戏/采集工具；
  采集只能是**注入的进程桩 + 时间桩**。
- 本批次证据等级上限 `offline_fixture`；`external_calls` 行数必须为 0。

---

## 1. 切片划分

| 切片 | 内容 | 目标测试 |
|---|---|---|
| B-1 | 确定性分析：`analyze` + `Analyzer` + `analysis` handler | `tests/product/test_metrics.py`（新） |
| B-2 | 比较协议与比较：`create_comparison_protocol` / `compare` | `tests/product/test_comparisons.py`（新） |
| B-3 | 自有采集：`capture` / `stop_capture` / `CaptureLease` / 重启核对 | `tests/product/test_capture_lifecycle.py`（新） |
| B-4 | VAL-T02 框架：`CaseRunner`/`CrashInjector`/故障窗口 | `tests/product/test_contracts.py`、`test_faults.py`（新） |

顺序不能乱：B-2 依赖 B-1 的摘要；B-4 依赖 B-1—B-3 的真实产出（否则只能记 `not_run`）。

---

## 2. 切片 B-1 — 确定性分析与摘要

**权威**：04 §5（`analyze` 返回 `TaskAccepted`）、§6（1—9 步）、§7、§9 的 PERF-V02/03/05；
data §6.2（`PerformanceSummary`、`MetricValue`、`MetricDenominator`、`AdaptiveLongFrame`）；
state §3.2（`analysis` 的 `DomainResult` 映射）；error §3。

### 2.1 产出

- `contracts/performance.py`：`MetricValue`、`MetricDenominator`、`AdaptiveLongFrame`、
  `PerformanceSummary`，以及 `AnalyzeInput` 若 `contracts/tasks.py` 中的 `AnalysisInput` 需要
  新字段（尽量不改）。
- `performance.py`：`analyze(input, context)`（注册 handler 后经任务执行）与可独立测试的
  `Analyzer`（纯函数：帧 + 清洗规则 → 指标），**不要把统计塞进事务**。
- `application.py`：注册 `analysis` handler（`self.tasks.handlers["analysis"] = ...`）。
- `tests/product/test_metrics.py`。

### 2.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 入口 | `Application.register_task` 只有在 handler 注册后才接受 `analysis`；未注册时保持 A 的拒绝行为 |
| 输入 | `artifact_id` 必须是本人可见的 `CaptureArtifact`；`bindings` 每个都用 `require_analyzeable(ref, context.user_scope_id)`；开放段/开放绑定 → `PRECONDITION_UNVERIFIED` |
| 一致性 | artifact 的 `session_id` / `segment_ids` 必须覆盖所引绑定；不一致 → `REFERENCE_INVALID` |
| 清洗 | 按 process/swapchain/流/段-绑定分开；只保留连续 valid 区段内的相邻间隔；坏值、重复、倒退、未知缺口断开，**不跨坏值重连**；加载/菜单/切出/热身只有在给出明确区间与理由时才排除；真实尖峰不得按离群删除 |
| 平均率 | `m` 个间隔，`T = sum(d)/1000`，`F = 1000*m/sum(d)`；多段累加 `m/T`；保留每段明细 |
| 分位数 | `P95`/`P99` 取升序第 `ceil(p*m)` 项；最慢 1% 用最大 `ceil(0.01*m)` 项的倒均值；`m=0`、`T=0`、尾部不足 20 个样本 → `unknown` + 原因，**不输出 0 FPS** |
| 长帧 | 每连续段按原 04 自适应阈值定位；保存实际阈值、间隔数、簇数、长帧总时长；相邻超阈值间隔同簇，遇普通间隔或缺口结束 |
| 质量 | 少于 30 秒或 1000 个有效间隔至少 `limited`；`valid/limited → DomainResult code=LogQuality`，`invalid` 且保留原件 → `partial` |
| 产物 | 新 `PerformanceSummary`（不可变），`manifest_file_id` 指向本次清洗/断段/排除的 manifest；**不覆盖任何旧摘要** |
| 副作用 | 不切配置、不推进当前指针、不创建 Outcome、不发模型；`external_calls` 为 0 |
| 幂等 | 同 key 同内容 → 回原 `TaskAccepted`/原摘要；异内容 → `IDEMPOTENCY_CONFLICT`；纯冲突（开放修订、缺原件）不消耗 key |

### 2.3 失败测试

1. `[10,20,30,40]`：`F=40`、`P95=P99=40ms`；最慢 1% 倒均值算法值 `25`，但按 20 尾部样本门槛对用户为 `unknown`。
2. `[60,70,16,50,80]` + 共同阈值 50ms：超阈为 60/70/80，两簇，长帧总时长 210ms；`=50` 不计；`T=.276s`，`time_share=210/276`，样本短 → `limited`。
3. 坏值/重复/倒退/缺口：断开而不是重连，有效间隔计数与 `effective_duration_s` 只来自有效区段。
4. 多流无主流证据：只给候选与 `unknown`，**不合并统计、不冠名原生 FPS**（用 `conflicting-streams.csv`）。
5. 无有效间隔：每个指标 `numeric=unknown` 且带原因，摘要不得出现 0。
6. 开放段/开放绑定 → `PRECONDITION_UNVERIFIED`，零摘要。
7. 他人 artifact → `RESOURCE_NOT_FOUND`（不泄露存在性）。
8. 同 key 重放返回同一摘要 ID；异内容 409；纯冲突不消耗 key（用开放段先触发一次，再用同 key 成功）。
9. 第二次分析产生新摘要，旧摘要字节不变、当前指针不变。
10. artifact 与其绑定的会话/段不一致 → `REFERENCE_INVALID`。
11. `external_calls` 行数为 0。

### 2.4 完成定义

- `test_metrics.py` 与全量通过；提交 `feat(perf-t03): ...`，正文含证据模板。

---

## 3. 切片 B-2 — 比较协议与比较

**权威**：04 §4.2、§6 第 6—8 步、§7、§9 的 PERF-V03/04/07；
data §6.2（`ComparisonProtocol`、`PerformanceComparison`、`MetricComparison`）、§9；
state §2（`ThresholdMode`、`ComparabilityLevel`）。

### 3.1 产出

- `contracts/performance.py`：`CreateComparisonProtocolRequest`、`ComparisonProtocol`、
  `CompareInput`、`MetricComparison`、`PerformanceComparison`。
- `performance.py`：`create_comparison_protocol(request, context)`（同步，返回 `ResourceResult`）
  与 `compare(input, context)`；复用 `require_comparison_protocol` 的类型守卫语义。
- `tests/product/test_comparisons.py`。

### 3.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| fixed | `threshold_ms` 可 null → 后台展开项目默认 50ms；非空须有限且 `>0` |
| baseline_derived | `threshold_ms` 必须 null；两组基线引用均非空；summary 固定具体原件/流/范围，binding 集合与 summary 完全一致；开放绑定 → 拒绝 |
| 派生阈值 | 从冻结基线的**全部有效间隔**取中位数，实际阈值 `max(50ms, 2×中位数)`；禁止客户端自报阈值或计算结果 |
| 冻结 | 创建即冻结：事务外准备，短事务内复核文件 hash/引用可用性；`frozen_at` 由后台生成；同 key **不重算**、不改 `frozen_at`；改内容/规则/阈值必须新 key、新协议 ID，无原地修改 |
| 去重 | 重复 `baseline_summary_ids`、重叠原始区间 → 拒绝；缺原件或有效间隔 → 不冻结（`PRECONDITION_UNVERIFIED`） |
| 比较 | 读取协议**具体版本**；两侧按同一阈值严格大于重算；`K/T`、长帧时长/T 按 04 §6；阈值恰等不计 |
| 不可重算 | 原件不可得或超 120 秒 → 仅该项 `status=unknown`、`delta=null` 并写原因，其余指标仍比较；不为比较新建 TaskType、不重发外部调用 |
| 可比性 | 逐项检查游戏/构建、设备/驱动、场景、分辨率/FG/流、生效/配置、协议/清洗；关键项全通过且只声明设置变化 → `controlled`；可解释的少量次要差异 → `approximate`；关键未知/`mixed` → `descriptive`；单项不可比不得掩盖其他观察 |
| 事后探索 | `exploratory=true` 明确探索；`false` 时若后测早于冻结、冻结前已登记结果或先后无法证明 → `PRECONDITION_UNVERIFIED`；上传时间不证明采样时序；不得倒填 `frozen_at` |
| 副作用 | 不改变旧摘要、当前指针、旧协议；比较结果只能新建 |

### 3.3 失败测试

1. 空协议库：fixed 默认冻结 50ms；`threshold_ms=0`/负数/NaN → 拒绝。
2. 基线中位 40ms：derived 冻结 80ms，`frozen_at` 为后台时间；同 key 重放同 ID 且 `frozen_at` 不变；异内容 409、协议数不变。
3. `baseline_derived` 带 `threshold_ms`、空基线、开放绑定 → 拒绝且零协议。
4. 重复 summary ID、重叠原始区间 → 拒绝。
5. `[60,70,16,50,80]` 两侧共同阈值 50：两侧都计入尖峰，阈值恰等不计；`K/min`、`time_share` 正确。
6. 60 秒 2 簇 vs 120 秒 4 簇 → 都报 2 簇/分钟；零时长 → `unknown`。
7. 缺原件：仅该项 `unknown/delta=null` 且有原因，其余项仍出结果。
8. 事后非探索比较 → `PRECONDITION_UNVERIFIED`；`exploratory=true` 可通过且不得声称预先计划。
9. 配置 `unknown`/`mixed` → 最多 `descriptive`，不得 `controlled`。
10. `CompareInput.protocol_id` 传 `TestProtocol` id → `REFERENCE_INVALID`；传错版本 → 读具体版本而不是"最新"。
11. 旧摘要/当前指针字节与行不变；`external_calls` 为 0。

### 3.4 完成定义

- `test_comparisons.py` 与全量通过；提交 `feat(perf-t03): ...`。

---

## 4. 切片 B-3 — 自有采集、租约与安全收尾

**权威**：04 §1（能力门禁）、§3、§4.3（`CaptureLease`）、§5（`capture`/`stop_capture`）、
§7（停止/取消/进程退出行）、§9 的 PERF-V06；
state §2（`TaskType.capture`）、§3.1（取消与终态）、§6.1/§6.2；error §3（`ACTIVE_CAPTURE_EXISTS`）。

### 4.1 产出

- `game_app/migrations/0011_capture_leases.sql`（见 §5）。
- `performance.py`：`capture(input, context)`、`stop_capture(input, context)`、`CaptureService`。
- `adapters/presentmon.py` 或新 `capture_adapter` 接口：**只定义协议 + 桩**，
  不启动任何真实进程、不提权、不停止外部会话。
- `application.py`：注册 `capture` handler；`stop_capture` 是同步控制入口。
- `tests/product/test_capture_lifecycle.py`。

### 4.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 适配器门禁 | 只允许**已验证**适配器；未注册/未验证的 `adapter_id` → `INPUT_VALIDATION_FAILED`（`field_errors[path=adapter_id]`）；本批次测试只用注入桩 |
| 唯一活动采集 | 同会话已有 `queued`/`running` 采集时，即使不同 key 也 `ACTIVE_CAPTURE_EXISTS`，`resource_refs` 返回已有 task |
| 租约 | `CaptureLease` 状态仅 `prepared`/`owned`/`finalized`/`unresolved`；每次变化追加事件；`native_name` 由后台 task ID 生成；同一 session 的未 finalize 租约唯一 |
| 启动 | 工具启动前先登记 `owned` 意图；租约与任务同事务登记，提交后才允许执行 |
| 停止 | `stop_capture` 在短事务追加 `cause=capture_stop_requested` 事件（`from_state=to_state=running`），**不设 `cancel_requested`**；任务仍 `running` 到文件收尾 |
| queued 停止 | 按任务取消处理（`cancelled`） |
| 终态 | 终态任务返回现有结果，不新增效果；租约不因 Task 终态自动清除 |
| 归属核验 | 重启后只停止 `PID + 启动时刻 + native_name` 三者都能核验为本任务工具的进程；无法确认 → `PRECONDITION_UNVERIFIED`，**不启动第二次采集**、不用"停止全部会话"的命令 |
| 收尾 | 只有原件已发布、文件状态已登记才 `finalized`；否则 `unresolved` 并阻止新采集；输出未就绪 → 任务 `partial`/`retained_only`，不得假造最后区段时长 |
| 段闭合 | 采集得到的实际结束点用于闭合段/绑定（沿用 A 的双修订 + 后台时钟规则）；未知结束点保持开放 |
| 幂等 | 同 key 同内容回原响应；异内容 409；纯冲突不消耗 key |

### 4.3 失败测试

1. 同会话不同 key 再启动 → `ACTIVE_CAPTURE_EXISTS`，`resource_refs` 指向已有 task，任务数不变。
2. 未验证适配器 → 拒绝，无任务、无租约。
3. 正常生命周期：`prepared → owned → finalized`，每步一条事件；`finalized` 前不可被消费。
4. `stop_capture` 对 running：追加 `capture_stop_requested` 事件，`cancel_requested` 仍为 `false`，任务仍 running。
5. `stop_capture` 对 queued：任务转 `cancelled`，无租约残留。
6. `stop_capture` 对终态：返回原结果，不写新事件。
7. 重启核验：同 PID 不同启动时刻 → 不认为归属本任务，`unresolved` 阻止新采集。
8. 归属无法确认 → `PRECONDITION_UNVERIFIED`，且没有第二次启动调用（桩计数为 1）。
9. 输出未就绪 → 任务 `partial`，段不闭合、不产生假时长。
10. 租约不因 Task 终态清除：任务结束后未 finalize 的租约仍阻止新采集。
11. 同 key 重放同响应；异内容 409。
12. `external_calls` 为 0；没有真实进程被创建（桩记录启动调用次数）。

### 4.4 完成定义

- `test_capture_lifecycle.py` 与全量通过；提交 `feat(perf-t04): ...`。
- 报告必须写明：真实工具/游戏未启动，PERF-V06 的"真实 Windows/PresentMon"部分为 `not_run`。

---

## 5. 迁移 `0011_capture_leases.sql`

只追加。字段按 04 §4.3（含 `EntityHeader` 的对象仍写 `objects`，本表是可重建投影）：

```sql
CREATE TABLE IF NOT EXISTS capture_leases (
    task_id            TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    native_name        TEXT NOT NULL,
    process_pid        INTEGER CHECK (process_pid IS NULL OR process_pid > 0),
    process_started_at TEXT,
    file_id            TEXT NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('prepared','owned','finalized','unresolved')),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- One lease per session may be non-finalized: a prepared/owned/unresolved
-- lease blocks a second capture even when the first task has ended.
CREATE UNIQUE INDEX IF NOT EXISTS ux_capture_lease_active
    ON capture_leases(session_id)
    WHERE state <> 'finalized';

CREATE INDEX IF NOT EXISTS ix_capture_leases_session
    ON capture_leases(session_id, state);
```

> 若实现需要额外列，追加在本迁移内并同步更新本节与 `04-performance.md`。

---

## 6. 切片 B-4 — VAL-T02 确定性与故障框架

**权威**：08 §3/§4/§5/§9/§10（VAL-V02/03/08）；implementation-plan 的 VAL-T02 段。

**重要边界**：C/D/E 批次尚未实现，本切片只建立**框架 + 现有模块的可运行用例**。
不得把缺失模块写成 `passed`，不得宣称 VAL-T02 全量结案。

### 6.1 产出

- `tests/product/support/validation.py`：`CaseSpec`、`CaseResult`、`RunManifest`（测试工件，
  不写产品库）；`ScenarioRunner`（驱动 `Application`，**不走 HTTP**，Q1 未回答）。
- 扩 `tests/product/support/spies.py`：`SpyProvider` 增加"已执行但响应丢失"模式（现有
  `mode="unknown"` 已覆盖超时，补一个不返回但已计数的分支即可）；`CrashInjector`。
- `tests/product/test_contracts.py`：用例矩阵运行器。
- `tests/product/test_faults.py`：故障窗口。

### 6.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 状态 | `CaseResult.status` 仅 `passed`/`failed`/`not_run`/`blocked`；未执行不得写 `passed`；空集/跳过不等于全通过 |
| 工件 | 每条用例记录 `requirement_ids`、步骤、期望断言、mock 组件；断言必须可由记录/数值/调用计数验证 |
| 数量断言 | 对象数、事件数、绑定数、调用计数精确相等（不用 `>=` 掩盖多写） |
| 故障注入 | `CrashInjector` 只在临时库/临时目录的指定边界抛错；绝不触碰玩家库或 `runs/` |
| 故障窗口 | 至少覆盖：提交前崩溃、改名后提交前崩溃、响应丢失后同 key 重试、同 key 并发、文件被篡改、无检查点恢复、未知调用 |
| 诚实报告 | 报告列出全部 `not_run`/`blocked` 及其原因（缺哪个模块/哪个证据） |

### 6.3 失败测试（框架自证）

1. `CaseResult` 未执行写 `passed` → 被拒绝（构造校验）。
2. `ScenarioRunner` 跑 `PERF-V01`（开放段分析被拒）、`PERF-V09`（协议入口）等**已实现**用例，
   断言对象/事件数量；每个结果都进报告。
3. `CrashInjector` 在提交边界抛出 → 无部分事实；同 key 重试成功（复用 A 的确定性冻结文件）。
4. 响应丢失 + 同 key 重试 → 同 ID，供应商发送计数不增。
5. 文件被篡改 → `ARTIFACT_UNAVAILABLE` 或 `PRECONDITION_UNVERIFIED`，无消费。
6. 无检查点恢复 → 新 parent 任务仅一个，未知调用不重发。
7. C/D/E 相关用例标 `not_run` 且原因指向缺失模块，报告不得显示全绿。

### 6.4 完成定义

- `test_contracts.py`、`test_faults.py` 与全量通过；提交 `test(val-t02): ...`（若含产品代码修复用 `fix(...)`）。
- 报告中明确：VAL-T02 只完成框架与现有模块串联，**未结案**。

---

## 7. 完成定义（本批次）

- 四个切片各自提交、各自评审；`test_metrics.py`、`test_comparisons.py`、
  `test_capture_lifecycle.py`、`test_contracts.py`、`test_faults.py` 全部存在并运行过。
- 全量 `py -3.13 -B -m pytest -q -p no:cacheprovider` 全绿（≥ 490 + 新增）。
- `external_calls` 行数为 0；没有启动真实进程/游戏/工具。
- 每个切片评审的 Critical/Important 已在本会话内修完（先红后绿），Minor 记录不修。
- 推送 `origin/main`，工作区干净，本地不落后远端。

## 8. 明确不做

- 不实现 HTTP/CLI/页面（Q1）；不实现 KNW/REC/FBK/IMP 模块；不宣称 VAL-T02 全量结案。
- 不启用真实 PresentMon、不启动游戏、不查询真实 GPU、不使用真实模型。
- 不把 `not_run` 写成 `pass`，不补默认值（无 60 秒/3 次/1080p），不为审计插入未发生的状态。
- 不重做 A 已交付的 `inspect_log`/`associate_log`/协议创建/会话接口。

## 9. 提交与证据模板

按 `batch-2d-handoff.md` §5：

```text
task_id / spec_revision / prerequisite_tasks / decision_and_gate_status
changed_files / fixture_id_and_hash / focused_command / observed_exit_code
assertions_checked / provider_send_count / object_and_event_counts
evidence_level / not_run_or_blocked_cases / remaining_scope / commit_revision
```

- 目标命令：`py -3.13 -B -m pytest tests/product/<本切片文件> -q -p no:cacheprovider`
- 收尾命令：`py -3.13 -B -m pytest -q -p no:cacheprovider`
- 前缀：`feat(perf-t03):`、`feat(perf-t04):`、`test(val-t02):`；契约改动单独 `docs(perf-t03):`。

## 10. 报告

写入 `.superpowers/batch-runs/report-B.md`，格式同批次 A：

```text
批次：B（PERF-T03、PERF-T04、VAL-T02 框架）
基线：186cbb8（490 passed, 1 skipped）
提交：<每切片 sha + 一行摘要>
评审：<每切片 Critical/Important 数量与修复状态>
全量：<N> passed, 1 skipped
推送：<range> → origin/main @ <sha>
证据等级：offline_fixture
Rulings：<逐条，含 cost if wrong>
Deferred minors：<逐条>
Blocked：<Q1/Q2/G-* 相关跳过项及 VAL-T02 未结案说明>
下一步建议：批次 C
```
