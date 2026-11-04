# 独立游戏画质演示版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. If the user explicitly chooses delegated execution, use superpowers:subagent-driven-development instead. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **仅用于临时演示。正式开发不得以本文件及 `demo/` 下任何设计、代码、数据、接口、测试或结果作为参考、实现依据、验收依据或事实证据。**

**Goal:** 在 `demo/` 内交付可离线启动、可完整操作、可重置恢复的独立演示服务，首先打通 RTX 4060＋使命召唤：现代战争 II 的推荐—采用—测试—反馈—复测闭环，再按同一实现覆盖 RTX 5090＋极限竞速：地平线 6。

**Architecture:** Python 标准库 HTTP 服务、单一模拟任务工作线程、独立 SQLite 和受管理文件；原生 HTML/CSS/JavaScript 六页工作台。案例内容由固定本地资料和模板提供，操作通过后台事务改变真实演示状态，指标从同一组演示日志确定性计算。所有 API、数据和运行产物与正式模块隔离。

**Tech Stack:** Python 3.11+（本机使用 `py -3.13`）、`http.server`、`sqlite3`、`unittest`、JSON/CSV、原生 JavaScript ES modules、SVG/Canvas；不增加生产或演示第三方运行依赖。

**Spec:** [DESIGN.md](./DESIGN.md) `demo-design/1.0`、[AGENTS.md](./AGENTS.md)，以及本文 §2 固化的用户已确认展示顺序。

## Global Constraints

- 所有新增内容在 `demo/`，不改正式模块、依赖及文档。
- 不导入 `game_app` 或 `game_collector`，不纳入其包发现与 CLI。
- 仅使用 `demo/fixtures/` 与 `demo/runtime/`，不读取根目录研究样本和 `runs/`。
- 自有 `demo-api/1`；不复刻或声称兼容 `product-contract/0.4`。
- 服务仅绑定 `127.0.0.1`，默认端口 `8765`；所有资源同源本地，不使用 CDN 或在线字体。
- 实际外部调用为零；不启动游戏、不读取真实设置、不探测硬件、不调用模型、不启动真实采集工具。
- 页面常驻“演示模式”；报告首行明确“演示报告，非真实实测”。
- 案例 ID 固定为 `mw2-rtx4060` 与 `fh6-rtx5090`，状态、来源、配置、结果、历史和导出相互隔离。
- CPU、内存、显存、驱动、版本等未确认字段保留未知；1080p/4K 属演示预设而非自动探测结果。
- 推荐、声明采用和观测不同；本演示只有模拟观测，不把确认按钮写成已自动修改游戏。
- 图表、指标、比较与报告使用同一份原件和口径；禁止独立硬编码提升百分比。
- 查询不推进任务；重试复用原操作键；回滚追加版本；重置递增 epoch 并阻止旧任务写入。
- 单条 JSON 上限 1MiB；上传单文件上限 5MiB；不接受任意路径或压缩包执行。
- 本文所有命令和测试为后续执行指令。本轮只写规划，所有复选框初始未完成，不声称测试已运行。

---

## 1. 执行边界、准备与交付策略

本计划是一个独立演示子系统的单一实施计划，分二十个可验证任务顺序执行，不重新建立正式项目实施计划，不补正式产品能力。

开始实现前记录当前 `git status --short`、`git log -1 --oneline`，保护非本任务改动。不因工作区存在其他人的修改而清理或覆盖它们。隔离工作区如有需要，在执行时按相关技能处理；本轮编写规划不创建工作树。

所有 Python 测试使用演示自己的 `unittest`，避免根目录 pytest 的配置和正式夹具参与。任务 1 创建包标记后，统一回归命令：

```powershell
py -3.13 -B -m unittest discover -s demo/tests -t . -v
```

各任务的目标命令只运行该任务及直接依赖的行为。先增加有明确断言的失败测试，再最小实现，最后通过；只在新行为、失败或关联问题出现时扩大检查。不为固定文案逐行增加测试。

新模块第一次导入失败可以定位缺失文件，但不算业务行为的失败证据；先提供最小可导入接口，再观察具体断言因行为缺失而失败。测试发现数为零、语法错误或错误的 import 路径都不能记为有效先红后绿。

本文提供每任务的建议提交命令，但本轮不执行 Git 提交或推送。后续是否提交遵从用户届时指令；未获推送指令时不推送。若提交，只暂存当前任务明确列出的 `demo/` 文件，不使用 `git add .`。

### 1.1 里程碑与依赖

| 里程碑 | 任务 | 可检查成果 |
|---|---|---|
| M1 可启动基础 | T01—T05 | 双案例样例合法、独立存储、HTTP 就绪、任务推进及重置可用 |
| M2 推荐与采用 | T06—T10 | 背景补充、资料、推荐、版本、会话可通过 API 连续操作 |
| M3 性能与反馈闭环 | T11—T15 | 同原件统计、采集/上传、比较、反馈、完整案例与报告可用 |
| M4 玩家界面 | T16—T19 | 六个页面及控制台均有真实操作，无空按钮 |
| M5 可交付演示 | T20 | 双案例全旅程、断网、刷新、重启、异常、启动脚本及讲解记录完成 |

主依赖顺序为 T01→T02→T03→T04→T05→T06→T07→T08→T09→T10→T11→T12→T13→T14→T15→T16→T17→T18→T19→T20。纯数值任务 T11 可独立于页面，但本计划默认顺序执行，不自动派发子代理。

每个里程碑完成不代表整个演示交付完成。两案例都必须执行最终验收；不能只更换游戏标题和 GPU 标签交付第二案例。

## 2. 已确认的 RTX 4060 展示顺序

1. 选“RTX 4060＋使命召唤：现代战争 II”，查看背景；不把目标 FPS 设为开始门槛。
2. 展示收集、提取、校验、补查，展开模拟来源和缺口。
3. 显示主推荐与画质/性能替代差异，本机结果区保持待测试。
4. 保存原配置 A0，再确认手动设置并采用 A1；实际游戏由演示者自行操作。
5. 保存测试计划，模拟采集得到 S1：整体流畅但有少量尖峰的样例。
6. **在后测前冻结比较协议 P1**；反馈“交战时偶尔卡一下，希望保持画面清晰”。
7. 给出一项阴影相关的演示调整 C1；确认后由 A1 合并生成 A2。
8. 复测得到 S2，用 P1 对比 S1/S2；用户独立选择是否接受画面。
9. 保持 A2，或拒绝后追加回滚版本 A3；查看历史并导出。

RTX 5090＋地平线 6 按同一结构讲解，但使用自己的 4K 演示预设、来源包、反射相关调整、纹理保护项和日志。A0/A1 等是讲解代号，不是数据库主键。

## 3. 文件划分与稳定接口

### 3.1 拟建文件责任

| 文件/目录 | 责任 | 首次任务 |
|---|---|---|
| `demo/__init__.py`、`app/__init__.py`、`tests/__init__.py` | 演示独立包标记，避免脚本/测试使用两个导入命名空间 | T01 |
| `demo/app/contracts.py` | 演示 Command、Reply、DomainResult、错误及基础字段 | T02 |
| `demo/app/scenarios.py`、`fixtures/` | 案例、来源、设置、推荐、规则与 trace recipe 的校验和读取 | T01 |
| `demo/app/store.py`、`files.py` | 六表数据库、事务、引用、幂等与受管理文件 | T02/T03 |
| `demo/app/service.py`、`actions.py` | 命令分发、查询、服务装配与显式动作注册 | T03 |
| `demo/app/http.py`、`server.py`、`start-demo.cmd` | HTTP、同源/大小限制、启动停止、端口错误 | T04 |
| `demo/app/tasks.py`、`controls.py` | 单线程模拟阶段、取消/恢复、模式/剧本/重置 | T05 |
| `demo/app/device.py` | 背景修订、模拟设备、补充及更正 | T06 |
| `demo/app/knowledge.py`、`recommendation.py` | 知识流程/导入/复用、模板推荐/替代/复核 | T07/T08 |
| `demo/app/configuration.py` | 设置目录、采用、手改、回滚、历史登记 | T09 |
| `demo/app/sessions.py` | 测试计划、会话、切段、绑定修订 | T10 |
| `demo/app/traces.py`、`metrics.py` | 确定性样例、CSV 解析、清洗与统计纯函数 | T11 |
| `demo/app/performance.py`、`comparison.py` | 模拟采集/日志关联/分析任务、比较口径与差异 | T12/T13 |
| `demo/app/feedback.py` | 反馈、偏好、诊断、变更及接受结果 | T14 |
| `demo/app/exports.py`、`seeding.py` | 报告/备份恢复、有效完整历史的一键构建 | T15 |
| `demo/static/index.html`、`styles.css`、`js/` | 本地工作台、草稿/API、六页与图表 | T04/T16—T19 |
| `demo/tests/support.py`、`test_*.py` | 临时库/时钟、领域行为、HTTP 与双案例集成 | 各任务 |
| `demo/DEMO_SCRIPT.md`、`VALIDATION.md` | 最终讲解步骤、实际验收证据与限制 | T20 |

性能相关拆成纯函数、会话、编排、比较四个文件，避免把所有逻辑塞进一个 `performance.py`。这一拆分仅细化演示内部实现，不修改设计的外部行为。

### 3.2 Python 公共类型

T02 在 `contracts.py` 定义以下类型，后续任务不擅自重命名字段：

```python
from dataclasses import dataclass, field
from typing import Any

JsonObject = dict[str, Any]

@dataclass(frozen=True)
class Scope:
    scenario_id: str
    workspace_epoch: int

@dataclass(frozen=True)
class Command:
    request_id: str
    scenario_id: str
    workspace_epoch: int
    expected_revision: int
    action: str
    payload: JsonObject

@dataclass(frozen=True)
class DomainResult:
    status: int = 200
    refs: list[JsonObject] = field(default_factory=list)
    task_id: str | None = None
    data: JsonObject = field(default_factory=dict)

@dataclass(frozen=True)
class Reply:
    status: int
    body: JsonObject

class DemoError(Exception):
    def __init__(self, code, message, *, status=400,
                 field_errors=None, recovery_actions=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field_errors = field_errors or []
        self.recovery_actions = recovery_actions or []
```

所有实体是 JSON 对象，共有 `id`、`kind`、`scenario_id`、`workspace_epoch`、`created_at`、`demo_only=true`、`data_origin`；可修订聚合额外有严格正整数 `revision`。对象引用统一 `{"kind": "AppliedConfiguration", "id": "demo_..."}`。所有“未知数值”为 JSON null 加 `unknown_reason`，不使用 NaN/Infinity 或数值零占位。

`Clock` 约定提供 `now_iso() -> str` 与 `monotonic() -> float`；真实时钟只用于演示后台等待，样例时间轴以 `simulated_relative_ms` 标记。测试的 FakeClock 可显式推进，不使用真实 sleep。

### 3.3 后台装配接口

下表中的接口在对应任务落地，后继任务以此消费：

| 所有者 | 签名/结果 |
|---|---|
| scenarios | `ScenarioCatalog.load(root: Path) -> ScenarioCatalog`；`get(scenario_id: str) -> JsonObject`；`list() -> list[JsonObject]` |
| store | `DemoStore(runtime_root: Path, *, clock)`；`initialize(catalog) -> None`；`workspace(scenario_id) -> JsonObject`；`entity(scope, entity_id, *, kind=None) -> JsonObject` |
| store | `mutate(command: Command, handler: Callable[[Transaction], DomainResult]) -> Reply`，重放检查先于新提交修订检查；epoch 校验先于所有写入 |
| transaction | `workspace() -> JsonObject`；`insert(kind, payload, *, origin) -> JsonObject`；`get(entity_id, *, kind=None) -> JsonObject`；`update_workspace(**fields)`；`emit(event, payload) -> int` |
| files | `ManagedFiles(runtime_root)`；`stage(data: bytes, media_type: str) -> JsonObject`；`register(tx, staged) -> JsonObject`；`read(file_record) -> bytes`，原件发布前就绪且校验 hash |
| service | `create_service(runtime_root: Path, *, fixture_root: Path, clock=None) -> DemoService`；`command(command: Command) -> Reply` |
| service | `workspace(scenario_id)`、`entity(scope, id)`、`task(scope, id)`、`history(scope, after=0, limit=20)`；查询返回字典，无副作用 |
| service | `register(action, validate, handle)`；validate 为纯字段校验；handle 接收 `ActionContext` 与已校验 payload，返回 DomainResult |
| service | `upload(command: Command, *, data: bytes, media_type: str, filename: str) -> Reply`，内部动作标识 `file.upload`，只由上传入口与测试工具调用，返回 File 引用 |
| context | `ActionContext(tx, command, catalog, files, clock)`，供领域动作读取固定案例和事务，不负责提交事务 |
| tasks | `TaskRunner(service, *, clock)`；`tick() -> bool` 推进至多一个到期阶段；`start()`/`stop()` 管理唯一工作线程 |
| store/tasks | `advance_task(scope, task_id, expected_phase, publish) -> bool`，内部短事务核对 epoch/取消/阶段，原子发布阶段事实和事件，重入不重复 |
| http | `create_http_server(service, *, host='127.0.0.1', port=8765) -> ThreadingHTTPServer` |

业务查询统一用 Scope；跨 scope 引用返回 404，不把其他案例对象取出后再改其标签。HTTP 从查询参数 `scenario_id` 与 `workspace_epoch` 构造 Scope；bootstrap/health/目录无隐式全局“当前案例”。

### 3.4 事务与重试的确定顺序

`DemoStore.mutate` 持应用写锁，打开连接并 `BEGIN IMMEDIATE`，依次做：

1. 校验案例存在和 epoch；旧 epoch 409，不能重放到新工作区。
2. 查 `(scenario_id, epoch, request_id)`；已登记则核对规范请求 hash，相同返回原 Reply，异内容 409。
3. 校验 `expected_revision` 与局部引用守卫。只读失败不消耗操作键。
4. 运行领域处理器，追加实体/事件，更新指针和工作区 revision。
5. 同事务登记规范请求、响应原件和操作键；提交后才返回 Reply。

规范 hash 包含 action、case、epoch、expected_revision 和 payload；同一草稿重试保留它们。新内容或冲突后重新确认才换操作键。内部任务推进不暴露给任意 HTTP payload，而走受控 `advance_task`。

上传 multipart 包含一个 `command` JSON 字段和一个 `file` 字段。command 保留相同的 request_id/case/epoch/revision，payload 只含标准化媒体类型、文件名标签、字节数和内容 hash；实际 bytes 另传 upload。文件体限 5MiB、multipart 总体额外允许 64KiB 开销；不允许多个文件、空 command 或无界 chunked 读取。文件名只用于显示，不作为磁盘路径；`file.upload` 不接受从 `/commands` 直接调用。h.upload 构造该命令，调用服务上传并从 File 引用取 ID。

### 3.5 动作注册表的完整清单

`actions.py` 只登记当前已实现且验证的动作。以下是最终必须覆盖的固定集合，任务消费时不使用斜杠缩写推断动作名：

```python
ACTION_OWNERS = {
    'context.save': 'T06',
    'device.scan': 'T06',
    'information.answer': 'T06',
    'information.amend': 'T06',
    'knowledge.collect': 'T07',
    'knowledge.import': 'T07',
    'knowledge.followup': 'T07',
    'knowledge.update': 'T07',
    'recommendation.generate': 'T08',
    'recommendation.variant': 'T08',
    'recommendation.review': 'T08',
    'setting.register': 'T09',
    'configuration.adopt': 'T09',
    'configuration.edit': 'T09',
    'configuration.rollback': 'T09',
    'configuration.record_history': 'T09',
    'plan.save': 'T10',
    'session.create': 'T10',
    'session.split': 'T10',
    'session.end': 'T10',
    'binding.revise': 'T10',
    'capture.start': 'T12',
    'capture.stop': 'T12',
    'log.inspect': 'T12',
    'log.associate': 'T12',
    'performance.analyze': 'T12',
    'comparison.freeze': 'T13',
    'comparison.run': 'T13',
    'feedback.save': 'T14',
    'feedback.next': 'T14',
    'preference.save': 'T14',
    'target.respond': 'T14',
    'outcome.save': 'T14',
    'task.cancel': 'T05',
    'task.resume': 'T05',
    'task.advance': 'T05',
    'demo.mode': 'T05',
    'demo.script': 'T05',
    'demo.reset': 'T05',
    'demo.seed': 'T15',
    'export.create': 'T15',
    'backup.create': 'T15',
    'backup.restore': 'T15',
}
```

`file.upload` 是 T03 的专用上传通道，不在上面公开命令集合内。`log.inspect` 属显式预览命令，可存原操作响应用于重放，但不创建会话/摘要；普通 GET 仍严格无副作用。`task.advance` 只允许 step 模式任务，其他模式返回状态冲突。

所有公共命令的 validator 拒绝未知字段；示例省略的可选字段只允许协议内明确的缺省值。本文接口表中的 payload 与测试若有冲突，先修正文档再实现，不能在处理器里默默兼容两个名字。

## 4. 任务实施卡

下列测试片段均写入实际任务所列文件，再执行目标命令。片段使用 `unittest`；每个类名及方法名为具体预期，不能替换成“对象存在即可”。

### T01：双案例内容、样例清单与独立包

**Files**：创建 `demo/__init__.py`、`demo/app/__init__.py`、`demo/tests/__init__.py`、`demo/app/scenarios.py`、`demo/fixtures/manifest.json`、`demo/fixtures/failure-scripts.json`；每个案例目录创建 `context.json`、`sources.json`、`settings.json`、`templates.json`、`rules.json`、`trace-recipes.json`；测试 `demo/tests/test_scenarios.py`。

**Interfaces**：产出 §3.3 ScenarioCatalog。fixture schema 为 `demo-fixtures/1`，顶层 `demo_only=true`，两案例 ID 固定；manifest 保存每个成员的相对路径与 SHA256。路径仅允许案例内部普通文件。

- [ ] 写如下失败测试，并增加缺成员/hash 错误/跨案例设置引用拒绝的反例。

```python
import unittest
from pathlib import Path
from demo.app.scenarios import ScenarioCatalog

class ScenarioTests(unittest.TestCase):
    def test_two_cases_have_distinct_settings_and_unknown_cpu(self):
        catalog = ScenarioCatalog.load(Path('demo/fixtures'))
        a = catalog.get('mw2-rtx4060')
        b = catalog.get('fh6-rtx5090')
        self.assertIn('4060', a['context']['gpu_name'])
        self.assertIn('5090', b['context']['gpu_name'])
        self.assertIsNone(a['context']['cpu'])
        self.assertIsNone(b['context']['cpu'])
        self.assertGreaterEqual(len(a['settings']), 12)
        self.assertTrue(set(x['id'] for x in a['settings']).isdisjoint(
            x['id'] for x in b['settings']))
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_scenarios -v`，记录尚缺读取/校验行为的失败。
- [ ] 实现 `ScenarioCatalog.load`：有界 JSON 读取、必填字段、成员 hash、设置/证据引用、两个 GPU 标签、模拟来源标识校验；目录越界、坏哈希直接报错，不能降级为空包。
- [ ] 固定两套十二项以上的演示设置，至少包含 `mw2.shadow_quality`、`mw2.texture_detail`、`fh6.reflection_quality`、`fh6.texture_detail`；两个质量项允许 high/medium/low，主方案的阴影/反射均预设 high，反馈调整到 medium，纹理保持 high。模板固定值引用目录，来源用 `demo://`，技术支持未知不推断。trace recipe 定义 baseline/recommended/adjusted/no_gain/short 五类，具体帧 CSV 在 T11 生成。
- [ ] 重跑目标测试，确认两案例内容独立且不含真实来源断言；建议提交 `git add -- demo/__init__.py demo/app/__init__.py demo/tests/__init__.py demo/app/scenarios.py demo/fixtures demo/tests/test_scenarios.py`，`git commit -m "feat(demo): add two isolated scenario fixture packs"`。

### T02：独立存储、引用与幂等

**Files**：创建 `demo/app/contracts.py`、`demo/app/store.py`、`demo/tests/test_store.py`；修改 T01 catalog 的错误输出仅在需要统一 DemoError 时进行。

**Interfaces**：产出 §3.2 公共类型、DemoStore 与 Transaction。数据库六表见设计 §8.2；workspace 包含 `scenario_id`、`workspace_epoch=1`、`revision=1`、`context_id`、`current_applied_id=null`、`mode='narrated'`、`script='none'`。

- [ ] 写事务中途失败零实体/指针/操作键、同键丢响应重放、异内容冲突、跨案例引用和旧 epoch 拒绝的失败测试。

```python
import tempfile
import unittest
from pathlib import Path
from demo.app.contracts import Command, DomainResult
from demo.app.scenarios import ScenarioCatalog
from demo.app.store import DemoStore

class StoreTests(unittest.TestCase):
    def test_retry_replays_before_stale_workspace_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DemoStore(Path(directory), clock=None)
            store.initialize(ScenarioCatalog.load(Path('demo/fixtures')))
            cmd = Command('retry-1', 'mw2-rtx4060', 1, 1, 'test.save', {})
            def save(tx):
                item = tx.insert('Note', {'text': '演示'}, origin='user_declared')
                return DomainResult(201, [{'kind': 'Note', 'id': item['id']}])
            first = store.mutate(cmd, save)
            second = store.mutate(cmd, save)
            self.assertEqual(first, second)
            self.assertEqual(store.workspace('mw2-rtx4060')['revision'], 2)
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_store -v`，确认事务/重放测试先失败。
- [ ] 实现 §3.4 顺序，显式锁与连接生命周期；bool 不充整数修订，JSON 拒绝非有限数；scope 引用由 `Transaction.get` 统一核对。启动样例背景作为初始化事实，初始化重跑不重建工作区。
- [ ] 增加两个线程以同操作键同时提交只生成一份实体的测试，以及重置后旧键不能写入新 epoch 的存储原语测试。文件阶段尚未落地时不自报文件发布通过。
- [ ] 通过目标命令；建议提交 `git add -- demo/app/contracts.py demo/app/store.py demo/app/scenarios.py demo/tests/test_store.py`，`git commit -m "feat(demo): persist scoped facts and replay commands atomically"`。

### T03：命令协调、受管理文件与测试工具

**Files**：创建 `demo/app/service.py`、`demo/app/actions.py`、`demo/app/files.py`、`demo/tests/support.py`、`demo/tests/test_service.py`、`demo/tests/test_files.py`。

**Interfaces**：产出 §3.3 DemoService、ActionContext、ManagedFiles。`register_builtin_actions(service)` 初期不登记未实现动作，未知 action 400，不能空跑成功。`DemoCase` 与 `DemoHarness` 固定下列接口供后继测试调用。

| 测试工具 | 精确约定 |
|---|---|
| `DemoCase(unittest.TestCase)` | setUp 创建 TemporaryDirectory、FakeClock、`self.h=DemoHarness(...)`；tearDown 停止服务并清理 |
| `DemoHarness.command(action, payload=None, *, scenario='mw2-rtx4060', request_id=None, epoch=None, expected_revision=None) -> Reply` | 缺省取当前工作区修订；显式传值保留；内部保留生成 Command 供重试 |
| `h.workspace(scenario='mw2-rtx4060') -> dict` | 只读查询 |
| `h.entity(entity_id, *, scenario='mw2-rtx4060') -> dict` | 按当前 epoch 查询 |
| `h.ref(reply, kind) -> str` | 在结果 refs 中取唯一匹配 ID，多/零匹配令测试失败 |
| `h.clock.advance(seconds)` | 推进固定单调和 ISO 时钟 |
| `h.run(task_id) -> dict` | T05 补齐：反复 tick/推进 FakeClock 至终态或 waiting_input，最多一百步，超限测试失败 |
| `h.upload(data, media_type, *, scenario='mw2-rtx4060') -> str` | 受管理登记，只返回文件 ID，不解析内容 |

- [ ] 写未知动作不登记 mutation、文件缺失/篡改不可消费、未发布暂存文件不可查和文件名路径越界拒绝的失败测试。

```python
from demo.tests.support import DemoCase
from demo.app.contracts import DemoError

class ServiceTests(DemoCase):
    def test_missing_handler_is_not_reported_as_completed(self):
        with self.assertRaises(DemoError) as caught:
            self.h.command('not.registered')
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.h.workspace()['revision'], 1)
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_service demo.tests.test_files -v`，确认关键行为先失败。
- [ ] 实现显式 validator/handler 注册，Reply 统一包装为 `demo_only`、`request_id`、`workspace_revision`、`refs`、`task_id`、`data`；后台实体读取强制 scope。文件用受管理 ID 和 SHA256，临时就绪后登记，事务失败遗留暂存不开放下载。
- [ ] 创建 FakeClock 与上述测试工具；测试默认全部使用临时目录，禁止 runtime_root 指向正式数据位置；HTTP 上传尚未实现时 h.upload 直接调用同一受管理发布入口。
- [ ] 目标测试通过；建议提交 `git add -- demo/app/service.py demo/app/actions.py demo/app/files.py demo/tests/support.py demo/tests/test_service.py demo/tests/test_files.py`，`git commit -m "feat(demo): coordinate explicit actions and managed files"`。

### T04：可启动 HTTP 服务与最小页面

**Files**：创建 `demo/app/http.py`、`demo/server.py`、`demo/start-demo.cmd`、`demo/static/index.html`、`demo/static/styles.css`、`demo/static/js/app.js`、`demo/tests/test_http.py`。

**Interfaces**：产出 `create_http_server` 和设计 §11 的完整通用路由。未落地领域命令返回明确 400；页面此时只提供就绪/两案例占位布局，不能声称六页功能完成。

- [ ] 用本地随机端口写 health、静态资源、bootstrap、未知 action、同源校验、大小限制和路径穿越失败测试。

```python
import json
from urllib.request import urlopen
from demo.tests.support import DemoCase, running_http

class HttpTests(DemoCase):
    def test_health_announces_demo_and_two_scenarios(self):
        with running_http(self.h.service) as base:
            with urlopen(base + '/api/demo/v1/health') as response:
                health = json.load(response)
            with urlopen(base + '/api/demo/v1/bootstrap') as response:
                bootstrap = json.load(response)
        self.assertIs(health['demo_only'], True)
        self.assertEqual(len(bootstrap['scenarios']), 2)
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_http -v`；`running_http(service)` 在 support 中创建 host=127.0.0.1/port=0 的后台测试线程并在 finally 关闭。
- [ ] 实现路由白名单与正确 HTTP 状态；reject NaN/Infinity；有 Origin 的 POST 只接受本机服务同源，GET 不写领域状态。multipart 用标准库 `email.parser.BytesParser` 解析有界请求，不能使用 Python 3.13 已无的 `cgi`。处理 malformed boundary、多个文件和超限。
- [ ] `server.py` 支持模块/脚本两入口，仅导入 `demo.app` 同一命名空间；按 `__file__` 定位资源。cmd 用 `%~dp0` 定位，检查 `py -3.13`，端口占用只报错不杀进程。Ctrl+C 正常关闭 HTTP/工作线程/连接。
- [ ] 目标测试与启动探测通过；建议提交 `git add -- demo/app/http.py demo/server.py demo/start-demo.cmd demo/static demo/tests/test_http.py demo/tests/support.py`，`git commit -m "feat(demo): serve a local offline demo entry point"`。

### T05：任务阶段、模式、取消、恢复和 epoch 重置

**Files**：创建 `demo/app/tasks.py`、`demo/app/controls.py`、`demo/tests/test_tasks.py`、`demo/tests/test_controls.py`；修改 `service.py`、`store.py`、`actions.py`、`server.py`、`tests/support.py`。

**Interfaces**：产出 TaskRunner 和 `advance_task`；登记 `task.cancel/resume/advance`、`demo.mode/script/reset`。`enqueue(tx, kind, payload, *, parent_id=None) -> dict` 创建 queued 任务，阶段发布器通过 `TaskRunner.register(kind, phases)` 显式注册。

- [ ] 写不靠 GET 推进、等待输入释放执行槽、取消后阶段不再发布、恢复关联新任务、重启保留成果、重置阻止旧 tick 写入的失败测试。

```python
from demo.tests.support import DemoCase

class ControlTests(DemoCase):
    def test_reset_changes_epoch_without_touching_other_case(self):
        before_a = self.h.workspace()
        before_b = self.h.workspace('fh6-rtx5090')
        self.h.command('demo.reset', {'scope': 'current'})
        after = self.h.workspace()
        self.assertEqual(after['workspace_epoch'], before_a['workspace_epoch'] + 1)
        self.assertIsNone(after['current_applied_id'])
        self.assertEqual(self.h.workspace('fh6-rtx5090'), before_b)
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_tasks demo.tests.test_controls -v`，确认失败后再实现。
- [ ] 实现 narrated/fast/step 三模式，默认八秒资料、三秒推荐、十二秒模拟采集参数来自阶段定义；单案例重任务槽和单工作线程；waiting_input 不持槽。阶段发布与事件同事务；取消意图和 epoch 在提交前重核。
- [ ] 重启将未完成任务标中断 partial，resume 创建 parent 关联新任务；已完成 stage key 不重复。reset 递增 epoch 并创建新初始背景，不改另一案例；清理只能处理登记在 runtime 内的产物。补齐 h.run，禁止测试用真实 sleep。
- [ ] 目标测试通过；建议提交 `git add -- demo/app/tasks.py demo/app/controls.py demo/app/service.py demo/app/store.py demo/app/actions.py demo/server.py demo/tests/support.py demo/tests/test_tasks.py demo/tests/test_controls.py`，`git commit -m "feat(demo): persist staged tasks and reset isolated workspaces"`。

### T06：背景、设备模拟、补充回答与更正

**Files**：创建 `demo/app/device.py`、`demo/tests/test_device.py`、`demo/tests/test_information.py`；修改 `actions.py`、`tests/support.py`。

**Interfaces**：登记 `context.save`、`device.scan`、`information.answer/amend`。context payload 为 `fields` 和 `expected_context_id`；回答 payload 为 `request_id`、`question_revision`、`context_revision`、`answers`、`skipped_fields`。背景修订不自动推进旧推荐的适用状态为通过。

- [ ] 写设备未知、回答不变成自动探测、不重复询问、两修订冲突、终态更正不复活源任务和自定义分辨率降级的失败测试。

```python
from demo.tests.support import DemoCase

class DeviceTests(DemoCase):
    def test_custom_gpu_keeps_sample_unverified(self):
        original = self.h.workspace()['context_id']
        reply = self.h.command('context.save', {
            'expected_context_id': original,
            'fields': {'gpu_name': '演示者填写的其他显卡'},
        })
        context = self.h.entity(self.h.ref(reply, 'ContextSnapshot'))
        self.assertEqual(context['applicability'], 'needs_confirmation')
        self.assertEqual(context['data_origin'], 'user_declared')
        self.assertIsNone(context['cpu'])
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_device demo.tests.test_information -v`。
- [ ] 字段白名单校验：GPU/CPU 文本非空，分辨率宽高必须成对正整数，未知保留 null；选择案例不填驱动/显存。模拟 scan 明确 `origin=simulated`，不能改为 system_probe。问题保存已答、跳过、原话及归一值。
- [ ] 对 open/partial 问题回答仅问剩余关键项；resolved/skipped 更正创建新问题与派生背景，旧任务/结果引用不变。`restore_preset=true` 明确恢复 fixture 背景修订并恢复模板适用范围，保留旧自定义历史。
- [ ] 目标通过；建议提交 `git add -- demo/app/device.py demo/app/actions.py demo/tests/support.py demo/tests/test_device.py demo/tests/test_information.py`，`git commit -m "feat(demo): record device context and follow-up answers"`。

### T07：资料收集、缺口、导入、更新与复用

**Files**：创建 `demo/app/knowledge.py`、`demo/tests/test_knowledge.py`、`demo/tests/test_knowledge_import.py`；修改 `actions.py`、`tasks.py`、`tests/support.py`。

**Interfaces**：登记 `knowledge.collect/import/followup/update`；collect payload 为 `source_ids`、`mode='discover'|'seeded'`、`budget={max_sources,max_stages}`。提供 `h.knowledge(*, scenario=...) -> str`，启动 collect、h.run 并返回 KnowledgeSnapshot ID。

- [ ] 写阶段成果计数一致、重复内容不算独立支持、P1 未闭合 partial、同包重复复用、跨案例包拒绝、更新不改历史的失败测试。

```python
from demo.tests.support import DemoCase

class KnowledgeTests(DemoCase):
    def test_collect_exports_inspectable_demo_sources(self):
        snapshot_id = self.h.knowledge()
        snapshot = self.h.entity(snapshot_id)
        sources = [self.h.entity(i) for i in snapshot['source_ids']]
        self.assertTrue(sources)
        self.assertTrue(all(x['url'].startswith('demo://') for x in sources))
        self.assertEqual(snapshot['accepted_count'],
                         sum(x['status'] == 'accepted' for x in snapshot['records']))
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_knowledge demo.tests.test_knowledge_import -v`。
- [ ] 六阶段分别发布来源/提取/检查/缺口，不用一个完成事件代替全部过程；公共 URL 仅保留“未读取线索”，不请求外网。导入仅 `demo-knowledge/1`，先完整校验再事务发布；指纹从规范输入字节生成。
- [ ] followup 针对既有缺口发布 fixture 修订；budget 不足保留 partial。update 标记依赖旧快照的推荐 needs_review，生成新知识版本；正式采集包或错误案例包明确拒绝。可复用时返回已有演示快照并记录复用事件。
- [ ] 目标通过；建议提交 `git add -- demo/app/knowledge.py demo/app/actions.py demo/app/tasks.py demo/tests/support.py demo/tests/test_knowledge.py demo/tests/test_knowledge_import.py`，`git commit -m "feat(demo): stage evidence collection and scoped knowledge import"`。

### T08：主推荐、替代差异、目标和复核

**Files**：创建 `demo/app/recommendation.py`、`demo/tests/test_recommendation.py`；修改 `actions.py`、`tests/support.py`。

**Interfaces**：登记 `recommendation.generate/variant/review`；generate payload 为 `context_id`、`knowledge_id`；variant 为 `recommendation_id`、`variant='quality'|'performance'`。`h.recommendation(*, scenario=...) -> str` 完成知识和推荐，返回推荐 ID。

- [ ] 写推荐不采用、不填本机 FPS、至少十二项有来源、变体不改主模板、自定义背景降级和跨案例知识拒绝的失败测试。

```python
from demo.tests.support import DemoCase

class RecommendationTests(DemoCase):
    def test_recommendation_does_not_invent_a_local_result_or_adoption(self):
        recommendation = self.h.entity(self.h.recommendation())
        self.assertGreaterEqual(len(recommendation['settings']), 12)
        self.assertIsNone(recommendation['local_summary_id'])
        self.assertIsNone(self.h.workspace()['current_applied_id'])
        self.assertTrue(all(s['evidence_ids'] for s in recommendation['settings']))
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_recommendation -v`。
- [ ] 将建议目标 `target_proposal`、样例公开参考 `reference_results`、本机结果引用 `local_summary_id` 分开；数值区间不求中点，未知项保持未知。三种方案取值从目录/模板读取，依赖组合失败不发布“可操作”。
- [ ] review 新建复核快照，旧推荐保留；variant 复制固定基准再应用显式差异，不原地修改模板或当前配置。GPU 自定义时 `readiness='provisional'` 并给原因。
- [ ] 目标通过；建议提交 `git add -- demo/app/recommendation.py demo/app/actions.py demo/tests/support.py demo/tests/test_recommendation.py`，`git commit -m "feat(demo): publish traceable recommendation variants"`。

### T09：设置目录、A0/A1、修改、回滚与历史配置

**Files**：创建 `demo/app/configuration.py`、`demo/tests/test_configuration.py`；修改 `actions.py`、`tests/support.py`。

**Interfaces**：登记 `setting.register`、`configuration.adopt/edit/rollback/record_history`。采用 payload 为 `origin`、`recommendation_id|null`、`expected_current_id|null`、`settings`、`preset|null`、`confirmation`。`h.adopt(*, scenario=...) -> str` 建推荐，保存演示原配置再采用推荐，返回 A1 ID。

- [ ] 写 A0/A1 父链、过时指针零写入、回滚追加、历史登记不推进当前、同名未知设置不自动合并和未知项保留的失败测试。

```python
from demo.tests.support import DemoCase

class ConfigurationTests(DemoCase):
    def test_rollback_adds_a_version_instead_of_erasing_the_edit(self):
        a1 = self.h.adopt()
        edited = self.h.command('configuration.edit', {
            'expected_current_id': a1,
            'changes': {'mw2.shadow_quality': 'low'},
            'confirmation': '我已手动调整',
        })
        a2 = self.h.ref(edited, 'AppliedConfiguration')
        reply = self.h.command('configuration.rollback', {
            'expected_current_id': a2, 'target_id': a1,
            'confirmation': '我已手动恢复',
        })
        a3 = self.h.entity(self.h.ref(reply, 'AppliedConfiguration'))
        self.assertEqual(a3['parent_id'], a2)
        self.assertEqual(a3['rollback_target_id'], a1)
        self.assertEqual(a3['settings'], self.h.entity(a1)['settings'])
        self.assertEqual(self.h.entity(a2)['settings']['mw2.shadow_quality'], 'low')
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_configuration -v`。
- [ ] A0 允许无推荐保存预设或至少一项声明；A1 从推荐复制已确认值，但 `activation='user_declared'`，无真实 active_confirmed。手动编辑从当前固定父版本合并，未知值和未展开预设不丢失；目录新增独立 ID，明确更正另建后继。
- [ ] rollback 逐项恢复目标已知内容并新建版本；record_history 保存过去声明与时间依据且不动 current。变更来自诊断时保存 change_set_id；执行确认所指 base 与当前不同则冲突，不用最新配置偷偷重算。
- [ ] 目标通过；建议提交 `git add -- demo/app/configuration.py demo/app/actions.py demo/tests/support.py demo/tests/test_configuration.py`，`git commit -m "feat(demo): preserve adopted configuration versions and rollback history"`。

### T10：测试计划、会话、切段与绑定修订

**Files**：创建 `demo/app/sessions.py`、`demo/tests/test_sessions.py`；修改 `actions.py`、`tests/support.py`。

**Interfaces**：登记 `plan.save`、`session.create/split/end`、`binding.revise`。会话 payload 为 `context_id`、`applied_id|null`、`plan_id|null`、`run_conditions`；返回 Session、Segment、Binding。`h.session(*, scenario=..., applied_id=None) -> dict` 返回这些 ID 和修订，不隐式分析。

- [ ] 写无配置会话、双修订冲突、切段同边界、闭合后旧绑定保留、计划版本不替换旧会话、历史补绑不动当前的失败测试。

```python
from demo.tests.support import DemoCase

class SessionTests(DemoCase):
    def test_unknown_configuration_is_allowed_but_not_confirmed(self):
        session = self.h.session(applied_id=None)
        segment = self.h.entity(session['segment_id'])
        binding = self.h.entity(segment['binding_id'])
        self.assertIsNone(binding['applied_id'])
        self.assertEqual(binding['configuration_state'], 'unverified')
        self.assertIsNone(segment['end_s'])
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_sessions -v`。
- [ ] 计划四个指标 key 与设计一致，时长/次数未填可 null，不隐式填 60秒/3次；演示默认主线由页面显式选择 60秒计划，保存说明不等于真实执行。开始会话保存固定背景/采用/计划引用及活动段。
- [ ] split/end 在一个事务核对所有活动段及 segment/binding revision；边界使用后台单调差或明确模拟区间，不用浏览器当前时间猜过去。binding.revise 追加快照和修订，不改旧摘要。提供 `require_closed(tx, segment_id, segment_revision, binding_revision)` 给 T12/T13。
- [ ] 目标通过；建议提交 `git add -- demo/app/sessions.py demo/app/actions.py demo/tests/support.py demo/tests/test_sessions.py`，`git commit -m "feat(demo): bind test sessions to immutable configuration versions"`。

### T11：确定性 trace、CSV 清洗与统计纯函数

**Files**：创建 `demo/app/traces.py`、`demo/app/metrics.py`、`demo/tests/test_traces.py`、`demo/tests/test_metrics.py`；修改 `fixtures/*/trace-recipes.json` 及 manifest（这里的 `*` 仅表示 T01 已明确的两个案例目录，执行时分别指名）。

**Interfaces**：`generate_trace(scenario_id, label, recipe) -> bytes`；`parse_trace(data: bytes, *, scenario_id: str) -> ParsedTrace`；`clean_trace(parsed, *, exclusions=None) -> CleanTrace`；`summarize(clean) -> dict`；`long_frames(clean, threshold_ms: float) -> dict`。ParsedTrace 保存 frames/限制/hash，CleanTrace 保存连续 run、有效间隔和排除清单，不能只返回扁平数组丢失断点。

- [ ] 写固定生成一致、时间累计、错误行断链、零样本未知、尾部门槛及公式失败测试。

```python
import unittest
from demo.app.metrics import summarize_intervals, long_frame_runs

class MetricTests(unittest.TestCase):
    def test_rate_uses_total_duration_and_tail_stays_unknown(self):
        result = summarize_intervals([10.0, 20.0, 30.0, 40.0])
        self.assertEqual(result['average_present_rate'], 40.0)
        self.assertEqual(result['interval_p95_ms'], 40.0)
        self.assertIsNone(result['slowest_1pct_fps'])

    def test_fifty_ms_is_not_strictly_longer_than_fifty_ms(self):
        result = long_frame_runs([[60.0, 70.0, 16.0, 50.0, 80.0]], 50.0)
        self.assertEqual(result['cluster_count'], 2)
        self.assertEqual(result['interval_count'], 3)
        self.assertEqual(result['total_ms'], 210.0)
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_traces demo.tests.test_metrics -v`；在 metrics.py 明确定义测试所用 `summarize_intervals(intervals)`、`long_frame_runs(runs, threshold_ms)` 为公开纯函数，由 summarize/long_frames 复用。
- [ ] CSV 只接受设计中的七列与 `application_present`，首事件间隔为空；scenario_id/trace_id 一致，demo_only 必须 true。timestamp 与 interval 用有限浮点且按 0.001ms 容差检查；未知格式不改名猜列。坏行断开邻接，不跨断点重连，尖峰保留。
- [ ] recipes 采用整数微秒累计及固定重复周期，无运行时随机数。两案例、五 label 独立；主线生成约六十秒且不少于 2000 个有效间隔，short 不满足尾部显示条件；调整样例减少尖峰，无改善样例保留相近尖峰。最后区间范围按实际文件计算，不硬标精确六十秒。平均/P95/P99/尾部/长帧按 DESIGN §9；质量 `limited` 为有效时长<30秒或有效间隔<1000，否则 `usable`，尾部另检查二十个样本。
- [ ] 目标通过，保存数据/公式确定性证据；建议提交 `git add -- demo/app/traces.py demo/app/metrics.py demo/tests/test_traces.py demo/tests/test_metrics.py demo/fixtures/mw2-rtx4060/trace-recipes.json demo/fixtures/fh6-rtx5090/trace-recipes.json demo/fixtures/manifest.json`，`git commit -m "feat(demo): derive charts and metrics from deterministic frame traces"`。

### T12：模拟采集、文件预览、时间关联与摘要任务

**Files**：创建 `demo/app/performance.py`、`demo/tests/test_capture.py`、`demo/tests/test_log_import.py`、`demo/tests/test_analysis.py`；修改 `actions.py`、`tasks.py`、`tests/support.py`。

**Interfaces**：登记 `capture.start/stop`、`log.inspect/associate`、`performance.analyze`。分析 payload 为 `artifact_id`、`segment_id`、`segment_revision`、`binding_revision`；采集指定 session/segment/binding 与演示 label。`h.summary(label='recommended', *, scenario=..., applied_id=None) -> str` 取显式采用或 h.adopt，建会话、生成并闭合样例段、分析，返回 Summary ID。

- [ ] 写开放段拒绝、重复 start/stop 不重复生成、未知流不分析、历史关联不改当前、文件变更 hash 拒绝、图表与原件引用一致的失败测试。

```python
from demo.tests.support import DemoCase

class AnalysisTests(DemoCase):
    def test_summary_keeps_one_source_for_metrics_and_curve(self):
        summary = self.h.entity(self.h.summary('recommended'))
        artifact = self.h.entity(summary['artifact_id'])
        self.assertEqual(summary['source_hash'], artifact['content_hash'])
        self.assertEqual(summary['chart']['source_hash'], summary['source_hash'])
        self.assertEqual(summary['data_origin'], 'simulated')
        self.assertEqual(summary['frame_stream'], 'application_present')
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_capture demo.tests.test_log_import demo.tests.test_analysis -v`。
- [ ] 模拟 capture 只创建演示任务/逻辑租约，不填 PID；默认到点或显式 stop 后生成对应样例，提前停止选短样例/裁切原件，结果不能谎称完成完整计划。模拟区段以样例时间轴闭合并注明时基，真实等待时长另存。
- [ ] log.inspect 只预览不建会话；associate 的 existing 模式核对双修订，historical 模式要求显式过去开始时间与声明依据，不能用上传时间；source_anchor/target_anchor_s 计算单位换算，范围必须在原件内。用户文件始终 `uploaded_demo_file`，不升级成真实实测。分析先 require_closed，再从 file hash→parse→clean→summary，同时产保峰曲线与文本表，不改旧摘要。
- [ ] 补齐 h.summary，目标通过；建议提交 `git add -- demo/app/performance.py demo/app/actions.py demo/app/tasks.py demo/tests/support.py demo/tests/test_capture.py demo/tests/test_log_import.py demo/tests/test_analysis.py`，`git commit -m "feat(demo): simulate captures and analyze scoped log artifacts"`。

### T13：冻结比较口径、前后差异与资格降级

**Files**：创建 `demo/app/comparison.py`、`demo/tests/test_comparison.py`；修改 `actions.py`、`tests/support.py`。

**Interfaces**：登记 `comparison.freeze/run`。freeze payload 为 `mode='fixed'|'baseline_derived'`、`threshold_ms|null`、`baseline_summary_ids`、`exploratory`；run 为 `protocol_id`、`before_summary_id`、`after_summary_id`。输出 Comparison 内各 metric 有 before/after/delta、单位、可比较原因、共同阈值与分母。

- [ ] 写冻结不重算、严格共同阈值、跨案例拒绝、事后降级/拒绝预先计划、未知绑定不显示受控和缺原件局部降级的失败测试。

```python
from demo.tests.support import DemoCase

class ComparisonTests(DemoCase):
    def test_one_frozen_threshold_is_used_for_both_sides(self):
        a1 = self.h.adopt()
        s1 = self.h.summary('recommended', applied_id=a1)
        frozen = self.h.command('comparison.freeze', {
            'mode': 'fixed', 'threshold_ms': 50.0,
            'baseline_summary_ids': [s1], 'exploratory': False,
        })
        protocol = self.h.ref(frozen, 'ComparisonProtocol')
        s2 = self.h.summary('adjusted', applied_id=a1)
        result = self.h.command('comparison.run', {
            'protocol_id': protocol,
            'before_summary_id': s1, 'after_summary_id': s2,
        })
        comparison = self.h.entity(self.h.ref(result, 'Comparison'))
        self.assertEqual(comparison['threshold_ms'], 50.0)
        self.assertEqual(comparison['before_long_frames']['threshold_ms'], 50.0)
        self.assertEqual(comparison['after_long_frames']['threshold_ms'], 50.0)
        self.assertIs(comparison['demo_only'], True)
```

本测试只验证同口径，不证明设置收益；即便两次采用 ID 相同，样例差异也只能描述，不给单项因果结论。

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_comparison -v`。
- [ ] baseline_derived 读固定原件重算 `max(50, 2×median)`，固定模式 null 用 50；不接受客户端派生结果。重复/重叠基线拒绝。数值差异未定义时 null，原值为零不算相对提升百分比。
- [ ] 依据事件序号判断模拟协议早于后测；上传无可靠先后则只准 exploratory。相同案例/背景/条件/闭合绑定是前置守卫；额外设置变动、未知配置/条件、缺原件标局限，不生成“已证实改善”。受支持的描述性比较保留非依赖原件指标，但共同阈值重算不可用时明确 null。
- [ ] 目标通过；建议提交 `git add -- demo/app/comparison.py demo/app/actions.py demo/tests/support.py demo/tests/test_comparison.py`，`git commit -m "feat(demo): freeze comparison protocols and report scoped differences"`。

### T14：反馈、保护项、单项调整与结果接受

**Files**：创建 `demo/app/feedback.py`、`demo/tests/test_feedback.py`；修改 `actions.py`、`configuration.py`、`tests/support.py`。

**Interfaces**：登记 `feedback.save/next`、`preference.save`、`target.respond`、`outcome.save`。反馈为 `session_id`、`summary_id|null`、`satisfied|null`、`symptoms`、`severity|null`、`raw_text`、`protected_setting_ids`；next 引用 feedback/current_applied/summary；Outcome 引用 change_set/applied/comparison 并保存 `accepted` 与理由，不允许客户端直接提交“改善=true”。

- [ ] 写保持、无数据补测、未知输入不足、保护项冲突、基于 A1 合并生成 A2、拒绝可恢复和连续两轮无改善停止的失败测试。

```python
from demo.tests.support import DemoCase

class FeedbackTests(DemoCase):
    def test_stutter_adjustment_preserves_texture_and_does_not_adopt_itself(self):
        a1 = self.h.adopt()
        summary = self.h.entity(self.h.summary('recommended', applied_id=a1))
        saved = self.h.command('feedback.save', {
            'session_id': summary['session_id'], 'summary_id': summary['id'],
            'satisfied': False, 'symptoms': ['stutter'], 'severity': 'occasional',
            'raw_text': '交战时偶尔卡一下，希望保持画面清晰',
            'protected_setting_ids': ['mw2.texture_detail'],
        })
        next_step = self.h.command('feedback.next', {
            'feedback_id': self.h.ref(saved, 'Feedback'),
            'applied_id': a1, 'summary_id': summary['id'],
        })
        changes = self.h.entity(self.h.ref(next_step, 'ChangeSet'))
        self.assertEqual(changes['base_applied_id'], a1)
        self.assertEqual(set(changes['changes']), {'mw2.shadow_quality'})
        self.assertEqual(self.h.workspace()['current_applied_id'], a1)
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_feedback -v`。
- [ ] 规则顺序按设计 §10：满意先保持；无基线/摘要补测；关键未知补查；合法单项且不冲保护项才调整；否则不足。MW2 对应阴影、FH6 对应反射，不能互换；自由文本不理解就保存未知。偏好/目标响应追加版本，推断不冒充明确声明。
- [ ] ChangeSet 包含原值/新值、base、假设、证据、测试/恢复说明；配置采用显式校验 change_set 的 base 与实际合并值。Outcome 从固定比较读取数据变化，接受情况独立；连续两轮无改善触发 stop，不循环提出同一失败改动。
- [ ] 目标通过；建议提交 `git add -- demo/app/feedback.py demo/app/configuration.py demo/app/actions.py demo/tests/support.py demo/tests/test_feedback.py`，`git commit -m "feat(demo): guide feedback adjustments and keep acceptance separate"`。

### T15：一键完整案例、报告、备份恢复与失败剧本

**Files**：创建 `demo/app/seeding.py`、`demo/app/exports.py`、`demo/tests/test_seed.py`、`demo/tests/test_exports.py`、`demo/tests/test_backup.py`；修改 `controls.py`、`actions.py`、`tests/support.py`。

**Interfaces**：登记 `demo.seed`、`export.create`、`backup.create/restore`。seed payload 为 `position='recommendation'|'comparison'` 与 `expected_epoch`；导出选择固定实体 ID，格式 markdown/json；备份恢复用上传后的 file_id。`h.story(*, scenario=..., accepted=True) -> dict` 逐条执行真实演示动作、完成 A0→A1→S1→P1→C1→A2→S2→比较→Outcome，不调用 seed，以免最终测试被预填数据自证。

- [ ] 写完整 seed 引用合法、重复加载不叠加、导出标识/数值一致、备份 hash/路径/案例校验、恢复新 epoch 与活动任务不复活的失败测试。

```python
from demo.tests.support import DemoCase

class SeedTests(DemoCase):
    def test_complete_case_is_scoped_and_contains_real_artifact_references(self):
        untouched = self.h.workspace('fh6-rtx5090')
        reply = self.h.command('demo.seed', {
            'position': 'comparison',
            'expected_epoch': self.h.workspace()['workspace_epoch'],
        })
        comparison = self.h.entity(self.h.ref(reply, 'Comparison'))
        before = self.h.entity(comparison['before_summary_id'])
        self.assertTrue(self.h.entity(before['artifact_id'])['content_hash'])
        self.assertEqual(self.h.workspace('fh6-rtx5090'), untouched)
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_seed demo.tests.test_exports demo.tests.test_backup -v`。
- [ ] seed 构建器生成关系完整、逻辑顺序正确的对象图，统一 `origin=demo_seed`；文件先就绪，单事务发布新 epoch/指针/全部事实，发布失败保留原工作区。recommendation 位置不应假造已采用；comparison 位置拥有原件和完整采用链。种子加载和常规旅程共享纯构造/校验函数，不能绕过引用守卫。
- [ ] JSON/Markdown 报告复用摘要/比较值，分享不含绝对路径。备份 `demo-backup/1` 只接收演示格式，base64 文件逐个 hash 校验，最大 5MiB；超限明确 413，支持按当前案例备份而不自动裁掉引用。恢复保留业务历史和原件，重新定位到新 epoch，mutation 键/活动执行不恢复。失效原件、丢响应、保存失败等脚本仅作用当前 epoch。
- [ ] 补齐 h.story；目标通过；建议提交 `git add -- demo/app/seeding.py demo/app/exports.py demo/app/controls.py demo/app/actions.py demo/tests/support.py demo/tests/test_seed.py demo/tests/test_exports.py demo/tests/test_backup.py`，`git commit -m "feat(demo): load complete stories and export isolated demo reports"`。

### T16：前端工作台、请求草稿与双案例状态

**Files**：创建 `demo/static/js/api.js`、`drafts.js`、`state.js`、`components.js`、`pages/overview.js`；修改 `app.js`、`index.html`、`styles.css`；创建 `demo/tests/test_web_flow.py` 记录服务侧重复提交行为。

**Interfaces**：`createApi(base='/api/demo/v1')` 提供 get/command/upload；`openDraftStore()` 提供 prepare/get/mark/list；`acceptSnapshot(current, incoming) -> bool`；页面导出 `render(root, snapshot, actions)`。state 使用每案例缓存和当前路由，不把“当前案例”当服务全局变量。

- [ ] 先在实际浏览器验证空白骨架缺少案例切换/状态恢复，再添加以下可在浏览器控制台执行的版本守卫检查，并写 HTTP 同请求两次只产生一次结果的 unittest。

```javascript
const { acceptSnapshot } = await import('/js/state.js');
console.assert(acceptSnapshot(
  { scenario_id: 'mw2-rtx4060', workspace_epoch: 2, revision: 5 },
  { scenario_id: 'mw2-rtx4060', workspace_epoch: 1, revision: 99 }
) === false, '旧 epoch 不得覆盖新页面');
console.assert(acceptSnapshot(
  { scenario_id: 'mw2-rtx4060', workspace_epoch: 2, revision: 5 },
  { scenario_id: 'mw2-rtx4060', workspace_epoch: 2, revision: 6 }
) === true, '同工作区较新快照可接受');
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_web_flow -v`；浏览器检查用 `py -3.13 -B demo/server.py --port 8765` 启动后真实操作，截图与结果在 T20 汇总。当前环境无浏览器工具时记录 not_run，不能用静态资源 200 代替页面验收。
- [ ] 实现左导航六页、顶部案例/GPU 摘要、常驻演示标识、浅色青绿样式、本地字体。活动页任务约一秒轮询，隐藏页降低频率，终态停止；切页取消旧订阅，旧 epoch/较旧 revision 响应不得覆盖。
- [ ] IndexedDB 先持久化 request_id/body/epoch/revision 再发送；发送中禁用重复按钮但保留取消；unknown 原键重试；409 保留草稿并要求核对。IndexedDB 不可写时阻止变更发送并显示可复制输入，不能无键发送。overview 实现两案例选择、背景编辑、模拟识别、补充面板。
- [ ] 手动验证刷新恢复、切换两案例、网络响应延后到达、写入失败时按钮状态；目标通过后建议提交 `git add -- demo/static demo/tests/test_web_flow.py`，`git commit -m "feat(demo): add an offline workspace shell with durable command drafts"`。

### T17：知识与推荐界面、基线采用和配置差异

**Files**：创建 `demo/static/js/pages/knowledge.js`、`recommendation.js`、`demo/static/js/forms.js`；修改 `components.js`、`app.js`、`styles.css`；修改 `demo/tests/test_web_flow.py` 补充页面对应 API 旅程。

**Interfaces**：knowledge/recommendation 各导出 render；`buildAdoptionPayload({recommendation, expectedCurrentId, settings, confirmation}) -> object` 不补生效声明、不取最新父版本；来源详情由实体 ID 查询，模板 ID 不作为真实 URL 执行。

- [ ] 先按浏览器执行“分析—展开来源—查看方案—保存 A0—确认 A1”，记录当前缺少页面/动作的断点；增加如下 payload 检查和服务侧主线回归。

```javascript
const { buildAdoptionPayload } = await import('/js/forms.js');
const payload = buildAdoptionPayload({
  recommendation: { id: 'demo_rec_example' },
  expectedCurrentId: 'demo_applied_a0',
  settings: { 'mw2.shadow_quality': 'high' },
  confirmation: '我已手动设置'
});
console.assert(payload.expected_current_id === 'demo_applied_a0');
console.assert(payload.confirmation === '我已手动设置');
console.assert(!('active_confirmed' in payload), '玩家声明不能自动升级为观测');
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_web_flow demo.tests.test_configuration -v`，确认新增旅程断言先失败，再连接页面行为。
- [ ] 资料页用真实任务阶段/记录计数渲染，取消/部分结果/补查/导入/需复核可操作；来源抽屉显示本地片段、表格、模拟性质、重复关系。推荐页主方案至少十二行，可切替代、展开依赖/来源，目标/公开参考/本机待测分区。
- [ ] 采用对话框区分 A0 基线与 A1 推荐采用；支持预设/逐项确认、添加设置、编辑、冲突差异和回滚确认。表单值由固定版本产生，409 不直接替换成后台当前值重新发送。主屏提供“下一步：测试这套方案”，不假造已测试 FPS。
- [ ] 浏览器核对键盘焦点、空/加载/错误状态及两套十二项独立内容；建议提交 `git add -- demo/static/js/pages/knowledge.js demo/static/js/pages/recommendation.js demo/static/js/forms.js demo/static/js/components.js demo/static/js/app.js demo/static/styles.css demo/tests/test_web_flow.py`，`git commit -m "feat(demo): expose evidence recommendations and manual adoption"`。

### T18：测试、采集、上传、曲线与前后比较界面

**Files**：创建 `demo/static/js/pages/performance.js`、`demo/static/js/charts.js`；修改 `forms.js`、`components.js`、`app.js`、`styles.css`；修改 `test_web_flow.py`。

**Interfaces**：`renderFrameTimeChart(element, chart)` 只使用后台提供的曲线与 source_hash；所有图根元素具有 `data-source-hash` 与可访问说明，结果区具有 `data-summary-id`。设置表单提交 plan/session/capture/log/comparison 的原始声明，不生成伪造观测字段。

- [ ] 先在真实浏览器完成“保存计划—开始采集—停止—查看摘要—冻结口径—复测—对比”，记录缺少交互断点；结果页存在后执行以下来源一致性检查。

```javascript
const panel = document.querySelector('[data-summary-id]');
const scenario = document.querySelector('[data-scenario-id]').dataset.scenarioId;
const epoch = document.querySelector('[data-workspace-epoch]').dataset.workspaceEpoch;
const url = `/api/demo/v1/entities/${panel.dataset.summaryId}` +
  `?scenario_id=${encodeURIComponent(scenario)}&workspace_epoch=${epoch}`;
const summary = await (await fetch(url)).json();
console.assert(panel.querySelector('[data-source-hash]').dataset.sourceHash
  === summary.source_hash, '曲线必须来自当前摘要原件');
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_web_flow demo.tests.test_analysis demo.tests.test_comparison -v`，浏览器断言按实体 GET 直接返回对象的接口约定执行。
- [ ] 页面显示固定采用版本、计划、活动段与配置未知；模拟等待时长和样例数据时长分开展示。保峰曲线标 ms、来源、长帧和排除区间，指标未知显示原因。上传先预览，再选已有段/过去会话、确认锚点与时间依据，再分析；格式错误不加载默认成功结果。
- [ ] 比较口径在第二次采样前提示保存，返回冻结阈值由后台展示；事后只能探索性。前后引用/分母/共同阈值清楚，公开参考不混入 A/B 曲线。导出 CSV 与曲线绑定同一 file ID/hash。
- [ ] 两案例分别验证模拟中断、提前停止、坏 CSV、开放段、样本不足与缺原件；建议提交 `git add -- demo/static/js/pages/performance.js demo/static/js/charts.js demo/static/js/forms.js demo/static/js/components.js demo/static/js/app.js demo/static/styles.css demo/tests/test_web_flow.py`，`git commit -m "feat(demo): visualize sample captures and comparable performance results"`。

### T19：反馈、历史、报告和演示控制界面

**Files**：创建 `demo/static/js/pages/feedback.js`、`history.js`、`demo/static/js/controls.js`；修改 `forms.js`、`components.js`、`app.js`、`styles.css`；修改 `test_web_flow.py`。

**Interfaces**：各页面 render；控制台调用 demo.mode/script/seed/reset，无前端自行伪造实体。历史引用固定 ID，浏览历史不推进 current。备份恢复必须上传取得 file ID，不能把任意文件路径提交服务。

- [ ] 先执行“反馈卡顿—保护纹理—查看 C1—确认 A2—复测—接受/拒绝—回滚—导出”，增加如下服务端真实旅程测试，不用 seed 代替实际用户操作。

```python
from demo.tests.support import DemoCase

class WebStoryTests(DemoCase):
    def test_rejecting_the_visual_result_keeps_the_comparison_and_adds_rollback(self):
        story = self.h.story(accepted=False)
        outcome = self.h.entity(story['outcome_id'])
        current = self.h.entity(self.h.workspace()['current_applied_id'])
        self.assertIs(outcome['accepted'], False)
        self.assertEqual(current['rollback_target_id'], story['a1_id'])
        self.assertEqual(current['parent_id'], story['a2_id'])
        self.assertTrue(self.h.entity(story['comparison_id'])['demo_only'])
```

- [ ] 执行 `py -3.13 -B -m unittest demo.tests.test_web_flow demo.tests.test_feedback demo.tests.test_exports -v`。
- [ ] 反馈页保存原话/症状/严重程度/保护项，清晰区分假设、证据、未知；C1 展示原值→新值和恢复方式，确认后再进入复测。结果接受与数值变化独立；无改善分支提供保持/补测/停止，不只一个“继续优化”。
- [ ] 历史页显示 A0/A1/A2/A3、固定摘要/比较/来源链，支持补录过去配置与绑定；报告/备份/恢复、原件不可用有明确反馈。控制台提供两位置完整案例、三速度、失败剧本、当前/全部重置；重置/恢复/加载在有状态时先显示影响范围。
- [ ] 浏览器验证重置时仍有任务/旧标签页、完整案例来回切换、导出标识及键盘确认；建议提交 `git add -- demo/static/js/pages/feedback.js demo/static/js/pages/history.js demo/static/js/controls.js demo/static/js/forms.js demo/static/js/components.js demo/static/js/app.js demo/static/styles.css demo/tests/test_web_flow.py`，`git commit -m "feat(demo): complete feedback history and presentation controls"`。

### T20：双案例端到端、浏览器验收与交付说明

**Files**：创建 `demo/tests/test_end_to_end.py`、`demo/tests/test_isolation.py`、`demo/DEMO_SCRIPT.md`、`demo/VALIDATION.md`；修改 `demo/README.md`，必要的缺陷修复仅限上述既有 `demo/` 文件。

**Interfaces**：复用 h.story 跑实际动作；`story` 返回 `a0_id/a1_id/a2_id/s1_id/s2_id/protocol_id/change_set_id/comparison_id/outcome_id`；拒绝时另有 rollback_id。h.story 不调用 demo.seed，seed 单独验收。

- [ ] 写两案例全旅程、互相引用拒绝、后台断网/不碰正式文件、刷新/重启、重置竞态和版本幂等的最终失败测试。

```python
from demo.tests.support import DemoCase

class EndToEndTests(DemoCase):
    def test_both_stories_complete_without_sharing_configuration_ids(self):
        a = self.h.story(scenario='mw2-rtx4060', accepted=True)
        b = self.h.story(scenario='fh6-rtx5090', accepted=True)
        self.assertNotEqual(a['a1_id'], b['a1_id'])
        for scenario, story in [('mw2-rtx4060', a), ('fh6-rtx5090', b)]:
            outcome = self.h.entity(story['outcome_id'], scenario=scenario)
            self.assertEqual(outcome['scenario_id'], scenario)
            self.assertIs(outcome['accepted'], True)
```

- [ ] 执行全量演示命令 `py -3.13 -B -m unittest discover -s demo/tests -t . -v`；只有实际输出退出码 0 才填写 passed。测试总数按实际记录，不预设数字；正式 pytest 数字不写入演示验收。
- [ ] 真实浏览器依次验证 1366×768、1920×1080、窄屏和键盘，检查无外网请求、无缺失静态文件、无未处理控制台错误；六页各有截图/记录。手工走完 MW2 主线，再走 FH6；另做 seed、坏 CSV、关键字段未知、无改善、过时指针、提交后丢响应、重启与重置旧页验证。通过标志来自具体观察，不用代码阅读代替。
- [ ] 编写 DEMO_SCRIPT：五分钟 MW2 主线逐步点击、每步讲什么/应看到什么、真实游戏菜单手动切换点、快速加载救场与 reset；附 FH6 的独立设置/反馈差异。VALIDATION 每项记录命令/退出码、浏览器条件、结果/截图、真实发生和模拟发生的能力表、剩余限制；未运行列 not_run 而非通过。
- [ ] README 改为实际启动说明：根目录命令、双击入口、改端口、停止、两案例、一键加载、状态目录、备份/重置、已验证环境。检查 Git 仅包含 demo 改动，所有声明保留；建议提交 `git add -- demo/tests/test_end_to_end.py demo/tests/test_isolation.py demo/DEMO_SCRIPT.md demo/VALIDATION.md demo/README.md`，`git commit -m "test(demo): verify and document both offline presentation stories"`。若有缺陷修复文件，逐项指名追加暂存，不夹带正式改动。

## 5. 功能覆盖与任务追踪

| 设计功能 | 实现任务 | 验证任务/设计验收 |
|---|---|---|
| D01 游戏/安装背景 | T01/T06/T16 | T20；V02/V19 |
| D02 设备/补充/更正 | T06/T16 | T06/T20；V04 |
| D03 来源发现/指定来源 | T07/T17 | T07/T20；V05/V07 |
| D04 提取/校验/去重/补查 | T07/T17 | T07/T20；V05/V06 |
| D05 预算/取消/恢复 | T05/T07/T17 | T05/T20；V05/V16/V17 |
| D06 知识导入/复用 | T07/T17 | T07/T20；V06 |
| D07 主推荐/替代/复核 | T08/T17 | T08/T20；V07 |
| D08 目标/参考/本机结果区分 | T08/T12/T17/T18 | T08/T20；V07/V12 |
| D09 基线/目录/采用 | T09/T17 | T09/T20；V08 |
| D10 历史/回滚/过去补录 | T09/T10/T19 | T09/T20；V08/V09 |
| D11 会话/切段/绑定 | T10/T12/T18 | T10/T20；V09/V10 |
| D12 计划/比较协议 | T10/T13/T18 | T13/T20；V13 |
| D13 模拟采集/中断 | T05/T12/T18 | T12/T20；V10 |
| D14 预览/上传/关联 | T03/T04/T12/T18 | T12/T20；V11 |
| D15 统计/曲线/质量 | T11/T12/T18 | T11/T20；V12 |
| D16 前后比较/资格 | T13/T18 | T13/T20；V03/V13 |
| D17 反馈/偏好/目标接受 | T14/T19 | T14/T20；V14/V15 |
| D18 诊断/变更/补测 | T14/T19 | T14/T20；V14 |
| D19 结果/停止/拒绝恢复 | T14/T19 | T14/T20；V15 |
| D20 历史/导出/备份 | T15/T19 | T15/T20；V18 |
| D21 异常/重试/恢复 | T02/T03/T05/T15/T16 | T20；V04/V05/V11/V16/V17 |
| 离线/展示标识/界面/正式隔离 | T01/T04/T16—T20 | T20；V01/V20/V21 |

每个 V01—V21 在 VALIDATION.md 保留一行，双案例适用项分别登记 A/B 的观察结果。未完成高级操作不可通过隐藏入口、灰掉按钮或删除验收行消除。

## 6. 每任务记录、停止条件与最终完成标准

执行者在本文相应复选框打勾前，实际记录任务编号、变更文件、目标命令/退出码、关键行为、未执行项及提交（若有）；可将简短记录追加到 `demo/VALIDATION.md` 的开发记录区。未完成任务保持空框，不把“代码写完”当“验证通过”。

遇到以下情况，保留当前成果并记录具体受阻项，继续不依赖它的文档或验证工作：

- Python/浏览器工具不可用：明确缺失内容，不自动联网安装，不伪造启动或界面验证。
- 需要真实游戏设置、硬件数据、真实 FPS 或外部来源：当前计划只支持标识清楚的样例；不得悄悄启用真实能力补洞。
- 固定模板与编辑后的背景不匹配：显示条件/待确认，不用改标签复制同一成功结果。
- 设计与实现所需字段冲突：先在 `demo/` 内修订设计/计划，再落地；不修改正式契约。

最终完成需同时满足：

- [ ] T01—T20 对应成果与行为验证完成，两案例分别走通。
- [ ] 全量演示自动测试退出码 0，浏览器验收有实际证据；not_run 清楚披露且不冒充通过。
- [ ] 4060 主线完整呈现 A0→A1→S1→P1→C1→A2→S2→比较→接受/恢复，5090 案例使用独立内容。
- [ ] 断网可用，刷新/重启/双击/重置/备份恢复可演示，原件、曲线与导出一致。
- [ ] 六页及高级操作无假按钮，样例/声明/模拟数据边界清楚。
- [ ] `demo/` 内声明保留，正式源码/测试/依赖/研究样本/真实运行产物未被本次工作修改或消费。
- [ ] 启动命令、讲解脚本、验证记录和剩余限制随目录交付。

## 7. 实施交接方式

当前状态：只有设计和规划，尚未创建本计划中的应用源码、fixture、测试或启动服务。本文二十个任务及其命令均未在本轮执行。

用户要求开始后，默认在当前会话使用 executing-plans 按任务顺序实施并报告里程碑。只有用户选择委派或适用指令明确要求时才采用 subagent-driven-development；本规划不自动启动子代理、不自动推送。

下一步实际编码起点为 T01。执行者先阅读本目录 AGENTS、DESIGN 与本计划，再记录 Git 状态；不得从搭页面开始跳过样例、状态与原件一致性要求。

