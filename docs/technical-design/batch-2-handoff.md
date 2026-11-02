# 批次二交接：IMP-T01 / DEV-T02+PERF-T01 / IMP-T02—T03+DEV-T03—T04

> 日期：2026-11-02；基线提交 `24e5c52`（`main`，未推送）；产品契约 `product-contract/0.4`。
> 本文是**执行指令**，不是设计权威。字段/状态/错误的唯一权威仍是
> [`coding-handoff.md`](./coding-handoff.md)、[`implementation-plan.md`](./implementation-plan.md)、
> [`shared/`](./shared/) 三份契约与 `01`/`02`/`02a`/`04` 模块规格。
> 本文只写"从哪继续、按什么顺序、每步如何证明"，与规格冲突时以规格为准并先修规格。

---

## 0. 现状快照

**已完成并提交（6 个任务）**

| 任务 | 提交 | 交付 |
|---|---|---|
| ENG-T01 | `768f09b` | `contracts/{primitives,enums,common}`、`errors.py`、`repository.py`、`migrations/0001—0002` |
| VAL-T01 | `c59af75` | `tests/product/support/fixtures.py`、`conftest.py`、`tests/fixtures/product` |
| ENG-T02 | `2ef29e3` | `tasks.py`、`application.py`、`contracts/tasks.py`、`migrations/0003` |
| ENG-T03 | `e9b9473`/`8d4dd70` | `files.py`、`migrations/0004` |
| KNW-T01 | `2ddf56c` | `knowledge.py`、`contracts/knowledge.py`、`contracts/device.py`(GameContext) |
| DEV-T01 | `4bed74d` | `device.py`、`adapters/windows_probe.py`、`contracts/information.py`、`migrations/0005` |
| 两轮评审修复 | `c813667`、`d757eee`、`24e5c52` | 见各提交正文 |

**基线命令与期望**

```powershell
py -3.13 -B -m pytest -q -p no:cacheprovider
# 期望：182 passed, 1 skipped（跳过项为 Windows 上不生效的 POSIX 权限位断言）
```

**已完成部分可直接复用的能力**

| 能力 | 位置 |
|---|---|
| 短事务 + 锁竞争映射 | `repository.ProductRepository.unit_of_work()`；锁忙/提交失败均为 `STORAGE_COMMIT_FAILED` |
| 不可变版本、聚合快照、引用 | `UnitOfWork.insert_versioned/insert_object/insert_object_bytes/insert_reference/append_snapshot` |
| 幂等登记与重放 | `UnitOfWork.insert_idempotency/find_idempotency/tombstone_idempotency` |
| 文件发布（staging→校验→原子改名→库内发布→幂等） | `FileService.publish(...)`，支持 `writer` 与 `PublishHooks` 故障注入 |
| 供应商调用审计（prepared→in_flight→succeeded/unknown） | `TaskContext.invoke_provider(...)`；恢复时按检查点复用已成功调用、不重发 |
| 本机设置目录（已定位槽/未知位置/旧歧义桶/后继唯一） | `UnitOfWork.register_local_setting/import_legacy_local_settings/resolve_local_setting/list_local_setting_heads` |
| 声明游戏槽 | `UnitOfWork.get_or_create_game_declaration_slot/claim_game_declaration_slot` |

**已知跨任务未落实项（不要误以为已完成）**

1. `contracts/tasks.py` 的 `TaskInputPayload` 只有 `KnowledgeInput`/`AnalysisInput`/`DeviceScanInput`。
   **IMP-T01 必须加入 `KnowledgeImportInput` 并在 `tasks.DEFAULT_ROUTES` 登记 `/api/knowledge-imports`**；
   后续 `LogImportInput` 等由 PERF 任务加入。
2. `IdentityMapping` 目前只在 `resolve_game_identity(..., legacy_namespace=, legacy_value=)` 显式传参时写入；
   采集器运行命名空间的批量映射属 **IMP-T01/T02**。
3. `prepare_game_context`、`list_installations`、`create_installation`、`register_local_setting`(服务层)、
   `list_local_settings` 属 **DEV-T02**；仓库层仅有目录原语。
4. `TaskProgress.phase` 仍按状态粗归约，未按 `phase:<键>` 事件归约（07 §5）；留待后续任务统一。
5. `WindowsProbeAdapter` 在测试中从不运行；真实硬件未验证。

---

## 1. 新会话启动流程（每个任务都照做）

1. `git status --short`、`git log -1 --oneline`、`git branch --show-current`；确认工作区干净、HEAD 为上一任务提交。
   **保护已有改动，不整库 reset。**
2. 读本任务对应的权威段落（见各切片"先读"），不读就写代码＝返工。
3. 运行基线，记录 `160→182` 之外的任何变化。
4. **先写失败测试**（`superpowers:test-driven-development`），运行并确认"因行为缺失而失败"，不是因拼写/导入错误。
5. 最小实现；目标测试通过后跑**全量**测试。
6. 按 §6 模板记录证据，提交（一个任务一个提交），不 force、不推送除非用户要求。

**不要做的事**：不新建平行计划/GSD 结构；不联网安装依赖；不调用真实模型/供应商/硬件/游戏；
不把未运行写成通过；不在测试中读取玩家真实库或 `runs/` 金样本（只读副本或临时目录）。

---

## 2. 切片 2A — IMP-T01 冻结与格式识别

**权威**：02a §2/§4/§6/§10；data §4.1（ImportSnapshot/FileManifest/FileEntry）；state §8.2；07 §4/§6。

**依赖**：ENG-T01/03 ✅、VAL-T01 ✅。**无内部依赖，可立即开始。**

### 2.1 目标与边界

只做"稳定冻结 + 格式识别"，**不发布 ImportSnapshot**（那是 IMP-T03），不做引用映射（IMP-T02），不建知识包。

产出：

- `game_app/importer.py`：`FormatRegistry`（格式识别）与 `FrozenInputReader`（稳定复制与指纹）。
- `tests/product/test_import_freeze.py`。
- `tests/fixtures/product/collector-run-smoke/v1/`：**自造**的标准组夹具（5 份必需 JSON，极小内容），
  配 `manifest.json` 走 VAL-T01 的 `FixtureLoader` 校验。
  hash 生成可照 `tests/fixtures/product/smoke-inputs/v1/manifest.json` 的写法：
  `sha256` 十六进制 + 字节长度，写完再跑一次 `test_fixture_loader.py` 确认未被改坏。
- 契约补充：`contracts/tasks.py` 增加 `KnowledgeImportInput`（data §9：
  `kind="knowledge_import"`、`file_ids: ID[]`（非空）、`game_context_id: ID`、
  `mapping_version: string`、`identity_mapping_ids: ID[]`），
  `tasks.DEFAULT_ROUTES["knowledge_import"] = "/api/knowledge-imports"`。

**格式契约（02a §4，必须逐条落实）**

| 规则 | 实现要求 |
|---|---|
| 标准组 `collector-run/v0.1` | 必需 `run.json`/`sources.json`/`records.json`/`gaps.json`/`extractions.json`；缺任一 → 不支持该组 |
| 派生输出 | `candidates.json`、`report.md` 存在则原样归档，**不参与事实裁决**；`candidates.json` 必须由 `records.json` 重建核对 |
| 诊断附件 | `knowledge.sqlite3` 只读保留，不与 JSON 互相补缺，不执行任何写语句 |
| 识别依据 | 顶层结构/`contract_version`/类型/必要键；**不靠文件名独断** |
| run 必需可解释 | `id`/`input`/`game_id`/`status`/`stop_reason`；`finished_at` 缺失或变化不证明稳定 |
| 跨文件一致 | `sources.id`↔`sources.revision_id`、`records.reference_scope`、`extractions.source_id` 必须能定位 |
| 不伪造 | 缺运行事实不得造 `TaskRun`；`interrupted` 旧运行照原样接受 |

**候选重建的确定规则**（照抄 `game_collector/storage.py:34`，不要新造过滤条件）：

```python
candidates = [r for r in records
              if r["record_type"] == "configurations"
              and r["record_status"] == "accepted"
              and r["recommendation_status"] != "excluded_branch"
              and not r.get("superseded_by")]
```

`candidates.json` 的元素是完整 record 形状（含 `local_id`），因此核对**按 `local_id` 集合**比较，
不比较字节/顺序/格式：
磁盘集合 ⊆ 重建集合 → 通过（旧文件可能省略某些派生项）；
磁盘集合 ⊄ 重建集合（出现 `records.json` 中不存在的 `local_id`）→ 判定为不同检查点的拼接，
`IMPORT_SNAPSHOT_INCONSISTENT`。`candidates.json` 整体缺失不算失败，只写 limitation。

**FormatInspection（02a §5，局部只读 DTO，全字段 R）**

```
format_id: string?  required_files: string[]  missing_files: string[]  limitations: string[]
```

`inspect_format(...)` 只读、不发模型、不产生持久事实。

**稳定复制（02a §6.1—6.2）**

- 只复制**调用方明确给出的一个目录**中的标准组成员；不枚举其他磁盘、不递归猜测、不解压归档。
- 复制前后各算一次 `(size, sha256)`；两次必须相等，且复制后复读原目录同组文件再核一次。
- **任一文件在复制窗口内变化 → `IMPORT_SNAPSHOT_INCONSISTENT`**，原目录不触碰。
- `mtime` 相同**不能**作为"同一检查点"的证据；只用字节与 hash。
- 指纹 = 规范排序的相对名 + 字节数 + sha256（`FileManifest`/`FileEntry`，data §4.1）。
- 冻结结果落到受管理 staging（可复用 `FileService.publish` 的 staging/原子改名/幂等；
  若复用，注意 `publish` 会在同一事务写 `ManagedFile`——IMP-T01 只冻结、不发布 ImportSnapshot，
  因此 `writer` 传 `None`，不要把"文件就绪"当成"导入已发布"）。
  `publish` 以 `Mapping[str, bytes]` 接收内容，会整文件读入内存：本切片夹具足够小，
  但要在 task 记录里写明"超大运行的流式哈希未实现"这一限制，不要静默假设它能处理任意大小。

### 2.2 先写这些失败测试

1. 合法标准组：`inspect_format` 识别为 `collector-run/v0.1`，`missing_files=[]`。
2. 缺 `records.json`：`format_id=None` 或 `missing_files=["records.json"]`，并明确失败不得 publish。
3. 复制窗口内 `sources.json` 变化（用小函数在拷贝中途改写副本）：`IMPORT_SNAPSHOT_INCONSISTENT`。
4. `mtime` 相同但字节不同：仍判为变化（证明只看 hash）。
5. 磁盘上 `candidates.json` 含 `records.json` 中不存在的 `local_id`：判为组不一致，不发布。
   （`candidates.json` 缺失不算失败，只记 limitation。）
6. 非标准结构（例如顶层是数组而非对象、`contract_version` 未知）：`format_id=None`，`limitations` 说明原因。
7. 冻结后原目录逐文件 hash 与冻结前完全相同（证明只读）。
8. 原运行 `interrupted=true`：接受冻结，不造 `TaskRun`，`limitations` 保留原 `status/stop_reason`。

### 2.3 完成定义

- `py -3.13 -B -m pytest tests/product/test_import_freeze.py -q` 与全量测试通过。
- 冻结前后原目录字节 hash 不变；变化输入被拒绝；无网络、无模型、供应商发送计数 0。
- 提交信息按 `feat(imp-t01): ...`，正文列出断言与限制。

---

## 3. 切片 2B — DEV-T02 安装与本机目录 ＋ PERF-T01 格式与标准帧适配

### 3A. DEV-T02

**权威**：01 §2/§4/§5.1/§5.2/§10；data §3.2/§4.3（LocalSettingDefinition 版本化、SettingRef）；07 §4（精确键与唯一索引）；state §7（问题边界）。

**依赖**：DEV-T01 ✅、KNW-T01 ✅（仓库层目录原语已在 `24e5c52` 修好）。

**必做接口（01 §5.1/§5.2）**

| 接口 | 要点 |
|---|---|
| `prepare_game_context(game_revision_id, platform, installation_id, base_context_id=None)` | 纯函数返回 `GameContext` 候选，**不提交**、不探测、不联网；`base` 仅限同 series/同安装的固定原上下文；不补默认 build/process/设备值 |
| `list_installations(InstallationCatalogQuery)` | 只读分页，同用户/游戏；`installation_id`+`revision`+`display_label`+固定 `game_context_id`；不泄露路径 |
| `create_installation(CreateInstallationRequest, MutationContext)` | 201 返回 `InstallationRegistration`、`GameInstallation`、新 `GameContext`；`origin=user_provided`、`private_path_ref=null`；不同 key/同标签不自动合并 |
| `register_local_setting(RegisterLocalSettingRequest, MutationContext)` | 201 返回 `LocalSettingRegistration`、`LocalSettingDefinition`、manifest 的 `ManagedFile`；`namespace` 后台固定 `player` |
| `list_local_settings(installation_id)` | 每 series 当前头：`definition_id/series_id/raw_name/menu_path/location_known`；`[]` 合法；GET 无写入/无探测 |

**identity_mode 规则（01 §5.2，必须逐条测）**

- `auto`：`menu_path` 非空、`reuse_local_setting_id=null`；事务内精确键唯一则复用，否则新 series/`version=1`。
- `distinct`：`menu_path=[]`、`reuse_local_setting_id=null`；每个新 key 新建独立 series。
- `reuse`：显式 `reuse_local_setting_id` 指向同安装同 `player` 的**当前头**，原名/路径逐字相符。
- 路径未知却 `auto`、缺选择 ID、组合非法 → 422 `INPUT_VALIDATION_FAILED`。
- `supersedes` 非空时必须 `identity_mode=reuse` 且 `reuse_local_setting_id` 等于它；父非当前头/已有后继 → `LOCAL_SETTING_CONFLICT`。
- 目录更正**不迁移值**、不改历史引用；同 key 丢响应回同 ID，异内容 `IDEMPOTENCY_CONFLICT`。
- 每父一个直接后继；补齐/清除路径时同事务进出已定位精确索引。

**先写的失败测试**

1. 同游戏两个商店目录 → 两个独立 `GameInstallation`，不按标签合并。
2. 路径未知（`private_path_ref=null`）仍能建立上下文，且不扫盘。
3. `auto` 同键并发只产生一个定义；`distinct` 同名为两条独立 series。
4. `auto` 在 `menu_path=[]` 时拒绝；不静默取首项。
5. 跨安装条目 → `REFERENCE_INVALID`；过时头 → `LOCAL_SETTING_CONFLICT`。
6. 无已验证适配器时 `capabilities.*` 为 `not_run`，**不得产生 `ConfigurationObservation`**。
7. `list_local_settings` 返回当前头且 `location_known == bool(menu_path)`。
8. 声明 manifest 落盘（`ManagedFile`）且原 key 重放返回同 `definition_id`/同 manifest。

### 3B. PERF-T01

**权威**：04 §4（`NormalizedFrame`/单位/时钟/流）、§10 任务卡、PERF-V05/06。

**依赖**：ENG-T03 ✅、DEV-T02（3A）、VAL-T01 ✅。

**必做**

- `game_app/adapters/presentmon.py`：`FormatRegistry`（版本化格式指纹/列映射/单位/时基/流）与有界解析。
- `tests/product/test_log_formats.py`；`tests/fixtures/product/` 下自造小型 CSV 样本 + manifest。
- `CaptureArtifact.source_schema/source_column_map/time_mapping` 形状按 data §6.1 复用。

**规则**

- 未知列/未知单位/缺失主流/`FG` 未知/时基 offset 未知 → **保留原件、结果 `limited`/`invalid`，不猜列不猜换算**。
- 格式未登记 → `UNSUPPORTED_ARTIFACT_FORMAT`，原件保留，不生成 `valid` 结论。
- `G-FORMAT` 无真实样本 ⇒ **不得声称支持某真实工具**；注册表仅启用离线夹具中明确验证过的格式，并在 checks 标注证据等级。
- 解析在写事务外、建议 120 秒有界；不发网络/模型。

**先写的失败测试**

1. 未知列 → 拒绝解析该列，`limitations` 说明，不静默丢弃整行。
2. 单位不符（如 ms 列给 `s`）→ 不换算，标 unknown。
3. 缺主流（present stream 无法确定）→ `LogQuality.invalid` 或 `limited`，不给确定统计。
4. `FG` 未知 → 保持 `unknown`，不默认 `application_present`。
5. `TimeMapping.offset/scale` 未知 → 不强行绑定历史区段。
6. 未登记格式 → `UNSUPPORTED_ARTIFACT_FORMAT` 且原件仍可读。

---

## 4. 切片 2C — IMP-T02 → IMP-T03、DEV-T03 → DEV-T04

### 4A. IMP-T02 作用域与导入记录

**权威**：02a §4.1/§7；data §4.1；02a §10。

消费 KNW-T01 身份映射与 IMP-T01 冻结字节；产出 `ImportedRecord` 与 `upstream_envelope`、诊断、`Source`/`SourceRevision`。

- 唯一键 `(import_snapshot_id, capture_scope_id, record_type, local_record_id)`；`record_snapshot_id` 即 `EntityHeader.id`。
- 查询只用该四元键，**不拆 `row.id` 冒号猜作用域**。
- 先分配类型化节点再解析 payload 内引用；坏引用使该记录与依赖它的记录隔离，其余保留。
- 原 `accepted` 不因导入升级；原 `quarantined` 不因复制成功升级；`superseded_by` 只在本作用域/源修订内解析。
- `payload_hash` 用规范 JSON（UTF-8、键排序、紧凑、保留数值类型）；manifest 用字节 hash，两者不可互换。
- 失败测试：两 run 两 source 同 `local_id`；重复键；坏 setting 外键；`superseded` 目标缺失；隔离记录传递；实际 `model` 缺失时 `returned_model=null`。

### 4B. IMP-T03 发布与故障恢复

**权威**：02a §6；state §8；07 §6；IMP-V03。

- `ImportSnapshot` 按 `(import_fingerprint, mapping_version, mapping_context_hash)` 去重；同输入重试返回原导入，**不绕过 rejected**。
- 只改身份映射/规则 → 新 `mapping_context_hash` + `parent_import_snapshot_id`；原件变 → 新指纹。
- 文件窗口用 `FileService.publish` 的既有保证：改名后未提交 = 孤立文件不可消费；提交后丢响应 = 同 key 回原 ID。
- `DomainResult.code` 为 `published` 或 `retained_only`（映射见 state §3.2）。
- 失败测试：并发相同输入不同提交；改名后进程退出；提交后丢响应；`rejected` 重试不新建；导入不发模型（发送计数 0）。

### 4C. DEV-T03 采用与历史事实

**权威**：01 §5（采用/历史）；state §4.1—§4.4；data §5.2；DEV-V03—05、DEV-V07。

- `adopt_configuration`：origin 守卫表严格（`baseline`/`recommendation`/`change_set`/`manual_edit`/`rollback`）；`expected_current_applied_id` 冲突 409 且零部分写。
- 调整从 `ChangeSet.base` 完整合并；`manual_edit` 从固定当前 base 完整复制；未改项/未知/预设原样保留。
- `setting_ref_replacements` 仅 `manual_edit`：旧值/单位逐字核对、同 series 严格后继、一对一、不成链；否则 `LOCAL_SETTING_CONFLICT`/`REFERENCE_INVALID`/`PRECONDITION_UNVERIFIED` 按 01 §5.2 分流。
- `record_historical_configuration`：独立版本族 `version=1`、`registration_kind=historical_record`、`origin=baseline`；**不检查/不改当前指针、不写绑定**；`adopted_at` 无依据即 `null`。
- 同一事务写 `PlayerDeclaration`、采用版本、父/变更/恢复引用、`CurrentAppliedEvent`、`CurrentAppliedPointer`、幂等响应。
- 失败测试：两客户端 expected A 并发；恢复历史目标新建 ID；当前 B 补录过去 A 后指针不变；无映射同 series 换 ID 拒绝。

### 4D. DEV-T04 回答归一与接续

**权威**：01 §5.3；state §7.1—§7.3；data §8.2；DEV-V01/05/08、B-STA-13。

- `validate_information_answer`：校验 `expected_request_version` 与 `expected_context_revision`；旧版本 409 不部分落库。
- `conflict_selections` 索引对应候选；`FieldResolution` 保留旧依据；`answers` 与 `skipped_fields` 不得交叉。
- `create_information_amendment`（D-P04）：源须为链头 `resolved`/`skipped`；`amendment_root_ref` 固定根快照；根字段域不缩小；`facts_only` **不写任何 `TaskInputRevision`/`TaskEvent`/`TaskRun`**。
- 终态后补只存新事实，不回写终态任务输入/成果，不复活。
- 失败测试：部分回答；`unresolved` 原话；冲突选择；明确跳过；过时 request；上下文失效；取消竞态；两轮根域不同字段更正且先前更正不回退。

---

## 5. 提交与证据模板

每个任务一个提交，正文含：

```text
task_id / spec_revision / prerequisite_tasks / decision_and_gate_status
changed_files / fixture_id_and_hash / focused_command / observed_exit_code
assertions_checked / provider_send_count / object_and_event_counts
evidence_level / not_run_or_blocked_cases / remaining_scope / commit_revision
```

- 目标命令：`py -3.13 -B -m pytest tests/product/<本任务文件> -q -p no:cacheprovider`
- 收尾命令：`py -3.13 -B -m pytest -q -p no:cacheprovider`（必须全绿，含既有 45 项原型回归）
- 提交信息前缀：`feat(imp-t01):`、`feat(dev-t02):`、`feat(perf-t01):` 等；修复提交按既有 `fix(...)` 风格。
- 推送与否则由用户决定；不 force、不覆盖远端。

---

## 6. 仍然阻塞 / 不在本轮

| 项 | 状态 |
|---|---|
| Q1 产品形态（本地网页） | 未确认；阻塞 ENG-T04/UX 全量 |
| Q2 首游戏/适配范围 | 未确认；不得预启用 FH6 等真实适配器 |
| Q3 确认方式/玩家测试 | 未确认；不影响离线实现 |
| G-WIN / G-GAME / G-FORMAT / G-MODEL / G-WEB / G-IMAGE / G-EFFECT | 均无真实证据；相关能力保持 `not_run`/禁用 |
| R03 | 仍为建议 |
| IMP-T04 研究格式 mapper | 需独立标注夹具；在 2C 之后单独排期 |
| KNW-T02/T03、REC-*、PERF-T02+、FBK-*、ENG-T04+、UX-*、VAL-* | 未开始；依赖对应切口完成 |

**判定规则**：规格缺口 → 先修规格文档再改代码；实现偏差 → 先写失败测试再修；
不得为了"让演示通过"手填 ID、补默认值、追填历史或自动重发外部请求。
