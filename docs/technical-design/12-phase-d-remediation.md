# 独立审计问题修订与复核记录

> 日期：2026-11-02。依据：[独立审计AUD-D-01—04](./11-phase-d-audit.md)及用户“修正问题、给最终编码交接文档、推送已有成果”的指令。
> 当前新产品建议基线：`product-contract/0.4`；公共知识仍0.1。范围为技术设计修订、交接与Git提交推送，本轮未开始业务实现。

## 1. 修订结果与权威分工

四项问题已写入各自权威定义，关闭级别为`static_design`；不是程序通过或真实环境验证。09/10的初次关闭记录和11的审计反例作为历史保留，最新状态以本文件及[编码交接](./coding-handoff.md)为准。

| 问题 | 当前定义/最小修订 | 消费者及验收 |
|---|---|---|
| AUD-D-01：后继字段域缩水 | [01 §5.3](./01-device-game-configuration.md#53-d-p04-已处理问题的关联更正)、[data §8.2](./shared/data-contracts.md#82-补充问题与回答)、[state §7.3](./shared/state-conventions.md#73-d-p04-更正链与创建原子性)：新facts_only保存amendment_root_ref，后继选择根域而非直接源.fields；基底保留前轮历史派生事实 | DEV-T04/ENG-T02/UX；DEV-V08、B-STA-13、UX-V09、VAL-V08 |
| AUD-D-02：未知位置同名误合并 | [01 §5.2](./01-device-game-configuration.md#52-d-p02-本机设置条目登记)、[07 §4](./07-data-and-architecture.md#4-物理存储索引与局部记录)：auto仅已定位，unknown位置明确distinct/reuse；GET local-settings返回当前头供选择；未知位置不受位置唯一键合并 | ENG-T01/DEV-T02/UX；DEV-V07、ENG-V07、UX-V09、VAL-V08 |
| AUD-D-03：显式替换缺输入 | [data §5.2/9](./shared/data-contracts.md#52-实际采用与观测)、01 §5.2、[state §4.2](./shared/state-conventions.md#42-采用事务)：AdoptConfigurationInput.setting_ref_replacements，仅manual_edit、固定当前base、重新确认旧值/单位及新声明，一事务保存完整B | DEV-T03/UX；DEV-V07、ENG-V07、UX-V09、VAL-V08 |
| AUD-D-04：文件前置遗漏 | [实施计划 §4](./implementation-plan.md#4-全局依赖与实施顺序)及01/02模块任务表：KNW-T01、DEV-T01均增加ENG-T03，DEV-T02传递继承；批次顺序已改 | ENG-T03→KNW-T01/DEV-T01→DEV-T02；文件故障断言并入原任务DoD |

同一事实仍只有一个写入所有者：01校验本机字段/目录/采用，02校验公共身份，04定义测试/比较协议，07协调事务/版本/权限/幂等。06只消费返回引用，08只规定证据层级。没有新建平行DTO或GSD/.planning目录。

## 2. 修订后的反例走查

### 2.1 GPU之后再补显存

R0包含GPU/显存，状态skipped。创建R1仅问GPU：root_ref固定R0的已处理revision，R1.fields只有GPU。回答后保留派生历史设备快照D1。再从R1创建R2选择显存，字段资格从R0固定域取得，不因R1没问显存而422；R2以D1所在历史上下文为基底，回答生成D2，只覆盖显存，GPU更正及其出处仍来自D1。根/直接源snapshot和manifest引用都固定，不读当前设备。

R0再建并列后继仍409；R1尚open/partial仍需先完成/跳过；过时修订无维护写。root_ref不可由客户端自填。根或原件缺失时拒绝并给定位，旧问题仍保留；所有facts_only创建/回答/跳过/失效处理均无源TaskRun/TaskEvent/TaskInputRevision/成果写入。旧0.3链仅在能从既有manifest及历史版本证明根时创建新0.4后继，不倒填旧对象。

### 2.2 两个“质量”，都不记得位置

第一次选择“作为不同条目登记”：menu_path=[]、identity_mode=distinct、reuse_local_setting_id=null，得到S1。第二次用户明确另一个条目仍distinct、新key，得到S2；它们是两个series，可分别声明High/Low。响应丢失重送各自原key仍得原ID。若是同一个条目，GET local-settings选S1并reuse，不创建第三个series。未知位置auto为422，不能暗中按名字取第一项。

已定位auto同键并发仍复用一个有效头。之后补齐路径，只移动被更正series的头/位置槽，不能迁移采用值；两个不同series争同一已定位目标只有一个成功，另一方409。旧格式已有同键多头通过歧义桶保留并拒绝自动选择，不为迁移唯一索引删除历史。

### 2.3 目录更正后的新配置

A含L1=High、K及未知U。目录更正产生同series后继L2，A仍引用L1。用户另行确认manual_edit：base_applied_id=expected_current_applied_id=A；映射L1→L2，expected_old_value=High及旧单位与A严格匹配；declared_settings给L2的新值/单位/本次生效声明。复制A，删映射中的L1，按新声明加L2，K/U/预设保留，得到B且parent=A。B只含L2，旧A/摘要/来源不变。

没有映射时不自动替换；错旧值/旧头409、旧值未知先独立确认、跨安装/series422、一对多/替换链422；其他origin非空映射拒绝。任一失败无半声明/采用/指针。该操作记录显式采用事实，不将元数据更正包装成ChangeSet收益或受控实验。

### 2.4 文件发布顺序

顺序为ENG-T01＋VAL-T01→ENG-T02→ENG-T03，之后才能按完整DoD实施KNW-T01/DEV-T01；DEV-T02继续依赖二者。可以提前读规格/写纯规则反例，但不能在缺文件服务时把这些任务标完成。01上下文桩仍可避免KNW↔DEV循环；文件发布必须用真实临时目录/产品库，不用桩冒充故障恢复证据。

## 3. 版本、请求和历史兼容

新增字段和行为改变可表达范围，因此新产品对象写0.4。完整兼容规则在[data §12.2](./shared/data-contracts.md#122-独立审计修订的04兼容)，HTTP/CLI版本入口在[07 §11](./07-data-and-architecture.md#11-c5-路由集成登记)。

- 新客户端显式保存并发送X-Product-Contract-Version；不向拒绝未知字段的业务JSON塞Schema字段。旧草稿保持原Schema，已有key按原版本/hash/响应返回，不以0.4默认重算。
- 已登记key显式版本头不符409；无版本头、无已登记key的新变更422。旧版本无法安全执行的新提交明确拒绝，用户重新确认后才建立0.4新草稿，不能自动换版本/换key。
- 旧Adopt读取空替换视图，不补造旧值替换；旧LocalSettingRegistration原请求/原响应不变，无法区分的历史不强行拆分；新显式更正才生成新事实。
- 公共知识0.1、原Collector接口/导出、TaskType/TaskState不变。Q1—Q3/R03仍待确认/建议；工具、模型、图片和效果门禁保持原状态。

## 4. 验收与实施交接

保留34个任务、63个模块场景和42个基础/架构场景编号；只扩充DEV-V07/08、ENG-V07、UX-V09、VAL-V08及B-DAT-14/B-STA-13/B-ERR-12，并同步全局任务卡/测试路径。新增两个依赖边后应有110条显式边，完整静态检查结果见下节。

已消除四个发现所指的文档阻断；不是保证此后不可能发现新缺口。后续实现遇到未定义输入/守卫，先形成最小反例并修对应所有者，不在UI或适配器里补猜测规则。当前可交接的首个离线基础切片详见[编码交接](./coding-handoff.md)，不要把设计修订提交当作业务实现完成记录。

## 5. 本轮验证记录

本节在提交前记录实际执行结果。检查范围包括当前设计链接/锚点/表格/JSON、请求示例、34任务及110条依赖、63模块/42基础编号与计划覆盖、旧C验收保留、修改范围及既有原型离线回归。程序级新产品用例、HTTP/浏览器/数据库故障、实际迁移/硬件/游戏/模型/图片/效果均未执行，保持not_run。

- 标准库只读扫描覆盖本目录全部22份Markdown（含历史审计/提示/交接）：234个本地链接/锚点、150个表格、14个JSON及2个Python示例语法未检出错误；5种新增POST示例与DTO字段集合一一相同。
- 34源任务/依赖表/任务卡集合相同，110条边全部指向已登记任务，34节点可拓扑排序；KNW-T01、DEV-T01、DEV-T02的前置闭包均包含ENG-T03。
- 对比C提交abd08c2：原55个模块验收表格行内容全部相同，8个后加场景扩断言后仍共63个，计划范围简写展开后全覆盖；基础/架构42项保留。
- `py -3.13 -B -m pytest -q -p no:cacheprovider`：45 passed in 2.53s，限既有采集原型；没有新增产品测试或依赖。`git diff --check`通过。
- 扫描器是标准库临时脚本，不是完整Markdown渲染/请求Schema执行器；没有生成业务代码、改金样本或运行产品迁移。正文反例为规则走查，新增产品验收仍待后续实现。
- 对开始时72个Git可见工作文件的SHA-256复核：18个既有文档有本次修订，另新增本记录及coding-handoff.md；所有内容变化均在technical-design目录，阶段A快照、源码、依赖和冻结样本不变。此前未提交的D/P文档与本次修订一并提交；推送状态及提交ID以Git实际结果和交付消息为准，不在提交前预填成功。
