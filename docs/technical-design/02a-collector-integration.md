# 02a 采集原型只读接入与兼容导入详细设计

> 阶段 C2；2026-11-01；共享建议基线 `product-contract/0.4`（D-P兼容回补）。02a是原型与产品的边界适配器，尚未实现。（独立审计修订；兼容见共享数据§12.2）
> 来源：[采集原型设计](../../web-collector-prototype-design.md)、[现有 storage.py](../../game_collector/storage.py)、[workflow.py](../../game_collector/workflow.py)、[schema.py](../../game_collector/schema.py)、[共享数据 §4/10](./shared/data-contracts.md)、[状态 §8](./shared/state-conventions.md)。

## 1. 职责、现状与边界

已实现证据：Store 为每运行写 `knowledge.sqlite3` 及六份 JSON，并逐文件替换；records 有 local_id/reference_scope，source.id 是捕获作用域。原库 `INSERT OR REPLACE` 不是产品事件历史，六文件不保证同一检查点共同提交。02a 只读冻结输入，创建产品 ImportSnapshot/ImportedRecord；不改原运行、不让旧库成为产品主库、不推测历史调用精确发送状态。

新收集的产品路径由02/07审计协调；初始导入不调用模型。保留原 Collector CLI 作为独立运行入口，不把未加持久边界的 `Collector.run` 直接包装成具备 B3 取消/恢复的产品任务。

## 2. 功能与输入类型

| 功能 | 触发/输入/前置 | 处理及输出 | 异常/验收 |
|---|---|---|---|
| IMP-F01 冻结文件组 | 已停止写入的原运行；后台取得受管理文件ID | 前后hash与完整文件组核对，生成 ImportSnapshot | 复制期间变动/缺必需文件 rejected；IMP-V01 |
| IMP-F02 身份/引用映射 | KnowledgeImportInput、02确认的映射 | 按导入/捕获/类型/局部ID定位新全局ID | 同名不跨作用域补齐；重复键隔离；IMP-V02 |
| IMP-F03 发布/去重 | manifest/格式/检查可解释 | 原件就绪后事务登记映射、记录与发布；交02组包 | 三元指纹去重，丢响应不重建；IMP-V03 |
| IMP-F04 研究资料归档 | 四份研究/检查JSON，原字节hash | 格式识别及必要映射；不足只存原件/诊断 | 不冒充 Extraction 合规；IMP-V04 |
| IMP-F05 原型语义保留 | 原状态/严格性/模型标识等 | 无损 envelope 与局限，产品用途另查 | 不把导入成功变采集充分；IMP-V05 |

## 3. 组件与流程

建议 `game_app/importer.py` 包含 FrozenInputReader、FormatRegistry、ScopedReferenceMapper、ImportValidator；原件发布由07 FileManager，公共 Game/包所有者为02。没有可识别格式时使用 retained_only 结果，不能让模型现场发明转换程序。

```mermaid
flowchart LR
    A[停止写入的旧输出] --> B[复制及前后hash]
    B --> C[受管理冻结文件组]
    C --> D[格式与身份检查]
    D --> E[同作用域类型化引用映射]
    E --> F[只读ImportedRecord与诊断]
    F --> G[07文件及库发布]
    G --> H[02构建知识包]
    D --> X[保留原件但不发布可用记录]
    E --> X
```

## 4. 文件集合、格式识别与数据规则

标准 `collector-run/v0.1` 文件组要求 `run.json/sources.json/records.json/gaps.json/extractions.json`；`candidates.json` 与 `report.md` 为派生输出，存在则原样归档，但 candidates 必须重建核对，不参与事实裁决。`knowledge.sqlite3` 可作为只读诊断附件，不与 JSON 相互补缺；不运行旧库写语句，也不把不同检查点拼成一个“成功运行”。

格式识别先看顶层结构、contract_version、类型与必要键，不靠文件名独断。run 必须有可解释的 id/input/game_id/status/stop_reason，sources/records/gaps/extractions 为对应数组；run.finished_at 缺失或正在变化不证明稳定。原运行可为已停止的中断，不要求伪造正常完成。跨文件 source_id/revision_id/reference_scope、原 game_id 必须相符；extractions 的 source_id 必须定位相应 source，保留未解析 raw。缺少运行事实的文件不得伪造 TaskRun。

| 产品对象 | 关键约束 | 兼容与未知 |
|---|---|---|
| ImportSnapshot | unique(import_fingerprint,mapping_version,mapping_context_hash)；manifest原字节 | identity_mapping_ids+规则版本进入context hash；修复新快照关联parent |
| ImportedRecord | unique(import_snapshot_id,capture_scope_id,record_type,local_record_id)；id=record_snapshot_id | legacy_record_id 原样，不拆冒号；payload_hash 为规范JSON，manifest为字节hash |
| Source/SourceRevision | 规范URL保留必要参数，捕获修订独立 | URL/正文相同不代表作者独立；失败来源无正文hash不编造 |
| RecordExtension | 原 NumericValue 不改，边界严格性未知另存扩展 | 旧 lower/upper 没有包含标志时 bool=null/status=unknown |
| KnowledgeRecordRef | 由02在具体包分配后构造，校验成员与hash | 未发布/隔离记录不能从此路径变成事实 |

### 4.1 引用解析与记录隔离

先分配所有类型化节点，再解析 payload 内 setting/configuration/environment/evidence IDs。查询键固定为 `(import_snapshot_id,capture_scope_id,record_type,local_record_id)`，不得按 legacy row.id 字符串推导。一个坏引用使该记录及依赖它且无法自洽的记录隔离；其余完整记录可保留，包发布仍要求被选成员的依赖闭合。重复键、无从定位 source revision 的记录保留诊断 envelope；不允许 last-write-wins。

ImportedRecord.payload 保留原类型JSON，upstream_envelope 保存原 row 的状态、issues、检查/提取/适用性及 superseded_by 等字段；产品 checks 另存。原 accepted 可以因新增用途检查被限制，原 quarantined 不能仅因复制成功升级。superseded_by 只在明确的原作用域和源修订关系内解析；找不到目标则保留未知/诊断，不借别运行同名对象填补。

### 4.2 四份研究/检查文件

| 原资料 | 识别结构与角色 | 首次导入策略 |
|---|---|---|
| fh6-extraction-results.json | record_version/run/sources/recommendation/多种 environment/configurations/measurements | `research-result/v1` 只读归档；映射条目需显式源定位、类型/关系校验，不能直接当 Extraction |
| fh6-new-source-extraction.json | run/sources/dot/gg 分区 | `research-source-sections/v1` 逐来源分区定位；不同分区local ID不相等 |
| fh6-extraction-checks.json | extraction_sha256/gold_sha256/多组checks | `research-checks/v1` 作为历史检查证据，不作为新知识事实 |
| fh6-new-source-checks.json | 检查摘要/来源对比 | 先按已核验结构登记附件；无匹配版本映射则 retained_only，不猜字段 |

没有完整映射程序/金样本时，研究资料的成功含义仅“原件已保留”，不发布假结构化记录。未来启用任一 FormatRegistry mapper 必须提交逐字段映射表、源定位与 IMP-V04 夹具；原字节、未知、FPS区间、HDR观察及条件分支均不改写。此门禁可使部分研究资料暂不能推荐，但不阻止标准 Collector 文件组导入。

## 5. 接口与返回

`import_knowledge(input:KnowledgeImportInput,context:MutationContext)->TaskAccepted` 由07注册 knowledge_import；任务成果为 ImportSnapshot 及可发布 ImportedRecord/诊断引用，DomainResult.code=published 或 retained_only。input.file_ids 非空，后台解析为冻结组，客户端/模型不提供任意本机目录。`inspect_format(file_ids:ID[])->FormatInspection` 只读、不发模型；FormatInspection 为局部 DTO：`format_id:string?,required_files:string[],missing_files:string[],limitations:string[]`，全 R。

`map_references(snapshot_id:ID)->ImportValidation` 仅针对 staging，局部返回 `accepted_record_ids:ID[],diagnostic_record_ids:ID[],checks:CheckResult[]`，全 R；不能跳过发布事务直接交推荐。导入后02 `build_package` 决定声明范围，不保证自动产生 actionable 推荐。解析/复制建议任务上限 120 秒为初值；大文件分块哈希并检查取消，无网络重试；存储错误可原 key 重试，显式修复需改变映射版本/上下文/输入指纹。

导入前玩家可先为本次采集运行声明游戏映射：`declare_import_identity(run_id,legacy_game_id,game_revision_id)` 写入 `legacy_namespace="collector_run:<run_id>"` 的 IdentityMapping，target 为 Game.series_id，`declared_by_user_scope_id` 为声明者，check 为 `identity.declared`（pass，注明非外部验证）。provisional 游戏只能由声明者本人声明，verified 公开，legacy_unverified 拒绝；同一 `(namespace, legacy_value)` 已指向另一游戏时拒绝且不覆盖。映射缺失时导入仍为 retained_only；使用他人声明的映射按 RESOURCE_NOT_FOUND 处理。`sources.json` 的内联 `document` 只映射 `content_hash`/`truncated`/`retrieval_method` 到 SourceRevision，`document_file_id` 保持 null，不为内联文档另写受管理文件。

```json
{
  "kind": "knowledge_import",
  "file_ids": [
    "00000000000000000000000000000201",
    "00000000000000000000000000000204",
    "00000000000000000000000000000205",
    "00000000000000000000000000000206",
    "00000000000000000000000000000207"
  ],
  "game_context_id": "00000000000000000000000000000202",
  "mapping_version": "collector-import/1",
  "identity_mapping_ids": ["00000000000000000000000000000203"]
}
```

上例五个file ID分别定位标准组的必需文件；实际字段/媒体类型与manifest也须核对，不由命名猜出同目录其他文件。首版直接提交每个必需文件 ID，不支持隐式文件组对象或归档解包。

## 6. 冻结、发布与恢复事务

1. 验证输入/权限/幂等与稳定来源。后台只读复制由用户选择的原运行，不枚举任意磁盘；文件上传由07保存受管理ID。
2. 原目录复制前记录必需文件大小/hash，复制到 staging 后复算，再读原目录相同文件清单/hash；任一变化则 IMPORT_SNAPSHOT_INCONSISTENT。操作日志保存逐文件manifest，不把文件mtime一致当共同检查点证据。
3. 原字节指纹去重；新快照 staging，解析格式、确认身份映射与源作用域，分配新ID并检查依赖。原件修复/身份映射改变新建parent快照，rejected旧快照保留。
4. 完成必要文件最终位置准备；短事务保存 ImportedRecord/状态/检查/关系及ImportSnapshot发布，幂等响应按注册任务规则不改写。此处后台发布事件不是第二次 HTTP 创建响应。
5. 02读取 published 结果并按声明范围组包；原采集 partial/interrupted 事实仍保留。缺文件、坏引用与未知身份不进入可用包。
6. 重启只核对已注册产品导入任务和操作日志，不能复活原 Collector 内存；同输入重试取原导入/诊断，不绕过 rejected。临时文件可保留，原运行永远只读。

## 7. 失败与原型能力差距

| 情况 | 行为/恢复 |
|---|---|
| 六文件不同检查点 | candidates 重建及源/引用/状态核对失败则不发布；重新停止旧运行后显式新输入 |
| 旧同名 local_id 跨source/run | 独立ImportedRecord；不得合并证据或支持票数 |
| run.interrupted=true | 历史执行事实 interrupted，原 partial_scope/budget_exhausted 原样存；导入任务另有自己的状态 |
| 只有研究文件/没有run | ImportSnapshot原件，不造历史TaskRun；无法映射则retained_only |
| 实际返回模型缺失 | 当前 ModelClient 会用请求名回填 model；旧产物无法证明是供应商实际返回，产品returned_model保持null并说明，不能补造C09合规证据 |
| 原型_call无持久发送审计 | 旧日志仅历史过程证据；未来产品接入必须在07边界补审计，不能声称内部精确恢复已支持 |
| 图片线索/未知严格边界 | 保留缺口/扩展unknown，不改old payload以过校验；另显式补查任务 |
| 磁盘/改名/库失败 | state §8 文件窗口；保留manifest/诊断，未发布不可消费，原 key可查询 |

## 8. 依赖与设计选择

沿 Python/Pydantic/SQLite 现有栈。JSON 文件导入优于直接挂旧 SQLite，理由是可冻结字节、独立检查与跨运行作用域清楚；旧数据库只作诊断附件。原型实例不得作为并发单例（run 会重置内部列表），产品任务也不能通过共享 Collector 对象“续跑”。依据为本次读取源码，无新增运行验证。

选择格式注册与确定性映射，拒绝“模型把整个旧JSON修成新Schema”的方案，防止无依据改变值。旧 Schema 对额外字段有限制，但产品归档仍保留原envelope；结构失败对象只作诊断，不强行实例化 accepted ImportedRecord。外部格式/库版本沿07核验清单，无新的第三方能力承诺。

## 9. 验收场景

| 编号/需求/目的 | 初态/输入 | 操作 | 输出/状态断言 | Mock / 真实环境 |
|---|---|---|---|---|
| IMP-V01 / A05：稳定冻结 | 标准组，另有缺records/复制中sources改变/旧candidates | 冻结、前后hash、重建候选 | 变化/缺必需文件不发布；候选以records重建，原目录不变 | 临时文件；真实本地FS，无网络 |
| IMP-V02 / A02：作用域 | 两run两source同local_id；同组重复键/坏外键 | 分配映射与解析、尝试包发布 | 不跨域误连，重复/坏依赖隔离，完整子集可供02明确组包 | 固定JSON/临时库；免真实服务 |
| IMP-V03 / D02、A05：发布幂等 | 同三元指纹，两客户端；改名后/提交后丢响应 | 并发导入、重启与原key重试 | 一个导入身份，孤立文件不可消费，已提交回原task，不重发模型 | 故障注入/临时库；免网络 |
| IMP-V04 / C08、兼容 | 四份研究资料副本；lower_bound/HDR/区间/未知图表 | 格式归档、启用/禁用mapper、请求推荐输入 | 原hash不变；未映射retained_only；有映射也不把观察/区间改建议/均值，未读图缺口保留 | 冻结副本/映射桩；免真实服务 |
| IMP-V05 / A06、C09 | interrupted旧run、缺模型真实返回、原quarantined | 导入并读取产品视图 | 原状态/envelope保留；不虚构历史TaskRun、费用/模型返回或accepted升级 | 固定产物；免付费 |

## 10. 实施任务与 DoD

| 任务/目标 | 依赖 | 拟建组件/文件 | 必须逻辑与接口 | 验收/完成定义 |
|---|---|---|---|---|
| IMP-T01 冻结与格式识别 | ENG-T01/03、VAL-T01 | game_app/importer.py；tests/product/test_import_freeze.py | FormatInspection、标准组校验、稳定复制与指纹 | IMP-V01；原件hash不变，变化输入拒绝 |
| IMP-T02 作用域与导入记录 | IMP-T01、KNW-T01 | importer.py；test_scoped_import.py | 全类型映射、envelope、依赖隔离、旧状态/模型局限 | IMP-V02/05；坏引用不跨源补，去重键可验证 |
| IMP-T03 发布与恢复 | IMP-T02、ENG-T02/03 | importer.py；test_import_publish.py | ImportSnapshot三元去重、文件/库窗口、published/retained_only | IMP-V03；故障后同身份、未发布不消费 |
| IMP-T04 研究格式映射 | IMP-T01—03、独立标注夹具 | importer.py 格式注册；test_research_import.py | 每格式显式映射表和定位，保留未知/严格性/图片缺口 | IMP-V04；未过夹具mapper不启用，原件不得回写 |
