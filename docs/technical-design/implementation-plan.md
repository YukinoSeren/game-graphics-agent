# 全产品实施计划：D阶段建议基线

> **For agentic workers:** 后续获得编码授权后使用 executing-plans 按任务执行；需要委派时再使用 subagent-driven-development。先读本计划与其引用规格；按 `- [ ]` 记录实际完成，不把计划中的预期结果当作已运行结果。

**Goal:** 在保留现有采集原型的前提下，逐步实现可追溯的单游戏知识、推荐、手动采用、性能与反馈闭环。

**Architecture:** 建议新增 `game_app/` 单应用包，所有领域事实按所有者写入，由应用层协调SQLite短事务、受管理文件和持久任务。`game_collector/`保留独立CLI；新产品外部调用逐次经过预算与持久审计，旧产物只读冻结导入。

**Tech Stack:** Python `>=3.11`、Pydantic 2、标准库SQLite，复用requests/BeautifulSoup；本机基线Python 3.13。HTTP/模板/图表仍为Q1路线下的待固定适配层，不在本次安装或改依赖。

**Spec:** [D-P契约补全记录](./10-contract-completion.md)、[D全局复核](./09-global-review.md)、[索引与决策](./README.md)、[共享数据](./shared/data-contracts.md)、[共享状态](./shared/state-conventions.md)、[共享错误](./shared/error-conventions.md)，以及下表所列模块规格。 当前入口：[审计修订](./12-phase-d-remediation.md)、[编码交接](./coding-handoff.md)；09/10保留历史证据。

## 1. 全局约束与执行状态

- 当前只交付文档；**34个实现任务均未开始**，本计划不构成恢复编码或运行真实游戏/付费模型的指令。
- 原知识契约`0.1`和建议产品契约`product-contract/0.4`分开；字段、枚举、错误唯一定义在shared，局部请求在模块，路由只在07集成。
- 已确认C01—C11及交接原则继续有效；Q1—Q3待确认，R03为建议，未确认不能写成已批准需求。
- 玩家手动修改设置；应用不自动写游戏/驱动、不自动提权、不停止其他采集会话。
- 推荐、玩家声明、程序观测分开；历史对象、原件、回答、输入版本和事件保留；未知不补默认真值。
- 当前采用按持久用户/安装定位；ChangeSet从实际配置合并，历史补录不推进当前指针；分析引用闭合的固定段/绑定修订。
- 外部调用先持久审计再可能发送；`outcome_unknown`不隐式重发；旧终态不复活；本地幂等不宣称供应商恰好一次。
- 同一逻辑提交保持key/内容；同步事实和原响应同事务；文件先就绪再发布；数据库事务不包网络或长复制。
- 统计用完整有效输入，图表采样不改统计；共同阈值、时间分母、比较资格、接受与效果不能混为一项成功。
- 原始设计、冻结研究JSON、旧运行输出不为新测试改写；只能从副本构建版本化夹具。
- 不引入Redis、Celery、云账号、微服务或无证据FPS预测；不承诺工期或未测工具支持范围。

本计划保留C的34个任务编号；D-P01—D-P04先行文档补全已完成，不计作新增业务实现任务。模块V场景为原55＋新增8＝63，B基础验收另计，均不是已通过产品测试数。当前共享基线0.4，0.1/0.2/0.3旧对象/请求兼容见shared data §12.1/12.2。

## 2. 文件结构与唯一职责

下列除明确标“既有”外均为**拟建文件/目录**；不得在状态报告中写成已经存在。

| 路径 | 职责/创建归属 |
|---|---|
| `game_app/__init__.py`、`game_app/contracts/primitives.py`、`enums.py`、`errors.py`、`common.py` | ENG-T01：严格ID/时间/引用/基础包装、公共DTO/状态/错误；不把所有领域实现塞入公共文件 |
| `game_app/contracts/device.py`、`knowledge.py`、`recommendation.py`、`performance.py`、`feedback.py` | 分别由DEV/KNW/REC/PERF/FBK首个需要者实现领域类型；只能转录相应规格，不自创另一套含义 |
| `game_app/repository.py`、`migrations/` | ENG-T01：对象登记、固定版本/聚合历史、引用索引、UnitOfWork、迁移 |
| `game_app/tasks.py`、`application.py` | ENG-T02：持久输入/任务/调用、幂等、预算、短控制、所有者编排；领域任务补其处理器 |
| `game_app/files.py` | ENG-T03：受管理路径、manifest、文件发布/恢复/备份；不解释领域内容 |
| `game_app/device.py`、`adapters/windows_probe.py`、`adapters/games/__init__.py` | DEV：只读探测、安装、声明/采用/历史与问题校验；目标游戏适配器按验证结果登记 |
| `game_app/importer.py` | IMP：稳定冻结、格式/作用域映射、原状态保留、发布；具体mapper按格式分文件后仍属本任务 |
| `game_app/knowledge.py`、`collection.py` | KNW：公共身份、用途覆盖、包、补查与更新；采集路径不递归开启另一重任务 |
| `game_app/recommendation.py` | REC：候选/约束/动作/目标/readiness与复核消费 |
| `game_app/performance.py`、`adapters/presentmon.py` | PERF：会话/绑定、日志/时钟、确定性分析/比较、采集租约；纯算法复杂时在同包拆小文件 |
| `game_app/feedback.py` | FBK：原话、解释、偏好/目标接受、下一动作、ChangeSet、Outcome与停止 |
| `game_app/http.py`、`cli.py`、`templates/`、`static/` | ENG-T04与UX：外部入口/安全/页面/草稿；不复制领域判断；静态资源本地分发 |
| `tests/product/`、`tests/fixtures/product/`、`tests/integration/` | VAL先建夹具/证据框架；各实现任务提交自己的行为测试，集成层串联完整链 |
| `pyproject.toml`（既有） | ENG-T01加入`game_app*`包发现并做安装包检查；ENG-T04再加入经验证的可选入口/依赖矩阵，防止仅源码可导入却未打包 |
| `game_collector/providers.py`（既有）、其他采集器文件 | KNW-T03只修改必要的原始响应/逐调用边界；CLI行为和原导出兼容保留，所有既有测试继续通过 |

原C常用`performance.py`等短名均解释为`game_app/`下路径；测试表中的短文件名均在`tests/product/`下。不另建与`game_collector`同名包。

## 3. 实施前的契约补全

四项来自[D问题清单](./09-global-review.md#3-全局问题清单)，现已按下列检查项完成文档补全；关闭位置及静态走查见[补全记录](./10-contract-completion.md)。下列勾选只表示契约已写入唯一权威模块，不表示代码/测试/迁移已执行。

### D-P01 空库首次进入

- [x] 对照02 §5、01 §5、06 UX-F01、shared data §3/9、07 §11，明确首次选择/登记公共Game与安装上下文的调用归属；区分已有目录选择、未知名称、同名歧义和路径未知，不把模糊名当自动合并依据。
- [x] 在02定义公共身份入口、01定义本机上下文入口或明确协调输入，给完整字段/必填/null/默认/来源/响应引用；如采用内置目录，说明目录版本/初始化及空目录失败分支，不能只写“预置游戏”。
- [x] 回补07路由、06如何取得合法ID、shared必要兼容说明、08场景；错误沿既有码，不接受客户端自报检查通过。
- [x] 静态从空库走“只选择游戏→上下文→设备未知也可knowledge”；再走重复提交与身份歧义，不手填任何后台ID。检查声明/文件/幂等原响应的事务边界。
- [x] 记录D-REV-01关闭位置及新增验收编号；未完成前KNW-T01/DEV-T02只可验证内部契约，不称完整首次进入已实现。

### D-P02 本机设置登记

- [x] 在01补玩家声明创建LocalSettingDefinition的局部输入/响应及依据保存；共享对象字段继续引用data §4.3，不向旧AdoptConfigurationInput塞未定义的自由对象。
- [x] 定义同安装namespace/raw_name/menu_path的重用/歧义/更正规则、作用域和错误；不做无依据中文同名归一，不把玩家声明自动发布到公共库。
- [x] 回补07路由、06表单和08验收；说明新条目取得ID后如何进入SettingDeclaration，以及登记成功但采用冲突时两次提交的可见结果。
- [x] 走“无推荐/无配置适配器→登记自定义项→baseline→局部ChangeSet→保留该项→历史补录”；注入同key丢响应与跨安装引用，明确期望对象数量/拒绝行为。
- [x] 记录D-REV-02关闭位置，才能将DEV-T02/03与UX相关场景判为全量可实施。

### D-P03 测试协议创建

- [x] 在04定义TestProtocol创建/版本输入，区别于已有CreateComparisonProtocolRequest；说明场景/运行条件声明、受控变量、MetricDefinition引用与时长/重复数的来源。
- [x] 给固定版本返回、错误与幂等；后台生成ID/版本/时间，不允许客户端通过协议声明自报controlled。未定义项仍允许null并说明限制。
- [x] 回补07路由、06在采样前选择协议的步骤、05 ChangeSet.protocol_id引用及08空库场景；新增同步操作不凭空扩TaskType。
- [x] 走“创建TestProtocol→baseline会话→创建ComparisonProtocol→调整后复测→Outcome”，断言两个协议ID类型不同、历史协议不被更正覆盖、实际条件仍逐轮核对。
- [x] 记录D-REV-03关闭位置。未完成可保留test_protocol_id=null的自然游玩/探索，但不能报告已实现受控协议旅程。

### D-P04 已处理问题更正

- [x] 在01/07明确resolved/skipped后的更正由哪种外部操作开始；若复用context-declarations，补字段到原问题/历史上下文的关系与返回新问题定位，不能只更改当前快照了事。
- [x] 在shared state §7/必要data与error中落实新旧关系、两版本校验、终态不复活、superseded不可写及幂等响应；保持旧回答和旧请求内容不可变。
- [x] 回补06“稍后补充”动作和08验收，区分仍open的终态后补、已skipped的更正、当前事实更正、过去区段依据补充。
- [x] 静态走“明确跳过→原任务终态→更正→关联新请求/新快照→显式新分析”，并发取消/过时上下文/同key重送均有断言；过去段仍需独立补绑。
- [x] 记录D-REV-04关闭位置。所有新增局部请求均有唯一归属后，重新核对07/06/08引用及版本兼容。

这些补全项沿现有已确认原则展开；若提出改变首版范围或确认方式，须先回到Q台账，不借技术缺口替用户批准产品选择。

完成映射：D-P01→02 §5.1/01 §5.1（KNW-V06、DEV-V06）；D-P02→01 §5.2（DEV-V07）；D-P03→04 §4.4（PERF-V09）；D-P04→01 §5.3/shared state §7（DEV-V08、B-STA-13）。共同由ENG-V07/UX-V09/VAL-V08覆盖接口串联。

## 4. 全局依赖与实施顺序

依赖表只列任务边；共享契约、D-P项与真实验证门禁另作前提。逗号表示全部需要；不使用`T01/03`或`T01—03`等可能被误读的缩写。UX-T01以稳定的领域只读**契约**为前提，可以先用固定视图；ENG-T04路由适配用服务桩验证，不依赖完整页面。

| 任务 | 任务依赖 | 优先交付物/模块规格 | 本地验收入口 |
|---|---|---|---|
| ENG-T01 | 无 | 公共契约、版本库/UoW、包发现；[07 §10](./07-data-and-architecture.md#10-实施任务与完成定义) | `tests/product/test_repository.py` |
| VAL-T01 | 无 | 夹具/结果规范；[08 §10](./08-validation-and-delivery.md#10-实施任务与-dod) | `tests/product/test_fixture_loader.py` |
| ENG-T02 | ENG-T01 | 任务/幂等/预算/调用审计/短控制 | `tests/product/test_tasks.py` |
| KNW-T01 | ENG-T01, ENG-T03 | 公共身份/用途级复用及真实声明发布；[02 §10](./02-knowledge-and-collection.md#10-实施任务与-dod)；D-P01 | `tests/product/test_identity.py` |
| ENG-T03 | ENG-T01, ENG-T02 | 文件发布、启动恢复、备份 | `tests/product/test_files.py` |
| DEV-T01 | ENG-T01, ENG-T02, ENG-T03 | 只读获取/原件发布/失败提问；[01 §10](./01-device-game-configuration.md#10-实施任务与-dod) | `tests/product/test_device.py` |
| IMP-T01 | ENG-T01, ENG-T03, VAL-T01 | 冻结与格式识别；[02a §10](./02a-collector-integration.md#10-实施任务与-dod) | `tests/product/test_import_freeze.py` |
| DEV-T02 | DEV-T01, KNW-T01 | 安装/配置目录/本机条目；D-P01/02 | `tests/product/test_context.py` |
| IMP-T02 | IMP-T01, KNW-T01 | 作用域/记录映射 | `tests/product/test_scoped_import.py` |
| DEV-T03 | DEV-T02, ENG-T01, ENG-T02, ENG-T03 | 采用、合并、历史事实；D-P02 | `tests/product/test_adoption.py` |
| PERF-T01 | ENG-T03, DEV-T02, VAL-T01 | 固定格式/单位/时钟/流；[04 §10](./04-performance.md#10-实施任务与-dod) | `tests/product/test_log_formats.py` |
| IMP-T03 | IMP-T02, ENG-T02, ENG-T03 | 快照发布/去重/故障恢复 | `tests/product/test_import_publish.py` |
| DEV-T04 | DEV-T01, DEV-T03, ENG-T02 | 回答归一/冲突选择/终态后补；D-P04 | `tests/product/test_information_answers.py` |
| PERF-T02 | ENG-T01, ENG-T02, DEV-T03 | 会话/闭合绑定/声明与测试协议；D-P03 | `tests/product/test_sessions.py` |
| FBK-T01 | ENG-T01, ENG-T02, DEV-T03 | 原话/解释/偏好/目标接受；[05 §10](./05-feedback-optimization.md#10-实施任务与-dod) | `tests/product/test_feedback.py` |
| KNW-T02 | KNW-T01, IMP-T03, ENG-T03 | 用途覆盖/包发布 | `tests/product/test_packages.py` |
| PERF-T03 | PERF-T01, PERF-T02 | 分析/比较/比较协议/日志关联 | `tests/product/test_metrics.py` |
| PERF-T04 | PERF-T01, PERF-T02, ENG-T02, ENG-T03 | 自有采集租约/收尾/恢复 | `tests/product/test_capture_lifecycle.py` |
| VAL-T03 | VAL-T01, IMP-T03, DEV-T02, PERF-T01 | 兼容矩阵/未见评价；真服务需明确预算 | `tests/integration/test_compatibility.py` |
| KNW-T03 | KNW-T02, ENG-T02, IMP-T02 | 自主补查/标准文件组/持久调用审计 | `tests/product/test_collection_tasks.py` |
| REC-T01 | KNW-T02, DEV-T01, ENG-T02 | 候选/用途/约束；[03 §10](./03-recommendation.md#10-实施任务与-dod) | `tests/product/test_recommendation_gate.py` |
| IMP-T04 | IMP-T01, IMP-T02, IMP-T03 | 研究mapper/独立标注门禁 | `tests/product/test_research_import.py` |
| REC-T02 | REC-T01, KNW-T03 | 主方案/动作/目标/readiness；R03 | `tests/product/test_recommendation_actions.py` |
| REC-T03 | REC-T01, REC-T02, ENG-T01, ENG-T03 | 推荐发布/复核消费接口 | `tests/product/test_recommendation_review.py` |
| FBK-T02 | FBK-T01, KNW-T03, REC-T02, PERF-T03 | 下一动作/合法ChangeSet/LoopGuard | `tests/product/test_next_actions.py` |
| KNW-T04 | KNW-T02, KNW-T03, REC-T03 | 更新影响/图像启用门禁 | `tests/product/test_impacts.py` |
| FBK-T03 | FBK-T02, DEV-T03, PERF-T02, PERF-T03, ENG-T03 | 效果/接受/Outcome与manifest | `tests/product/test_outcomes.py` |
| ENG-T04 | ENG-T01, ENG-T02, ENG-T03, DEV-T04, IMP-T03, KNW-T03, REC-T03, PERF-T03, FBK-T03 | HTTP/CLI路由/安全/查询/导出；Q1 | `tests/product/test_http.py` |
| UX-T01 | ENG-T01, ENG-T02, ENG-T03 | 六页只读视图/固定历史；[06 §10](./06-player-experience.md#10-实施任务与-dod)；领域只读契约稳定 | `tests/product/test_view_models.py` |
| UX-T02 | UX-T01, DEV-T04, ENG-T04 | 草稿/回答/刷新/未知响应 | `tests/integration/test_submissions.py` |
| UX-T03 | UX-T02, PERF-T04, FBK-T03, ENG-T04 | 全旅程/可访问/协议/日志/历史/导出 | `tests/integration/test_journey.py` |
| VAL-T02 | VAL-T01, ENG-T01, ENG-T02, ENG-T03, DEV-T04, IMP-T03, KNW-T04, REC-T03, PERF-T03, PERF-T04, FBK-T03 | 全模块确定性/故障串联最终结案；框架可在ENG-T03后开始 | `tests/product/test_contracts.py`、`test_faults.py` |
| VAL-T04 | VAL-T02, VAL-T03, UX-T03 | 浏览器闭环、演示与真实效果分级证据 | `tests/integration/test_journey.py`及效果报告 |
| ENG-T05 | ENG-T04, UX-T03, VAL-T04 | 最终集成/分发/部署诊断；不反向阻断测试服务 | `tests/integration/test_distribution.py` |

VAL-T02新增依赖是**最终串联完成所需**的显式化，未改变08基础测试框架可以先建的意图。VAL-T03实际未见采集及模型矩阵在KNW-T03适配器到齐后补测，不因其较早具备测试入口就宣称全量通过。UX-T01没有新增领域实现边，读取契约可以由固定fixture提供；遇字段歧义先修所有者规格，不在页面补默认。

### 4.1 建议交付批次

批次是便于审阅的交付顺序，不是同批所有任务能并行或必须等整批完才开始后继。精确前置以§4任务表为准。

| 批次 | 顺序与可并行分支 | 阶段产物 |
|---|---|---|
| P / 契约准备 | D-P01—D-P04；Q决策登记；共享版本兼容复核 | 四处入口无隐式ID/来源/状态依赖；保留未获批准选择 |
| I / 安全基础 | ENG-T01与VAL-T01可独立；再ENG-T02→ENG-T03；文件发布到齐后才进入KNW-T01/DEV-T01 | 临时库内版本/任务/文件/故障可验证，不发外部请求 |
| II / 输入与原事实 | DEV-T02→DEV-T03→DEV-T04；IMP-T01→IMP-T02→IMP-T03；PERF-T01及VAL-T02框架穿插 | 声明/基线、稳定旧资料、可解释格式或明确禁用能力 |
| III / 两条独立主线 | 知识：KNW-T02→KNW-T03→REC-T01/02→REC-T03→KNW-T04；性能：PERF-T02→PERF-T03/04 | 可追踪推荐与可复算本机分析，互不要求对方伪造输入 |
| IV / 反馈与评价 | FBK-T01可在DEV-T03后；FBK-T02→FBK-T03；IMP-T04、VAL-T03取得各自样本后推进 | 合法下一动作、独立效果/接受；未见/兼容证据单列 |
| V / 入口与页面 | ENG-T04和UX-T01按稳定契约增量；UX-T02→UX-T03；VAL-T02全量串联结案 | 用真实浏览器走桩闭环，参数/错误/草稿可恢复 |
| VI / 范围交付 | VAL-T04逐级记录；ENG-T05最终分发核验 | 带限制清单的可复现交付包；缺真实效果则不声称local_effect完成 |

## 5. 各任务实施清单与完成定义

每个任务遵循相同五步：**先冻结最小输入/预期断言→运行目标测试确认行为尚未实现→实现该任务的接口/不变量→运行目标测试和受影响既有回归→核对证据并独立提交**。下面给出每个任务特有的文件、接口和反例；不要求为文档排版增加程序测试。

目标命令统一为 `py -3.13 -m pytest <该任务列出的测试路径> -q`。第一次运行预期因缺失接口/断言失败；实现后才期待通过。测试不存在时属于任务未开始，不是已失败的产品能力。每个实现提交包含源文件、对应测试/夹具、必要规格变动和脱敏运行记录。

### 5.1 工程与证据基础

- [ ] **ENG-T01 产品版本库与公共类型。** 创建§2公共文件、`repository.py`/`migrations/`及`tests/product/test_repository.py`，调整既有包发现。消费data §1/2/9、state §2、error §2/4；提供固定ID/修订读取和显式UnitOfWork给后继所有者。先测严格bool/int、缺字段与null、重复版本/跨类型引用、旧投影可重读、事务中途异常零部分事实。外键开关逐连接核对，事务不包网络。验收B-DAT/B-STA/B-ERR基础形状及ENG-V03/05的存储部分；领域采用/恢复场景后续串联，不能据此整条打pass。包构建后确认包含新包且旧CLI入口保留。 0.4补已定位位置槽/未知位置独立series/旧歧义桶及InformationRequestRevisionRef读取，B-DAT-14不改旧字节。

- [ ] **VAL-T01 夹具与结果规范。** 创建`tests/product/conftest.py`、`test_fixture_loader.py`、`tests/fixtures/product/`与测试专用证据写入器。按08 §4/5实现FixtureManifest、CaseSpec、CaseResult、RunManifest和`load_fixture/run_case/write_report`契约。先测原字节hash损坏拒绝、逻辑label到合法UUID稳定映射、旧期望不覆盖、新run_id保留旧失败。保留mock/真实环境分类与not_run/blocked，不把空测试集算通过。VAL-V01在文档阶段只检查边界；未来编码时用明确的新授权范围作对照，不把“禁止新增业务代码”误用成永久产品测试要求。

- [ ] **ENG-T02 持久注册与单槽执行。** 创建`tasks.py`、`application.py`、`test_tasks.py`，消费ENG-T01事务与公共TaskInput/MutationContext；产生07 §5的`register_task/get_task/cancel_task/resume_task`和领域协调入口。先用SpyProvider测同key并发/异hash、提交后丢响应、queued重启、waiting释放槽、取消先提交、null检查点但有未知调用。prepared/in_flight及预算占用在发送前持久化；恢复核对全部父链。ENG-V01/02/05、B-ERR/B-STA相关断言；真实文件响应持久化由ENG-T03接入后再补故障窗口。

- [ ] **ENG-T03 文件与启动恢复。** 创建`files.py`、`test_files.py`，消费UoW/任务审计，产出ManagedFile、FileOperation恢复与07 §6/7发布/备份路径。先在临时目录注入改名后DB前、提交后响应前、文件占用、缺失hash、磁盘写失败；断言不可消费孤立文件、同key回原ID、未知响应不重发供应商、迁移失败恢复可读。采用标准库SQLite备份机制并核对manifest；不复制活动主库文件冒充一致备份。ENG-V04/05/06；Windows占用行为另列integration，不对玩家库注入。

### 5.2 公共身份、原型导入与知识

- [ ] **KNW-T01 公共身份/复用索引。** 创建`contracts/knowledge.py`、`knowledge.py`、`test_identity.py`，按D-P01已闭合入口及02 §4/5实现身份/别名/IdentityMapping和`assess_reuse`。消费固定GameContext/包索引，输出明确匹配/候选/未知及理由；不得导入IMP实现来建立身份。夹具含同名不同平台、未经核实别名、原game_id不同命名空间、空库首次选择。KNW-V01/03、D-P01新增场景；无FPS/设备仍能建立公共收集输入。 ENG-T03后才能按完整任务开始，声明发布/改名后DB前故障使用真实文件服务，01上下文可用桩。

- [ ] **IMP-T01 稳定冻结与格式识别。** 创建`importer.py`、`test_import_freeze.py`。消费受管理文件和02a标准组规则，产出FormatInspection/冻结manifest；`run/sources/records/gaps/extractions`五份必需，candidates派生重建。先测复制中原件变化、缺records、mtime相同但hash不同、混入旧candidates、未知研究结构。原目录字节不变；失败不得published；IMP-V01。仅从临时副本运行，不以本地未入库runs作为唯一CI输入。

- [ ] **IMP-T02 作用域与只读记录。** 在`importer.py`实现ScopedReferenceMapper/ImportValidator，创建`test_scoped_import.py`。消费KNW-T01身份映射及冻结字节，输出ImportedRecord/原envelope、source修订和诊断。先建两run两source同local_id、重复键、坏setting外键、superseded目标缺失、quarantined、实际model缺失夹具；断言四元键唯一、坏依赖传递隔离、不跨源补引用、不升级状态/模型。IMP-V02/05；原payload/hash和原ID逐字可回读。

- [ ] **IMP-T03 发布与故障恢复。** 扩展`importer.py`，创建`test_import_publish.py`；消费02a映射及ENG文件/任务，按三元指纹去重并保存published或retained_only结果。先并发相同输入不同提交、改名后退出、发布后丢响应，再读同key；只一个ImportSnapshot身份，原任务返回不变。变更mapping_context/hash才新建parent快照，rejected不靠重试绕过。IMP-V03、ENG-V04交叉断言；导入不发模型，产品任务不伪装旧采集任务。

- [ ] **KNW-T02 用途覆盖与包发布。** 扩展`knowledge.py`，创建`test_packages.py`；消费已发布导入，提供CoverageEvaluator/`build_package`和固定KnowledgePackage。先测accepted但目标用途unknown、隔离记录只诊断、包成员缺依赖、图像关键条件缺失、纯研究聚合无单一执行事实。覆盖每格有状态/依据，私人成绩不进入公共包；collection三字段按真实执行事实给值或全null。KNW-V02/03；没有关键覆盖不能因网页数宣称充分。

- [ ] **KNW-T03 自主补查及持久调用。** 创建`collection.py`、`test_collection_tasks.py`并修改必要的既有`game_collector/providers.py`边界。消费KnowledgeInput、覆盖gap、任务预算；产出FollowupPlanner、CollectionSnapshotWriter标准组及经02a映射的新包。先以供应商桩测无目标检索、两轮无增益/预算停、一次URL多错误共享重读、未知调用、响应真实model缺失。真实envelope.model与兼容model分开，不以回填冒充实际返回；原CLI保持。KNW-V02/04/05、45项既有回归；真实认证/模型请求须另有明确预算与G-MODEL证据。

- [ ] **IMP-T04 研究格式mapper。** 创建`test_research_import.py`和必要的逐格式mapper。消费四份冻结研究/检查文件的副本与独立标注，不改金样本；输出有版本/定位的映射，无法解释保持retained_only。先测HDR观察、区间FPS、非严格边界未知、跨分区同ID及图片缺图例；断言不改建议/均值/边界。IMP-V04；测试桩通过不等于所有研究格式已启用，逐格式记录manifest和启用条件。

- [ ] **KNW-T04 更新与图像门禁。** 扩展`knowledge.py`、创建`test_impacts.py`；消费新SourceRevision/包及REC-T03的`consume_impact`，产出去重ImpactNotice/复核事件。先测同通知重投、无实际受影响依赖、superseded旧推荐、文件损坏、图像mapper禁用；旧推荐内容/当前采用/历史Outcome不变。KNW-V04/05、REC-V05；G-IMAGE未通过时只交付缺口和禁用机制，不填写“已读图片”。

### 5.3 设备、安装、实际配置与回答

- [ ] **DEV-T01 只读获取与失败提问。** 创建`contracts/device.py`、`device.py`、`adapters/windows_probe.py`、`test_device.py`；按01 §2/5实现`scan_device`、原件/单位/FieldValue与集中问题。先用探测桩测全失败、一个成功一个失败、多GPU未选择、显存不可靠、超时和明确跳过。失败列表不表示机器无GPU，声明不升级观测；DEV-V01/02。脚本只允许打包白名单参数；真实G-WIN未通过字段保持unknown。 依赖ENG-T03，原件发布不能用未完成文件服务冒充；补发布故障断言。

- [ ] **DEV-T02 安装与本机目录。** 创建`adapters/games/__init__.py`、`test_context.py`并扩展`device.py`，消费KNW身份和D-P01/02入口。实现`declare_context`、独立安装/路径保护/LocalSettingDefinition登记及配置读取注册。先测同游戏两个商店/路径未知、多候选PID重用、跨安装条目、不受管理路径和无配置适配器；门禁不通过不得造ConfigurationObservation。DEV-V02/03与D-P新增场景；G-GAME未验证可保留声明上下文，不能预启用FH6支持。 补list_local_settings及identity_mode/reuse_local_setting_id、未知同名distinct/复用/补齐路径竞争。

- [ ] **DEV-T03 当前采用与历史事实。** 创建`test_adoption.py`、`test_historical_configuration.py`；消费固定推荐/实际base/ChangeSet及D-P02条目，提供01 §5的`adopt_configuration/record_historical_configuration`。先以A含自定义/未知项、C只改一项生成B，再连续调整、两客户端expected A冲突、恢复历史目标、当前B补录过去A。断言完整合并、父链、rollback新ID、零半声明、历史不推进指针、无推荐baseline合法。DEV-V03—05、ENG-V03；手动设置事实需保留来源，不能只点击就active_confirmed。 DEV-V07补仅manual_edit的显式引用替换：固定base/旧值单位/新声明同事务，K/U/预设保留；无映射同series换ID拒绝。

- [ ] **DEV-T04 回答归一和继续。** 创建`test_information_answers.py`，扩展`device.py/tasks.py`；提供`validate_information_answer`及D-P04更正操作，消费request/context两修订。先测部分回答、unresolved、conflicting选择、跳过、过时request、上下文失效409维护、终态后补、skipped更正、取消竞态。断言新回答/快照/输入修订原子、合法回答不丢、FieldResolution保留旧依据、终态输入不变。DEV-V01/05、ENG-V02、D-P04新增场景；04历史绑定仍独立提交。 DEV-V08/B-STA-13补GPU→显存两轮更正，根域不缩小、先前派生值保留；旧链缺根证据拒绝。

### 5.4 会话、统计、协议与采集

- [ ] **PERF-T01 固定格式与帧适配。** 创建`contracts/performance.py`、`adapters/presentmon.py`、`test_log_formats.py`，消费受管理原件与FormatRegistry规则，产出有版本/选项/列单位/时钟/流的解释。先测未知列、单位错、缺主流、FG未知、时间offset未知；原件保留而非假ready。PERF-V05/06；抽象日志仅用于离线规则，真正启用registry须G-FORMAT的真实固定样本和manifest，不凭主分支文档猜列。

- [ ] **PERF-T02 会话、绑定与测试协议。** 创建`performance.py`、`test_sessions.py`，按04 §4/5及D-P03实现`create_session/end_segment/split_segment/end_session/bind_configuration`和TestProtocol入口。先测applied=null、开放绑定、结束追加闭合修订、双修订竞态、遗漏活动段、手工声明来源、历史配置补绑和两个协议类型区分。PERF-V01/08及D-P03场景；旧开放快照不可分析，当前指针/旧摘要不被更新。

- [ ] **PERF-T03 分析、比较与历史日志关联。** 创建`test_metrics.py`、`test_comparisons.py`、`test_log_association.py`，提供`analyze/compare/create_comparison_protocol/inspect_log/associate_log`。用`[10,20,30,40]`和`[60,70,16,50,80]`、坏值/断段/多流/零时长、60秒2簇与120秒4簇夹具，断言算法和逐项unknown。另测derived基线40ms冻结80ms、相同key不改frozen_at、事后非探索拒绝、历史[900,960)s映射[0,60)s、过时关联零部分写。PERF-V02—05/07/08；固定manifest可复算，120秒等初值另压测，不能靠降采样改统计。

- [ ] **PERF-T04 自有采集与安全收尾。** 创建`test_capture_lifecycle.py`，扩展采集适配器及`performance.py`；提供`capture/stop_capture`、CaptureLease及取消/重启检查。先测不同key重复启动、queued停止、running正常停止与cancel区分、重启PID相同启动时刻不同、归属unresolved、输出未就绪。PERF-V06、ENG未知窗口；只停止可确认归属的本任务进程，租约不因Task终态自动清除。G-GAME/G-FORMAT不通过时真实启动禁用，桩生命周期通过单独报告。

### 5.5 推荐、反馈与效果

- [ ] **REC-T01 候选与约束执行。** 创建`contracts/recommendation.py`、`recommendation.py`、`test_recommendation_gate.py`；消费固定包/DeviceSnapshot，提供`assess_candidates/validate_actions`所需候选与用途检查。先测Laptop同名、wrong game、无邻近依据、unknown前提、未核实别名、复杂数值边界；不得eval模型表达式或把unknown当false。REC-V01/02；错误引用不发布版本，accepted不直接actionable。

- [ ] **REC-T02 主方案、动作、替代与目标。** 创建`test_recommendation_actions.py`，按R03审阅后的03 §6/7提供readiness归约及主方案/动作/目标。先测DLSS/FSR未落实分支、互斥、明确保护偏好、没有替代证据、区间FPS/HDR观察/无目标依据；替代应用后全量重验，不凑三套，不生成默认60FPS。REC-V03/04；综合方案保留作者基准与替换证据，不冒称整套实测或收益可加总。

- [ ] **REC-T03 发布与复核消费。** 创建`test_recommendation_review.py`，实现`recommend/publish_recommendation`及03 §5 `consume_impact(notice_id)`。消费固定输入/目标内容和文件发布，输出RecommendationVersion/TargetProposal/初始review与消费标记。先测目标引用身份一致、发布中途失败、同通知两次、superseded旧内容、重新计算新ID；推荐/采用分开，复核不自动调用模型。REC-V05；消费接口先于KNW-T04实现，避免任务环。

- [ ] **FBK-T01 原话、偏好与目标接受。** 创建`contracts/feedback.py`、`feedback.py`、`test_feedback.py`；实现05 §5 `record_feedback`及两类同步声明。先测空文本有选项、模型失败、显式新解析保留原话/InterpretationLink、tentative不扩global、撤回新版本、目标接受并发。FBK-V01/02；原话与注册原子保存，解释失败不丢原件；PREFERENCE_VERSION_CONFLICT/TARGET_ACCEPTANCE_CONFLICT不覆盖当前声明。

- [ ] **FBK-T02 下一动作与合法ChangeSet。** 创建`test_next_actions.py`，实现`plan_next_action`、ChangeSetValidator、LoopGuard。消费01实际base、02证据、03推荐、04摘要及FBK原话；先测满意keep、无基线仅排查/measure、旧值未知不可恢复、破坏显式偏好、依赖组不闭合、重复被拒方向无新证据。FBK-V03/06；合法adjust才保存ChangeSet，不递归发knowledge/模型任务；研究/测量只返回后继意图交07。

- [ ] **FBK-T03 Outcome、重复波动和接受。** 创建`test_outcomes.py`，实现`record_outcome`/OutcomeEvaluator与受管理评估manifest。先测C基于A但B绑定错/比较反序、附加改动、未测接受、更好但拒绝、三轮波动内/一致改善、平均升尾部退、探索性协议；冻结选中摘要/协议/分组/规则。FBK-V04/05；接受与效果独立，legacy按state §5归约，当前指针不变。真实improved只限G-EFFECT所证明范围。

### 5.6 入口、页面与最终门禁

- [ ] **ENG-T04 外部入口与分发基础。** Q1 已定纯 CLI：只创建`cli.py`与`test_cli.py`（`http.py`/`test_http.py`、UX-T01—03、VAL-T04、ENG-T05 的页面部分不实现，保留设计）；按07 §11登记的命令入口复用同一应用服务与领域契约，更新既有pyproject的已验证可选入口/依赖。先以服务桩测同源/会话、canonical route含实际ID、跨用户不可见、上传规范hash、GET无写、原key回原响应、客户端伪造provenance/controlled被拒。ENG-V01/06；服务可独立启动供后续浏览器测试，不等待ENG-T05。固定版本矩阵失败就保持未启用，不用“latest”掩盖未验兼容。

- [ ] **UX-T01 六页读取与历史。** 创建`templates/`、`static/`、`test_view_models.py`；消费领域只读契约与固定fixture，不另建领域真值。先测无FPS入口、provisional/needs_review同时显示、keep_current未知、三类帧率分开、历史固定revision、图表脚本失败仍有同值文本、私有路径不泄露。UX-V01/03/07；涉及D-P首次选择/目录选择的字段须以已补全规格为准，不把内部ID输入框当成产品入口。

- [ ] **UX-T02 提交草稿与问题恢复。** 创建`static/submissions.js`、`templates/information.html`、`tests/integration/test_submissions.py`；消费MutationDraft和真实ENG-T04路由。先在真实浏览器/临时库测写IndexedDB失败不发送、双击、响应丢失/刷新、403令牌恢复、410、部分回答、skipped更正、旧request冲突。UX-V02/06；原key/业务内容保持，下一批回答才新key，焦点与有效草稿保留，SpyProvider发送计数不增加。

- [ ] **UX-T03 完整旅程与可访问性。** 创建`tests/integration/test_journey.py`，补六页表单/历史/导出。经页面走首次选择→失败补充→无目标收集→主推荐→独立基线/未知继续→手动采用声明→日志导入或自有采集→协议冻结/比较→反馈/Outcome→历史补录/手动恢复；另走历史日志人工关联。UX-V04/05/07/08、VAL-V06/07。普通流程不手改JSON/ID，键盘/文本替代/真实下载可用；真实采集禁用时导入分支可先验收，但任务全量及相关能力保持未完成标签。

- [ ] **VAL-T02 全模块确定性与故障门禁。** 创建`test_contracts.py`、`test_faults.py`及测试专用SpyProvider/CrashInjector/ScenarioRunner；先在ENG-T03后建立基础框架，随后按§4依赖到齐串联B验收、63个模块V及D-P新增场景的离线部分。输入为抽象安装/A/C/B、两客户端、坏文件、无检查点未知调用；断言ID/事件/绑定/调用数量和零部分事实。VAL-V02/03及D01—D05；测试报告必须列未执行/受阻场景，空集/跳过不等于全通过。

- [ ] **VAL-T03 真实兼容与未见评价。** 创建`tests/integration/test_compatibility.py`及独立格式/标注manifest。消费IMP/DEV/PERF真实样本、之后的KNW模型适配器，记录G-WIN/G-GAME/G-FORMAT/G-MODEL/G-IMAGE矩阵。先冻结模型输出再独立标注，按字段/关系/条件/定位/隔离分别给分母；已见四篇仍属回归。VAL-V04；无样本的能力blocked/not_run，不能因离线桩通过而开启适配器；真服务调用预算另行明确。

- [ ] **VAL-T04 浏览器闭环与演示证据。** 扩展`test_journey.py`与脱敏效果报告，消费ENG-T04可运行测试服务、UX-T03及前两类VAL证据，不等待ENG-T05最终安装包。先用真实浏览器/供应商桩核验失败恢复和协议/日志修补入口，再在明确游戏/预算/玩家参与条件下按G-EFFECT记录真实手动采用与重复测量。VAL-V05—07；分别报告integration与local_effect。未测效果不能写“闭环已改善”，玩家拒绝复测仍保存接受情况/缺口。

- [ ] **ENG-T05 最终集成与分发。** 创建`tests/integration/test_distribution.py`、打包/运行清单，完成入口/静态资源本地分发及部署诊断；消费VAL-T04证据再作干净安装/启动检查。安装包应包含`game_app`和既有CLI，运行不依赖开发工作目录或公共CDN，服务状态不泄密，重复实例不启动第二重工作器。ENG-V01—06、UX-V01—08及声明范围门禁全部对应证据；尚受阻的真实范围不得把任务标成全产品完成，限定演示需明确写剩余范围。

## 6. 验收映射、测试步骤与提交粒度

### 6.1 需求与最终负责点

| 需求 | 主要实现任务 | 必须执行的验收/最终证明 |
|---|---|---|
| C01/C05 | DEV-T01/02、KNW-T01、REC-T01 | DEV-V02、REC-V01/02；同名笔记本不误配、邻卡不迁移FPS |
| C02/C03 | KNW-T02/03、UX-T01/02 | KNW-V01/02、UX-V01；无目标关卡、预算/增益/缺口可追溯 |
| C04/C06 | REC-T02、PERF-T03、FBK-T03、UX-T01/03 | REC-V03/04、UX-V03、FBK-V05；三类数值隔离，不造无依据确定值 |
| C07 | DEV-T03、PERF-T04、ENG-T04、UX-T03 | DEV-V03、ENG-V06、UX-V04；文件写监视与真实手动流程分列 |
| C08 | IMP-T04、KNW-T04、VAL-T03 | IMP-V04、KNW-V04；未读图关键格仍缺，长视频后置 |
| C09 | ENG-T02/03、KNW-T03、VAL-T03 | KNW-V05、ENG-V05/06；请求/真实返回model分开，密钥不出现 |
| C10 | D文档范围检查，未来按明确编码授权更新执行记录 | VAL-V01不当作产品功能；本次只改技术设计目录 |
| C11/D04 | DEV-T01/04、ENG-T02、UX-T02 | DEV-V01/05、ENG-V02、UX-V02、D-P04新增用例；等待不死锁、来源不升级 |
| D01 | DEV-T03、FBK-T02/03、PERF-T02、UX-T03 | DEV-V04、FBK-V03/04、VAL-V02；A→C→B与下一轮/恢复完整 |
| D02 | ENG-T02/03/04、IMP-T03、PERF-T04、UX-T02 | ENG-V01/04/05、IMP-V03、PERF-V06、UX-V06、VAL-V03 |
| D03 | DEV-T02/03、PERF-T02/03、UX-T03 | DEV-V03/05、PERF-V01/05/08、UX-V04/07/08；独立基线/未知/历史不切当前 |
| D05 | PERF-T03、FBK-T03、UX-T03 | PERF-V02—05/07、FBK-V05、UX-V05/08、VAL-V07；共同阈值/分母/冻结/效果资格 |

B契约验收按各章节完整读取，不仅复制上表编号摘要：ENG-T01负责基础形状/引用/版本及B-DAT-14读取视图；ENG-T02负责任务/调用/幂等及B-STA-13模式分流；ENG-T03负责文件窗口；DEV/PERF/FBK负责业务守卫；VAL-T02最终串联。63个模块V包括原55及D-P新增8，原编号保留，新增映射见下一表。

| 新增场景 | 实现任务/主要测试路径 | 必须补入的DoD |
|---|---|---|
| KNW-V06 | KNW-T01，`tests/product/test_identity.py` | list_games/select_game空库、候选、provisional声明槽、原key；上下文构造可先以01契约桩验证，DEV-T02/ENG-T04再集成 |
| DEV-V06 | DEV-T02，`tests/product/test_context.py` | prepare_game_context/list_installations/create_installation；无路径安装及过时revision，无默认GPU/配置 |
| DEV-V07 | DEV-T02/03，`tests/product/test_local_settings.py`、`test_adoption.py` | register_local_setting、精确键/直接后继唯一；目录更正不迁移旧值，登记与采用两事务；0.4未知同名消歧、显式引用替换保留K/U及旧A |
| DEV-V08 | DEV-T04/ENG-T02，`tests/product/test_information_answers.py` | create_information_amendment、facts_only每分支均无源TaskRun/TaskEvent/TaskInputRevision写入；根域连续不同字段更正/旧链恢复，不回退先前事实 |
| PERF-V09 | PERF-T02/03，`tests/product/test_test_protocols.py` | list_metric_definitions/create_test_protocol、显式规则版本、指标映射惰性事务登记、父版本、错协议类型 |
| ENG-V07 | ENG-T01/02/03/04，`tests/product/test_http.py`、`test_faults.py` | 新类型/唯一索引/空平台键编码、文件恢复、0.4/旧0.1—0.3响应兼容和本地目录GET零写 |
| UX-V09 | UX-T01/02/03，`tests/integration/test_journey.py` | 空库到四入口全程无手填ID；unknown原key/规则版本保留，键盘/错误动作可恢复；消歧/显式替换/根字段选择及版本头随草稿恢复 |
| VAL-V08 | VAL-T02/04，`tests/product/test_contracts.py`及浏览器旅程 | 先离线规则/故障、再浏览器，63个模块V和B-DAT-14/B-STA-13/B-ERR-12各层证据分开 |

### 6.2 每个任务的最小执行记录

```text
task_id / spec_revision / prerequisite_tasks / decision_and_gate_status
changed_files / fixture_id_and_hash / focused_command / observed_exit_code
assertions_checked / provider_send_count / object_and_event_counts
evidence_level / not_run_or_blocked_cases / remaining_scope / commit_revision
```

仅需保留与任务有关的字段；纯算法没有provider调用时明确计数为0或不适用。命令输出和失败反例脱敏，运行记录不覆盖旧结果。

任务局部测试通过后运行受影响既有测试；改采集器时必须跑完整原45项。新的产品任务与契约消费者接通后运行相应综合场景；不为每次文档变动反复执行真实外部服务。

建议每个任务一个可审阅提交，复杂任务按“契约/行为/故障覆盖”拆可独立验证的提交；不要提交只有空目录/空函数的完成标记。任务跨文件时仍按职责分文件，不为凑单提交把所有模块集中到一处。

## 7. 里程碑与证据分层

保留原08的M0—M6含义，按事实报告状态。批次编号I—VI是实施顺序，M编号是产品范围验收，二者不互相替代。

| 里程碑 | 所需任务/证据 | 当前状态 |
|---|---|---|
| M0 设计与基线 | A/B/C/D文档、D-P闭合、Q状态、原型回归 | D报告/计划及四项契约文档已补全；Q未确认，产品实现未开始 |
| M1 知识输入 | ENG-T01—03、IMP-T01—03、KNW-T01—03及相关VAL | 仅旧CLI子集已实现；产品接入未开始 |
| M2 推荐 | REC-T01—03、身份/包/采用只读输入及模型门禁 | 未开始；离线候选可先测，真实质量另证 |
| M3 采用与性能 | DEV-T01—04、PERF-T01—04及固定真实格式/目标能力 | 未开始；导入分支优先，采集支持单独声明 |
| M4 反馈闭环 | FBK-T01—03、固定协议/采用/比较链、重复测量规则 | 未开始；桩证明规则，local_effect证明本机范围 |
| M5 产品演示 | ENG-T04/05、UX-T01—03、VAL-T02—04、脱敏限制清单 | 未开始；不能以桩流程或截图冒充真实兼容/改善 |
| M6 迁移评价 | 第二游戏/新作者/新版式独立样本；IMP-T04/VAL-T03扩展证据 | 未开始；不作为单游戏有限范围演示的隐式前提 |

`static_design`、`offline_fixture`、`integration`、`local_effect`分别报告。真实格式缺失不会阻止写算法单元测试，但阻止相应解析器启用；真实采集未通过不阻止已验证导入分支先验收，但UX-T03/最终范围不能据此全部打勾。图片能力保留在KNW-T04/VAL-T03的增强范围，没有独立样本就保持未完成。

## 8. 首个实施切片与交接方式

D-P01—D-P04及独立审计修订已形成0.4规格，先读coding-handoff.md；获得明确编码指令后可启动 **ENG-T01＋VAL-T01→ENG-T02→ENG-T03**。首个代码切片只在临时库/目录和供应商桩内证明：固定历史可读、幂等注册、等待释放槽、未知调用不重发、文件未发布不可消费。它不需要FH6真实路径、付费模型或UI样式批准。

首个切片的验收动作顺序：

1. 保存开始提交/工作区差异与版本；读取shared和07/08。
2. 冻结临时输入、合法UUID、坏引用、同key请求、未知调用和文件故障夹具。
3. 逐任务完成§5.1的失败测试→最小实现→目标测试→证据→提交。
4. 在基础任务接通后运行ENG-V01/02/04/05的已覆盖部分和VAL-V03，不把依赖未来领域的断言计作通过。
5. 执行`py -3.13 -m pytest -q`核对原型，打包检查新包可导入、旧CLI可用；记录未运行的真实范围。
6. 然后进入KNW-T01/DEV-T01及导入/配置分支；受Q或G门禁影响的操作必须先满足各自条件。

下一位开发者先读本计划§3及补全记录，不能从旧`collector-implementation-plan.md`的已勾选Task直接延伸产品实施。D-P文档现已闭合；若后续要求设计审阅，检查已写契约，若明确要求实现某个切片，只开展已授权范围。

## 9. 本轮计划自检与未执行项

- [x] 34个任务编号与C模块任务逐一对应，无重复或漏项；新增文档补全项单独编号。
- [x] 所有显式任务依赖可定位，依赖图无环；KNW-T01独立于IMP-T02，REC-T03独立于KNW-T04，ENG-T04独立于UX，VAL-T04使用ENG-T04测试服务而非依赖ENG-T05。
- [x] 每个任务列出拟建文件、输入/产出职责、反例、目标测试及验收编号；公共执行步骤与任务特有断言共同构成DoD。
- [x] 四个已补全入口、Q1—Q3/R03和七类真实验证门禁已显式化，没有用实施细节悄然替代产品决定。
- [x] 本地链接、Markdown表格/围栏、JSON示例、任务/验收引用经静态检查；仅技术设计目录有内容变更。
- [ ] 尚未创建任务源码/测试/夹具，未运行任何产品场景。
- [x] D-P01—D-P04已在对应权威模块补全，新增8个模块及3个基础验收场景，0.3兼容及路由/页面已同步。
- [ ] 未核验外部兼容、图像或本机效果，未执行产品迁移/新验收。

初次D扫描为17份Markdown/158个本地链接/131个表格/8个JSON；D-P补全后的新扫描及范围见[补全记录](./10-contract-completion.md)。任务表/任务卡仍为34项，当前模块V引用为63项；不沿用初次数字冒充新扫描。

本节的完成项只指D交付的文档检查；不能当作M1—M6或34个实施任务的完成记录。
