# 独立代码评审提示词（首个实现切片，b94f2e1..d757eee）

> 用途：在新会话中做一次全新上下文的代码评审。只评审、不改代码。
> 生成日期：2026-11-02；评审基线：`d757eee`（已推送 origin/main）。
> 边界：本文件是评审指令，不是设计权威；权威仍是 `coding-handoff.md` 与 `implementation-plan.md`。

---

## 复制以下内容到新会话

你是这个仓库的独立代码评审者。只做评审，不要修改任何代码、不要提交、不要开始新任务实现。

仓库：`E:\code\game`，分支 `main`，HEAD = `d757eee`（已推送到 `origin/main`）
评审范围：`b94f2e1..d757eee` 共 9 个提交（产品实现的第一个大切片）

### 一、先建立事实

1. 读 `docs/technical-design/coding-handoff.md`（当前编码交接与边界）、
   `docs/technical-design/implementation-plan.md` §4 依赖表与 §5.1–5.3 任务卡、
   `docs/technical-design/shared/{data-contracts,state-conventions,error-conventions}.md`、
   `docs/technical-design/07-data-and-architecture.md` §4–§6。
2. 跑一次基线并记录结果：

   ```powershell
   py -3.13 -B -m pytest -q -p no:cacheprovider
   ```

   期望 `160 passed, 1 skipped`；跳过项是 Windows 上不生效的 POSIX 权限位断言。
3. 用 `git log --oneline b94f2e1..d757eee` 与 `git diff --stat b94f2e1..d757eee` 了解改动面。

### 二、本次评审范围（只有这些）

已完成并提交的 6 个任务：

| 任务 | 文件 |
|---|---|
| ENG-T01 产品版本库 | `game_app/contracts/{primitives,enums,errors,common}.py`、`repository.py`、`migrations/0001-0002` |
| VAL-T01 夹具与证据 | `tests/product/{conftest,test_fixture_loader}.py`、`tests/product/support/fixtures.py`、`tests/fixtures/product` |
| ENG-T02 持久任务 | `game_app/{tasks,application}.py`、`contracts/tasks.py`、`migrations/0003` |
| ENG-T03 文件发布与备份 | `game_app/files.py`、`migrations/0004` |
| KNW-T01 公共身份/声明槽/复用 | `game_app/knowledge.py`、`contracts/knowledge.py`、`contracts/device.py`(GameContext) |
| DEV-T01 只读获取与失败提问 | `game_app/device.py`、`game_app/adapters/windows_probe.py`、`contracts/information.py`、`migrations/0005` |

明确不在范围（不要报为缺陷）：

- 无 HTTP/CLI/UI；无推荐/性能/反馈/知识包构建；无 IMP-*、DEV-T02 之后的任务
- 真实 Windows 硬件探测、真实模型、真实游戏、浏览器均未执行（`WindowsProbeAdapter` 在测试中从不运行）
- Q1–Q3 未确认、各真实门禁（G-WIN/G-GAME/G-FORMAT/G-MODEL/G-WEB/G-IMAGE/G-EFFECT）无证据，这是已知状态

### 三、重点核查（逐项给结论）

1. **事务边界**：能否出现半事实（任务无输入/首事件/幂等记录；`ManagedFile` 无 committed 操作；committed 操作无幂等响应；`GameSelection` 无声明文件或无 `GameContext`）。
2. **幂等**：同 key 重放、并发同 key、异 hash、旧 schema 版本、墓碑；是否存在任何路径重放时重新执行外部调用或改写原响应。
3. **未知调用**：任何异常/崩溃路径是否会导致供应商请求被重发；`prepared` 与 `in_flight` 是否在发送前持久化。
4. **文件发布**：孤立文件是否可被消费；改名后未提交的重试；staging 与 commit 之间被篡改；托管文件名的路径逃逸（含 Windows 盘符、`..`、绝对路径、非规范名）。
5. **恢复**：启动时 `interrupted`/`waiting` 语义；`resume` 是否核对整条父链调用；checkpoint 归属与输入修订校验。
6. **身份与复用**：不同游戏是否会合并；同一游戏是否会被拆成两个 series；未核实别名/他人 provisional 是否会匹配；声明槽键是否严格等于 `NFC(raw_name).strip().casefold()` + 平台原值；`needs_choice` 是否会静默取首项；`assess_reuse` 是否可能把错平台、未发布、跨游戏、有未闭合关键缺口的包判为 `reuse`。
7. **设备诚实性**：是否存在伪造默认值（0、1080p、60Hz、第一个 GPU、"机器无 GPU"）；每个已知值是否都指向可读取的已发布原件；`failed`/`timeout`/`not_attempted`/`parse_failed` 是否可区分；GPU 字段是否按探测索引而非列表位置解析；只读适配器是否存在命令注入或未登记路径。
8. **契约保真**：bool 是否被当 int、NaN/Infinity、未知字段、跨类型引用、时间戳是否真为 `Timestamp`、枚举取值、`InformationRequest` 与 `DeviceSnapshot` 的形状约束。
9. **SQLite**：每连接 `foreign_keys`、唯一约束是否真正可执行（注意 NULL 语义）、`BEGIN IMMEDIATE`、锁竞争、共享连接跨线程。

### 四、复核前几轮已修的问题（确认修好且没引入新问题，不要重复报为"未修"）

- `c813667`：非 `ProviderRejected` 异常一律 `outcome_unknown`；托管路径拒绝盘符；`publish` 在提交事务内重读幂等；`interrupted` 写 `stop_reason`；`cancel` 的幂等上下文；模型调用必须有 prompt/schema 版本；provider 调用必须有文件存储；`object_references` 唯一索引。
- `d757eee`：GPU 按索引解析；显式 package 必须同游戏；复用读取所有不可变版本；声明槽归一化；未知平台不判 `reuse`；0/负数测量不记已知；未知 `game_context_id` 先拒绝；`diagnosis.` 前缀 scope；问题绑定具体 `DeviceSnapshot`。

### 五、输出格式

1. **按严重度排序的已核实缺陷清单**（Critical / Important / Minor）。每条给：
   - `文件:行号`
   - 一句话缺陷
   - 具体失败场景（输入/状态 → 错误结果）
   - 最小修复

   只报你能在代码里追踪到真实执行路径的问题；不要报猜测、风格、重构建议。

2. **"已复核通过"小节**：列出上述重点中确实没发现问题的项，各一句结论。

3. **"证据与未验证"小节**：本次实际执行了什么命令、退出码、测试数；哪些能力仍是 `not_run`/`blocked`。

4. 如果发现某条前轮修复不完整或引入回归，单列在**"前轮修复复核"**小节，并说明证据。

不要修改工作区。评审结束后只输出报告。

---

## 使用说明

- 评审者若报告缺陷，回到主会话按"先写失败测试、再最小修复"的顺序处理；每条修复都要有能失败的测试作为证据。
- 若评审者建议修改规格而不是代码，先判断是规格缺口还是实现偏差，并在对应权威文档中落字，不在代码里临时补规则。
- 评审通过后，按 `implementation-plan.md` §4 依赖表继续下一批任务（当前可开工：`IMP-T01`、`DEV-T02`）。
