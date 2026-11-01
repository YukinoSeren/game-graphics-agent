# D-P01—D-P04 契约补全与关闭记录

> 历史补全记录：此处0.3关闭结论随后受到独立审计限定。2026-11-02四项审计问题修订及0.4兼容见[12修订记录](./12-phase-d-remediation.md)，当前实施入口见[编码交接](./coding-handoff.md)；下文保留当时事实。

> 日期：2026-11-01；执行授权：用户要求“开始D-P01—D-P04契约补全”。范围为技术设计文档，未创建业务源码/测试、未安装依赖、未调用模型/启动游戏或迁移产品库。
> 新产品建议基线：`product-contract/0.3`；公共知识仍为`0.1`。34个拟建实现任务均未开始，Q1—Q3/R03状态不变。

## 1. 交付与关闭结论

四项缺口已补为可引用的外部请求、资源返回、来源/状态守卫、事务/幂等及失败动作；定义写在对应模块，shared管理跨域字段/状态/错误，07只集成，06/08分别承接玩家流程与验收。这里的“关闭”仅表示**文档契约闭合**，不表示任何新增接口已经运行。

| 补全项/原问题 | 唯一定义 | 集成/状态/来源 | 新增主要验收 |
|---|---|---|---|
| D-P01 / D-REV-01 | [02 §5.1 游戏目录/选择](./02-knowledge-and-collection.md#51-d-p01-空库游戏目录与选择)、[01 §5.1 安装](./01-device-game-configuration.md#51-d-p01-本机安装登记与选择) | GameSelection及声明manifest、用户范围provisional Game、01构造GameContext；路径未知安装独立登记 | KNW-V06、DEV-V06 |
| D-P02 / D-REV-02 | [01 §5.2 本机设置登记](./01-device-game-configuration.md#52-d-p02-本机设置条目登记) | player namespace、精确键/后继唯一、LocalSettingRegistration；登记/采用分离 | DEV-V07 |
| D-P03 / D-REV-03 | [04 §4.4 测试协议/指标目录](./04-performance.md#44-d-p03-测试协议创建与指标目录) | 明确metric_registry_version、规则manifest、TestProtocol父版本/声明文件；比较协议另建 | PERF-V09 |
| D-P04 / D-REV-04 | [01 §5.3 关联更正](./01-device-game-configuration.md#53-d-p04-已处理问题的关联更正)、[shared state §7](./shared/state-conventions.md#7-informationrequest-回答与继续) | InformationAmendment、facts_only新请求/直接后继唯一；源任务任何分支均零写 | DEV-V08、B-STA-13 |
| 全局事务与普通旅程 | [07 §11 路由](./07-data-and-architecture.md#11-c5-路由集成登记)、[06 §7.1—7.4](./06-player-experience.md#71-d-p01-空库进入与本机记录)、[08 §11.1](./08-validation-and-delivery.md#111-d-p补全后的验收入口) | manifest先就绪、短事务发布/原响应；目录GET无写入/外部调用；客户端无手填后台ID | ENG-V07、UX-V09、VAL-V08、B-ERR-12 |
| 旧版本兼容 | [shared data §12.1](./shared/data-contracts.md#121-d-p补全的03读写兼容) | 旧字段只在读取视图展开；不改旧对象/哈希/响应，不伪造身份/计划证据 | B-DAT-14 |

新增模块验收8项（原55→63），基础验收3项；所有原编号保留。实现任务仍34项，扩充DoD和测试路径，未将这四项文档任务误计为产品功能完成。

## 2. 取舍与兼容边界

1. **游戏初始身份允许待核验。** 直接要求verified Game会使空库选游戏重新依赖预置目录或外部检索；本稿采用用户范围provisional身份，明确名称即可收集/归档。平台未知用空scope＋unknown检查，不默认商店/路径；没有身份/平台证据就不发布为匹配知识包。有歧义必须选候选，不以排序第一项替用户决定。
2. **本机目录先登记、采用另提交。** 在AdoptConfigurationInput里嵌入任意未定义条目会混淆名称身份与值事实；本稿先返回LocalSettingDefinition，再以原SettingDeclaration确认值。目录更正不自动迁移值/替换历史ID，避免自定义配置或回滚依据漂移。
3. **两类协议各有入口。** TestProtocol是测试计划，ComparisonProtocol是比较口径；都不能自行证明实际执行或受控。指标从固定项目规则目录选key/版本，空库首次创建由后台事务登记定义映射，既不要求前端手填ID，也不让GET产生持久副作用。
4. **更正使用新问题。** 直接改resolved/skipped旧请求会丢历史；重新运行源任务又可能重发调用。facts_only模式只保存新问题/回答/派生事实，即使原任务running也不改其输入。仍open的终态后补继续原路径，已处理问题另建关联后继，两者分开。
5. **新对象升0.3。** Game未知平台/身份、协议声明/父版本、问题继续模式扩展了共享表达范围。旧0.1/0.2对象只读映射，旧key仍按原Schema/hash/响应去重；旧Game没有证据就显示legacy_unverified，不能补verified；旧协议缺声明不能补造当时计划。

上述是现有需求的技术建议与边界补全，未批准本地网页形态、首游戏、一次确认方式或作者方案优先等待定产品选择；没有改变手动设置、来源可追溯、未知保留或无证据不归因的原则。

## 3. 静态走查：成功与失败路径

以下逐步对照定义和固定引用，属于static_design审阅，**未执行HTTP、数据库或浏览器程序**。它给未来开发者可转成CaseSpec的输入/操作/断言，而非伪造运行日志。

### 3.1 空库只输入游戏名

初态没有Game/安装/上下文/模型配置，用户仅给“示例游戏甲”。GET games返回空choices、next_cursor=null，零写入/零网络。POST game-selections按02完整示例填其余字段null，得到GameSelection＋provisional Game＋GameContext引用；平台仍未知、上下文无安装，页上显示待核验，不要求GPU/FPS/偏好。原key丢响应重送得到同一组ID；不同key并发同用户/同声明槽复用同series，不因SQL null唯一性漏检重复创建身份。

读取GameSelection.game_context_id后可提交KnowledgeInput且device_snapshot_id=null，预算显式给出；新收集在身份/平台检查前保留原件/缺口，不能冒称匹配包完成。有两个同名verified候选时返回needs_choice且game_context_id=null，只有明确选择的第二次提交才得到上下文，不能从歧义结果发起知识任务。

需要本机记录时POST installations引用返回上下文，返回独立安装/带安装的新上下文；路径null、标签仅展示、当前采用不变。随后device-scans失败→集中问题，未知快照/任务limited或unavailable按事实归约。选既有安装必须带其revision，跨游戏422/过时409均无半上下文。

### 3.2 无公共知识登记自定义项

初态有合法安装但无包/配置读取适配器。POST local-settings原名与可空菜单路径，后台namespace=player并从声明manifest得到证据；返回定义及registration。两客户端同精确定位键复用定义，不从中文名推断其他来源等价。另将该ID填入SettingRef并提交baseline A，确认值由玩家声明而非system_probe。

ChangeSet仅改另一个有依据的条目，采用B从A完整合并，原自定义值/未改未知项保留。目录更正产生同series的后继版本但不改A/B里的旧ID；恢复旧名称仍另增ID/version，不复用祖先成环。当前精确键仅覆盖各series有效头；两个不同key争同一父，仅一方保存，另一方LOCAL_SETTING_CONFLICT。登记已提交而后续采用冲突时，登记保留、采用零部分事实；跨安装条目拒绝。旧历史配置补录仍需独立时段依据，不由新目录登记证明过去值。

### 3.3 空库保存测试计划并复测

初态有GameContext，数据库没有MetricDefinition/TestProtocol，但程序打包规则目录存在。GET metric-definitions返回固定key、registry_version及可空definition_id，零写。POST test-protocols使用04示例的metric_registry_version/keys、玩家场景/条件及显式null热身；后台同事务创建所需固定定义映射、TestProtocol及声明manifest。原key回同协议/文件，异内容409；两个并发创建复用规则映射ID。

基线会话明确引用此TestProtocol ID，实际时长/场景/生效仍逐轮核对。闭合基线后另POST comparison-protocols保存共同阈值；后测会话沿用测试计划，CompareInput只引用比较协议。错型ID为REFERENCE_INVALID。改计划新key＋固定父协议另建子版本，旧会话不取最新。缺规则版本、坏key、半分辨率、bool次数、跨游戏父均无半协议；没有实际测试不标controlled/local_effect。

### 3.4 跳过后补充与原任务并行

初态源问题skipped/resolved，原任务分别测试为终态和running两种情形。POST information-amendments带源两修订/所选字段/理由；创建新的facts_only问题，amends_request_id指源，source_task_id保留原触发关联，version/context revision各1。旧回答仅供参考，所选字段新答案仍unanswered，不从当前快照填过去事实。

对新问题POST information-responses保存新回答/派生事实/问题post版本，**对源TaskRun、TaskInputRevision、TaskEvent写入数量均应为0**；跳过、原历史引用暂不可用及源任务同时取消也保持此断言；原依据不可用不自动替代问题或改背景，修复后仍对本问题补答。同源不同key并发建后继只有一个成功，另一方返回INFORMATION_CONTEXT_CHANGED及后继；原key重试先回原201而不是再走后继冲突。源尚open/partial则引导原问题，源superseded则读取替代。再次更正从最新已处理后继发起，不改祖先。

过去配置只得到新依据，仍需HistoricalConfigurationInput→BindConfigurationInput；当前采用须另明确确认，分析须显式新建任务/预算。更正创建成功但回答断连是两个独立草稿，各重送原key，不重做源任务或自动调用模型。

## 4. 工程/安全与幂等复核

| 边界 | 本次新增操作必须满足的契约 |
|---|---|
| 权限 | 持久用户从MutationContext后台解析；provisional Game/本机记录/局部声明只在所属用户可见；客户端不能伪造user_scope、来源或检查 |
| 文件窗口 | 所有新声明manifest复用07 FileOperation；原key准备关联可恢复，最终事务前文件不可消费；纯冲突无半事实 |
| 并发唯一性 | 游戏声明槽显式编码null平台；条目精确键/两个更正后继/规则定义映射各有唯一约束，引用与原响应同事务 |
| 原响应 | 同key先查重，后台新ID/时间/本次派生文件不参与外部输入hash；提交的metric_registry_version属于业务内容，不能隐式改最新 |
| 旧格式 | 旧对象/请求/响应原字节不重写；映射不把未知升级为pass、系统观测或已执行；不借0.3迁移重跑旧key |
| 副作用 | 新增目录查询/同步创建均不探测/联网/调用模型；实际device_scan/knowledge等仍必须明确注册任务和预算 |

## 5. 验证记录与实施交接

检查范围为全部技术设计Markdown的链接/锚点、表格、JSON示例、请求字段示例一致性、ID格式、状态/错误归属、34任务与63场景引用、任务依赖及文件变更范围。新增验收场景只检查定义，不作为程序通过证据。

本轮静态检查实际结果：18份技术设计Markdown，183个本地链接/锚点、141个表格、14个JSON示例均未检出错误；5种新增POST请求的示例字段与各自DTO定义一致，均有对应路由。34个源任务、34行依赖表与34张任务卡集合一致，依赖图无环；原55个模块场景全部保留，新增8项、共63项均被计划引用；基础/架构场景共42项，TaskType/TaskState取值未变。工作区17份变更均限本技术设计目录，阶段A快照、源码、原始设计、依赖及冻结数据未改；git diff --check通过。

本轮不重复运行未改动的原型测试；初次D的45项通过、24/24冻结回归仍为既有记录。新的产品测试、工具/游戏/模型兼容、图像与本机效果都未执行；没有新增产品库可迁移。

下一步如明确授权编码，从[全局实施计划§8](./implementation-plan.md#8-首个实施切片与交接方式)的ENG-T01/VAL-T01基础切片开始；使用0.3字段与旧格式兼容规则，按新增用例扩充原34任务。Q选择与真实能力门禁仍按各自影响点检查，不把本次契约交付当作产品已实现。
