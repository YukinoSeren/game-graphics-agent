# 批次 2D 设计：接线、回答接续与契约补全

> 日期：2026-11-02；基线 `0f5f77c`（`main`，领先 `origin/main` 10 个提交，未推送）；
> 产品契约 `product-contract/0.4`。
> 本文只覆盖"进入 PERF-T02 / KNW-T02 之前必须补齐"的 8 项，不设计任何新的领域功能。
> 字段/状态/错误的唯一权威仍是 `shared/` 三份契约与 `01`/`02a`/`07` 模块规格；
> 本文与其冲突时以规格为准，并先修规格再改代码。

---

## 1. 意图与为什么现在

2C 结束后，四条切片在**单元层面**是自洽的，但拼起来还差三个洞，任何一个都会让下一步建立在空壳上：

1. `game_app/application.py:30` 只构造了 `TaskService`，`handlers` 为空；而
   `tasks.py:665` 对未知任务类型回落到 `lambda ctx: HandlerOutcome.completed()`。
   今天任何 `knowledge_import` / `device_scan` 任务都会**空跑成功且不写任何事实**。
2. `tasks.py:596-611` 唤醒任务时把**原输入字节原样复制**，只加了 `cause_ref`；
   而全仓没有 handler 读取 `InformationResponse`。DEV-T04 的"接续"实际只是把任务
   退回 `queued`，重跑会重新探测、重新提问，玩家刚给的答案被丢弃。
3. `ImportSnapshot` 的结果码要求同时有可用记录和已解析 `game_id`，而
   `IdentityMapping` 只能由 `IdentityService.resolve_game_identity` 写入，且要求
   verified 证据。导入侧只读不写，因此**所有真实导入永远停在 `retained_only`**，
   KNW-T02 组包拿不到可用事实。

另有两处诚实性缺口和一处审计缺口（归属、发布事件、phase 归约），一并处理。

## 2. 目标 / 非目标 / 反目标

**目标**

- 应用层可真正执行已实现的两种任务，并对未实现的类型**明确失败**而不是假成功。
- 玩家回答后重跑的任务**消费回答**，不再重问；关键项闭合后能走到 `completed`。
- 导入存在一条受控的身份映射写入路径，使 `published` 可达。
- `DeviceSnapshot` 有归属，采用/回答可校验。
- 导入发布有一条可审计的创建事件。

**非目标**

- 不实现 PERF/KNW/REC/FBK 任何领域功能；不新增会话、绑定、包、推荐。
- 不做 HTTP/CLI 路由（ENG-T04），不碰 Q1。
- 不启用任何真实适配器、模型、硬件（G-* 门禁无证据）。

**反目标（即使技术通过也算失败）**

- 为了"让演示跑通"而给缺失 handler 补默认成功、手填 game_id、补默认值。
- 为了一条审计记录而伪造从未发生过的 `staging` 状态。
- 让回答路径通过重跑"碰巧"得到正确结果，而不是显式消费已存回答。

## 3. 约束

- 契约优先：任何新字段先写进 `shared/data-contracts.md`（0.4），再改代码。
- 每个行为先有失败测试；全量 `py -3.13 -B -m pytest -q -p no:cacheprovider` 必须全绿。
- 不联网、不调用模型；供应商发送计数恒为 0。
- 迁移只追加；编号从 `0009` 起。
- 同 key 幂等先于新提交的前置校验；纯冲突不消耗 key。
- 事务内不嵌套 `UnitOfWork`；文件复制不包进长事务。
- 存储对象回读用 `model_validate_json`。

## 4. 决策

### D1 应用层接线与缺失处理器守卫

`Application` 成为唯一的组装点，构造时注册**已实现**的 handler：

```python
class Application:
    def __init__(self, repository, *, file_root, provider=None, probe=None, clock=utc_now):
        self.files = FileService(repository, file_root)
        self.tasks = TaskService(repository, provider=provider, files=self.files, clock=clock)
        self.imports = ImportService(repository, self.files)
        self.tasks.handlers["knowledge_import"] = self.imports.handle
        self.devices = None
        if probe is not None:                     # 无适配器时不注册 device_scan
            self.devices = DeviceService(repository, self.files, probe)
            self.tasks.handlers["device_scan"] = self.devices.handle
        self.adoption = AdoptionService(repository, self.files)
        self.answers = AnswerService(repository, self.files, tasks=self.tasks)
        self.amendments = AmendmentService(repository, self.files)
```

新增的同步入口（薄包装，不复制领域规则）：
`adopt_configuration`、`record_historical_configuration`、`answer_information`、
`create_information_amendment`、`declare_import_identity`（见 D3）。

**守卫**：`Application.register_task` 先检查 `task_input.kind in self.tasks.handlers`，
缺失时抛 `INPUT_VALIDATION_FAILED`（`field_errors=[path="kind", code="enum",
message="no handler is registered for this task type"]`），**不创建任务**。
`TaskService.run_once` 的默认回落改为：

```python
handler = self.handlers.get(kind)
if handler is None:
    # 代码缺陷，不是业务失败；不得伪装成 completed。
    → _finish(failed, error_code=INTERNAL_ERROR, stop_reason=execution/validation_failed,
              note="no handler registered for <kind>")
```

`TaskService` 保留"测试可直接设置 handlers"的能力；守卫只在 `Application` 生效。

### D2 唤醒后消费回答

**D2.1 回答的可见形状。** `InformationResponse.answers` 保存玩家原样提交的值，其中既有
可归一值，也有无法归一的原话，handler 不能把它整体当已知值。新增三个字段
（`data §8.2`，均由后台生成、同事务写入）：

```python
answered_fields: list[str]                 # 本次提交中成功归一为已知值的路径
unresolved_fields: list[str]               # 本次提交中保留原话、未进入已知值的路径
answered_values: dict[str, Scalar]         # 上述 answered_fields 的归一值
```

- 三者都是**本次响应自己的**结果，不是请求投影的累积状态。
- `answered_values[path]` 是归一后的值：冲突选择时是被选中的候选值，不是
  `answers[path]` 的原始提交值；跳过与未归一的路径不出现在这里。
- 三者互不交叉，且都必须是本请求字段域的子集；`answers` 保持原样不变。

**D2.2 读取路径。** 回答经 `cause_ref` 链不可达：`_should_wake` 只在**所有关键字段**
闭合时才调用 `append_input_revision_and_wake`（`answers.py:341-355`），而该调用是
`task_inputs.cause_ref` 的唯一写入者。只闭合部分关键字段、或只答非关键字段的回答
不会留下 `cause_ref` 行，重跑就会重新探测并重问。

因此新增**持久关联投影**（`0009`）：

```sql
CREATE TABLE task_responses (
    task_id     TEXT NOT NULL,
    sequence    INTEGER NOT NULL CHECK (sequence >= 1),
    response_id TEXT NOT NULL UNIQUE,
    request_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, sequence)
);
```

`AnswerService` 在写回答的同一事务内插入一行（`task_id = request.source_task_id`，
`sequence` 为该任务当前最大值 + 1）。**仅 `continuation_mode=source_task` 的响应
写入该表**：`facts_only` 更正不得让源任务在重跑时消费它，否则等于间接改写源任务
（state §7.2 的零写入规则）。`TaskContext` 增加：

```python
@property
def answered_values(self) -> dict[str, AnsweredValue]:
    """按 sequence 升序合并本任务所有回答；后到的响应覆盖先到的。
    只取 answered_values；若某路径在更晚的响应里被判 unresolved，则删除该路径
    （后来的"无法归一"否定先前值，不保留陈旧值）。"""
```

`AnsweredValue` 是本地 dataclass：`value: Scalar`、`response_id: str`、
`field_path: str`。保留 `response_id` 是为了让派生快照能引用**具体**的那次回答，
而不是只写一个无出处的标量。

**D2.3 DeviceService 的叠加与不重问。** `DeviceService.handle` 改为：

1. 取本任务已存回答 `answered = context.answered_values`。
2. 取该设备序列的最新快照（D2.4）作为基线；把 `answered` 中属于本次请求字段域的
   路径写成已知值：值为 `AnsweredValue.value`，并**追加一条
   `AcquisitionAttempt(method=user_provided, status=succeeded,
   information_response_id=AnsweredValue.response_id)`**，provenance 用
   `EvidenceRef(kind="local", entity_type="InformationResponse",
   entity_id=response_id, field_path=path)`。基线原有的 attempts/provenance 全部保留。
3. 只探测 `paths - answered.keys()`；已回答路径既不探测也不再提问。
4. `unknown_paths = paths - known`；为空时**不再 `suspend`**，按全已知给
   `available/completed`；部分已知给 `limited/partial`；全未知且无回答给
   `unavailable/failed`（沿用现状）。
5. 提问前执行**循环守卫**：若新请求的字段集合与"本任务最新一条请求"的字段集合完全
   相同，且 `answered` 相对上次没有新增或删除任何路径，则不再创建问题，任务以
   `failed` + `stop_reason=execution/input_unresolved` 结束，`last_error_code` 记
   `PRECONDITION_UNVERIFIED`。重复问同一组字段既不会带来新信息，也会让玩家陷入
   无限等待。

**D2.4 稳定的设备序列（DEV-T01 修正）。** 现在每次扫描都 `series_id=new_id()`、
`device_series_id=new_id()`（`device.py:373-375`），"设备序列"只有一版，重跑必然另起
一炉。改为用**槽投影**确定序列（与 `local_setting_slots`/`game_declaration_slots`
同一模式）：

```sql
CREATE TABLE device_series_slots (
    user_scope_id   TEXT NOT NULL,
    scope_key       TEXT NOT NULL,   -- "installation:<id>" / "context:<id>" / "user"
    series_id       TEXT NOT NULL,
    device_series_id TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (user_scope_id, scope_key)
);
```

- `scope_key`：任务输入有 `game_context_id` 时，若该 context 绑定安装则
  `installation:<installation_id>`，否则 `context:<game_context_id>`；两者皆无时用
  常量 `user`（每个用户一条设备序列）。
- 首次扫描创建槽与新的两个 id；后续扫描读取槽，`version` = 该序列最新版本 + 1。
- 槽只由 `DeviceService` 在写快照的同一事务内维护。

**D2.5 后续 `unresolved` 否定先前值。** 同一路径在响应 1 已答、响应 2 被判
`unresolved` 时，`answered_values` 不再返回该路径，字段回到待问状态。理由：玩家后一次
的"无法归一"是对先前值的否定，保留陈旧值等于用旧答案冒充新事实。

### D3 采集运行的身份映射写入路径

`IdentityService` 增加同步入口（02 所有，`data §3.1` 补 `declared_by_user_scope_id`）：

```python
class ImportIdentityMappingRequest(StrictModel):
    run_id: str              # run.json 的 id，原字保留
    legacy_game_id: str      # run.json 的 game_id，原字保留
    game_revision_id: ID     # 玩家为该运行指定的 Game 版本

def declare_import_identity(self, request, context: MutationContext) -> ResourceResult
```

写入一条 `IdentityMapping`：

- `legacy_namespace = f"collector_run:{run_id}"`（与 `importer._resolve_game` 的
  查找键完全一致，不得改写）
- `legacy_value = legacy_game_id`
- `target_game_id = Game.series_id`
- `declared_by_user_scope_id = context.user_scope_id`
- `checks`：一条 `pass` 的 `identity.declared`（method = `player declaration`，
  note 说明这是玩家为本次导入指定的游戏，不是已验证的外部身份）

守卫：

- Game 版本存在且对调用者可见：`provisional` 必须是本人声明；`verified` 公开；
  `legacy_unverified` 是旧记录读取视图，**拒绝**（`REFERENCE_INVALID`），不据它
  声明新映射。
- 同 key 同内容回原响应，异内容 `IDEMPOTENCY_CONFLICT`。
- **同 `(namespace, legacy_value)` 已指向同一游戏**：返回既有映射的 id，不再新建
  记录——重复声明不产生新的 `mapping_context_hash`，因此也不会多出一个
  `ImportSnapshot`。
- 同一 `(namespace, legacy_value)` 已指向**另一**游戏：抛 `REFERENCE_INVALID`
  （`field_errors=[path="legacy_game_id", code="conflict"]`），**不覆盖既有映射**。

**使用侧的所有权**（这是安全边界，不是可选项）：`ImportPublisher._identity_mapping`
在按 id 读取后校验 `declared_by_user_scope_id`——非 `null` 且不等于提交者时按
`RESOURCE_NOT_FOUND` 处理（不泄露他人声明是否存在）；`null` 表示 `verified` 等公共
映射，任何用户可用。`_resolve_game` 只使用已通过该检查的映射。

路由 `POST /api/import-identity-mappings`。

该映射进入 `KnowledgeImportInput.identity_mapping_ids` 后，`mapping_context_hash` 改变，
生成带 `parent_import_snapshot_id` 的新快照——这正是 2C 已实现的行为，无需改动导入侧。

### D4 `DeviceSnapshot` 归属

`DeviceSnapshot` 增加 `user_scope_id: ID`（`data §3.2`），由 `DeviceService` 从
`context.user_scope_id`（持久任务所有者，非调用参数）写入。

- `AdoptionService._device_snapshot` 增加归属校验：不属于调用者的快照按
  `RESOURCE_NOT_FOUND` 处理（与安装一致，不泄露存在性）。
- `AnswerService._derive_snapshot` 只取本任务所有者的序列；`_require_task_owner`
  已挡住跨用户回答，这里再加一道，避免将来直接调用。

因为 `DeviceSnapshot` 是 0.4 才引入的类型，没有 0.1—0.3 旧对象，字段直接设为必填，
不需要兼容视图。**若将来存在旧快照，先加读取视图再放开必填**，不得反向放宽。

### D5 发布事件的诚实记录

新增 `domain_events` 表（07 §4 已设计、未实现），并在 `ImportPublisher` 的发布事务里
写**一条创建事件**：

| 字段 | 值 |
|---|---|
| `target_type` / `target_id` | `ImportSnapshot` / 快照 id |
| `dimension` | `publication` |
| `sequence` | 1 |
| `previous_state` | `null` |
| `next_state` | `published` 或 `rejected` |
| `cause` | `import_published` / `import_rejected` |
| `related_refs` | 声明文件 + 冻结原件（`ManagedFile`） |

**明确不做两段式 staging**：本切片没有消费者能观察到中间态，插入一条 `staging`
记录再改状态，是为了日志而发明一个从未存在的事实。`previous_state=null` 精确表达
"该对象以该状态诞生"。若将来出现 staging 消费者，再按 state §8.1 补真实的两阶段。

### D6 `TaskProgress.phase` 归约

- `TaskContext.phase(key)` 追加一条 `from_state == to_state` 的事件，`cause = f"phase:{key}"`，
  `key` 限定为 07 §5 的词表：`acquire/collect/extract/validate/decide/capture/analyze/finalize`。
- `_phase_for` 改为：优先取最近一条 `phase:<key>` 事件的 key；否则按现有状态回落
  （`queued` / `waiting` / `terminal`）。
- `DeviceService` 至少发 `phase:acquire`、`phase:validate`；`ImportService` 发
  `phase:extract`、`phase:validate`。不追求全覆盖，但已发出的必须真实。

### D7 已知债务裁决

| 债务 | 裁决 | 理由 |
|---|---|---|
| 并发同 tripe 导入落败方的声明文件（Minor 15） | **不改** | 该文件由落败方自己的 `file_operations` 行（`phase=committed`）可达，不是无主文件；清理属 ENG-T03 的 `abandon` 路径。加一条测试断言它可经 `file_operations` 定位。 |
| 回答归一只认设备路径（Minor 18） | **重构为注册表** | 抽出 `_FIELD_NORMALIZERS: dict[str, Callable[[Scalar], Scalar | None]]`；未知路径仍判 `unresolved`（绝不猜），但 PERF 加字段时只需加一条登记。 |
| 来源映射丢弃 `document`/`content_hash`/`truncated`（Minor 19） | **修，但不新增文件写入** | `document` 是内联的完整 `Document` 对象（`schema.py:252`）。冻结组里的 `sources.json` 原件本身已是受管理文件，内联文档已逐字保留；再为它单独写一份 `ManagedFile` 只会在 rejected 导入时留下无主文件。因此：`document.content_hash → SourceRevision.content_hash`、`document.truncated → SourceRevision.truncated`、`document.retrieval_method → capture_method`，`document_file_id` 保持 `null`，并追加一条 `source.document` 的 `not_run` 检查，注明"内联文档只存在于冻结原件中，未单独登记为受管理文件"。 |
| 玩家声称 `active_confirmed` 未核验（Minor 20） | **修归约** | 本切片没有任何核验来源（`SettingDeclaration.observation_id` 由后台核验字段/安装/时段，尚未实现），因此**每一条** `active_confirmed` 都在聚合层归约为 `unknown`：单项 `ConfirmedSetting.activation_state` 保留玩家声明，`AppliedConfiguration.activation_state` 记 `unknown`，并追加 `applied.activation` 的 `unknown` 检查，note 说明"没有可核验的生效来源"。将来实现核验后，只有核验通过的项才允许进入 `active_confirmed` 归约；`observation_id` 是预留入口，本切片不消费。 |
| `get_current_applied` 返回 `revision=0` 哨兵（Minor 21） | **修返回类型** | 改为 `tuple[str | None, int | None]`；无指针时两者皆 `None`，不再造一个非法的 `Revision`。 |

## 5. 需要修改的契约与规格

| 文件 | 改动 |
|---|---|
| `shared/data-contracts.md` §3.1 | `IdentityMapping.declared_by_user_scope_id: ID?`（D） |
| `shared/data-contracts.md` §3.2 | `DeviceSnapshot.user_scope_id: ID`（R） |
| `shared/data-contracts.md` §8.2 | `InformationResponse.answered_fields: string[]`、`unresolved_fields: string[]`、`answered_values: map<string,Scalar>`（后台生成，R） |
| `shared/state-conventions.md` §8.1 | 明确"对象以某状态诞生时写 `previous_state=null` 的创建事件，不得伪造未发生的中间态" |
| `07-data-and-architecture.md` §4 | `domain_events`、`task_responses`、`device_series_slots` 落地为表；说明其只记录实际发生的状态 |
| `01-device-game-configuration.md` §2 | 设备快照归属；重跑扫描沿用同一设备序列（槽投影） |
| `02a-collector-integration.md` §5 | 导入前先由玩家为该运行声明游戏映射（D3），映射缺失仍可 `retained_only`；映射的使用受声明者所有权约束 |

## 6. 迁移

`0009_domain_events.sql`（事件表 + 两张可重建投影）：

```sql
CREATE TABLE IF NOT EXISTS domain_events (
    target_type    TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    dimension      TEXT NOT NULL,
    sequence       INTEGER NOT NULL CHECK (sequence >= 1),
    previous_state TEXT,
    next_state     TEXT NOT NULL,
    cause          TEXT NOT NULL,
    related_refs   TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (target_type, target_id, dimension, sequence)
);

CREATE TABLE IF NOT EXISTS task_responses (
    task_id     TEXT NOT NULL,
    sequence    INTEGER NOT NULL CHECK (sequence >= 1),
    response_id TEXT NOT NULL UNIQUE,
    request_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (task_id, sequence)
);

CREATE TABLE IF NOT EXISTS device_series_slots (
    user_scope_id    TEXT NOT NULL,
    scope_key        TEXT NOT NULL,
    series_id        TEXT NOT NULL,
    device_series_id TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (user_scope_id, scope_key)
);
```

`DeviceSnapshot.user_scope_id`、`InformationResponse.answered_*`、
`IdentityMapping.declared_by_user_scope_id` 都在 JSON 载荷里，无需迁移。

## 7. 测试计划

新增/修改的测试文件与关键用例：

**`tests/product/test_application.py`（新）**

1. 构造 `Application(probe=stub)` 后 `device_scan` 与 `knowledge_import` 都可执行。
2. 未注册类型（如 `analysis`）注册被拒：`INPUT_VALIDATION_FAILED`，无任务行。
3. `TaskService` 直接 `run_once` 遇缺失 handler → `failed` + `INTERNAL_ERROR`，
   绝不 `completed`。
4. 无 `probe` 时 `device_scan` 注册被拒（而不是跑空）。

**`tests/product/test_information_answers.py`（扩）**

5. 回答关键项后重跑同一任务：不再探测已答路径，不产生第二条问题，任务 `completed`，
   快照值等于回答值且带新 attempt。
6. **部分回答（关键字段未闭合、因此没有 `cause_ref`）后重跑**：`answered_values`
   仍能读到该回答（经 `task_responses`），只问剩余字段，问题字段集合缩小。
7. 无进展重跑（同一字段集合、无新回答）：不建问题，任务 `failed` /
   `input_unresolved`。
8. `answered_fields` / `unresolved_fields` / `answered_values` 三者自洽：未归一原话
   只出现在 `unresolved_fields`，`answered_values` 里没有它。
9. 冲突选择：`answered_values[path]` 是被选中的候选值，不是 `answers[path]` 的原始值。
10. 先答后判 `unresolved`：`answered_values` 不再返回该路径，字段回到待问状态。
11. 派生快照里该值引用的 `AcquisitionAttempt.information_response_id` 就是产生它的
    那次响应。
12. `facts_only` 更正的回答**不**写入 `task_responses`：源任务重跑时不消费更正，
    且 `task_inputs` / `task_events` / `tasks.revision` 计数不变。

**`tests/product/test_device.py`（扩）**

13. 同一 `(user, installation)` 的两次扫描落在同一 `series_id`/`device_series_id`，
    `version` 递增；无安装时按 `context:` 键，两者皆无时按 `user` 键。
14. `DeviceSnapshot.user_scope_id` 等于任务所有者。

**`tests/product/test_import_identity.py`（新）**

15. 声明映射后导入得到 `published`，`game_id` 等于声明的系列。
16. 映射改指另一游戏被拒，不覆盖旧映射。
17. 同 key 重放同一映射返回同 ID；**不同 key 重复同一 (namespace, value, game)**
    返回既有映射 id，且不产生新快照。
18. 未声明映射的导入仍为 `retained_only`（现状保持）。
19. 使用他人声明的映射导入 → `RESOURCE_NOT_FOUND`，不产生快照。
20. `legacy_unverified` 游戏不能用于新声明。

**`tests/product/test_import_publish.py`（扩）**

21. 发布写一条 `domain_events`：`previous_state=null`、`next_state` 与状态一致、
    `related_refs` 含声明文件与冻结原件。
22. rejected 导入同样有一条创建事件。
23. 落败方的声明文件可经 `file_operations` 定位（Minor 15 的裁决依据）。

**`tests/product/test_adoption.py` / `test_historical_configuration.py`（扩）**

24. 他人 `DeviceSnapshot` 不能用于采用：`RESOURCE_NOT_FOUND`。
25. 声称 `active_confirmed` 时，单项保留声明、聚合 `activation_state=unknown` 且有
    `applied.activation` 检查。
26. `get_current_applied` 无指针返回 `(None, None)`。

**`tests/product/test_scoped_import.py`（扩）**

27. `sources.json` 的 `document.content_hash` / `truncated` / `retrieval_method`
    映射到 `SourceRevision`；`document_file_id` 为 `null` 且 `source.document` 检查
    为 `not_run`；不新增任何受管理文件。

**`tests/product/test_tasks.py`（扩）**

28. `phase:<key>` 事件改变 `TaskProgress.phase`；无 phase 事件时回落不变。

## 8. 切片与完成定义

| 切片 | 内容 | 目标测试 | 提交前缀 |
|---|---|---|---|
| 2D-A | D1 + D5 + D6 | `test_application.py`、`test_import_publish.py`、`test_tasks.py` | `feat(eng-t02):` / `feat(imp-t03):` |
| 2D-B | D2 + D4 + Minor 20/21 | `test_information_answers.py`、`test_device.py`、`test_adoption.py` | `feat(dev-t04):` |
| 2D-C | D3 + Minor 18/19 | `test_import_identity.py`、`test_scoped_import.py` | `feat(knw-t01):` |

每个切片的完成定义：目标测试与全量通过；`external_calls` 行数为 0；提交正文按
`batch-2c-handoff.md` §6 模板记录证据；不推送。

依赖：2D-A 先做（2D-B/C 的测试都经由 `Application` 或复用其守卫）；2D-B 与 2D-C
可并行。

## 9. 风险与明确不做

- **风险：D2 会改变 DEV-T01 的快照序列语义。** 既有测试若依赖"每次扫描新序列"需同步
  修正；这是修正而非放宽（原实现让 `device_series_id` 失去意义）。
- **风险：`task_responses` 是新的写入路径。** 漏写会让部分回答在重跑时被重新提问；
  测试 6 专门覆盖"关键字段未闭合、因此没有 `cause_ref`"的场景。
- **风险：D3 让玩家能把一个采集运行指向任一可见游戏。** 这是玩家的显式声明，检查为
  `pass` 且注明非外部验证。它只影响该用户自己的导入快照：映射带
  `declared_by_user_scope_id`，导入侧拒绝他人映射（测试 18），不产生公共事实。
- **不做**：HTTP/CLI 路由与鉴权（ENG-T04）、staging 两段式、`LogImportInput`（随
  PERF-T02 一起加）、会话/绑定/包/推荐、任何真实适配器、内联 source document 的
  独立文件登记。

## 10. 留给实施者

- `Application` 里各服务的字段命名与是否懒构造。
- 循环守卫的判定是否抽成独立小函数，以及"本任务最新一条请求"的查询写法。
- `answered_values` 的内部数据结构（合并语义已在 D2.2/D2.5 定死：按 `sequence`
  升序、后到覆盖、`unresolved` 删除）。
- `_FIELD_NORMALIZERS` 的登记形式（模块级常量 vs 类属性）。
- `domain_events` 是否同时服务 `current_applied_events`（本文裁决：不合并，
  指针事件已有专用表，保持单一来源）。
- `task_responses.sequence` 的分配写法（`MAX(sequence)+1` 在写事务内即可，无需额外锁，
  因为回答写入本身就在 `BEGIN IMMEDIATE` 内）。
