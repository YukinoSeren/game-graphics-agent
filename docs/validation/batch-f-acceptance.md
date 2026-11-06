# 批次 F 验收包（VAL-T02 矩阵实测结果）

> 第 1—5 节由矩阵运行记录（`.jsonl`）直接生成，不是手工抄写；
> 第 6—9 节是按同一批结果整理的说明，不含新的运行数据。生成命令：
>
> ```
> py -3.13 -B -m pytest "tests/product/test_contracts.py::test_the_matrix_runs_existing_modules_and_records_every_result" -q -p no:cacheprovider --basetemp=.tmp-report
> ```

## 1. 运行清单（RunManifest）

| 字段 | 值 |
|---|---|
| `run_id` | run-contracts |
| `started_at` | 2026-11-06T03:09:35.130656+00:00 |
| `git_revision` | d9f6efbfb8dd48cc657507b4f257610931238832 |
| `python_version` | 3.13.9 |
| `contract_versions` | product-contract/0.4 |
| `dependency_versions` | pydantic=2.13.4, pytest=8.3.3 |
| `model_requested` | null |
| `model_returned` | null |
| `prompt_version` | null |
| `environment_note` | offline fixtures and temporary databases; no HTTP, no network, no real game, capture tool or model |

证据等级：`offline_fixture`（`local_effect` 为 `not_run`）。`model_requested` / `model_returned` / `prompt_version` 均为 `null`，本轮没有调用任何模型。

## 2. 结果计数

| status | 数量 | 含义 |
|---|---|---|
| passed | 60 | 离线执行并通过 |
| not_run | 1 | 未执行（附原因） |
| blocked | 7 | 缺证据或环境（附 `blocked_by`） |
| failed | 0 | 执行后失败 |
| **合计** | **68** | |

### 按 owner 分列

| owner | passed | blocked | not_run | 合计 |
|---|---|---|---|---|
| PERF | 8 | 1 | 0 | 9 |
| IMP（F-1 研究映射） | 2 | 0 | 0 | 2 |
| UX | 9 | 1 | 0 | 10 |
| KNW | 6 | 0 | 0 | 6 |
| B 基础契约 | 3 | 0 | 0 | 3 |
| FBK | 6 | 0 | 0 | 6 |
| REC | 5 | 0 | 0 | 5 |
| DEV | 8 | 0 | 0 | 8 |
| IMP | 4 | 0 | 0 | 4 |
| ENG | 7 | 0 | 0 | 7 |
| VAL | 2 | 5 | 1 | 8 |
| **合计** | **60** | **7** | **1** | **68** |

（IMP 的条目含 F-1 的两条研究映射 case；UX 含一条 CLI 入口 case。）

**通过率口径**：`passed / (passed + failed)`。`not_run` 与 `blocked` 不计入分母，也不被当作通过；清单见第 4 节。

## 3. 逐 case 结果

### PERF

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `PERF-V09` | D-P03, PERF-V09 | passed | — | offline_fixture |
| `PERF-V01` | D03, PERF-V01 | passed | — | offline_fixture |
| `PERF-V02` | D05, PERF-V02 | passed | — | offline_fixture |
| `PERF-V03` | D05, PERF-V03 | passed | — | offline_fixture |
| `PERF-V04` | D05, PERF-V04 | passed | — | offline_fixture |
| `PERF-V05` | D03, PERF-V05 | passed | — | offline_fixture |
| `PERF-V07` | D02, D05, PERF-V07 | passed | — | offline_fixture |
| `PERF-V08` | D03, PERF-V08 | passed | — | offline_fixture |
| `PERF-V06` | D02, PERF-V06 | blocked | G-FORMAT (no real sample) | offline_fixture |

### IMP（F-1 研究映射）

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `IMP-V04-research-retained` | C08, IMP-V04 | passed | — | offline_fixture |
| `IMP-V04-research-mapping` | C08, IMP-V04 | passed | — | offline_fixture |

### UX

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `UX-V01-cli-entry` | C02, C03, UX-V01 | passed | — | offline_fixture |
| `UX-V01` | C02, C03, UX-V01 | passed | — | offline_fixture |
| `UX-V03` | C04, C06, UX-V03 | passed | — | offline_fixture |
| `UX-V02` | C11, D04, UX-V02 | passed | — | offline_fixture |
| `UX-V04` | C07, D03, UX-V04 | passed | — | offline_fixture |
| `UX-V05` | D01, D05, UX-V05 | passed | — | offline_fixture |
| `UX-V06` | D02, UX-V06 | passed | — | offline_fixture |
| `UX-V07` | D03, UX-V07 | passed | — | offline_fixture |
| `UX-V08` | D02, D03, UX-V08 | passed | — | offline_fixture |
| `UX-V09` | D-P01, D-P04, UX-V09 | blocked | Q1 (CLI only) | offline_fixture |

### KNW

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `KNW-V06` | D-P01, C02, KNW-V06 | passed | — | offline_fixture |
| `KNW-V01` | C01, C02, KNW-V01 | passed | — | offline_fixture |
| `KNW-V02` | C03, KNW-V02 | passed | — | offline_fixture |
| `KNW-V03` | IMP-V02, KNW-V03 | passed | — | offline_fixture |
| `KNW-V04` | C08, KNW-V04 | passed | — | offline_fixture |
| `KNW-V05` | C09, KNW-V05 | passed | — | offline_fixture |

### B 基础契约

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `B-ERR-12` | B-ERR-12 | passed | — | offline_fixture |
| `B-DAT-14` | B-DAT-14 | passed | — | offline_fixture |
| `B-STA-13` | B-STA-13 | passed | — | offline_fixture |

### FBK

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `FBK-V01` | C11, FBK-V01 | passed | — | offline_fixture |
| `FBK-V02` | D04, FBK-V02 | passed | — | offline_fixture |
| `FBK-V03` | D01, FBK-V03 | passed | — | offline_fixture |
| `FBK-V04` | D01, FBK-V04 | passed | — | offline_fixture |
| `FBK-V05` | D05, FBK-V05 | passed | — | offline_fixture |
| `FBK-V06` | FBK-V06 | passed | — | offline_fixture |

### REC

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `REC-V01` | C01, C02, REC-V01 | passed | — | offline_fixture |
| `REC-V02` | C05, REC-V02 | passed | — | offline_fixture |
| `REC-V03` | C04, REC-V03 | passed | — | offline_fixture |
| `REC-V04` | C06, REC-V04 | passed | — | offline_fixture |
| `REC-V05` | D02, REC-V05 | passed | — | offline_fixture |

### DEV

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `DEV-V01` | C11, DEV-V01 | passed | — | offline_fixture |
| `DEV-V02` | C01, C05, DEV-V02 | passed | — | offline_fixture |
| `DEV-V03` | C07, D03, DEV-V03 | passed | — | offline_fixture |
| `DEV-V04` | D01, DEV-V04 | passed | — | offline_fixture |
| `DEV-V05` | D03, DEV-V05 | passed | — | offline_fixture |
| `DEV-V06` | D-P01, DEV-V06 | passed | — | offline_fixture |
| `DEV-V07` | D-P02, DEV-V07 | passed | — | offline_fixture |
| `DEV-V08` | D-P04, DEV-V08 | passed | — | offline_fixture |

### IMP

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `IMP-V01` | IMP-V01 | passed | — | offline_fixture |
| `IMP-V02` | IMP-V02 | passed | — | offline_fixture |
| `IMP-V03` | D02, IMP-V03 | passed | — | offline_fixture |
| `IMP-V05` | IMP-V05 | passed | — | offline_fixture |

### ENG

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `ENG-V01` | D02, ENG-V01 | passed | — | offline_fixture |
| `ENG-V02` | D04, ENG-V02 | passed | — | offline_fixture |
| `ENG-V03` | D01, ENG-V03 | passed | — | offline_fixture |
| `ENG-V04` | A05, ENG-V04 | passed | — | offline_fixture |
| `ENG-V05` | A06, ENG-V05 | passed | — | offline_fixture |
| `ENG-V06` | C09, ENG-V06 | passed | — | offline_fixture |
| `ENG-V07` | D-P01, ENG-V07 | passed | — | offline_fixture |

### VAL

| case_id | 需求 | status | blocked_by | 证据 |
|---|---|---|---|---|
| `VAL-V03` | D02, VAL-V03 | passed | — | offline_fixture |
| `VAL-V07` | D02, D05, VAL-V07 | passed | — | offline_fixture |
| `VAL-V04` | C04, C08, VAL-V04 | blocked | no independent annotation | offline_fixture |
| `VAL-V05` | C06, D05, VAL-V05 | blocked | G-EFFECT (no real game) | offline_fixture |
| `VAL-V06` | C02, C11, D02, VAL-V06 | blocked | Q1 (CLI only) | offline_fixture |
| `VAL-V08` | D-P01, D-P04, VAL-V08 | blocked | Q1 (CLI only) | offline_fixture |
| `VAL-V01` | C10, VAL-V01 | not_run | — | offline_fixture |
| `VAL-V02-full` | VAL-V02 | blocked | G-EFFECT (no real game); G-FORMAT (no real sample); Q1 (CLI only); no independent annotation | offline_fixture |

## 4. not_run / blocked 逐条原因

| case_id | status | blocked_by | 原因 |
|---|---|---|---|
| `PERF-V06` | blocked | G-FORMAT (no real sample) | no real capture sample exists in this environment: no PresentMon CSV, version or options were available, so the fixed-version format registration cannot be verified |
| `VAL-V04` | blocked | no independent annotation | no independent annotation exists; only a same-session model recheck is available (F-R3) |
| `VAL-V05` | blocked | G-EFFECT (no real game) | needs a real game and player rounds; the evidence level here is offline_fixture (F-R12) |
| `VAL-V06` | blocked | Q1 (CLI only) | the C5 page journey needs a browser and an HTTP surface; Q1 fixed the product as CLI-only |
| `UX-V09` | blocked | Q1 (CLI only) | the empty-library journey is a page flow; Q1 fixed the product as CLI-only |
| `VAL-V08` | blocked | Q1 (CLI only) | the D-P journey needs a browser; Q1 fixed the product as CLI-only |
| `VAL-V01` | not_run | — | VAL-V01 checks a design-only change set; this batch changed product code, so that boundary check does not apply to it (the original-bytes clause is asserted by the module tests) |
| `VAL-V02-full` | blocked | G-EFFECT (no real game); G-FORMAT (no real sample); Q1 (CLI only); no independent annotation | 66 of 66 required case families are registered and every one that can run offline did run; full closure still waits on: G-EFFECT (no real game); G-FORMAT (no real sample); Q1 (CLI only); no independent annotation |

## 5. 调用与对象计数口径

矩阵内**没有任何真实 provider**：每个 case 的 `mock_components` 写明它用的桩或模块测试，`observations` 记录 `external_calls=0` 与 `provider_sends=0`。逐 case 的 `observations` 保存在运行记录（`.jsonl`）中，本文件不复抄。

## 6. 证据等级分层

| 等级 | 本批次状态 | 说明 |
|---|---|---|
| `static_design` | 已使用 | 规格与设计文档 |
| `offline_fixture` | **本矩阵全部 case** | 临时库、离线夹具、桩 provider |
| `integration` | 部分 | G-WIN 真实只读探测、CLI 分发包检查、CLI 端到端旅程 |
| `local_effect` | **未取得** | 无真实游戏与玩家重复测量 |

不同等级分别报告，互不替代：离线夹具通过不代表真实兼容，也不代表本机效果改善。

## 7. 批次切片交付状态

| 切片 | 任务 | 状态 | 关键产出 |
|---|---|---|---|
| F-1 | IMP-T04 研究格式映射 | 完成 | 逐字段映射表、独立标注门禁、`ResearchImporter`、`load_research_annotations` |
| F-2 | VAL-T02 矩阵与故障 | 完成 | 63 个模块 V + 3 个 B-* 全登记；08 §7 六类故障边界各有注入 |
| F-3 | ENG-T05 CLI 分发 | 完成 | 包数据、入口、非仓库目录运行、单写者锁 |
| F-4 | VAL-T03 证据脚手架 | 完成（全部 `blocked`） | 格式/标注 manifest 与分母结构 |
| 追加 | 单写者锁（07 §1） | 完成 | `game_app/lock.py`，跨进程验证 |
| 追加 | G-WIN 真实只读探测 | 部分 | 见 `report-gwin-2026-11-04.md` |
| 追加 | G-FORMAT 采样入口 | 完成（无真实样本） | `inspect_sample` + 模拟样本解析路径 |
| 追加 | **CLI 入口补齐（Q1）** | 完成 | `Application` 入口可达数 19/46 → 49/56；33 个顶层命令；两条端到端旅程 |

### CLI 入口补齐

Q1 决定产品只有 CLI，但补齐前 CLI 只暴露 46 个 `Application` 入口中的 19 个，且游戏目录、
游戏选择、安装登记、本地设置这些入口连 `Application` 包装都没有——玩家无法从命令行开始任何旅程。
补齐后可达 49/56；仍不可达的 3 个是 `capture` / `stop_capture` / `get_capture_lease`，
它们需要一个已验证的采集适配器（G-GAME/G-FORMAT），本批次不启用。

本轮补齐后：

- 新增命令覆盖：`game`、`installation`、`local-setting`、`device scan`、`file upload`、
  `knowledge collect`、`import run/declare-identity`、`package build`、`session create/end-segment/split/end/bind`、
  `adopt`、`historical-configuration`、`answer`、`amendment`、`log inspect/associate`、`analyze`、`compare`、
  `test-protocol create`、`comparison-protocol create/get`、`metric list`、`artifact get`、`feedback get`。
- 请求文档先按契约模型做 **JSON 模式**校验，非法输入是稳定的 `INPUT_VALIDATION_FAILED`，不是 traceback。
- 修掉两处耦合缺陷：
  1. 安装登记/本地设置原先挂在 `DeviceService` 上，没有探测适配器就完全不可用——它们本不探测，
     现在归 `ContextService`，只有 `device scan` 需要 `--probe`。
  2. `PackageBuildResult` 等 dataclass 结果无法打印，会让 `package build` 落成 `INTERNAL_ERROR`。
- 新增两条端到端旅程（`tests/integration/test_cli_journey.py`）：
  空库 → 选择 → 安装 → 扫描 → 上传 → 声明身份 → 导入 → **在身份未核验处按设计拒绝**；
  已验证游戏 → 导入 → 组包 → 会话 → 采用 → 结束段 → 日志预览/关联 → 分析。

## 8. 外部证据缺口（未取得）

这些不是代码缺陷：相应能力保持 `blocked` / `not_run`，未声称通过。

| 门禁 | 缺什么 | 当前影响 |
|---|---|---|
| G-FORMAT | 真实 PresentMon 原 CSV、固定版本、启动参数 | 不注册任何真实工具格式；`format.real_tool_support` 保持 `not_run`；`PERF-V06` blocked |
| 独立标注 | 与本会话模型无关的标注者（协议见 `tests/fixtures/product/research/annotations/README.md`） | 四份研究原件全部 `retained_only`；`VAL-V04` blocked |
| G-GAME | FH6 构建号、配置映射、进程与启动身份 | 自动读取配置/绑定采集能力不启用 |
| G-MODEL | 固定请求参数、原始返回 envelope、真实 model 名、用量 | 不声称模型兼容或真实提取质量 |
| G-IMAGE | 图片样本 + 独立标注 + 位置/单位/图例校验 | 图像 mapper 保持禁用，不计覆盖 |
| G-EFFECT | 真实游戏、玩家重复测量、冻结协议 | `VAL-V05` blocked；`local_effect` 未取得 |
| G-WIN（剩余） | 桌面多真实 GPU、第二台机器、适配器超时样本 | 已取得一台笔记本的单次只读探测，不足以判通过 |
| Q1 页面 | 浏览器旅程所需 HTTP 与页面 | `VAL-V06`、`VAL-V08`、`UX-V09` blocked |

## 9. 可复核命令

```text
py -3.13 -B -m pytest tests/product -q -p no:cacheprovider
py -3.13 -B -m pytest tests/integration -q -p no:cacheprovider
py -3.13 -B -m pytest tests/test_cli_config.py tests/test_providers.py     tests/test_schema_reader.py tests/test_validation.py tests/test_workflow.py     -q -p no:cacheprovider
```

真实设备探测（需授权）：`GAME_APP_REAL_PROBE=1 py -3.13 -B -m pytest tests/integration/test_real_probe.py -q`
