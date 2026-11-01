# 阶段 B2：共享数据契约

> 建议基线 `product-contract/0.2`；2026-11-01；未实现。相对 0.1 的读取兼容规则见 §12。
> 依据：[架构基准](../00-architecture-overview.md)、[确认与决策台账](../README.md)、[公共知识契约 v0.1](../../../knowledge-intake-contract.md)、[原始 07](../../design/07-data-and-architecture.md)。
> 本文是新增共享逻辑对象及 DTO 的唯一定义。公共原载荷继续由旧契约与现有 Schema 解释；不得据此回改冻结样本。

## 1. 范围、类型记法与通用约束

字段规则是本轮实现建议，继承的业务约束见索引。状态枚举与转移统一引用[state-conventions.md](./state-conventions.md)，错误与公共调用约定统一引用[error-conventions.md](./error-conventions.md)。本篇不另建状态机。

| 记法 | 精确定义 |
|---|---|
| `ID` | 新实体的 UUID 小写十六进制字符串，32 位；不携带游戏名、时间或可推导顺序；旧 ID 不是此类型 |
| `LegacyID` | 原始非空字符串，逐字保存，不按冒号拆解或规范化 |
| `Hash` | SHA-256 小写十六进制，64 位；同时保存所哈希的内容类型/编码或文件字节语义 |
| `Timestamp` | UTC、带时区 ISO 8601 字符串；未知历史时间不填导入时间冒充 |
| `Revision` | 正整数，起始 1；由所有者分配，同一聚合内严格递增，不按时间排序代替 |
| `Scalar` | 严格的 string/int/有限 float/bool；bool 不自动转 int，字符串数字不自动转数值 |
| `T[]` / `map<string,T>` | 有序列表 / 唯一键映射；未声明顺序语义的列表也应稳定导出 |
| `T?` | 字段必须出现，允许值为 null；并不等于字段可省略 |
| `R` / `D=...` | R：字段必传，无隐式默认；D：创建时可省略并使用所写默认；读取/导出必须显式输出 |

请求拒绝未知字段；导入旧格式保留原始 JSON，不因产品请求拒绝额外字段而丢弃旧载荷。空字符串、NaN、Infinity 不表达未知。空列表只表达“该快照声明没有成员”，不能表达“尚未检查”；后者由检查/覆盖状态表示。

### 1.1 实体头、不可变版本与投影

所有下文持久对象均包含 EntityHeader；嵌入值对象不包含头。表中标“版本对象”的实体另含 VersionHeader。创建时间由后台生成；外部来源日期另存。

| 类型/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| EntityHeader.id | ID，后台生成 | 全产品对象 ID 唯一；跨类型引用仍须校验类型 |
| EntityHeader.schema_version | string，D=`product-contract/0.2` | 新产品对象的格式版本；不是公共知识原契约版本 |
| EntityHeader.created_at | Timestamp，后台生成 | 创建该产品对象的时间 |
| EntityHeader.created_by_task_id | ID?，D=null | null 表示同步用户操作或历史导入无对应产品任务；不得虚构任务 |
| VersionHeader.series_id | ID，R | 同一逻辑对象的版本族；ID 本身始终指单一版本 |
| VersionHeader.version | Revision，后台分配 | `(实体类型, series_id, version)` 唯一；不覆盖旧版本 |
| ObjectRef.entity_type / entity_id | string / ID，R | 类型必须是本契约登记的实体；读取时检查存在、可见与可用性 |
| VersionRef.entity_type / entity_id / series_id / version | string / ID / ID / Revision，R | 四者必须指向同一版本，不能只存 series_id 后取最新 |

`TaskRun`、`InformationRequest`、`GameInstallation`、`PlaySession`、`SessionSegment` 的当前状态等是有事件历史的聚合投影，可通过 revision 做比较更新；它们的输入快照、回答、开始/结束事实、内容修订与事件仍追加保存。其余事实对象默认不可变；发布/调用状态也由事件形成当前投影，撤回、重绑、复核、删除以新版本/事件/墓碑表达。物理表与索引在 07 细化，不要求每个头单独成表。

聚合的每次 revision 保存完整不可变投影快照，历史可按 `(实体类型,id,revision)` 读取；InformationRequest 的该修订键名为 version。只保留当前行和状态名变化不足以复现旧场景/时段。任务输入、回答与专用事件已有独立记录的部分可引用原件，不必重复复制大载荷。

除 TaskEvent、RecommendationReviewEvent、CurrentAppliedEvent 与 ConfigurationBinding 等专用记录外，通用状态历史使用 `StateEvent={target_ref:ObjectRef,dimension:StateDimension,sequence:Revision,previous_state:string?,next_state:string,cause:string,related_refs:ObjectRef[]}`（全字段 R，含 EntityHeader）。previous/next 只能取目标状态维度的合法值；维度与目标类型映射见 state §2，sequence 在目标与维度内唯一。每次保存投影与对应事件同事务提交，不能只存最终状态。请求事件的 related_refs 必须包含对应旧/新上下文版本及回答引用。

### 1.2 时间、单位、空值与引用

- 时间段使用半开区间 `[start,end)`；相对时间必须附 time_basis 和 unit。TimeRange 的 end 必须为有限值；仅活动区段及其 BindingRange 可用 end=null 表示尚未结束，不能以此生成分析时长。
- 内存统一计算值以 byte、间隔以 ms、时长以 s 保存；原文单位原样保留，换算另列方法和输入引用。原文 GB/GiB 不混同。
- 标识/关系的 null 表示表格明确允许的“无该关系”；未知测量/设备值一律用 FieldValue 或 NumericValue 的未知分支。不得要求所有可空外键都编造 unknown_reason。
- 历史引用不得指向“当前指针”。引用失效、类型错配、跨游戏/安装、资源未发布或被隔离时，由消费者拒绝相应操作，历史仍可展示原因。
- 本机路径仅在后台私有对象中存在；公开 DTO、模型输入、可分享导出使用脱敏后的文件/路径引用。

## 2. 获取、值与来源包装

### 2.1 FieldValue<T> 与获取尝试

| 字段 | 类型与必填/默认 | 含义与形状约束 |
|---|---|---|
| value_state | ValueState，R | 与获取结果独立，枚举见 state §2 |
| value | T?，R | known 时非 null；其他状态为 null |
| unknown_reason | LegacyUnknownReason?，D=null | unknown 时必须为旧契约五类原因之一；其他状态为 null |
| candidates | FieldCandidate<T>[]，D=[] | conflicting 时至少两份不一致依据；known 可保留未选依据；不按“最后写入”消解冲突 |
| provenance | Provenance[]，D=[] | known 至少一份实际依据；unknown 也可记录失败/缺项来源 |
| acquisition_status | AcquisitionStatus，R | 最近一次已登记获取的结果；not_attempted 时无获取尝试，不等于值未知的所有可能原因 |
| attempts | AcquisitionAttempt[]，D=[] | 按尝试顺序追加；系统失败后玩家回答成功，历史失败仍保留 |
| applicability_note | string?，D=null | not_applicable 时必须说明操作/条件依据；不能以不适用冒充未知 |
| resolution | FieldResolution?，D=null | conflicting→known 时保存选定依据、理由和原冲突引用；旧快照不变 |

`FieldCandidate<T>={value:T, provenance:Provenance[]}`，两字段 R，来源数组非空。

`AcquisitionAttempt={id:ID, method:AcquisitionMethod, status:AcquisitionStatus, attempted_at:Timestamp, error_code:string?, information_response_id:ID?}`：前四项 R；后两项 D=null。实际尝试的 status 不得为 not_attempted；failed 须有脱敏 error_code；skipped 须关联明确跳过的回答。时间表示尝试时间，不代替 value 的观测时间。

`FieldResolution={selected_provenance_indexes:int[], reason:string, prior_object_ref:ObjectRef}`：均 R；索引指本快照 provenance，且非空。选定用户声明时方法仍为 user_provided/player_confirmed；不伪装成 system_probe。

获取失败可对应 `value_state=unknown, unknown_reason=parse_failed, acquisition_status=failed`；明确跳过可对应 `unknown/not_provided/skipped`；未探测可对应 `unknown/not_provided/not_attempted`。这些组合不扩充旧公共 unknown_reason，获取过程由新包装表达。

### 2.2 Provenance、证据与检查

| 类型/字段 | 类型与必填/默认 | 规则 |
|---|---|---|
| Provenance.method | AcquisitionMethod，R | 表示取得/声明方式，不表示真实性分数 |
| Provenance.observed_at | Timestamp?，D=null | 来源没提供观测时刻则 null，不用抓取时刻替代 |
| Provenance.recorded_at | Timestamp，R | 该依据被登记的时间 |
| Provenance.evidence_refs | EvidenceRef[]，R | 非空；能定位来源、用户回答、观测或派生输入 |
| Provenance.note | string?，D=null | 保留适用时间/局限或原话的引用说明 |
| EvidenceRef | 带 kind 的联合类型，R | knowledge / source_location / local / artifact，字段见下表 |
| CheckResult.check_id / definition_version | string / string，R | 检查名与规则版本，不用一句 verified 汇总所有检查 |
| CheckResult.status | CheckStatus，R | 未执行/失败/未知/不适用均不能当通过 |
| CheckResult.input_refs / evidence_refs | ObjectRef[] / EvidenceRef[]，R | 可为空但须 note 解释无输入或无外部证据的结构检查 |
| CheckResult.checked_at / method | Timestamp? / string，R | 未执行时 checked_at=null；method 说明规则、同模型或独立核对 |
| CheckResult.note | string?，D=null | unknown、fail、not_applicable 时须说明原因 |

| EvidenceRef.kind | 其余必须字段 | 定位规则 |
|---|---|---|
| knowledge | record:KnowledgeRecordRef | 面向已发布包的公共证据；校验包成员、accepted 及当前用途适用性 |
| source_location | source_revision_id:ID、block_id:string、scope:LegacyScope、row:int?、column:int?、quote:string、quote_hash:Hash | row/column 必须出现，可空；cell 必须给二者，索引从 0 开始；保留章节路径于来源块中；原始引用片段不超过现有 Proof 的 600 字符限制 |
| local | entity_type:string、entity_id:ID、field_path:string? | field_path 必须出现，可空；类型限 InformationResponse/PlayerDeclaration/ConfigurationObservation/ConfigurationBinding/PerformanceSummary/Feedback/AppliedConfiguration；需具体不可变记录，不引用当前指针 |
| artifact | file_id:ID、content_hash:Hash、locator:string、time_range:TimeRange? | 字段均 R；定位受管理原始探测输出/配置原件/日志，不用观测对象自我引用充当依据；hash 与文件登记一致 |

`LegacyScope` 和 `LegacyUnknownReason` 原样引用[旧契约 §4—5](../../../knowledge-intake-contract.md)，不再定义第二套枚举。agent_derived 的依据必须最终追溯到输入事实；本机结果不得悄然进入公共包。

## 3. 游戏、安装与设备身份

### 3.1 公共 Game 与身份映射

| 对象/字段 | 类型与必填/默认 | 约束与修改方式 |
|---|---|---|
| Game（版本对象）.canonical_name | string，R | 规范展示名，不用名字哈希充当稳定游戏身份 |
| Game.aliases | GameAlias[]，D=[] | 别名带来源；未核实候选不得用于自动合并 |
| Game.platform_scope | string[]，R | 非空；每个平台保留原标识映射，不能假设不同平台构建号可比较 |
| GameAlias | `{name:string, platform:string?, evidence_refs:EvidenceRef[], status:CheckStatus}` | 全字段 R；有通过依据才生效，未知候选仅展示 |
| IdentityMapping.id / legacy_namespace / legacy_value | ID / string / LegacyID，R | namespace 含采集运行或研究文件指纹；不能只按 legacy_value 全局唯一 |
| IdentityMapping.target_game_id / checks | ID? / CheckResult[]，R | null 表示尚未确认映射，只能暂存；确认生成新映射记录 |

所有 `game_id` 指规范游戏的稳定 `Game.series_id`；需要冻结名称/平台等元数据时另存 `game_revision_id=Game.id`。不能因增加别名的新 Game 版本使历史安装变成另一个游戏，也不能只从稳定 ID 读取最新元数据替代历史。

同游戏别名、多平台与重装都不直接合并本机安装。来源游戏名、请求游戏名及历史 game_id 原值保留；规范 Game ID 的建立需现有明确名/显式别名或复核依据，不能按相似名称猜测。

### 3.2 本机上下文

下列头的 created_at 不代替实际探测时间；用户作用域为本机持久 user_scope_id，非网页登录会话。

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| GameInstallation.user_scope_id / game_id | ID / ID，R | 安装级聚合；同游戏两个商店或目录可有不同 id |
| GameInstallation.revision | Revision，后台分配 | 安装元数据投影修改须 expected revision，旧上下文仍可读 |
| GameInstallation.origin | string，R | system_probe 或 user_provided；路径未知也可由玩家选择建立本机上下文 |
| GameInstallation.platform / private_path_ref | FieldValue<string> / ID?，R | private_path_ref=null 表示未知目录；禁止为继续记录扫描任意盘符 |
| LocalPathBinding.path / purpose / evidence_refs | string / string / EvidenceRef[]，R | 后台私有、绝对路径；由 01 验证和登记，导出/模型输入不带 path；不是 CaptureArtifact 路径 |
| GameContext（版本对象）.game_id / game_revision_id / installation_id | ID / ID / ID?，R | game_revision_id 必须属于 game_id 的版本族；公共收集可无安装；采用和会话操作必须绑定安装或玩家指定的路径未知上下文 |
| GameContext.platform / build_identifiers | FieldValue<string> / BuildIdentifier[]，R | 构建未知可空列表，但需 checks 说明未取得 |
| GameContext.process_candidates / selected_process | ProcessIdentity[] / ProcessIdentity?，R | 无法确认不选第一项；选择依据在 checks/来源中 |
| GameContext.adapter_id / adapter_version | string? / string?，D=null | 尚无已验证适配器时两者均 null |
| GameContext.capabilities / checks | AdapterCapabilities / CheckResult[]，R | can_identify/can_read_config/can_bind_capture 各为 CheckResult；不以单一 supported 布尔替代 |
| BuildIdentifier | `{scheme:string, raw_value:string, platform:string, provenance:Provenance[]}` | 全字段 R；scheme 如 platform_build/file_version/player_report，仅说明标识来源，不编造版本序 |
| ProcessIdentity | `{pid:int, started_at:Timestamp?, executable_path_ref:ID?, provenance:Provenance[]}` | 全字段 R；PID>0；启动时间未知不得标为已可靠绑定，避免 PID 重用误连 |
| DeviceSnapshot（版本对象）.device_series_id / captured_at | ID / Timestamp，R | 区分设备身份与某次快照；用户后补新建快照，不改历史 |
| DeviceSnapshot.gpus / selected_gpu_id | GpuSnapshot[] / ID?，R | 全部适配器；未确定当前游戏 GPU 时 selected_gpu_id=null |
| DeviceSnapshot.cpu / memory_bytes | FieldValue<string> / FieldValue<int>，R | 正内存字节；未知不填 0 |
| DeviceSnapshot.device_form / os_version | FieldValue<string> / FieldValue<string>，R | 台式/笔记本来源不明保留未知 |
| DeviceSnapshot.display / power_context | DisplayContext / map<string,FieldValue<Scalar>>，R | 功耗/电源等扩展键由适配器声明，不猜 TGP |
| GpuSnapshot.id / raw_name / vram_bytes / driver_version | ID / FieldValue<string> / FieldValue<int> / FieldValue<string>，R | GPU id 仅在该设备快照内唯一；引用时必须同时给 device_snapshot_id |
| DisplayContext.desktop_resolution / refresh_hz | FieldValue<Resolution> / FieldValue<float>，R | 正刷新率；桌面分辨率不冒充游戏渲染分辨率 |
| Resolution | `{width:int,height:int}` | 二者 R，均>0；无默认 1080p |

## 4. 公共知识包装与包引用

### 4.1 原件、修订与导入快照

| 对象/字段 | 类型与必填/默认 | 规则 |
|---|---|---|
| Source.original_url / canonical_url | string / string，R | 规范化保留必要参数；同规范 URL 的多个修订不丢原 URL |
| Source.identity_checks | CheckResult[]，R | URL 一致不证明作者/测试独立性 |
| SourceRevision.source_id / retrieved_at | ID / Timestamp，R | 每次捕获有独立修订引用；失败捕获也可存档 |
| SourceRevision.upstream_revision_id / content_hash | LegacyID? / Hash?，R | 失败无正文时均可 null；不得虚构内容哈希 |
| SourceRevision.document_file_id / capture_method / capture_scope | ID? / string / string，R | 成功修订必须有可定位原件或必要正文块；图片线索不等于读图 |
| SourceRevision.metadata / truncated / checks | SourceMetadataV01 / bool / CheckResult[]，R | 作者/发布/更新/测试时间沿用原字段，不相互替代 |
| SourceRelation.from_revision_id / to_revision_id / relation / evidence_refs / checks | ID / ID / string / EvidenceRef[] / CheckResult[]，R | relation 表达转载、相同捕获内容、更正或独立性；未知用 checks 表达，不把同域名/异域名当事实 |
| ImportSnapshot.origin / legacy_run_id / manifest | string / LegacyID? / FileManifest，R | origin=collector_run 或 research_json；研究文件无 run_id 时为 null |
| ImportSnapshot.import_fingerprint / import_status / mapping_version | Hash / PublicationStatus / string，R | 指纹由规范排序的文件相对名、字节数与哈希计算；原件字节不变则指纹不变 |
| ImportSnapshot.mapping_context_hash / parent_import_snapshot_id | Hash / ID?，R | 前者覆盖具体身份映射引用及检查规则版本；首次 parent=null；修复上下文/规则或原件后新建关联快照 |
| ImportSnapshot.identity_mapping_ids / limitations | ID[] / string[]，R | 游戏身份未解决或跨文件一致性不足时不可发布给推荐 |
| FileManifest.entries | FileEntry[]，R | 非空，所有路径为受管理快照内相对路径；原目录不直接成为资源 |
| FileEntry | `{relative_name:string, byte_length:int, sha256:Hash, media_type:string}` | 全字段 R，字节数>=0；摘要不是对解析后重序列化内容的哈希 |
| ImportedRecord.record_snapshot_id / import_snapshot_id | ID / ID，R | record_snapshot_id 是该不可变载荷全局 ID；其余 EntityHeader.id 与其相等，不另分配第二身份 |
| ImportedRecord.capture_scope_id / local_record_id / legacy_record_id | LegacyID / LegacyID / LegacyID，R | 采集器 capture_scope_id=原 row.reference_scope；local_record_id=原 row.local_id；不拆 row.id 猜局部 ID |
| ImportedRecord.source_revision_id / record_type | ID / string，R | 来源修订必须经映射存在；类型在原 RECORD_GROUPS 中或明确的新导入器类型 |
| ImportedRecord.payload / payload_hash / upstream_status | 原类型 JSON / Hash / LegacyRecordState，R | 原载荷只读，未知原字段保留；产品不因成功导入就把 quarantined 升为 accepted |
| ImportedRecord.upstream_envelope | JSON，R | 保存原 row 的全部字段与状态/检查历史；payload 是其中 payload 的类型化只读视图，不丢旧 extraction_status/applicability 等字段 |
| ImportedRecord.checks / extension | CheckResult[] / RecordExtension，R | 产品检查另存；结构坏的原载荷可作诊断原件，但不成为可用 record |

`ImportSnapshot` 对 `(import_fingerprint,mapping_version,mapping_context_hash)` 去重；相同输入重试返回原导入，不绕过 rejected 状态重跑。明确修复身份映射/检查规则时上下文 hash 改变，或映射程序/原件改变时版本/指纹改变，才可建立新的关联快照。

`ImportedRecord` 的唯一键为 `(import_snapshot_id,capture_scope_id,record_type,local_record_id)`；发生重复时登记冲突，禁止 last-write-wins。同一 URL 或相同 local_id 出现在另一个运行时是另一记录；可另建内容关联，不合并出处或独立支持票数。payload_hash 使用 UTF-8、对象键排序、紧凑分隔、保留数值类型的规范 JSON；完整原文件字节 hash 另在 manifest 中，不能相互替代。

旧输出不具备整组文件共同提交保证。02a 必须从已停止写入的运行复制到受管理暂存快照，核对复制前后哈希、必需文件与引用；候选清单由 records 重建核对，不能信任可能来自另一检查点的 candidates.json。失败则保持未发布并说明限制；具体复制/验证操作在 C2 细化。

### 4.2 KnowledgeRecordRef 与包

| 类型/字段 | 类型与必填/默认 | 规则 |
|---|---|---|
| KnowledgeRecordRef.package_id / record_snapshot_id / source_revision_id | ID / ID / ID，R | package_id 指具体不可变包版本；record 必须属于该包 |
| KnowledgeRecordRef.capture_scope_id / local_record_id | LegacyID / LegacyID，R | 保留原始定位，不能仅凭它们跨包连接 |
| KnowledgeRecordRef.record_type / payload_hash | string / Hash，R | 与 ImportedRecord 完全相符；有 hash 也不能忽略作用域 |
| KnowledgePackage（版本对象）.game_id / game_revision_id / platform_scope | ID / ID / string[]，R | 非空平台，game_revision_id 引用 game_id 对应版本族的具体 Game 版本 |
| KnowledgePackage.parent_package_id / import_snapshot_ids | ID? / ID[]，R | 首版 parent=null；来源既可导入也可后续受控新采集，所有输入须可追溯 |
| KnowledgePackage.source_revision_ids / record_snapshot_ids | ID[] / ID[]，R | source_revision_ids 无重复；可用 records 仅包含通过入库门槛者 |
| KnowledgePackage.setting_catalog / configuration_templates / measurement_evidence / constraints | KnowledgeRecordRef[]，R | 按原类型分组，均须属于本包；measurement 数字未知定义不能被改成 average_fps |
| KnowledgePackage.diagnostic_record_ids | ID[]，D=[] | 可关联隔离记录供修复，推荐/反馈不得作为事实输入 |
| KnowledgePackage.coverage_matrix | CoverageCell[]，R | 声明范围内每格均有状态与依据，未知格不省略 |
| KnowledgePackage.critical_gaps / noncritical_gaps | KnowledgeGap[]，R | 关键性相对于某个 operation_scope，非对来源整体一刀切 |
| KnowledgePackage.checks / relation_ids | CheckResult[] / ID[]，R | 同时表达已执行和未执行的检查；来源关系显式引用 |
| KnowledgePackage.collection_result / stop_reason / collection_execution_ref | CollectionResult? / CollectionStopReason? / ObjectRef?，R | 只在有明确范围的采集执行证据时给结果/原因及 TaskRun 或 ImportSnapshot 引用；纯研究导入/多运行聚合无单一执行事实时三者均 null，原运行事实仍各自保留 |
| KnowledgePackage.publication_status | PublicationStatus，R | 发布完整性与收集范围独立；partial_scope 的部分成果也可完整发布，null 收集状态不表示 coverage 已满足 |
| KnowledgePackage.declared_scope / checked_at | string / Timestamp?，R | 声明覆盖范围及最近复核时间；未复核为 null，年龄不证明适用性 |
| CoverageCell | `{key:string,dimensions:map<string,Scalar>,operation_scope:string,status:CoverageStatus,evidence_refs:EvidenceRef[],note:string}` | 全字段 R；本包 key 唯一；covered/not_applicable 必须有依据；维度词表和关键项归约由 C2 定义 |
| KnowledgeGap | `{id:ID,legacy_gap_id:LegacyID?,operation_scope:string,priority:GapPriority,question:string,record_refs:KnowledgeRecordRef[],diagnostic_record_ids:ID[],status:GapStatus,stop_reason:CollectionStopReason?,evidence_refs:EvidenceRef[],checks:CheckResult[]}` | 全字段 R；隔离记录只从 diagnostic_record_ids 关联，不伪装包内可用证据；resolved 须有新相关证据与重验依据，非 resolved 不伪填闭合证据 |

包发布时的成员、原载荷和检查结论不可覆盖。新版本改变公开事实时创建新包；复核事件记录影响，历史包保留。单条 applicability 是相对于指定输入的检查，不把包本身打为永久适用于任意设备。

### 4.3 原始数值、扩展边界与设置身份

`SettingValueV01`、`NumericValueV01`、`SourceMetadataV01` 表示[现有 schema.py](../../../game_collector/schema.py)中的原结构；值类型和数值类型语义继续引用旧契约 §4。产品包装不改写这些 JSON。

`RecordExtension={boundary_semantics:BoundarySemantics?, setting_identity_ref:ID?, checks:CheckResult[]}`，全字段 R；不涉及的扩展为 null。

`BoundarySemantics={lower_inclusive:bool?,upper_inclusive:bool?,evidence_refs:EvidenceRef[],status:CheckStatus}`，全字段 R。旧 NumericValue 无边界包含标志时，适用边界的 bool 必须 null、status=unknown；不存在的另一侧也为 null，但由 number_kind 区分不存在与未知。严格性影响过滤时不得默认为 >= 或 >，应补证据或降级。后续若改旧值形状必须另增公共契约版本，不能只修改本产品扩展说明。

`SettingIdentityMapping={id:ID,game_id:ID,platform_scope:string[],source_setting_refs:KnowledgeRecordRef[],display_name:string,checks:CheckResult[]}`：全字段 R；ID 为产品设置身份，映射通过才可将不同来源原名作为同一设置；未通过的原 Setting 仍独立引用。C2 定义版本适用范围和别名核验算法，不从中文译名猜合并。

本机原配置不应依赖公共知识先存在。`LocalSettingDefinition` 是 01 登记的不可变本机目录条目，含 EntityHeader 及 `installation_id:ID, namespace:string, raw_name:string, menu_path:string[], evidence_refs:EvidenceRef[]`（均 R）；名称和值来自受控适配器或玩家声明，不自动成为公共知识。`SettingRef` 为带 kind 的联合类型：`{kind:"knowledge",record:KnowledgeRecordRef}` 或 `{kind:"local",local_setting_id:ID}`，分支内字段均 R。跨本机条目/公共条目的归一仍需有依据的映射；未映射不能按名字直接相等。

## 5. 推荐与配置版本

### 5.1 推荐内容

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| RecommendationVersion（版本对象）.parent_id | ID?，R | 首次为 null，重算另建版本 |
| RecommendationVersion.input | RecommendationInput，R | 精确输入快照；不能只存 task_id 后取最新输入 |
| RecommendationVersion.base_configuration_ref | KnowledgeRecordRef?，R | 没有完整基准时可 null；必须声明临时参考局限 |
| RecommendationVersion.actions / alternatives | RecommendationAction[] / RecommendationAlternative[]，R | 无依据的替代项可空，不为凑三套造值 |
| RecommendationVersion.target_proposal / reference_results | TargetProposal? / KnowledgeRecordRef[]，R | 无依据可不提具体目标；公开测量不得放进本机结果 |
| RecommendationVersion.declared_scope / readiness / checks | string / RecommendationReadiness / CheckResult[]，R | readiness 的总体归约由 C3 细化；不能仅以 accepted 推导 actionable |
| RecommendationAction.action_id / setting_ref | ID / SettingRef，R | action_id 在此推荐内唯一；必要时另附 setting_identity_ref:ID?（D=null）；本机条目本身不证明推荐值有公共证据 |
| RecommendationAction.action / value / current_value | RecommendationActionKind / SettingValueV01? / FieldValue<Scalar>，R | set_value 必须给 fixed 值；conditional_strategy 给 conditional/alternatives；keep_current/check_in_game/defer 可 value=null 且说明当前未知 |
| RecommendationAction.menu_path / reason / evidence_refs | string[] / string / EvidenceRef[]，R | 菜单位置未知可空并在 checks 注明；理由不得替代证据 |
| RecommendationAction.dependency_group_id / restart_required / checks | ID? / FieldValue<bool> / CheckResult[]，R | 联动组共同校验；未知重启条件不填 false |
| RecommendationAlternative | `{id:ID,label:string,base_recommendation_id:ID,action_overrides:RecommendationAction[],checks:CheckResult[]}` | 全字段 R；作为推荐侧候选差异，不直接作为可采用 ChangeSet |
| TargetProposal.value / basis_refs | NumericValueV01 / EvidenceRef[]，R | 目标单位 FPS；unknown 允许无具体数；目标自身不可变，不把后续接受情况回写推荐 |
| TargetProposal.frame_stream_kind / note | FrameStreamKind / string，R | 目标不自动写入帧率上限；与 frame_cap_setting 分离 |
| TargetAcceptance.proposal_id / session_id / accepted / feedback_id / supersedes_id | ID / ID / bool / ID / ID?，R | 05 追加记录玩家对具体目标的接受/拒绝及原反馈；无记录表示未问/未知，后续改意向关联旧记录而不改推荐 |
| RecommendationReviewEvent.recommendation_id / previous_state / next_state | ID / ReviewState / ReviewState，R | 追加事件由 03 写，状态守卫见 state；不改推荐内容 |
| RecommendationReviewEvent.affected_refs / reason / new_package_id | KnowledgeRecordRef[] / string / ID?，R | 仅记录有依据的影响；新包关系未知时可 null 并说明 |

TargetProposal 是包含 EntityHeader 的持久对象，作为推荐内容一并保存；TargetAcceptance.proposal_id 与 PlaySession.target_proposal_ref 指其 id，不能指列表位置或新生成的同值对象。目标接受、偏好声明的模块局部输入见 C4/05；通用错误码在 error 唯一定义。

推荐侧替代差异不复用要求 base_applied_id 的 ChangeSet：玩家尚未采用时没有实际配置基线。采用后如需执行替代差异，应先以实际配置实例化新的 ChangeSet 并重新校验；不能假设当前配置等于主推荐。

### 5.2 实际采用与观测

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| AppliedConfiguration（版本对象）.user_scope_id / installation_id / game_id | ID / ID / ID，R | 同一安装的当前指针唯一；game_id 与安装一致 |
| AppliedConfiguration.registration_kind | ConfigurationRegistrationKind，D=`current_adoption` | 区分推进当前指针的采用与仅登记过去事实；historical_record 的守卫见 state §4.4 |
| AppliedConfiguration.device_snapshot_id / game_context_id | ID / ID，R | 固定当时背景；未知字段在快照内表达 |
| AppliedConfiguration.origin / recommendation_id | AppliedOrigin / ID?，R | baseline 可无推荐；origin=recommendation 必须有推荐引用 |
| AppliedConfiguration.parent_applied_id / change_set_id / restored_from_applied_id | ID? / ID? / ID?，R | 首次 parent 可空；调整 parent=ChangeSet.base_applied_id；恢复 parent=当前版本，restored_from 指目标 |
| AppliedConfiguration.settings / preset_reference | ConfirmedSetting[] / string?，R | 至少有已确认项或显式预设引用；不掌握的预设项不展开，未知配置会话不造空采用对象 |
| AppliedConfiguration.adopted_at / activation_state / checks | Timestamp? / ActivationState / CheckResult[]，R | current_adoption 必须给声明采用时间；historical_record 仅在有当时依据时给历史采用时间，否则 null；登记时间见 created_at，不用补录时间冒充历史采用时间；确认不等于实际生效 |
| AppliedConfiguration.declaration_id | ID，R | 指向本次玩家声明，不以采用对象自我引用代替声明依据 |
| PlayerDeclaration.installation_id / input / raw_text / submitted_at | ID? / AdoptConfigurationInput或HistoricalConfigurationInput或ContextDeclarationInput / string / Timestamp，R | 同步保存玩家提交内容及原话；安装未定的上下文声明可 installation=null；采用/历史配置声明须安装一致；原话可空，结构化确认仍保留；不含幂等键/认证信息 |
| ConfirmedSetting.setting_ref / setting_identity_ref | SettingRef / ID?，R | 同一配置内同一规范设置或原始引用唯一，冲突不能保留成两个可执行真值 |
| ConfirmedSetting.value / unit / confirmation_method / activation_state | FieldValue<Scalar> / string? / AcquisitionMethod / ActivationState，R | 实际采用值不含待实例化候选/条件；user_provided、player_confirmed、system_probe 不混淆 |
| CurrentAppliedPointer.user_scope_id / installation_id / applied_id / revision | ID / ID / ID? / Revision，R | 唯一键 `(user_scope_id,installation_id)`；初始 applied_id=null；每次推进保存事件及预期当前 ID |
| CurrentAppliedEvent.user_scope_id / installation_id / pointer_revision / previous_applied_id / next_applied_id | ID / ID / Revision / ID? / ID，R | 追加事件；首个 previous=null，next 为本事务新采用版本；按用户/安装/指针修订唯一 |
| ConfigurationObservation.installation_id / device_snapshot_id / game_context_id | ID / ID / ID，R | 与一次具体只读探测或导入关联，不是推荐 |
| ConfigurationObservation.observed_at / adapter_id / adapter_version | Timestamp / string / string，R | 已读取位置/时刻，不代表游戏进程已生效 |
| ConfigurationObservation.settings / artifact_file_ids / checks | ConfirmedSetting[] / ID[] / CheckResult[]，R | value 的来源 method=system_probe；不得用玩家回答冒充程序观测 |

`SettingDeclaration={setting_ref:SettingRef,value:Scalar?,unknown_reason:LegacyUnknownReason?,unit:string?,claimed_activation:ActivationState,activation_note:string?,observation_id:ID?}`，全字段 R。value=null 时须给 unknown_reason，非 null 时 reason 必须空；observation_id 只是玩家选定的已有观测引用，由后台核验其字段/安装/时段，不能由客户端借此自报 system_probe。claimed_activation 仅为玩家声明，服务端仍核对重启/冲突依据，再构造 ConfirmedSetting 与来源包装。

公共资料未发现的本机设置用 LocalSettingDefinition 定位，不编造 KnowledgeRecordRef，也不阻止先记录原配置基线。无法可靠归一的原话仍保存在声明/回答/观测原件，相关值保持未知；不为此丢掉会话。

完整配置快照包括已知、自定义、未知项及预设引用。调整从基础实际快照合并差异，未改项原样保留。只有查看推荐或历史不能推进 CurrentAppliedPointer；历史后补也不切换当前指针。origin 的合法引用组合见 state §4。

## 6. 会话、区段与性能引用

### 6.1 会话与原件

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| PlaySession.installation_id / game_id / device_snapshot_id / game_context_id | ID / ID / ID / ID，R | 可无推荐/采用，但必须有明确玩家选择的本机上下文 |
| PlaySession.started_at / ended_at | Timestamp / Timestamp?，R | 活动中 ended_at=null；结束时间不得早于开始 |
| PlaySession.revision / lifecycle | Revision / SessionLifecycle，R | 开始/结束保存追加事件；聚合修改比较 revision，不改写当时输入快照 |
| PlaySession.initial_applied_id / recommendation_id | ID? / ID?，R | 不能以推荐填空采用；未知配置 allowed |
| PlaySession.configuration_state / scenario / target_proposal_ref | ConfigurationState / FieldValue<string> / ID?，R | target_ref 只指提议，不证明玩家接受 |
| PlaySession.test_protocol_id / run_conditions | ID? / RunConditions，R | 自然游玩可无协议；运行条件不从最新推荐补填 |
| SessionSegment.session_id / start_s / end_s / time_basis | ID / float / float? / string，R | 相对会话 start；start>=0、end>start；活动中 end=null；不把日志时钟强行当 UTC |
| SessionSegment.revision / lifecycle | Revision / SessionLifecycle，R | 场景/区间投影修订独立于 binding_revision；后补更正保存事件和新分析版本 |
| SessionSegment.applied_id / configuration_state / binding_revision | ID? / ConfigurationState / Revision，R | 这是当前绑定投影；其不可变绑定历史见 ConfigurationBinding |
| SessionSegment.scenario / excluded_ranges / stream_selection | FieldValue<string> / ExcludedRange[] / StreamSelection，R | 清洗和流选择均带依据；未知主流不能合并统计 |
| SessionSegment.run_conditions | RunConditions，R | 与场景/时段一同保存在具体 segment revision，不能事后按当前配置覆盖 |
| ConfigurationBinding.segment_id / binding_revision / applied_id | ID / Revision / ID?，R | 唯一键 `(segment_id,binding_revision)`，只追加；初次也建 revision=1 |
| ConfigurationBinding.configuration_state / effective_range / evidence_refs / supersedes_revision | ConfigurationState / BindingRange / EvidenceRef[] / Revision?，R | 活动段可用开放结束点；结束/切段追加闭合绑定；后补须适用于当时区间的依据；不改旧摘要 |
| BindingRange | `{start:float,end:float?,time_basis:string,unit:string}` | 全字段 R，start>=0，非空 end 为有限值且>start，unit=s 或 ms；end=null 仅适用于对应活动段修订，不可进入分析；与段边界及时基一致 |
| TimeRange | `{start:float,end:float,time_basis:string,unit:string}` | 全字段 R，end>start，unit=s 或 ms；精确换算必须有映射依据 |
| ExcludedRange | `{range:TimeRange,reason:string,evidence_refs:EvidenceRef[]}` | 全字段 R；不可因真实游戏尖峰离群而排除 |
| StreamSelection | `{process:ProcessIdentity?,swapchain_id:string?,frame_stream_kind:FrameStreamKind,checks:CheckResult[]}` | 全字段 R；空选择意味着未确认，不能生成 valid 目标流结论 |
| ManagedFile.manifest / relative_path / file_status | FileEntry / string / PublicationStatus，R | 路径位于受管理目录内，不由模型或浏览器直接指定系统路径；对外只返回 id |
| CaptureArtifact.session_id / segment_ids / file_id | ID / ID[] / ID，R | 原始文件哈希见 ManagedFile；配置与绑定从具体 segment/binding 取，不读当前配置 |
| CaptureArtifact.tool_name / tool_version / options | string / string? / map<string,Scalar>，R | 版本未能确认则 null 并限用；不猜未知工具参数 |
| CaptureArtifact.source_schema / source_column_map / time_mapping | string / ColumnMapping[] / TimeMapping，R | schema 指经核验格式指纹；未知格式只保留原件，不能猜列解析 |
| CaptureArtifact.quality / checks | LogQuality / CheckResult[]，R | valid/limited/invalid 不与配置可比性合并 |
| ColumnMapping | `{source_column:string,target_field:string,source_unit:string,target_unit:string,conversion:string}` | 全字段 R；conversion 为登记的确定性转换名/版本，不执行模型代码 |
| TimeMapping | `{source_basis:string,target_basis:string,source_unit:string,target_unit:string,offset:float?,scale:float?,checks:CheckResult[]}` | 全字段 R；未知 offset/scale 只能保留原时基，不能强绑历史区段 |

`RunConditions={output_resolution:FieldValue<Resolution>,render_resolution:FieldValue<Resolution>,frame_generation:FieldValue<bool>,upscaling:FieldValue<string>,scene_conditions:map<string,FieldValue<Scalar>>}`，全字段 R。分辨率/FG 等须有观测或玩家声明；天气/昼夜/路线等已知条件在 scene_conditions 按适配器声明键保存，未知条件不猜值。

`TestProtocol` 为版本对象（04 所有），含 `game_id:ID,scenario:FieldValue<string>,conditions:RunConditions,controlled_variables:string[],metric_definition_ids:ID[],warmup_seconds:float?,sample_duration_seconds:float?,repeat_count:int?,note:string`（均 R）。时长非负、样本时长若有须>0、重复数若有须>=1；自然游玩或未规定项可 null，note 说明范围，不把原稿建议 60—120 秒/3 次写成通用充分性保证。该协议描述计划，实际 run_conditions 仍须逐轮核对；ComparisonProtocol 单独定义长帧比较口径。

历史日志人工关联使用04 §4.3的局部请求，由后台构造TimeMapping与StreamSelection；不改变LogImportInput的旧载荷。既有段关联须核对会话/段/绑定修订，新增artifact不能改旧映射。独立历史日志可在有实际开始时间依据时直接登记ended的PlaySession/SessionSegment及闭合初绑定；created_at表示登记时间，started_at/ended_at表示有来源的过去事实。初始applied_id=null/unverified，后补配置另按历史登记/绑定守卫，不能从当前采用反推。声明manifest用artifact EvidenceRef追溯，未核验的用户对应关系不升为系统观测。

### 6.2 统计与比较

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| MetricDefinition.metric_key / definition_version / unit | string / string / string，R | 名称相同但算法不同是不同定义；公共原 metric 不被覆盖 |
| MetricDefinition.algorithm_ref / frame_stream_kind / minimum_sample_rule | string / FrameStreamKind / string，R | 引用原始 04 的算法及分析器规则版本；不在本篇重写公式 |
| MetricValue.definition_id / numeric / unit / frame_stream_kind | ID / NumericValueV01 / string / FrameStreamKind，R | 输入数据不足 numeric=unknown；有限值不以 null 默默省略 |
| MetricValue.input_refs / denominator / method | ObjectRef[] / MetricDenominator? / string，R | 百分比/比率须有分母及输入；原来源未知定义按原文保存而非补公式 |
| MetricDenominator | `{value:float,unit:string,input_refs:ObjectRef[]}` | 全字段 R，值>=0；分母零时派生结果 unknown，不执行除零 |
| PerformanceSummary.artifact_id / bindings / analyzed_ranges | ID / BindingRef[] / TimeRange[]，R | 非空 bindings；每个 BindingRef 包含 segment_id:ID、segment_revision:Revision、binding_revision:Revision，均 R；读取该段元数据快照与当时绑定，不取当前投影；所引段及绑定必须已闭合，分析范围在二者交集内 |
| PerformanceSummary.analyzer_version / cleaning_protocol_version / manifest_file_id | string / string / ID，R | 原件、清洗、算法可复算；manifest 记录坏值/断段/排除及原始区间 |
| PerformanceSummary.valid_interval_count / effective_duration_s | int / float，R | >=0；只来自有效区段，不把缺口或排除时间计入 |
| PerformanceSummary.metrics / adaptive_long_frames / quality / checks | MetricValue[] / AdaptiveLongFrame[] / LogQuality / CheckResult[]，R | 每个有效连续区段各一项；无有效区段可空并在 checks 说明；质量与每项指标可用性独立，无有效数据可存诊断摘要但不能作有效比较 |
| AdaptiveLongFrame | `{range:TimeRange,threshold_ms:float?,cluster_count:int?,interval_count:int?,long_frame_duration_ms:float?,reason:string?}` | 全字段 R；无足够有效数据时统计 null 且 reason 非空；不用一个阈值冒充多段协议 |
| ComparisonProtocol（版本对象）.threshold_mode / threshold_ms / baseline_binding_refs | ThresholdMode / float / BindingRef[]，R | 阈值>0；baseline_derived 必须引用冻结基线，fixed 可空；汇总比较规则见原始 04 §7.2 |
| ComparisonProtocol.frozen_at / exploratory / cleaning_definition / clustering_definition / denominator_definition | Timestamp / bool / string / string / string，R | baseline_derived 须在查看调整后结果前冻结；事后探索另建协议并 exploratory=true |
| PerformanceComparison.summary_ids / protocol_id | ID[2] / ID，R | 两个不同摘要；引用协议具体版本而非“当前默认” |
| PerformanceComparison.level / per_metric / checks / limitations | ComparabilityLevel / MetricComparison[] / CheckResult[] / string[]，R | 一项不可比不隐藏其他观察；controlled 不等于统计显著或改善 |
| MetricComparison | `{metric_definition_id:ID,status:CheckStatus,left:MetricValue?,right:MetricValue?,delta:MetricValue?,reason:string?}` | 全字段 R；未知或不可比时 delta=null 并说明，不能计算伪改善比例 |

ComparisonProtocol的创建/默认展开/派生阈值计算与冻结由04 §4.2的CreateComparisonProtocolRequest定义，07只绑定路由。创建时保存不可变具体版本、后台frozen_at与固定基线/规则引用；修改需新协议，不改旧版本。探索性与冻结先后守卫用于新建比较，历史载荷/响应不改写；客户端不能自报冻结时刻或以exploratory=false代替时序检查。

原始 04 简称 Session，在共享模型中统一为 PlaySession；公共来源的 Comparison 仍是旧采集关系类型，不能混同 PerformanceComparison。本地统计不新写入公共 Evidence 的 local_measurement origin；若未来公开化需另行授权/脱敏流程。

## 7. 反馈、偏好与实验

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| Feedback.session_id / segment_id / raw_text | ID / ID? / string，R | segment=null 表示整会话；可仅有选项无文本，此时 raw_text=""，不是未知测量 |
| Feedback.selections / symptoms / interpretation_status | map<string,Scalar> / Symptom[] / InterpretationStatus，R | 保存玩家原选项与模型结构化解释；无时间不硬配尖峰 |
| Symptom | `{kind:string,severity:FieldValue<string>,range:FieldValue<string>,interpretation:InterpretationStatus,evidence_refs:EvidenceRef[]}` | 全字段 R；症状词表在 C4 定义；没有遥测不能确诊原因 |
| Preference（版本对象）.scope / scope_id / content | PreferenceScope / ID? / string，R | global 的 scope_id=null；game/experiment 须为对应游戏/变更组 ID |
| Preference.explicitness / lifecycle / feedback_id | PreferenceExplicitness / PreferenceLifecycle / ID，R | 推断仅 tentative；撤回另建版本，旧推荐保留原快照引用 |
| Diagnosis.input / hypotheses / primary_action / change_set_id | NextActionInput / Hypothesis[] / NextActionKind / ID?，R | primary_action=adjust 时必须有可校验 ChangeSet；其他动作 change_set_id=null |
| Hypothesis | `{id:ID,statement:string,support:EvidenceRef[],counter:EvidenceRef[],unknowns:string[],test_action:string}` | 全字段 R；假设不是已证实规则 |
| ChangeSet.base_applied_id / installation_id / changes | ID / ID / SettingChange[]，R | 基础为实际配置不可变版本；changes 非空；同一设置不得相互覆盖 |
| ChangeSet.hypothesis_id / evidence_refs / protected_preference_ids | ID / EvidenceRef[] / ID[]，R | 来源充分性由 C4 判定；明确偏好以版本引用保护 |
| ChangeSet.expected_direction / protocol_id / rollback_ref / checks | map<string,string> / ID? / ID / CheckResult[]，R | protocol_id 指 TestProtocol；rollback_ref 指同安装恢复目标的 AppliedConfiguration；未定义测试协议时 null 并限制结果，不能伪造固定场景 |
| SettingChange | `{setting_ref:SettingRef,old_value:FieldValue<Scalar>,new_value:Scalar,unit:string?,dependency_group_id:ID?,restart_required:FieldValue<bool>}` | 全字段 R；原值影响可恢复性时须补确认；不得用未知原值生成假恢复步骤 |
| ExperimentOutcome.change_set_id / base_applied_id / tested_applied_id | ID / ID / ID，R | 基础必须等于 ChangeSet.base；测试配置必须沿采用链对应本变更；附加改动须降级 |
| ExperimentOutcome.before_bindings / after_bindings / comparison_id | BindingRef[] / BindingRef[] / ID?，R | 无日志可 comparison=null；区段/版本关系仍须校验，不能用当前配置推断 |
| ExperimentOutcome.legacy_outcome / player_acceptance / effect_assessment | LegacyOutcome / PlayerAcceptance / EffectAssessment，R | 原枚举保存为兼容标签；独立维度为产品扩展，不反改旧义 |
| ExperimentOutcome.feedback_ids / evidence_refs / limitations | ID[] / EvidenceRef[] / string[]，R | 拒绝但未复测也保存原话；无日志不编造变化 |

接受情况与效果的组合归约见 state §5。只有文字且无实际基线时，允许 Diagnosis 输出排查/measure/research/insufficient_evidence；先取得少量原值/建立基线再创建正式 ChangeSet。该兼容分支落实原稿“可低确定性建议”而不虚构 base_applied_id。

## 8. 任务、问题、调用与幂等

### 8.1 任务与输入修订

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| TaskRun.type / user_scope_id / parent_id | TaskType / ID / ID?，R | 恢复或增预算创建新任务并关联 parent；非恢复首次 parent=null |
| TaskRun.revision / state / current_input_revision | Revision / TaskState / Revision，R | 聚合 revision 控制并发；input_revision 单独递增 |
| TaskRun.information_request_ids / budget / calls / checkpoint_id | ID[] / Budget / CallCounters / ID?，R | waiting_input 不占重队列；无持久检查点时 checkpoint=null |
| TaskRun.domain_result / stop_reason / result_refs | DomainResult? / TaskStopReason? / ObjectRef[]，R | 未有领域成果可 null；终态必须说明成果/原因/错误，Task completed 不等于 actionable |
| TaskRun.cancel_requested / last_error_code | bool / string?，D=false/null | cancel_requested 只为控制标记，不增终态枚举 |
| TaskRun.active_elapsed_seconds | float，D=0 | 非负，已持久计量的活动执行时长；等待不计入；检查点与事件中保存累计值 |
| TaskInputRevision.task_id / input_revision / input / supersedes_revision | ID / Revision / TaskInput / Revision?，R | 唯一键 `(task_id,input_revision)`；初始 supersedes=null；原输入不可变 |
| TaskInputRevision.cause_ref | ObjectRef?，D=null | 后补回答引起修订时关联 InformationResponse |
| TaskEvent.task_id / sequence / input_revision / from_state / to_state / cause / related_refs | ID / Revision / Revision / TaskState? / TaskState / string / ObjectRef[]，R | sequence 在任务内唯一；首事件 from_state=null；input_revision 的变更记录需可追溯到输入修订 |
| TaskEvent.active_elapsed_seconds | float，R | 保存事件提交时累计活动执行时长；不得因等待或重启把它重置为 0 |
| TaskCheckpoint.task_id / input_revision / completed_steps / artifact_refs / call_ids | ID / Revision / string[] / ObjectRef[] / ID[]，R | 只重用输入兼容且可验证的完成步骤；不恢复进程内未持久状态 |
| Budget.max_urls / max_searches / max_followup_rounds / max_queries_per_gap / max_model_calls / max_read_calls | int，D=12/12/2/2/16/36 | >=0，bool 非 int；与现有 Limits 默认一致，不保证覆盖充分 |
| Budget.max_seconds | float?，D=null | 非负的活动执行时间预算；waiting_input 的停留时间不消耗该预算；恢复任务预算显式新给 |
| CallCounters.search / read / model | int，D=0 | 已登记进入可能发送阶段的调用尝试次数，非成功次数或供应商计费次数；不把重读遗漏到计数之外 |

TaskType 的输入是带 `kind` 的 TaskInput 联合类型，见 §9；禁止通用任意 JSON 或模型自定义任务类型。CollectionResult/Readiness 等用带领域名的 DomainResult 包装，避免混进 TaskState。

`DomainResult={domain:TaskType,code:string,input_revision:Revision,checks:CheckResult[]}`，全字段 R；code 的合法集合和 Task 状态映射见 state §3。input_revision 必须是产生该成果的具体输入修订，不能以当前输入替换旧成果上下文。

### 8.2 补充问题与回答

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| InformationRequest.source_task_id / version / context_refs | ID / Revision / VersionRef[]，R | version 为请求乐观锁；问题关联具体历史或当前上下文，不只指最新设备 |
| InformationRequest.expected_context_revision / fields / state | Revision / RequestedField[] / InformationRequestState，R | expected_context_revision 是该请求上下文组合的修订号，非任意一个对象 version |
| InformationRequest.result_refs / superseded_by_request_id | ObjectRef[] / ID?，R | 被替代时指向替代请求或 note 说明无需新问题 |
| RequestedField | `{field_path:string,purpose:string,required_for:string[],candidates:Scalar[],answer_state:AnswerState,value:FieldValue<Scalar>}` | 全字段 R；required_for 说明影响哪些操作，空数组表示非关键；不能把空白自动当跳过 |
| InformationResponse.request_id / expected_request_version / expected_context_revision | ID / Revision / Revision，R | 必须与提交时请求一致；旧版本 409，不部分落库 |
| InformationResponse.answers / skipped_fields / raw_text | map<string,Scalar> / string[] / string，R | 两集合不能交叉、字段必须属于请求；自然语言归一不可靠时保留原话，字段仍 unresolved |
| InformationResponse.conflict_selections / submitted_at | map<string,int> / Timestamp，R | 无冲突选项可空映射；索引对应该问题候选依据；submitted_at 后台登记 |
| InformationResponse.result_refs / post_request_version / post_context_revision | ObjectRef[] / Revision / Revision，后台生成 | 回答与结果快照/请求/任务输入修订原子登记；自己的部分回答不使下一批回答过时 |

InformationResponse 的记录不可变；InformationRequest 是由回答/替代事件形成的投影。问题终态后补、关键项已明确跳过的继续规则见 state §7；领域字段更改仍由所属模块校验。

### 8.3 外部调用、去重与文件注册

| 对象/字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| ExternalCallRecord.task_id / input_revision / kind / call_state | ID / Revision / string / ExternalCallState，R | kind=search/read/model；状态与供应商返回事实分开审计 |
| ExternalCallRecord.provider / requested_model / returned_model | string / string? / string?，R | 非模型调用二者 null；模型缺返回名时 returned_model=null，不拿请求名代填 |
| ExternalCallRecord.prompt_version / output_schema_version / input_hash | string? / string? / Hash，R | 非模型可 null；模型调用必须给提示与 Schema 版本 |
| ExternalCallRecord.intent_saved_at / sent_at / finished_at | Timestamp / Timestamp? / Timestamp?，R | intent 保存先于可能发送；无可靠时刻不补造 sent_at |
| ExternalCallRecord.provider_request_id / response_file_id / usage | string? / ID? / ProviderUsage?，R | 供应商未返回计量则 null；响应原文受管理并脱敏，不存密钥/完整认证头 |
| ExternalCallRecord.billing_state / error_code | BillingState / string?，R | 未知费用不估金额；调用成功不等于提取/推荐验证通过 |
| ProviderUsage | `{input_tokens:int?,output_tokens:int?,reported_cost:string?,currency:string?}` | 全字段 R；金额仅在供应商真实报告时保存，金额与币种同时出现；未报告分量为 null |
| IdempotencyRecord.user_scope_id / method / canonical_route / key / request_hash | ID / string / string / string / Hash，R | 唯一键为 `(user_scope_id,method,canonical_route,key)`；路由包括实际目标资源 ID，不能仅按路径模板去重 |
| IdempotencyRecord.task_id / result_refs / response_status / response_body / lifecycle | ID? / ObjectRef[] / int / JSON / IdempotencyLifecycle，R | 注册后的响应快照不可随任务完成改写；task 当前状态从任务查询取；清理保留墓碑 |
| IdempotencyRecord.request_schema_version | string，R | 规范化规则版本固定；升级后旧键仍可匹配旧请求快照，不能当新请求再执行 |

response_body 只含受控 ID/状态及错误包，不保存未经清洗的请求或密钥。初次接受任务的响应即使是 queued，重试仍返回原接受语义并附同一 task_id；查询任务得到最新状态。完整事务、未知发送窗口、墓碑与规范 hash 规则由 B3 规定。

## 9. 跨模块 DTO 与公共调用边界

所有 *Input 字段按表格 R/D 规则校验；它们是逻辑参数，HTTP 路由的完整集合在 C/07 定义。同步成功返回 `ResourceResult`，异步返回 `TaskAccepted`；业务失败使用 B3 错误包，领域 blocked/partial 作为成功登记的领域结果返回。

| DTO | 字段（未标 D 均 R） | 消费者与副作用 |
|---|---|---|
| KnowledgeInput | kind=`knowledge`、game_context_id:ID、device_snapshot_id:ID?、prior_package_id:ID?、budget:Budget | 02 经 07 开任务；无设备可先收集，身份歧义保持条件或补充；无 FPS/偏好必填项 |
| KnowledgeImportInput | kind=`knowledge_import`、file_ids:ID[]、game_context_id:ID、mapping_version:string、identity_mapping_ids:ID[] | 02a 经 07 对已受管理的冻结文件组建导入快照；file_ids 非空，不接受任意本机路径，游戏身份未知只暂存 |
| ContextDeclarationInput | kind=`context_declaration`、installation_id:ID?、game_id:ID、fields:map<string,Scalar> | 01 保存玩家主动选择/更正的原始字段及声明；字段名须为 01 声明的上下文/路径字段白名单，不触发系统执行；非重任务输入 |
| RecommendationInput | kind=`recommendation`、game_context_id:ID、device_snapshot_id:ID、package_id:ID、preference_ids:ID[]（D=[]）、applied_id:ID?（D=null）、history_summary_ids:ID[]（D=[]） | 03 读取固定快照；ExplicitPreferences 是所引 Preference 的只读视图，不新增独立表 |
| AdoptConfigurationInput | kind=`adopt_configuration`、origin:AppliedOrigin、installation_id:ID、device_snapshot_id:ID、game_context_id:ID、expected_current_applied_id:ID?、recommendation_id:ID?、base_applied_id:ID?、change_set_id:ID?、restored_from_applied_id:ID?、declared_settings:SettingDeclaration[]、preset_reference:string?、claimed_activation:ActivationState | 01；后台准备声明与确认/来源事实，校验后将声明、完整合并快照与当前指针原子提交；按推荐一次确认继承哪些确定项仍沿 Q3 建议，不继承未落实条件项 |
| HistoricalConfigurationInput | kind=`historical_configuration`、installation_id:ID、device_snapshot_id:ID、game_context_id:ID、segment_ref:BindingRef、effective_range:TimeRange、adopted_at:Timestamp?、declared_settings:SettingDeclaration[]、preset_reference:string?、claimed_activation:ActivationState、evidence_refs:EvidenceRef[] | 01 经 07 同步登记历史配置及声明；evidence_refs 非空，核验已结束段的具体修订与时段依据，生成 registration_kind=historical_record、origin=baseline 的独立版本；不检查/推进当前指针，不继承当前值；绑定另走 BindConfigurationInput |
| CreateSessionInput | installation_id:ID、device_snapshot_id:ID、game_context_id:ID、applied_id:ID?、configuration_state:ConfigurationState、scenario:FieldValue<string>、test_protocol_id:ID?、run_conditions:RunConditions | 04；保存活动会话/初段/初绑定，初绑定 BindingRange.end=null，允许 applied=null；推荐引用从实际采用关系读取或显式另填可选 recommendation_id:ID?（D=null） |
| BindConfigurationInput | segment_id:ID、expected_segment_revision:Revision、expected_binding_revision:Revision、applied_id:ID、effective_range:TimeRange、evidence_refs:EvidenceRef[] | 04；同时校验场景/区间投影与绑定版本；追加绑定，不改旧摘要或当前指针；冲突不写入 |
| AnalyzeInput | kind=`analysis`、artifact_id:ID、bindings:BindingRef[]、analyzer_version:string、cleaning_protocol_version:string | 04；只接受已闭合段/绑定的具体修订；生成新摘要，不能静默用最新绑定重算覆盖旧摘要 |
| CaptureInput | kind=`capture`、session_id:ID、segment_ref:BindingRef、adapter_id:string、adapter_version:string、protocol_id:ID? | 04；protocol_id 指 TestProtocol；只允许已验证适配器；保存初始段/绑定修订，后续切段显式记新修订；同会话已有活动采集拒绝重复启动 |
| LogImportInput | kind=`log_import`、file_id:ID、session_id:ID、segment_ref:BindingRef、source_schema:string | 04；受管理上传原件，不接受任意路径，段/绑定版本固定，未知格式保留但不猜列 |
| FeedbackInput | kind=`feedback`、session_id:ID、segment_id:ID?、raw_text:string、selections:map<string,Scalar> | 05；保存原文/选项后解析，不强迫非空文本 |
| NextActionInput | kind=`next_action`、session_id:ID、bindings:BindingRef[]、summary_ids:ID[]、feedback_ids:ID[]、current_applied_id:ID?、package_id:ID? | 05；无日志可空摘要、无基线不得产正式可采用 ChangeSet；research 意图交应用层另建任务 |
| CompareInput | summary_ids:ID[2]、protocol_id:ID | 04；创建不可变 PerformanceComparison；查询既有结果无写副作用；具体创建路由 C 定义 |
| OutcomeInput | change_set_id:ID、base_applied_id:ID、tested_applied_id:ID、before_bindings:BindingRef[]、after_bindings:BindingRef[]、comparison_id:ID?、player_acceptance:PlayerAcceptance、feedback_ids:ID[] | 05；程序核对链路后推导效果与兼容标签，不接受客户端直接声明 controlled/improved |
| ResumeTaskInput | parent_task_id:ID、checkpoint_id:ID?、budget:Budget | 07；新建 parent 关联任务；有检查点须属于该父任务且输入兼容，仅复用可核验步骤；父任务没有检查点时显式传 null，从其已持久输入登记新任务，仍须核对全部调用审计，不以空检查点绕过未知调用限制 |
| DeviceScanInput | kind=`device_scan`、game_context_id:ID?、field_paths:string[] | 01 经 07；仅受控探测范围，未知字段路径拒绝 |
| InformationResponseInput | request_id:ID、expected_request_version:Revision、expected_context_revision:Revision、answers:map<string,Scalar>、skipped_fields:string[]、raw_text:string、conflict_selections:map<string,int> | 07＋领域校验者；短事务保存回答，不经过等待自身的重队列 |

`TaskInput` 联合类型只包含 KnowledgeInput/KnowledgeImportInput/RecommendationInput/AnalyzeInput/CaptureInput/LogImportInput/FeedbackInput/NextActionInput/DeviceScanInput；恢复任务保存可复用父输入及新的 budget，额外保存 ResumeTaskInput 作为原因，不把恢复本身当可任意执行的新业务类型。ContextDeclarationInput 与 HistoricalConfigurationInput 是同步声明，不进入该联合类型。

这些 DTO 是受信任应用层间的逻辑输入，不允许浏览器/模型自报 provenance、system_probe、检查通过或比较资格。HTTP/CLI 入口只接收声明原值、选择项和已有引用，07 与领域所有者核验后构造 FieldValue/RunConditions 等包装；具体外部请求体在 C/07 定义。原始声明先保存，再把后台生成的 PlayerDeclaration/InformationResponse 引用附到新事实中，避免要求客户端事先提供尚不存在的声明 ID。

`TaskAccepted={task_id:ID,input_revision:Revision,state:TaskState}`，全字段 R，初次 state=queued。`ResourceResult={refs:ObjectRef[]}`，R 且非空。公共创建调用带 `MutationContext={user_scope_id:ID,idempotency_key:string,request_id:ID}`，全字段 R；HTTP 用户作用域从后台解析，不信任客户端任意指定其他用户；客户端不提交 created_at/id。

## 10. 旧资料兼容映射与明确缺项

| 原字段/能力 | 产品映射 | 禁止推导/需保留限制 |
|---|---|---|
| run.id、game_id、input.game_name/game_aliases | ImportSnapshot.legacy_run_id、IdentityMapping 的命名空间与原值 | 旧 game_id 为 casefold 名称哈希截断，不证明别名/平台统一身份 |
| sources[].id、revision_id、document | capture_scope_id→SourceRevision 映射；upstream_revision_id；冻结原件 | 旧 source.id 是捕获作用域，不是跨运行 canonical Source ID；相同正文 hash 不证明相同作者或独立来源 |
| row.id/local_id/reference_scope/revision_id | 原样保存并建立 ImportedRecord；KnowledgeRecordRef 包含具体包与修订 | 不把同名 local_id 跨 source/run 直连；不得按冒号拆 ID 猜作用域 |
| payload 内 setting/configuration/environment/evidence IDs | 经同一 import_snapshot/capture_scope 内类型化映射解析 | 坏引用/重复 ID 保持诊断隔离，不能从别的来源拿同名对象填补 |
| record_status/extraction_status/applicability/recommendation_status/check_history | 各自保留为原状态/检查输入，再另存产品用途的 CheckResult | accepted 不等于本机适用；旧 pass 也不能绕过新的输入/用途核验 |
| SettingValue、EnvironmentField、Proof、scope | 保留原 JSON，产品 FieldValue/SourceLocationRef 为包装与映射 | 不将来源观察改成设置建议；环境字段自身出处不丢失 |
| NumericValue.lower/upper | 保留原类型并另存 BoundarySemantics | lower_bound/upper_bound 的严格性缺项不能猜；不能把区间中点当均值 |
| Evidence.origin | 沿用 source_statement/source_author_measurement/agent_derived | 旧 Schema 不接受契约提到的 local_measurement；本机统计留私人层，不冒充已支持该枚举 |
| Constraint.when/requires、verified_scope | 保留声明式等值条件及核验范围 | 复杂条件/版本序/别名/近邻 GPU 能力尚非完整执行器；未知表达不执行任意代码 |
| VersionImpact/转载线索/setting canonical_name | 保存已提供关系与检查范围 | 不因字段存在就声称完整传播、独立性证明或跨版别名归一 |
| completed_scope/partial_scope、stop_reason、interrupted | 原样保存，映射规则见 state §3 | interrupted=true 时不按 budget_exhausted 自动解释为正常预算结束 |
| gaps/candidates | 缺口原样保留、按原 records 重建可审阅候选 | candidates 不是可执行完整配置；跨文件一致性不足不可发布 |
| 四份研究 JSON | origin=research_json；文件 hash、人工/研究字段映射单独记录 | 不直接声称符合采集 Extraction Schema；不改原答案以通过导入 |

新增字段无法可靠取得时按未知/缺项表达；不为满足产品必填关系造假对象。不能发布的导入仍保留 ImportSnapshot 原件与错误，不进入可消费的 KnowledgePackage。来源发布时间/测试时间未知保持 null，导入时间只用于产品对象创建时间。

## 11. 结构化示例与契约验收

下例仅为 `FieldValue<Scalar>` 形状示例：玩家明确跳过显存补充。不是 FH6/本机探测结果；ID 为虚构 UUID，真实 Provenance 应指向实际回答。旧 unknown_reason 与获取结果各自保留。

```json
{
  "value_state": "unknown",
  "value": null,
  "unknown_reason": "not_provided",
  "candidates": [],
  "provenance": [],
  "acquisition_status": "skipped",
  "attempts": [{
    "id": "00000000000000000000000000000001",
    "method": "user_provided",
    "status": "skipped",
    "attempted_at": "2026-11-01T00:00:00Z",
    "error_code": null,
    "information_response_id": "00000000000000000000000000000002"
  }],
  "applicability_note": null,
  "resolution": null
}
```

下表为未来自动化契约验收，不是本轮已有测试。每行目的由“关联要求”与断言共同表达；输入均为受控抽象夹具，不使用真实 FH6 设置。

| 编号/关联要求 | 初态与输入 | 步骤 | 输出及状态断言 | Mock / 真实环境 |
|---|---|---|---|---|
| B-DAT-01 / A02 | 两次导入含同 local_id、不同 run/source；另有同运行重复键 | 建映射、解析引用、尝试包发布 | 不跨作用域误连；重复键诊断隔离；原 ID/hash 保留 | 固定 JSON；不需真实环境 |
| B-DAT-02 / A03、C11 | 探测失败、随后玩家回答；另有明确跳过和未探测项 | 生成新快照，读取新旧值 | 失败历史保留，user_provided 不变 observed；三个 unknown 过程可区分 | 探测桩/回答夹具；不需真实环境 |
| B-DAT-03 / A04、D01 | 实际 A 含推荐外自定义值，C 只改一项，当前已被别处推进 | 合并差异并提交过期 expected_current | 无静默覆盖；409；成功路径完整保留未改项/未知与正确父链 | 内存领域夹具，后续用临时 DB；不需游戏 |
| B-DAT-04 / D03、D04 | applied=null 的旧区段，之后确认当前配置 | 保存回答，再尝试把新当前值当过去依据绑定 | 会话可保存；无当时依据则不绑定；有历史依据则追加 binding_revision，旧摘要不变 | 时段与回答夹具；不需真实环境 |
| B-DAT-05 / A09 | 旧 lower_bound 无严格性，另有区间 FPS 与 HDR 检测 | 导入并请求严格边界过滤 | 原载荷不变；严格性 unknown，过滤受限；区间不求均值、观察不变开关建议 | 旧 Schema 固定夹具；不需真实环境 |
| B-DAT-06 / D05 | 两摘要使用不同绑定/阈值，另有零有效时长 | 建比较协议并校验输入 | 明确具体绑定/协议；零分母不除零；不能同口径重算则仅相应指标降级 | 人工时间序列；真实工具格式按 PERF-T01 另验 |
| B-DAT-07 / A12、A13 | 无基线文字反馈；另有接受但未测和更快但拒绝 | 生成下一动作/Outcome | 无假 ChangeSet/回滚值；接受与效果字段独立，不假造实测变化 | 反馈/模型输出桩；不需真实环境 |
| B-DAT-08 / A05 | 冻结导入缺文件或复制期间来源改变 | 校验 manifest 与引用，再请求发布 | 保留诊断原件，未发布，推荐不可用；不能导出伪成功包 | 临时文件与 DB；不需网络 |
| B-DAT-09 / 历史一致性、D05 | 旧摘要已固定 segment_revision；随后更正场景/排除范围 | 保存新段修订，再读旧摘要并以旧段版本提交绑定 | 旧元数据与旧统计仍可复现；绑定提交冲突；新分析显式引用新段修订 | 版本/时间区间夹具；不需真实环境 |
| B-DAT-10 / D03、D05 | 自然游玩无预计结束时间，配置可已知或未知 | 创建会话与开放初绑定，结束/切段后再分析；另用开放修订请求分析 | 初绑定 end=null 合法；闭合产生新段/绑定修订，旧修订保留；开放修订不可分析，有限时长不靠猜测 | 时间/绑定夹具；不需真实环境 |
| B-DAT-11 / D03、D04 | 旧段配置未知，当前指针为 B；后来取得旧段使用 A 的依据 | 提交 HistoricalConfigurationInput，随后显式补绑 | 新历史 A 有独立 ID/声明/时段依据，未知 adopted_at=null；不虚构与 B 的父链，当前仍为 B；补绑后新分析引用新修订 | 声明/历史区段夹具；不需真实环境 |
| B-DAT-12 / A06、D02 | queued 任务未建检查点即重启；另有首检查点前的未知调用 | 以 checkpoint_id=null、明确预算恢复；用外来检查点/隐去已有检查点另试 | 无检查点也可登记 parent 关联任务；外来/隐去已有检查点拒绝，未知调用不因 null 重发 | 调用桩/任务夹具；不需网络 |
| B-DAT-13 / 版本兼容、D02 | 0.1 采用对象/有限绑定及已登记幂等响应，另有公共知识原载荷 | 用兼容视图读取，再重送原提交；创建 0.2 历史记录 | 原对象/原载荷/旧 hash/响应不改写；旧采用映射 current_adoption，有限绑定可读；新历史对象标 0.2，不重跑旧任务 | 旧格式/幂等夹具；不需真实环境 |

## 12. B2 退出边界

本篇固定共享名称、字段、引用、必填/null/默认策略、不可变规则和原型映射；B3 在此基础上补齐合法状态与提交/恢复约定。C2/C3/C4 仍需完成覆盖与 readiness 归约、具体本机字段/设置扩展、候选算法、适配器签名、具体 CSV 格式与数值容差；这些模块缺项不由模型临时解释为已确定规则。

0.2 修订只影响产品建议契约，公共知识 v0.1 及冻结原件保持原格式。旧 product-contract/0.1 对象按原 schema_version 读取：AppliedConfiguration 在兼容视图中映射 registration_kind=current_adoption，原 adopted_at 保留；旧有限 effective_range 可无损读取为闭合 BindingRange，不为旧开放段猜造结束时间。新历史记录及开放绑定须按 0.2 写入；不能只换旧对象版本标签而补造事实。旧幂等记录继续按其 request_schema_version/原 hash 与响应核对，规范化规则和历史响应不随升级重写；具体迁移代码与旧记录夹具在 C1/07 定义。
