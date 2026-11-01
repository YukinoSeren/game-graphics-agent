# 07 数据模型、工程架构与接口设计

> 推荐设计 v1.0。以下表、接口与状态是后续实施规格建议，本轮不建立数据库或服务。  
> 返回：[总设计](../../project-design.md)。

## 1. 推荐技术组成

| 层 | 推荐 | 原因 |
|---|---|---|
| 核心逻辑 | Python；沿用现有 Pydantic、requests、BeautifulSoup | 复用已有采集程序，保持类型检查 |
| 本地 HTTP | FastAPI＋本地 ASGI 进程 | 给页面提供受控接口；框架是推荐选择，尚未安装或测试 |
| 页面 | 服务端模板＋少量原生 JavaScript | 首版减少独立前端工程与复杂状态框架 |
| 图表 | 本地静态分发 Apache ECharts | 帧间隔时间轴、区段和对比；不依赖运行时公共 CDN |
| 持久化 | SQLite＋JSON＋原始文件目录 | 单机课程项目，按需要索引，不先拆微服务 |
| 后台任务 | 同一进程的显式单工作队列 | 首版一次一个重任务，保留取消、预算与检查点 |
| 模型/检索 | 现有 DeepSeek / Exa 适配器 | 调用与错误继续审计；不增加多 Agent 框架 |

FastAPI 有模板与静态文件集成方式，适合上述页面路线；本稿不把框架后台任务等同于持久队列，长任务状态仍由项目自己保存。[官方模板文档](https://fastapi.tiangolo.com/advanced/templates/)

ECharts 的折线能力可用于时间序列，仍需提供文本摘要及可访问说明，不用图表代替全部数值信息。[官方折线说明](https://echarts.apache.org/handbook/en/how-to/chart-types/line/basic-line/)、[可访问说明](https://echarts.apache.org/handbook/en/best-practices/aria/)

建议在实施时固定依赖版本并运行兼容性测试；本稿不把“官方文档有能力”写成“目标机器已验证可用”。

## 2. 模块边界

```mermaid
flowchart TD
 UI[本地页面] --> API[受控应用接口]
 API --> TASK[任务和状态管理]
 TASK --> DEVICE[设备及游戏适配]
 TASK --> COLLECT[已有知识采集器]
 TASK --> REC[推荐服务]
 TASK --> PERF[日志导入及统计]
 TASK --> FEEDBACK[反馈和实验决策]
 DEVICE --> DB[(SQLite与本地文件)]
 COLLECT --> DB
 REC --> DB
 PERF --> DB
 FEEDBACK --> DB
 COLLECT --> EXT[搜索与模型接口]
 REC --> EXT
 FEEDBACK --> EXT
```

所有框是同一应用里的模块，不是独立部署服务。推荐、反馈读取通过校验的数据与版本快照，不直接依赖某次临时聊天记忆。

## 3. 公共知识层继续使用的对象

Game、Source/SourceRevision、Setting、Configuration、Environment、Evidence、Constraint、Comparison、VersionImpact 沿用[既有契约](../../knowledge-intake-contract.md)。

现有采集输出使用 local_id 与 reference_scope 关联。接入时显式保留原作用域，生成全局 KnowledgeRecordRef；不要把其他来源的同名局部 ID 当作同一记录。

KnowledgeRecordRef 最少含 package_id、capture_scope_id、local_record_id、revision_id/内容哈希。导入适配器生成映射表，原始输出和冻结样本不回改。

## 4. 本机闭环新增实体

| 实体 | 核心字段/关系 |
|---|---|
| DeviceSnapshot | id、读取时间、适配器版本、GPU列表、CPU/内存/显示背景、逐字段来源/未知原因 |
| InformationRequest | id、设备/游戏/会话上下文、自动获取失败原因、待补字段及用途、候选、用户回答/时间、未回答项 |
| GameInstallation | id、game_id、平台、路径引用、进程匹配线索、版本原标识及来源 |
| KnowledgePackage | id/version、game_id、来源修订集合、记录引用、覆盖矩阵、缺口、创建时间 |
| Preference | id、作用范围、内容、explicit/tentative状态、来源反馈、有效/撤回状态 |
| RecommendationVersion | id、parent_id、设备/游戏/知识包/偏好快照、基准配置、建议动作、目标提议、来源和校验状态 |
| AppliedConfiguration | id/version、推荐引用、逐项值与确认方法、采用时间、activation_state |
| ConfigurationObservation | id、读取时间、游戏/设备、字段观测、文件/工具来源、与采用配置差异 |
| PlaySession | id、游戏、设备快照、开始/结束、配置绑定、目标建议/玩家接受情况 |
| SessionSegment | id、session_id、时间范围、场景、applied_id、流选择与排除标记 |
| CaptureArtifact | id、session/segment、原始文件路径引用及哈希、工具/选项、schema、时间基准 |
| PerformanceSummary | id、artifact与segment引用、分析器/清洗协议、统计值、质量、比较资格 |
| Feedback | id、session/segment、原话、结构化症状、明确偏好、解析状态 |
| Diagnosis | id、输入证据、多个假设、支持/反例/未知、建议动作 |
| ChangeSet | id、base_applied_id、变更/依赖组、假设、来源、协议、rollback_ref |
| ExperimentOutcome | id、change_set、前后会话/摘要、可比级别、变化、玩家接受、结论 |
| TaskRun | id、任务类型、输入版本、状态、预算、调用、检查点、错误与停止原因 |

数值项均附 metric、unit、number_kind、frame_stream_kind（相关时）。百分比要有分母和输入引用，空值使用 null 与原因。

设备/游戏字段的 method 区分 `system_probe` 与 `user_provided`，采用配置声明使用 `player_confirmed`。InformationRequest 回答后生成对应快照/配置新版本，不把用户提供升级为程序观测；冲突保留原依据及本轮采用理由。

本地统计另有带定义版本的 MetricDefinition；“最慢1%间隔均值倒数”等项目指标不得只借旧字段名而丢掉算法。公共来源原有 metric 与本地可比性通过显式口径映射连接。

## 5. 核心关联与不变量

```mermaid
erDiagram
 RecommendationVersion ||--o{ AppliedConfiguration : adopted_as
 AppliedConfiguration ||--o{ SessionSegment : used_in
 PlaySession ||--|{ SessionSegment : contains
 SessionSegment ||--o{ CaptureArtifact : measured_by
 CaptureArtifact ||--o{ PerformanceSummary : analyzed_as
 PlaySession ||--o{ Feedback : described_by
 AppliedConfiguration ||--o{ ChangeSet : adjusted_from
 ChangeSet ||--o{ ExperimentOutcome : evaluated_as
```

必须保证：

1. Session 的实际配置不能默认为最新推荐；未确认就是未确认。
2. 配置、设备、知识包和统计协议使用版本引用；历史不能随当前值漂移。
3. 一条原始日志可有多个分析版本，但原文件哈希不变。
4. 多配置会话必须分段或标为 mixed；严格比较只选条件明确的区段。
5. 来源值、Agent 综合建议和玩家采用值各有独立对象。
6. 修改状态保存事件/历史；原始事实与来源不被新模型输出覆盖。
7. 任何结论引用实际存在、未隔离且适用范围足够的证据。

## 6. 推荐的物理存储

继续保留现有 sources/records 兼容导入。新增表可按功能逐步建立，不要求一次拆出所有逻辑实体：

- knowledge_packages、device_snapshots、game_installations。
- recommendation_versions、applied_configurations、configuration_observations。
- sessions、session_segments、capture_artifacts、performance_summaries。
- feedback、diagnoses、change_sets、experiment_outcomes、preferences。
- task_runs、task_events、schema_migrations。

首版复杂字段用 JSON；game_id、版本引用、状态、时间和常用关联建索引。关系由应用校验并尽量使用外键约束。不要只把全部结果放在一个无索引聊天文本中。

原始CSV、必要网页块/图片与导出文件存受管理目录，数据库保存哈希与相对引用。大规模逐帧数据首版保留CSV和必要统计，不要求逐帧写SQLite。

## 7. 对外操作接口

以下路径是推荐接口约定，非已上线API。创建操作返回对象ID或任务ID；获取结果返回状态和可追溯输入版本。

| 操作 | 输入 | 输出 |
|---|---|---|
| POST /api/device-scans | 探测范围、可选游戏上下文 | task_id，完成后DeviceSnapshot |
| POST /api/knowledge-tasks | game_id、device_id、包版本、运行配置/预算 | task_id |
| GET /api/tasks/{id} | 任务ID | 状态、阶段、活动、预算使用、可用成果 |
| POST /api/tasks/{id}/cancel | 取消请求、幂等键 | cancel_requested/终态与成果位置 |
| POST /api/recommendations | game_id、device_id、package_id、可选偏好/历史 | task_id或RecommendationVersion |
| POST /api/applied-configurations | recommendation_id、确认方式、改动项 | AppliedConfiguration |
| POST /api/sessions | applied_id、device_id、场景/协议 | PlaySession与初始区段 |
| POST /api/captures | session_id、采集适配器和协议 | task_id与CaptureArtifact |
| POST /api/log-imports | 受管理上传文件、session绑定、格式 | artifact_id，解析/分析任务 |
| POST /api/feedback | session/segment、原文与选项 | Feedback与解析任务 |
| POST /api/next-actions | session、summary、feedback及当前版本 | Diagnosis、keep/adjust/measure等动作 |
| GET /api/comparisons | 两个summary_id | 可比级别、差异、依据/限制 |
| GET /api/exports/{id} | 已生成导出ID | 文件或报告，不接受任意系统路径 |

新建会话、采用确认、反馈提交使用客户端幂等键，刷新重试不生成重复记录。玩家已经改变配置时，用版本冲突响应要求合并差异，不覆盖别人的新状态。

## 8. 任务状态与恢复

推荐的执行准备状态 actionable/provisional/blocked、更新复核状态 current/needs_review/superseded、知识入库状态 accepted/quarantined 分开存储。被新资料要求复核不代表原文提取错误；可执行的操作也不代表预计FPS已被本机验证。

TaskRun 统一状态：`queued → running → completed/partial/failed/cancelled/interrupted`。`cancel_requested` 是运行中的控制标记；最终状态仍必须说明已保存哪些成果。

采集任务里的 `completed_scope/partial_scope` 是领域结果，映射到 completed/partial，不替代全部任务状态。

| 情况 | 处理 |
|---|---|
| 页面刷新/断开 | 后台任务继续，重新按ID查询 |
| 取消 | 在外部调用边界停止新增调用，保存已有成果；不承诺立即中断已提交的付费请求 |
| 模型/网络失败 | 记录调用、保留原输出与已完成模块，输出partial或failed |
| 应用进程退出 | 重启后标记未结束任务interrupted，不能自动标完成 |
| 恢复任务 | 从已完成步骤创建有parent_id的新任务，只补未完成范围 |

不能盲目重发中断时已提交的模型请求，避免双重计费。已计量/未知计费状态分开保存。断点恢复是本设计新增能力，现有采集代码尚无完整入口。

## 9. 模型调用契约

每次调用保存任务、模型请求/返回标识、提示版本、Schema版本、输入数据哈希、输出、用量与验证结果。密钥不进入这些文件。

公共页面、日志附带文字和玩家反馈都按数据处理。模型输出只允许生成类型化建议，不能直接执行系统命令、改游戏文件或自创工具调用。

本地原始性能日志默认不上传；先由确定性程序生成必要摘要。向外部模型发送设备摘要、匿名场景、相关配置与反馈文本即可；去掉系统用户名、绝对路径和无关进程信息。

本地HTTP默认只绑定回环地址；状态变更验证同源与本次应用会话标识，不默认开放局域网。浏览器不保存服务密钥到localStorage；后台复用已有用户环境配置，设置页只回显已配置/未配置，不回传完整密钥。上传与导出使用受管理文件ID，不能提交任意系统路径。

## 10. 一致性、迁移与导出

一个采用版本及其差异在同一事务保存；一个摘要保存时绑定完整协议与原件引用。任务事件追加，文件写入先临时文件再原子替换；输出完整性校验失败不宣布保存成功。

Schema 变更有版本和显式迁移。研究 JSON 经导入适配器映射，原文件哈希保留；新字段不知道就保留未知，不为通过新Schema回改金样本。

删除某次会话时处理其依赖或保留不可识别摘要，不能留下伪有效外键。完整导出包含数据版本、来源引用和可分享范围说明；默认不附密钥和本机敏感路径。

## 11. 连接现有原型的顺序

先给采集输出加只读KnowledgePackage导入层；再增RecommendationVersion与AppliedConfiguration；随后接Session与日志；最后接反馈、ChangeSet和历史比较。前端调用相同服务接口，CLI继续保留。

本轮不会改变现有源代码、样本文件或运行结果。所有新增表、HTTP接口和页面都是待审阅的推荐设计。
