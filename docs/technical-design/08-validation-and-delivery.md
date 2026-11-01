# 08 验收基准、夹具、证据与交付详细设计

> C1 建立测试基准，C5 回补完整旅程；2026-11-01；`product-contract/0.2`。本文定义未来验收，不报告产品代码已实现或测试已通过。
> 依据：[原始 08](../design/08-validation-and-delivery.md)、[总设计](../../project-design.md)、[B 台账](./README.md)、[工程骨架](./07-data-and-architecture.md)。

## 1. 范围与证据等级

分别验收确定性规则、抽取正确性、推荐可操作性、本机使用价值，四者不能互相替代。已有代码和历史 45 项测试、Dot 选定固定值/语义检查只证明当时范围；本次不重跑付费采集，不把旧样本称未见集。准确数字及限制引用[实施报告](../../collector-implementation-report.md)与[验证摘要](../validation/fh6-dot-comparison.md)。

建议四类证据：`static_design`（文档检查）、`offline_fixture`（受控程序测试）、`integration`（真实文件/浏览器/适配器）、`local_effect`（真实游戏与玩家试验）。这些为验收记录的局部分类，不新增 TaskState；不同等级分别报告。Q1—Q3、首游戏及测试参与量仍待确认；不能把 M1—M6 写成已承诺工期。

## 2. 验收能力与需求覆盖

| 功能 | 触发/输入/前置 | 输出与流程 | 失败处理 |
|---|---|---|---|
| VAL-F01 夹具冻结 | 新用例、源原件、独立标注 | 生成 manifest→锁定输入/期望→运行→保留差异 | 错误期望另版本更正，保留旧结果，不回改金样本 |
| VAL-F02 规则回归 | B 契约、模块 V 用例、候选实现 | 校验字段/关系/并发/公式及记录数量 | 失败保存最小反例，禁止空结果算通过 |
| VAL-F03 外部兼容 | 工具版本、原始日志/适配器 | 只读核验格式、权限、异常与时钟 | 无法确认记 unsupported/待验证，不以官方文档代替实测 |
| VAL-F04 闭环评价 | 设备、基线、推荐、实验与反馈 | 冻结协议→重复采样→共同口径→体验评估 | 条件不明降级，不能以 FPS 单值证明成功 |
| VAL-F05 发布审阅 | 证据包、版本与未解决项 | 对照门禁→可复现报告/限制清单 | 任一关键伪造/错配必须阻止相应能力发布 |

| 原需求 | 主要验收编号/归属 | 必须看到的行为 |
|---|---|---|
| C01/C05 | DEV-V02、REC-V01/02 | 不误配笔记本、多 GPU；邻近参考附依据，不迁移 FPS |
| C02/C03 | KNW-V01/02、UX-V01 | 无目标可开始；自主补查，预算停止保留缺口 |
| C04/C06 | REC-V03/04、UX-V03 | 每项有来源/条件，目标、公开参考、本机结果分开 |
| C07 | DEV-V03、ENG-V06、UX-V04 | 无自动写游戏/驱动；声明采用保留来源 |
| C08 | KNW-V04、IMP-V04 | 未读图保持缺口；不启动长视频处理 |
| C09 | ENG-V05/06、KNW-V05 | 请求与返回模型分存，返回缺失为 null，密钥不出现 |
| C10 | VAL-V01 | 本次变更仅技术设计文档，未新增产品代码/依赖/运行结果 |
| C11 | DEV-V01/02、ENG-V02、UX-V02 | 自动失败后集中询问，部分回答/跳过/冲突有明确结果 |
| D01 | DEV-V04、FBK-V03/04、VAL-V02 | A→变更→B→复测→Outcome，连续调整保留自定义项，过期提交409 |
| D02 | ENG-V01/05、IMP-V03、UX-V06 | 幂等/墓碑/重启/未知调用计数一致；无检查点也安全恢复 |
| D03 | DEV-V05、PERF-V01/05/08、UX-V04/07/08 | 无推荐基线、未知配置、历史补录/日志关联均可保存；不改当前指针 |
| D04 | DEV-V01、ENG-V02、UX-V02 | 回答与输入修订原子，等待释放槽，终态不复活 |
| D05 | PERF-V02/03/04/07、FBK-V05、UX-V08 | 自适应定位与共同阈值分开，协议可创建冻结，分母/缺口/零时长正确 |

## 3. 组件与执行边界

建议 `tests/product/` 运行确定性测试；`tests/fixtures/product/` 保存抽象输入，真实原件登记独立只读目录和权限。未见来源先冻结提取输出再独立标注；标注者不能只看模型答案生成金样本。同模型回查只能叫一致性核对。

FixtureLoader 校验文件哈希与格式；ScenarioRunner 驱动应用服务/HTTP；SpyProvider 计数发送、模拟超时与已执行无响应；CrashInjector 只在临时库/临时目录指定边界退出；EvidenceWriter 输出脱敏报告。上述均为拟建测试组件，不允许故障注入到玩家已有产品库或采集运行目录。

## 4. 夹具与报告模型

本节模型仅用于验收工件，不参与产品领域存储；公共知识和产品输入仍使用原 Schema。所有 ID 是逻辑 fixture label，由 FixtureLoader 分配确定合法 UUID 并保存映射；不可把 A/B 当产品 ID 直接提交。

| 工件 | 字段（全 R，? 表示显式可空） | 唯一性/版本/来源规则 |
|---|---|---|
| FixtureManifest | fixture_id:string、version:int、schema_version:string、files:FileEntry[]、annotation_method:string、known_limitations:string[] | unique(id,version)；FileEntry 沿 shared，原字节 hash；内容修改增版 |
| CaseSpec | case_id:string、requirement_ids:string[]、purpose:string、initial_fixture:string、input_refs:string[]、steps:string[]、expected_assertions:string[]、mock_components:string[]、real_environment:string? | case_id 项目唯一；每断言可由记录/数值/调用计数验证；不写“正常即可” |
| CaseResult | case_id:string、fixture_version:int、run_id:string、status:string、evidence_level:string、observed_files:FileEntry[]、failure_notes:string[] | status 仅 passed/failed/not_run/blocked；未执行不能填 passed；运行记录只追加 |
| RunManifest | run_id:string、started_at:Timestamp、ended_at:Timestamp?、git_revision:string、python_version:string、dependency_versions:map<string,string>、model_requested:string?、model_returned:string?、prompt_version:string?、contract_versions:string[]、environment_note:string | 不知道实际模型返回名为 null；本机用户/路径脱敏；按运行唯一 |

数字容差采用指标测试声明的绝对/相对误差，例如纯双精度公式夹具建议 `abs<=1e-9 或 rel<=1e-9`；整数簇数/记录数必须精确相等。工具导出含舍入时单列其已核验精度，不把统一 1% 容差用于掩盖算法错误。

## 5. 测试接口与结构化示例

`load_fixture(fixture_id:string,version:int)->FixtureManifest` 只读，hash 不匹配即失败；`run_case(spec:CaseSpec,environment:RunManifest)->CaseResult` 仅在临时资源中变更；`write_report(results:CaseResult[],manifest:RunManifest)->受管理报告引用` 追加，导出遵守 07。重跑使用新 run_id，不能覆写旧失败报告。外部模型默认桩；真实服务调用必须另行明确预算，不作为离线测试的隐式步骤。

```json
{
  "case_id": "VAL-V03",
  "requirement_ids": ["D02"],
  "purpose": "首检查点前中断不导致未知调用重发",
  "initial_fixture": "interrupted-without-checkpoint@1",
  "input_refs": ["parent-input", "call-audit"],
  "steps": ["提交 checkpoint_id=null 的显式恢复", "同 key 重送"],
  "expected_assertions": ["新 parent 任务仅注册一次", "供应商发送计数不增加", "原任务仍 interrupted"],
  "mock_components": ["SpyProvider"],
  "real_environment": null
}
```

## 6. 采集、推荐与效果评价协议

抽取报告分别给字段正确/遗漏/多提、关系正确/错误、条件保留、定位有效、隔离数量及分母。全部隔离不能算高准确率；接受率提高不能以丢弃关键检查换取。四篇已见研究资料与官方更新仍为回归；新增作者/版式及第二游戏单列迁移集。

推荐按合法值、支持条件、组合、证据、未知、替代项和主方案选择理由验收；文本措辞可变，关键约束不能漂移。明确隔离：综合方案不得冒称作者整套实测，邻卡报告不得当本机预测，观察不得变成开关建议，图片未读不得关闭缺口。

本机实验在手动修改前保存基线，可无推荐。固定游戏/构建、设备/驱动、分辨率/FG、场景及有效时长；推荐每配置至少三次，记录采样顺序和热身。重复不足只作探索；一次玩家反馈不泛化到型号总体。先保存 ComparisonProtocol，再看调整后结果；同阈值重算簇/分钟和时间占比。玩家是否接受独立于指标效果；效应判定详见 05，不能把波动内的单次差异称稳定改善。

## 7. 故障注入与门禁

| 故障边界 | 注入方式 | 必须保留/阻止 |
|---|---|---|
| 注册提交前/后、响应前 | 临时 DB 回滚/提交后断连 | 无半任务；重试回同 ID/响应 |
| 外部 in_flight 后、响应存盘前 | SpyProvider 已执行但客户端超时 | outcome_unknown、原调用计数，禁止隐式重发 |
| 文件最终改名后、库发布前 | 临时目录完整文件+进程退出 | 不消费孤立文件；重试核验复用/受控清理 |
| 当前指针/回答/绑定并发 | 两连接屏障，同预期版本 | 一方提交，另一完整冲突；不丢回答或覆盖历史 |
| 首检查点前退出 | queued/首调用无 checkpoint 两分支 | 无调用可安全新任务；有未知调用仍阻止重复 |
| 损坏/迁移失败 | 修改临时副本哈希/失败迁移 | 保留原件、不可用原因；停止写入，不空成功 |

发现关键错配、虚构值、丢历史、重复未知调用或自动写设置时，阻止相应能力发布并保存反例；其余独立能力可继续测试。无真实工具/游戏环境时 CaseResult 为 not_run/blocked，不把 Mock 成功升级为兼容成功。

## 8. 外部依赖与真实验证清单

测试框架沿现有 pytest 范围；HTTP 测试适配器随最终固定 FastAPI 依赖矩阵验证；真实浏览器测试是交互集成，不能只模拟函数。工具信息核验入口与日期见 [07 §8](./07-data-and-architecture.md#8-技术依据替代与未验证项)、[01](./01-device-game-configuration.md)、[04](./04-performance.md)；文档来源不构成真实通过证据。

必须另行取得：目标 Windows 的只读探测样本、多 GPU/笔记本样本、FH6 游戏构建与设置/进程线索、固定 PresentMon 版本/参数/原 CSV/时钟、实际模型返回标识与用量、回环浏览器访问及响应丢失行为、至少一次手动采用与真实效果闭环。本次无游戏运行授权、无工具安装、无模型付费执行；以上保留待验证。

## 9. 验收用例与交付门槛

| 编号/需求/目的 | 初态与输入 | 操作 | 输出/状态断言 | Mock / 真实环境 |
|---|---|---|---|---|
| VAL-V01 / C10：设计边界 | 本次基准提交和工作区差异 | 核对文件/链接/Schema/状态名称 | 仅设计目录变更；未来用例均非运行成功；原件不改 | 无 Mock；本地 Git 静态检查 |
| VAL-V02 / D01—D05：闭环规则 | 抽象安装、A、自定义项、ChangeSet、两客户端、时间序列 | 按原 D 用例及模块 V 用例运行 | 指针/父链/绑定/共同阈值/回答/调用次数均满足对应断言 | 临时库/适配器桩；免游戏 |
| VAL-V03 / D02：无检查点恢复 | 上述 JSON CaseSpec 的父任务/未知调用 | 显式恢复、同 key 重试 | parent 任务仅一，未知调用发送增量零 | SpyProvider；免付费 |
| VAL-V04 / C04/C08：抽取质量 | 已见资料与独立冻结未见资料，含表列/条件/图片 | 冻结输出、独立标注、分类统计 | 分母可追溯、原件不变，未读图不报覆盖；关键错误隔离 | 规则用桩；真实提取需预算/独立标注 |
| VAL-V05 / C06、D05：效果限制 | 三轮基线/调整及玩家反馈，另有单次/不同场景 | 按同协议比较并评估 | 真实改善只限本环境；单次/场景差异降级；接受不等于 improved | 公式桩先行；local_effect 需真实游戏/玩家 |
| VAL-V06 / C02/C11、D02：页面旅程 | 新用户、模型失联、过时回答、响应丢失 | 走 UX-V01—08 与下节脚本 | 无入口目标关卡，刷新不重复调用，冲突可恢复，普通旅程不需查看 JSON | 浏览器/服务桩；真实回环集成必需 |
| VAL-V07 / D02/D03/D05：修补入口贯通 | 空协议库、基线中位40ms、历史日志[900,960)s及有依据的起点；过时/无依据分支 | 经HTTP冻结derived协议；原key重试；预览/关联历史日志并显式分析；尝试事后非探索比较 | 实际阈值80ms、协议ID/frozen_at不变；映射后[0,60)s且绑定闭合，新artifact可分析；过时无部分写，无依据不猜时间；事后走探索路径，旧对象/当前指针不改 | 固定序列/已验证CSV/临时DB/后台时间桩；真实浏览器集成另验 |

原始 M0—M6 里程碑含义保留：M1 知识输入、M2 推荐、M3 采用与性能、M4 反馈闭环、M5 产品演示、M6 迁移评价。每次只报告已完成的证据级别；第二游戏未验证不阻止单游戏声明范围演示，但禁止通用性宣传。

## 10. 实施任务与 DoD

| 任务/目标 | 依赖 | 拟建组件/文件 | 必须实现的接口/逻辑 | 验收/完成定义 |
|---|---|---|---|---|
| VAL-T01 夹具与结果规范 | B 共享契约 | tests/product/conftest.py；tests/fixtures/product/；未来验收报告工具 | FixtureLoader/CaseSpec/manifest/合法 UUID 映射、hash | VAL-V01；损坏夹具拒绝、旧期望不覆盖 |
| VAL-T02 确定性与故障门禁 | VAL-T01、ENG-T01—03 | tests/product/test_contracts.py、test_faults.py | CaseRunner/SpyProvider/CrashInjector；串联全部模块 V 用例 | VAL-V02/03；调用/对象数量断言可复现 |
| VAL-T03 兼容与未见评价 | VAL-T01、IMP-T03、DEV-T02、PERF-T01 | tests/integration/；独立标注与格式 manifest | 固定版本、真实文件样本、差异报告与分母 | VAL-V04；区分回归/未见，真实限制已登记 |
| VAL-T04 闭环与演示证据 | VAL-T02/03、UX-T03 | tests/integration/test_journey.py；脱敏效果报告 | 浏览器旅程、协议创建/冻结、历史日志人工关联、真实配置协议与前后比较、接受情况 | VAL-V05—07；无本机证据不得标 local_effect 通过 |

## 11. C5 完整旅程与交接

顺序执行：选择游戏→自动识别→失败时集中补充/明确跳过→无需 FPS 开始自主收集→看主推荐/来源/未知→保存原配置或选择配置未知→玩家手动采用→确认记录→启动/导入日志→闭合段与分析→反馈→keep/adjust/measure/research/insufficient_evidence→若 adjust 则基于当前 A 保存 ChangeSet、玩家采用为 B、复测、Outcome→历史比较/手动恢复确认。每步记录页面动作、对应任务/资源 ID、key 的复用或更新、来源/状态及后继；内部 ID 仅放验收证据，不要求玩家理解。

补充执行：基线分析后、复测前经comparison-protocols保存比较方式并读取实际冻结阈值，comparisons必须使用返回的具体ID；事后选择口径另建探索协议。外部日志既可按原log-imports尝试自动映射，也可从受管理文件直接log-inspection→log-associations，显式提供锚点/当时依据，得到新artifact/闭合绑定后再建analysis；不要求先伪建实时会话。串联PERF-V07/08、UX-V08、VAL-V07验证默认/派生阈值、同key丢响应、历史闭合、过时冲突与旧结果保护；没有实际依据的分支明确停在文件保留。

另走失败链：模型无配置→已有资料可读；未知调用→不隐式重发；活动段开放→先结束再分析；历史配置补录→当前指针不变；上下文失效→替代问题/解除等待；文件损坏→明确不可用；第二次点击/刷新→同一逻辑提交。C5 设计齐备后可进入 D 的全局审阅与实施计划，本次不自动进入 D 或业务编码。
