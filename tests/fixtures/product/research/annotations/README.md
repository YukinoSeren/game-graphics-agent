# 研究格式标注协议（IMP-T04 / F-R1、F-R3）

这里存放**独立标注**文件。只有同时满足本协议、且 `annotation_method=independent`、
`reviewer_is_session_model=false` 的标注，才会让对应研究格式的 mapper 被注册。

## 1. 谁能标注

- 标注者**没有参与本次提取**，也没有看过模型输出的提取 JSON。
- 或者：独立模型 + 盲测流程（先冻结标注，再比对提取结果）。
- **同一会话模型回查不算独立标注**。仓库里的 `fh6-*-checks.json` 自述就是这种回查，
  因此只能记 `model_recheck`，`mapper` 不启用，原件按 `retained_only` 保留。

## 2. 一个标注文件包含什么

文件名任意，内容是一个 `ResearchAnnotation`：

| 字段 | 含义 |
|---|---|
| `format_id` | `research-result/v1`、`research-source-sections/v1` 或 `research-checks/v1` |
| `source_file` | 被标注的原件文件名（与冻结副本同名） |
| `fixture_id` | 标注夹具的逻辑名 |
| `fixture_sha256` | **原件字节**的 sha256（小写 hex，64 位） |
| `annotation_method` | 只能是 `independent`；`model_recheck` 会被门禁拒绝 |
| `reviewer_source` | 标注者/流程的说明（不写私密路径或密钥） |
| `reviewer_is_session_model` | 必须为 `false` |
| `field_coverage` | 必须覆盖该格式映射表的**每一个** `product_field` |

`fixture_sha256` 的算法：

```bash
python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" fh6-extraction-results.json
```

## 3. 门禁会做什么

1. 格式不注册 → 原件仍被冻结、`ImportSnapshot` 仍发布，但结果是 `retained_only`。
2. 标注的 `source_file` 或 `fixture_sha256` 与冻结字节不一致 → 拒绝（`annotation_status=rejected`）。
3. 标注覆盖不全 → 拒绝，并列出缺哪些字段。
4. 通过后，映射只产出 `research_observation`；区间保留 `lower/upper` 且边界语义 `unknown`，
   HDR 单独成记录，`dot`/`gg` 分区不共享 local id。

## 4. 原件不得回写

标注只读冻结副本；仓库根的四份原件在任何路径下都不会被改写。

## 5. `annotation-template.json`

模板本身是 `model_recheck` + 空覆盖，**故意不被接受**：复制它并改个名字不会解锁任何格式。
必须真的完成独立标注，再填满 `field_coverage`。
