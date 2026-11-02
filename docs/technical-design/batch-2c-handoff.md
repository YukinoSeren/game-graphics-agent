# 批次二切片 2C 交接：IMP-T02 → IMP-T03、DEV-T03 → DEV-T04

> 日期：2026-11-02；基线提交 `acd104d`（`main`，领先 `origin/main` 6 个提交，未推送）；
> 产品契约 `product-contract/0.4`。
> 本文是**执行指令**，不是设计权威。字段/状态/错误的唯一权威仍是
> [`coding-handoff.md`](./coding-handoff.md)、[`implementation-plan.md`](./implementation-plan.md)、
> [`shared/`](./shared/) 三份契约与 `01`/`02a`/`04` 模块规格。
> 本文只写"从哪继续、按什么顺序、每步如何证明"，与规格冲突时以规格为准并先修规格。

---

## 0. 现状快照

**已完成并提交（自上一份交接 `24e5c52` 之后新增 5 个提交）**

| 任务 | 提交 | 交付 |
|---|---|---|
| ENG-T03 并发改名修复 | `0d028b9` | `FileService._write_finals` 接受"同 key 并发提交已放置相同字节"的共享冲突；不再是 `STORAGE_COMMIT_FAILED` |
| IMP-T01 冻结与格式识别 | `83a4851` | `game_app/importer.py`、`tests/product/test_import_freeze.py`、`tests/fixtures/product/collector-run-smoke/v1` |
| DEV-T02 安装与本机目录 | `88a5e42` | `device.ContextService`、`contracts/device.py` 新增 DTO、`tests/product/test_context.py` |
| PERF-T01 格式/单位/时钟/流 | `c75ba4b` | `game_app/adapters/presentmon.py`、`contracts/performance.py`、`tests/product/test_log_formats.py`、`tests/fixtures/product/offline-log-basic/v1` |
| 夹具字节保护 | `acd104d` | `.gitattributes`：`tests/fixtures/**/*.csv -text`，防止 `core.autocrlf` 改写夹具导致 manifest 哈希失效 |

**基线命令与期望**

```powershell
py -3.13 -B -m pytest -q -p no:cacheprovider
# 期望：263 passed, 1 skipped（跳过项为 Windows 上不生效的 POSIX 权限位断言）
```

另已用全新 `git clone` 验证：两份夹具逐文件字节与 manifest 一致，全量仍为 `263 passed, 1 skipped`。

**已完成部分可直接复用的能力**

| 能力 | 位置 |
|---|---|
| 冻结组读取 | `importer.FrozenInputReader.freeze(dir, user_scope_id=, idempotency_key=)` → `FrozenGroup(format_id, operation_id, file_ids: dict[相对名→file_id], manifest: FileManifest, inspection: FormatInspection)` |
| 冻结组内容回读 | `FrozenGroup.file_ids` + `FileService.read_managed_file(file_id)`；IMPORT-T02 只能经此读取冻结字节 |
| 格式识别 | `importer.FormatRegistry.inspect_format(_files)`；`FormatInspection.limitations` 为自由文本，不是结构化诊断 |
| 安装/上下文/本机目录 | `device.ContextService.prepare_game_context / create_installation / list_installations / register_local_setting / list_local_settings` |
| 本机目录原语 | `UnitOfWork.register_local_setting / list_local_setting_heads / get_local_setting_slot / list_local_setting_ambiguities / resolve_local_setting` |
| 更正链原语（DEV-T04 会用到） | `UnitOfWork.record_information_amendment / get_information_amendment_root`；`information_amendment_links.source_request_id` 已有 UNIQUE |
| 公共身份 | `knowledge.IdentityService.select_game / resolve_game_identity / assess_reuse` |
| 文件发布 | `FileService.publish(writer=..., hooks=PublishHooks(...))`、`store_artifact`、`read_managed_file` |
| 任务执行 | `tasks.TaskService`；`TaskContext.suspend / checkpoint / invoke_provider`；`DeviceService` 已能创建 `InformationRequest` 并把关键未知挂起为 `waiting_input` |
| 日志解析 | `adapters.presentmon.parse_log(...)`（纯函数，返回 `LogParseResult`，尚未发布 `CaptureArtifact`） |

**已知跨任务未落实项（不要误以为已完成）**

1. **IMP-T02/T03 完全未开始**：没有 `ImportSnapshot`、`ImportedRecord`、`Source`/`SourceRevision` 实现；
   批量 `IdentityMapping`（采集器运行命名空间）也未写。
2. **DEV-T03 完全未开始**：没有 `SettingRef`/`SettingDeclaration`/`ConfirmedSetting`/
   `AppliedConfiguration`/`PlayerDeclaration` 契约；没有 `current_applied` /
   `current_applied_events` 表（需新迁移，建议 `0006`）。
3. **DEV-T04 完全未开始**：没有回答提交入口；`tasks` 只有注册/恢复两条 `task_inputs` 插入路径，
   **没有"追加新输入修订"的路径**；没有 `waiting_input → queued` 的唤醒路径；更正链只有仓库原语，没有服务。
4. `contracts/tasks.py` 仍没有 `LogImportInput`、`AdoptConfigurationInput`、
   `HistoricalConfigurationInput` 的 Pydantic 模型（只有 shared 文档定义）。2C 按任务卡需要时再加入。
5. `TaskProgress.phase` 仍按状态粗归约，未按 `phase:<键>` 事件归约（07 §5）；不在 2C 范围。
6. `WindowsProbeAdapter` 在测试中从不运行；真实硬件未验证。
7. 2C 之后 PERF-T02（会话/绑定）、PERF-T03（分析/比较）、KNW-T02（包发布）仍未开始；
   `CaptureArtifact` 契约已存在但没有任何服务发布它。

---

## 1. 新会话启动流程（每个任务都照做）

1. `git status --short`、`git log -1 --oneline`、`git branch --show-current`；确认 HEAD 为上一任务提交。
   **保护已有改动，不整库 reset。** 工作区应只有两份未跟踪的交接文档（如仍在）。
2. 读本切片对应的权威段落（见各任务"先读"），不读就写代码＝返工。
3. 运行基线，记录 `263 passed, 1 skipped` 之外的任何变化。
4. **先写失败测试**（`superpowers:test-driven-development`），运行并确认"因行为缺失而失败"，
   不是因拼写/导入错误。
5. 最小实现；目标测试通过后跑**全量**测试。
6. 按 §6 模板记录证据，提交（一个任务一个提交），不 force、不推送除非用户要求。

**不要做的事**：不新建平行计划/GSD 结构；不联网安装依赖；不调用真实模型/供应商/硬件/游戏；
不把未运行写成通过；不在测试中读取玩家真实库或 `runs/` 金样本（只读副本或临时目录）；
不为"让演示通过"手填 ID、补默认值、追填历史或自动重发外部请求。

---

## 2. 切片 2C-A — IMP-T02 作用域与导入记录

**权威**：02a §4.1/§5/§7/§10；data §4.1；KNW-T01 身份映射；IMP-V02/05。

**依赖**：IMP-T01 ✅、KNW-T01 ✅、ENG-T01/03 ✅。**可与 DEV-T03 并行。**

### 2.1 目标与边界

消费 IMP-T01 的冻结字节与 KNW-T01 的身份映射，产出 `Source`/`SourceRevision`、
`ImportedRecord`（含 `upstream_envelope`）与诊断记录。**不发布 `ImportSnapshot`**（那是 IMP-T03），
不建知识包（KNW-T02），不发模型。

产出：

- `game_app/importer.py` 中的 `ScopedReferenceMapper` 与 `ImportValidator`（名字可调整，职责不变）。
- `tests/product/test_scoped_import.py`。
- 需要时新增 `contracts/knowledge.py`（或新 `contracts/imports.py`）中的 `Source`、
  `SourceRevision`、`ImportedRecord`、`RecordExtension`、`BoundarySemantics`。

### 2.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 唯一键 | `(import_snapshot_id, capture_scope_id, record_type, local_record_id)`；**禁止 last-write-wins**。重复键登记为冲突并保留诊断 envelope |
| 记录身份 | `record_snapshot_id` 即 `EntityHeader.id`，不另分配第二身份 |
| 作用域 | `capture_scope_id` = 原 `row.reference_scope`；`local_record_id` = 原 `row.local_id`；`legacy_record_id` = 原 `row.id` 原样；**不得按 `row.id` 冒号拆作用域** |
| 两段式解析 | 先分配全部类型化节点，再解析 payload 内的 setting/configuration/environment/evidence 引用；查询只用四元键 |
| 坏引用隔离 | 坏引用只隔离该记录及依赖它且无法自洽的记录；其余完整记录保留；诊断 envelope 保留原行 |
| 状态不升级 | 原 `accepted` 不因导入升级；原 `quarantined` 不因复制成功升级；`superseded_by` 只在本作用域/源修订内解析，找不到目标保留未知，不借别运行同名对象 |
| 两种 hash | `payload_hash` = 规范 JSON（UTF-8、键排序、紧凑、保留数值类型）；`manifest` 用原始字节 hash；两者不可互换 |
| 原值保留 | `payload` 是原行 payload 的类型化只读视图；`upstream_envelope` 保存原行全部字段（状态/issues/检查/提取/适用性/`superseded_by`） |
| 模型诚实 | 原产物没有实际返回模型时 `returned_model=null`，不拿请求名回填 |
| 不造历史 | 缺运行事实不得造 `TaskRun`；`interrupted` 原样保留 |

`RecordExtension={boundary_semantics:BoundarySemantics?, setting_identity_ref:ID?, checks:CheckResult[]}`；
旧 `NumericValue` 无边界包含标志时 `lower_inclusive/upper_inclusive=null`、`status=unknown`。

### 2.3 先写这些失败测试

1. 两 run、两 source 出现同一 `local_id`：生成两条 `ImportedRecord`，不合并证据、不跨源补引用。
2. 同组重复键（同 `capture_scope_id`/`record_type`/`local_id`）：拒绝或隔离，不得后者覆盖前者。
3. 坏 setting 外键：该记录与依赖它的记录进入诊断，其余记录保留。
4. `superseded_by` 目标缺失：保留未知/诊断，不跨运行填补。
5. 隔离记录传递：A 依赖坏 B，则 A 也被隔离；C 不依赖则保留。
6. 原 `quarantined` 复制成功后仍为 `quarantined`；原 `accepted` 导入后不被"升级"为可用事实。
7. 原产物缺实际模型：`returned_model` 为 null 且有说明。
8. `payload_hash` 与 manifest 字节 hash 不同且各自可复算。

### 2.4 完成定义

- `py -3.13 -B -m pytest tests/product/test_scoped_import.py -q` 与全量通过。
- 四元键在实现层真正唯一（迁移或事务内核对二者择一，需在提交正文说明选择与理由）。
- 原件/原 envelope 逐字可回读；无网络、无模型，供应商发送计数 0。
- 提交信息按 `feat(imp-t02): ...`。

---

## 3. 切片 2C-B — IMP-T03 发布与故障恢复

**权威**：02a §5/§6；state §8；07 §4/§6；IMP-V03。

**依赖**：IMP-T02 ✅、ENG-T02/03 ✅。

### 3.1 目标与边界

把 IMP-T02 的 staging 结果发布为 `ImportSnapshot` 与可用记录，提供三元指纹去重与故障恢复；
`DomainResult.code` 为 `published` 或 `retained_only`（state §3.2）。不发模型。

### 3.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 三元去重 | `(import_fingerprint, mapping_version, mapping_context_hash)` 相同 → 返回原导入；**不得因重试绕过 `rejected`** |
| 指纹 | 规范排序的相对名 + 字节数 + sha256；原件字节不变则指纹不变 |
| 上下文 hash | 覆盖 `identity_mapping_ids` 与检查规则版本；只改身份映射/规则 → 新 `mapping_context_hash` + `parent_import_snapshot_id`；原件变 → 新指纹 |
| 文件窗口 | 复用 `FileService.publish`：改名后未提交 = 孤立文件不可消费；提交后丢响应 = 同 key 回原 ID |
| 状态 | `staging → validated/rejected → published`；`rejected` 保留原件与原因，重试取原诊断 |
| 结果码 | 无可用记录/无映射 → `retained_only`（任务 `partial`）；有可发布记录 → `published`（`completed`） |
| 不重发 | 导入全程供应商发送计数 0；不把产品导入任务伪装成原采集任务 |
| 声明文件 | 快照 manifest 是受管理文件；`declaration`/`manifest` 引用与发布同事务 |

**实现陷阱（已在本仓库踩过，务必遵守）**

- `FileService.publish` 先调用 `writer`，再写 `ManagedFile` 对象。因此在 `writer` 内
  `uow.insert_reference(..., ObjectRef(entity_type="ManagedFile", ...))` 会因目标不存在而
  `REFERENCE_INVALID`。DEV-T02 的做法是**把文件 ID 存进对象字段**（`declaration_file_id`），
  不建引用行；IMP-T03 需要引用时用同一模式，或在 writer 之后另建引用。
- 同一 key 的重试必须复现 manifest 中写明的 ID：DEV-T02 用
  `uuid5(namespace, user:route:key:request_hash:role)` 派生；IMP-T03 若在 manifest 里命名
  `ImportSnapshot`/`Record` ID，应采用同样的确定性派生，否则崩溃恢复会准备出第二份不同的声明。
- 已登记 key 的幂等语义优先于新请求的引用/结构校验：先判断 `find_idempotency` 命中，
  再做"新提交"的前置校验；否则异内容同 key 会退化成 422 而不是 409。

### 3.3 先写这些失败测试

1. 并发相同输入、不同提交：只产生一个 `ImportSnapshot` 身份，两侧返回同一 ID。
2. 改名后进程退出（`PublishHooks.after_rename` 抛异常）：文件孤立不可消费，重试复用同文件。
3. 提交后丢响应（`after_commit` 抛异常）：同 key 重放返回原响应/原 ID，不新建。
4. `rejected` 输入重试：不新建、不绕过 rejected、不重算为成功。
5. 只改 `identity_mapping_ids`：新 `mapping_context_hash`、`parent_import_snapshot_id` 指向旧快照。
6. 原件字节改变：新 `import_fingerprint`，不关联旧快照。
7. 导入不发模型：`external_calls` 行数为 0。
8. 无可映射记录：`retained_only` + `partial`，原件仍可读。

### 3.4 完成定义

同上；目标测试 `tests/product/test_import_publish.py`；提交信息 `feat(imp-t03): ...`。

---

## 4. 切片 2C-C — DEV-T03 采用与历史事实

**权威**：01 §5（采用/历史）；state §4.1—§4.4；data §5.2；DEV-V03—05、DEV-V07。

**依赖**：DEV-T02 ✅、ENG-T01/02/03 ✅。**可与 IMP-T02 并行。**

### 4.1 目标与边界

实现 `adopt_configuration` 与 `record_historical_configuration`；只登记玩家声明与采用事实，
不写游戏/驱动文件、不执行设置。采用事务推进当前指针；历史登记**不碰指针、不写绑定**。

产出：

- `game_app/device.py`（或新 `game_app/adoption.py`）中的 `AdoptionService`。
- `contracts/device.py` 或新 `contracts/configuration.py`：`SettingRef`（knowledge/local 联合）、
  `SettingDeclaration`、`ConfirmedSetting`、`SettingRefReplacement`、`AppliedConfiguration`、
  `PlayerDeclaration`。
- 新迁移 `game_app/migrations/0006_current_applied.sql`：
  `current_applied`（unique `(user_scope_id, installation_id)`）与
  `current_applied_events`（unique `(scope, installation, pointer_revision)`）。
- `tests/product/test_adoption.py`、`tests/product/test_historical_configuration.py`。

### 4.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| origin 守卫 | 严格按 state §4.1 表；`baseline`/`recommendation`/`change_set`/`manual_edit`/`rollback` 各自必填/必空，不得混用 |
| 期望当前 | `expected_current_applied_id` 与指针比较；冲突 `CURRENT_CONFIGURATION_CONFLICT`，**零部分写** |
| change_set 合并 | 从 `ChangeSet.base` 完整快照合并差异；未改项/未知/自定义/预设原样保留 |
| manual_edit | 从固定当前 base 完整复制；无当前版本先建 baseline |
| 引用替换 | `setting_ref_replacements` 仅 `manual_edit`；旧值/单位逐字核对、同 series 严格后继、一对一、不成链；错误按 01 §5.2 分流（`LOCAL_SETTING_CONFLICT`/`REFERENCE_INVALID`/`PRECONDITION_UNVERIFIED`） |
| 恢复 | 新建版本：`parent` = 恢复前当前，`restored_from` = 目标；不把旧 ID 重新设成新事实 |
| 历史登记 | 独立版本族 `version=1`、`registration_kind=historical_record`、`origin=baseline`；`recommendation_id`/`parent_applied_id`/`change_set_id`/`restored_from_applied_id` 全 null；**不检查 expected_current、不写 CurrentAppliedEvent/Pointer、不写绑定**；`adopted_at` 无依据即 null，`created_at` 为登记时间 |
| 同事务 | `PlayerDeclaration`、采用版本、父/变更/恢复引用、`CurrentAppliedEvent`、`CurrentAppliedPointer`、幂等原响应一次提交；提交后才对外返回新 ID |
| 声明 | `PlayerDeclaration.input` 为 `AdoptConfigurationInput`/`HistoricalConfigurationInput`；至少一项已确认或显式预设，否则拒绝 |
| 冲突不消耗 key | 纯冲突不写幂等；同 key 异内容 `IDEMPOTENCY_CONFLICT` |

**契约缺口提醒**：`AppliedConfiguration.settings` 需要 `ConfirmedSetting`；`SettingRef` 的
`knowledge` 分支用 `KnowledgeRecordRef`（IMP-T02 产出），`local` 分支用本机定义 ID。
DEV-T03 在 IMP-T02 尚未完成时，测试可只用 `local` 分支，但**不要**为了跑通伪造
`KnowledgeRecordRef`。

### 4.3 先写这些失败测试

1. 两个客户端都以 `expected_current_applied_id=A` 并发采用：一个成功、一个 409，且冲突方零部分写。
2. 恢复历史目标：新建版本，`parent`=当前，`restored_from`=目标，目标 ID 不被复用为新事实。
3. 当前为 B 时补录过去 A：A 为独立版本族 `version=1`、`origin=baseline`，**当前指针仍为 B**，无绑定写入。
4. `manual_edit` 无映射却声明同 series 另一 ID → 拒绝；有映射但旧值/单位不符 → 拒绝。
5. `setting_ref_replacements` 出现链式/重复/跨安装 → 按 01 §5.2 分流错误。
6. 非 `manual_edit` 的 origin 带非空替换 → 拒绝。
7. 历史登记缺时段依据 → `PRECONDITION_UNVERIFIED`，不生成假历史配置。
8. 未知配置（无当前版本）会话/采用：不造空采用对象、不写指针。

### 4.4 完成定义

- `tests/product/test_adoption.py`、`tests/product/test_historical_configuration.py` 与全量通过。
- 提交信息 `feat(dev-t03): ...`；正文写明新迁移与指针唯一实现方式。

---

## 5. 切片 2C-D — DEV-T04 回答归一与接续

**权威**：01 §5.3；state §7.1—§7.3；data §8.2;DEV-V01/05/08、B-STA-13。

**依赖**：DEV-T01 ✅、DEV-T03 ✅、ENG-T02 ✅、`record_information_amendment` ✅。

### 5.1 目标与边界

实现 `validate_information_answer`、回答落库与等待任务接续，以及
`create_information_amendment`（D-P04）。回答只生成新事实，不推进当前采用指针、
不改历史绑定、不回写终态任务。

产出：

- `game_app/device.py`（或新 `game_app/answers.py`）中的 `AnswerService` / `AmendmentService`。
- `game_app/tasks.py` 增加"追加输入修订 + 唤醒等待任务"的内部路径（当前只有注册/恢复两条插入路径）。
- `tests/product/test_information_answers.py`。
- `InformationAmendment` DTO（`ENTITY_TYPES` 已登记该类型；`information_amendment_links`
  表及其 `source_request_id` UNIQUE 已存在）。

### 5.2 必须落实的规则

| 规则 | 实现要求 |
|---|---|
| 双版本校验 | `expected_request_version` 与 `expected_context_revision` 都要核对；旧版本 409，**不部分落库** |
| 答案归一 | `answers` 路径/类型必须属于本请求字段域；未知自然语言不能算 answered → `unresolved`；`answers` 与 `skipped_fields` **不得交叉** |
| 冲突选择 | `conflict_selections` 索引对应候选；选中后新快照保存 `FieldResolution`，保留旧依据；`value=conflicting` 时 `value=null` |
| 状态归约 | 全 unanswered→`open`；有回答/跳过但仍有未处理→`partially_answered`；无未处理且存在 skipped→`skipped`；全 answered→`resolved` |
| 接续 | 仅 `continuation_mode=source_task` 且源任务非终态：关键字段 answered/skipped 后 `waiting_input → queued`，沿用剩余预算；`facts_only` 不排队 |
| 终态后补 | 校验原历史上下文；保存回答与新事实；**不改终态任务输入/成果、不复活** |
| 取消竞态 | 事务内状态为准：取消先提交→终态后补；回答先提交→queued 后再取消 |
| 更正创建 | 源须为链头 `resolved`/`skipped` 的当前两版本；每源一个直接后继（表已有 UNIQUE）；`amendment_root_ref` 固定根快照；根字段域不缩小，允许未出现在直接源 `fields` 的根字段 |
| facts_only | 新问题 `version=1`、`expected_context_revision=1`、`continuation_mode=facts_only`、`amends_request_id` 非空、`amendment_root_ref` 非空；**不写任何 `TaskInputRevision`/`TaskEvent`/`TaskRun`** |
| 更正冲突 | 源 superseded/已有后继 → `INFORMATION_CONTEXT_CHANGED` 且指后继；尚 open/partial → `INFORMATION_REQUEST_STATE_CONFLICT`；纯冲突不消耗 key |
| 声明 | 更正创建写 `InformationAmendment` + 新 `InformationRequest` + 声明 manifest；manifest 固定根/直接源版本与上下文引用 |

**实现提醒**

- `DeviceService` 目前只创建 `InformationRequest` 并 `context.suspend(...)`；仓库没有
  "唤醒"路径。DEV-T04 必须显式追加 `task_inputs` 修订（内容为合并后的输入）并写
  `TaskEvent(cause=...)`，不要在 `tasks.py` 里偷偷改状态而不留事件。
- 存储对象回读必须用 `model_validate_json(stored.payload_bytes)`；
  直接 `model_validate(stored.payload)` 会因枚举/严格模式失败（本项目 `StrictModel` 为
  `strict=True, extra="forbid"`）。
- 回答产生的派生快照（新 `DeviceSnapshot`/`GameContext` 版本）与回答、问题修订同事务；
  但设置类的回答**不能**直接变成 `AppliedConfiguration`（必须另走 DEV-T03 入口）。

### 5.3 先写这些失败测试

1. 部分回答：未知项保留 `unanswered`，问题 `partially_answered`，任务仍 `waiting_input`。
2. `unresolved` 原话：无法归一的值保留原话，不进入 known 字段。
3. 冲突选择：选中候选后新快照带 `FieldResolution`，旧候选依据保留。
4. 明确跳过：无未处理项时 `skipped`；关键项已闭合的任务转 `queued`。
5. 过时 `expected_request_version` 或 `expected_context_revision`：409 且无部分写。
6. 取消竞态：取消先提交 → 终态后补不复活；回答先提交 → queued 后被取消。
7. `facts_only` 更正：源任务 `task_inputs`/`task_events`/`tasks.revision` 计数不变。
8. 两轮根域不同字段更正（R0 含 GPU/显存；第一轮改 GPU，第二轮从链头再选显存）：
   根域始终含两字段，第二轮可达且保留第一轮更正。
9. 每源一个直接后继：并发两个不同 key 只有一个成功，另一个返回后继定位。
10. 旧 `.0.3` 链缺根依据：拒绝，不猜根、不建半问题。

### 5.4 完成定义

- `tests/product/test_information_answers.py` 与全量通过。
- `facts_only` 分支的"零源任务写入"有可计数的断言（行数/事件数）。
- 提交信息 `feat(dev-t04): ...`。

---

## 6. 提交与证据模板

每个任务一个提交，正文含：

```text
task_id / spec_revision / prerequisite_tasks / decision_and_gate_status
changed_files / fixture_id_and_hash / focused_command / observed_exit_code
assertions_checked / provider_send_count / object_and_event_counts
evidence_level / not_run_or_blocked_cases / remaining_scope / commit_revision
```

- 目标命令：`py -3.13 -B -m pytest tests/product/<本任务文件> -q -p no:cacheprovider`
- 收尾命令：`py -3.13 -B -m pytest -q -p no:cacheprovider`（必须全绿）
- 提交信息前缀：`feat(imp-t02):`、`feat(imp-t03):`、`feat(dev-t03):`、`feat(dev-t04):`；
  修复提交按既有 `fix(...)` 风格。
- 推送与否则由用户决定；不 force、不覆盖远端。

**本仓库的具体注意事项**

- Windows + `py -3.13`；命令统一加 `-B -p no:cacheprovider`。
- **夹具字节**：`.gitattributes` 已把 `tests/fixtures/**/*.csv` 设为 `-text`；
  新增夹具后必须核对 manifest 的 `byte_length`/`sha256`，并可用 `git clone` 到临时目录复核。
  JSON 被强制 `eol=lf`，写入时保持 LF。
- **并发测试**：`test_files.py` 的并发用例历史上曾 flaky，已由 `0d028b9` 修；
  若再次偶发失败，先复现再改，不要靠重跑掩盖。
- **同 key 幂等的检查顺序**：先查已登记 key，再做新提交的前置校验（DEV-T02 的经验）。
- **事务边界**：`UnitOfWork` 内禁止嵌套 `unit_of_work()`；需要"事务内读"时用传入的 `uow`。
  短事务不包裹文件复制或网络。
- **不写半事实**：采用/回答/更正都必须"声明文件就绪 → 短事务重查守卫 → 原子写事实+幂等响应"。

---

## 7. 仍然阻塞 / 不在本轮

| 项 | 状态 |
|---|---|
| Q1 产品形态（本地网页） | 未确认；阻塞 ENG-T04/UX 全量 |
| Q2 首游戏/适配范围 | 未确认；不得预启用 FH6 等真实适配器 |
| Q3 确认方式/玩家测试 | 未确认；不影响离线实现 |
| G-WIN / G-GAME / G-FORMAT / G-MODEL / G-WEB / G-IMAGE / G-EFFECT | 均无真实证据；相关能力保持 `not_run`/禁用 |
| R03 | 仍为建议 |
| IMP-T04 研究格式 mapper | 需独立标注夹具；在 2C 之后单独排期 |
| PERF-T02/T03/T04、KNW-T02/T03、REC-*、FBK-*、ENG-T04+、UX-*、VAL-* | 未开始；依赖对应切口完成 |

**判定规则**：规格缺口 → 先修规格文档再改代码；实现偏差 → 先写失败测试再修；
不得为了"让演示通过"手填 ID、补默认值、追填历史或自动重发外部请求。
