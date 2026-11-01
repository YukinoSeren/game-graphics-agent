# 游戏画质知识采集原型

整个项目的分板块推荐设计见 [总设计 v1.1](./project-design.md)。当前暂停编码、审阅整体设计；本文仍用于说明已有采集原型，不代表整套产品已实现。

Python 单进程命令行程序，按 [知识契约](./knowledge-intake-contract.md) 读取公开网页、调用模型提取、校验出处及引用、执行定向补查，导出 SQLite / JSON / Markdown。

这是采集环节。玩家配置、个性化推荐、游戏设置修改和本机 FPS 采集不在本轮实现范围。

## 环境与运行

已在本机 Python 3.13 验证；支持 Python 3.11+。本机 `python` 是 Windows 应用别名，请用 `py -3.13`。

```powershell
Set-Location E:\code\game
py -3.13 -m pip install -e '.[test]'
py -3.13 -m pytest -q
py -3.13 -m game_collector --help
```

本次开发环境已有依赖，未另外安装。新环境可运行上述安装命令。`discover` 还需本机 `mcporter` 配好 Exa；`seeded` 不依赖搜索服务，除非补查触发搜索。

## DeepSeek 配置

详见 [DeepSeek 配置说明](./deepseek-config.md)。当前已成功执行模型列表凭据检查。

- API 地址：`https://api.deepseek.com`。
- 请求模型：用户指定的 `deepseek-v4-flash`；官方目前将该兼容名称路由到当前 Flash，不能据此宣称实际运行的是退役 V4 版本。请求名和返回名分别保存。[官方接口说明](https://api-docs.deepseek.com/quick_start/agent_integrations/)
- 使用 `response_format=json_object`，本地仍须做 JSON / Schema 校验；JSON 模式不是事实核验。[官方 JSON 说明](https://api-docs.deepseek.com/guides/json_mode/)
- 本项目默认启用 DeepSeek 思考模式、`reasoning_effort=high`，单次最多 48000 输出 token，读取超时 300 秒。可设置 `COLLECTOR_THINKING=disabled` 做更快的调试。
- 每轮默认最多 16 次模型调用；不做隐式重试，不估算未计量金额。

```powershell
py -3.13 -m game_collector --check-model
```

这条命令只请求模型列表，不生成文本；通过不代表已完成真实提取验收。

## 指定来源采集

```powershell
py -3.13 -m game_collector `
  --game 'Forza Horizon 6' --alias 'FH6' `
  --mode seeded `
  --url 'https://dotesports.com/forza/news/best-forza-horizon-6-pc-settings' `
  --max-pages 2 --max-searches 2 --followup-rounds 1 --max-model-calls 3
```

`--url` 可以重复。游戏名尽量使用网页中的正式名；中文名可用 `--alias` 显式补充。程序不自行猜测跨语言游戏别名。

## 自动发现来源

```powershell
py -3.13 -m game_collector `
  --game 'Forza Horizon 6' --alias 'FH6' `
  --gpu 'RTX 3060 Ti' --mode discover `
  --max-pages 6 --max-searches 8 --followup-rounds 2 --max-model-calls 8
```

显卡可省略，目标 FPS 和画质偏好没有前置参数或隐藏默认值。首轮分别搜索官方版本/兼容性、条件化测试、不同配置取舍；相同 URL 去重，正文采录内容相同则关联来源，不当作独立支持。

## 调试入口与输出

```powershell
# 仅验证搜索和读取：会明确报告提取未完成，不生成假记录。
py -3.13 -m game_collector --game 'Forza Horizon 6' --read-only --max-pages 2
```

默认保存到新的 `runs/日期时间-随机标识/`。`--output` 只能指定新目录或空目录，避免覆盖历史。

| 文件 | 内容 |
|---|---|
| `run.json` | 输入、预算、工具、实际模型、调用日志、完成状态和停止原因 |
| `sources.json` | 原始 URL、来源修订、正文块/表格/图片线索、哈希、失败与重复关系 |
| `extractions.json` | 模型原始文本、实际返回模型、用量、格式修复差异、Schema 问题及解析结果 |
| `records.json` | 原文主张、引用作用域、入库状态、适用性、分支检查及状态变更历史 |
| `candidates.json` | 尚可审阅的配置资料，含 needs_followup；不是可直接执行的最佳配置 |
| `gaps.json` | 缺口优先级、受影响记录、补查尝试、新证据及停止原因 |
| `knowledge.sqlite3` | runs / sources / records 索引和 JSON 载荷 |
| `report.md` | 可读报告、来源、结果范围和未解决问题 |

程序每次处理来源后检查点保存；中断会导出部分结果。目前没有自动恢复未完成调用的入口，也没有跨运行下载缓存；可审阅旧资料并发起新运行。

退出码：`0` 本次声明范围完成；`2` 正常导出部分成果；`1` 配置或运行错误。`completed_scope` 不表示已收集全部游戏设置。非关键 P2 未知可以保留，P0/P1 未解决时为部分结果。

## 已实现的核验与边界

- 原名、设置值、指标与单位、引用、原文片段、表格行列做确定性检查；可忽略 Markdown 展示样式，不删改原文档。
- 候选/条件/未知保持原类型；HDR 检测状态作为观察；FPS 区间不求中点均值。
- 结构不合法的对象单独隔离，保留原始模型响应；依赖坏记录的对象也不能通过。
- 有证据的明确排除约束可按已知条件过滤分支；新约束按精确原名重新检查已有配置，保存旧状态和新依据。
- 缺口关闭要求新的相关证据、字段/答案/作用范围一致；涉及旧记录时还要求相应重验。只取到另一篇文章不能关闭缺口。
- 一轮中同一 URL 的多个错误共享一次重读。来源自己的定位/Schema 错误先重读，不用无关作者的文章“修复”原文。
- 格式修复最多一次，只剥离 JSON 围栏或插入单个缺失的闭括号；不改数值和条件。更复杂错误保持隔离。
- 公开 HTML 失败可转 Jina；不自动登录。图片只登记位置，关键图像信息缺失时保持问题未解决。长视频暂缓。
- 页面只是数据，不能要求程序执行命令；搜索通过 Node argv 传参，错误信息不包含密钥和 HTTP 请求体。

这些是有限校验，不是完整事实证明。复杂依赖、跨来源版本影响、同义设置归一仍可能需要补查；未经执行的检查保持 unknown。四篇研究资料尚未全部通过自动逐项回归，不报告独立准确率。

实施证据见 [执行记录](./collector-progress.md) 和 [首轮实现报告](./collector-implementation-report.md)。

本轮最终证据：45 项离线测试通过；[实施报告](./collector-implementation-report.md)记录真实运行接受 89 条、隔离 3 条，仍为部分结果；[冻结样本对照摘要](./docs/validation/fh6-dot-comparison.md)中选定的 24 个固定值一致。不要把这组已见样本回归当作独立准确率。

## 仓库与本地文件

仓库保存源代码、测试、设计文档和冻结研究样本。密钥、环境变量文件、缓存、本地数据库及 `runs/` 调试产物不进入版本控制；验证结论保留在实施报告与精简对照摘要中。

文档中出现的 `E:\code\game` 是原开发目录，克隆后请切换到自己的项目目录执行命令。使用已有凭据配置模型，仓库不包含可用密钥。
