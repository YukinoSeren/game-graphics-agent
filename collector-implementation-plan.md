# 网页采集原型实现计划

> 执行方式：本会话按 executing-plans 与 TDD 逐项实现。已有设计已获用户授权进入开发；不再次请求设计确认。

**Goal:** 交付可运行的命令行采集程序，支持指定 URL、发现来源、带证据的提取、校验、定向补查、SQLite 与 JSON/Markdown 导出。

**Architecture:** Python 单进程；搜索、读取、模型为适配器。规则校验和预算由程序掌控，外部内容只作为数据。原始研究样本保持冻结。

**Tech Stack:** Python 3.13（兼容 3.11+）、标准库 SQLite、requests、BeautifulSoup、Pydantic、pytest。复用本机 mcporter/Exa；模型按实际可用凭据接入，显式记录失败。

**Spec:** [原型设计](./web-collector-prototype-design.md)、[知识契约](./knowledge-intake-contract.md)。

## 全局要求与实施决定

- 固定值、候选、条件、未知；指标类型与数值形状分开；保留环境作用范围及原文定位。
- 资料入库与推荐适用性分别判断；无法证明适用时不能标为通过。
- 不设置默认目标 FPS 或画质偏好；长视频及图片识别不在本轮实现范围。
- 每次外部调用前检查预算；失败、重试、模型原始响应与补查尝试均留档。
- 原目录没有 Git 仓库，在当前目录新增独立包，不初始化仓库、不复制研究资料；不适用 Git worktree/提交步骤。
- 开发采用离线确定性测试与小预算联网冒烟；真实模型服务不可用时如实输出失败，不用冻结答案冒充模型运行。

## 文件与职责

- `game_collector/schema.py`：Pydantic 类型、值、证据、配置、环境、缺口与提取包。
- `game_collector/reader.py`：URL 规范化、HTML 正文/表格/图片结构化、公开网页读取。
- `game_collector/providers.py`：Exa 搜索及 HTTP 模型适配；不执行网页中的指令。
- `game_collector/validation.py`：来源定位、引用、值一致性、环境/比较/约束检查与逐项问题。
- `game_collector/workflow.py`：预算、发现、重读、补查、修订与停止。
- `game_collector/storage.py`：SQLite 修订、运行快照与导出。
- `game_collector/__main__.py`：命令行入口。
- `tests/`：受控网页、错误输入、工作流与供应商接口测试。
- `README.md`、`collector-progress.md`：使用方式与执行证据。

## Task 1：字段与网页结构

**Interfaces:** `Extraction.model_validate(payload)`；`parse_html(url, html) -> Document`；`canonical_url(url) -> str`。

- [x] 写出失败测试：区间边界、未知原因、条件/候选、逐列表格与章节定位、脚本清除。
- [x] 运行 `py -3.13 -m pytest tests/test_schema_reader.py -q`；Expected：新模块缺失导致失败。
- [x] 实现上述类型及 HTML 解析；保留原始列名/行列坐标，不用附近设备条件填空。
- [x] 重跑同一测试；Expected：全部通过。

```python
with pytest.raises(ValidationError):
    NumericValue(number_kind='interval', lower=85, upper=70)
```

## Task 2：入库与适用性校验

**Interfaces:** `validate_extraction(extraction, document, context) -> list[dict]`；每条结果包含记录、状态、问题及适用性。

- [x] 写失败测试：假引用、无出处、未支持的固定值、跨场景比较、已知硬件约束及条件模板。
- [x] 运行 `py -3.13 -m pytest tests/test_validation.py -q`；Expected：模块缺失。
- [x] 实现确定性校验和有限证据约束；未检查的兼容性维持 unknown。
- [x] 重跑；Expected：全部通过。

```python
assert checked['record_status'] == 'quarantined'  # 不存在的证据块或引用
assert checked['applicability'] == 'unknown'  # 尚无兼容性依据
```

## Task 3：外部适配器

**Interfaces:** `search(query, limit)`；`read(url)`；`extract(document, context)`。

- [x] 写失败测试：Exa 输出解析、HTTP 错误脱敏、模型响应解析、禁止 shell 拼接、读取失败不冒充空结果。
- [x] 运行 `py -3.13 -m pytest tests/test_providers.py -q`；Expected：新模块缺失。
- [x] 实现可配置模型、Exa CLI 搜索、HTML 读取与 Jina 替代；调用超时且禁用隐式重试。
- [x] 重跑；Expected：全部通过。

```python
assert parse_search_results('Title: Guide\nURL: https://example.org/guide')[0]['url'] == 'https://example.org/guide'
```

## Task 4：预算、补查、存储与 CLI

**Interfaces:** `Collector.run(request, output_dir)`；SQLite `sources`、`records`、`runs`；`py -3.13 -m game_collector`。

- [x] 写失败测试：成功闭环、证据解决缺口、无证据不能关闭、硬预算、失败保留、修订不覆盖、相同内容去重。
- [x] 运行 `py -3.13 -m pytest tests/test_workflow.py -q`；Expected：新模块缺失。
- [x] 实现调用日志、最多一次同路径重试、定向重读与搜索、缺口优先级、停止原因及导出；CLI 提供 discover/seeded。
- [x] 重跑；Expected：全部通过。

```python
assert run['status'] == 'partial_scope'  # 关键缺口或来源/模型失败
assert run['calls']['search'] <= request['budget']['max_searches']
```

## Task 5：真实冒烟与交付

- [x] 执行全部测试 `py -3.13 -m pytest -q`；Expected：通过。
- [x] 用现有文字来源执行小预算 seeded 与 discover；保存调用数、模型标识、失败/停止原因，不宣称独立准确率。
- [x] 保留冻结样本 SHA256；补充 README、实施报告和讨论纪要，明确验收范围与未覆盖能力。
- [x] 对实现进行单独的代码审查并修复重要发现；重新运行相关测试。

## 审查重点

表格跨列误配；原文定位与模型幻觉；读完整网页不等于获取所有设置；候选与 HDR 观察误转固定建议；约束作用范围；未解决缺口误关闭；外部调用预算及异常退出；敏感凭据不写入日志；新修订保留旧依据。
