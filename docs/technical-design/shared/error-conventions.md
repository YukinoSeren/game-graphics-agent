# 阶段 B3：共享错误、幂等与重试契约

> 建议基线 `product-contract/0.2`；2026-11-01；未实现。读取兼容规则见 data §12。
> 对象字段见[data-contracts.md](./data-contracts.md)，合法转移与提交边界见[state-conventions.md](./state-conventions.md)。
> HTTP 部分沿 Q1 的本地网页建议展开；CLI 复用错误码、资源引用和动作语义。完整路由和各操作超时参数在 C/07 定义。

## 1. 成功、受限结果与错误的边界

- 输入被合法接受的异步创建返回 202 和 TaskAccepted；同步创建返回 201 和 ResourceResult，查询返回 200。请求成功接受不代表后台已经完成。
- 正常的 blocked 推荐、partial_scope 知识或 insufficient_evidence 决策是已登记的领域结果，不伪装为 HTTP 500，也不能只显示“成功”而隐藏限制。
- 类型/引用/并发守卫失败返回错误，不创建假版本。后台外部调用失败后先保存已得成果，再将 TaskRun 置为 partial/failed/cancelled/interrupted，并在任务查询中返回具体错误定位。
- 读取已有任务返回 200，即使任务 state=failed；响应携带该任务的错误包及已保存资源。查询本身找不到任务才返回 404。
- 同一业务错误在页面与 CLI 保持稳定 code；message 可本地化，不能靠解析 message 决定程序行为。CLI 原采集退出码 0/2/1 保持原义，新产品 CLI 的整体退出码在 C/07 映射，不直接改原型。

## 2. 错误包与动作结构

### 2.1 ErrorEnvelope

此处定义错误结构的唯一权威；data 中的 string error_code 引用本篇码表。

| 字段 | 类型与必填/默认 | 约束 |
|---|---|---|
| error.code | ErrorCode，R | 稳定机器码，见 §3；不直接输出供应商原始异常 |
| error.category | ErrorCategory，R | validation/conflict/not_found/security/dependency/external/storage/internal |
| error.message | string，R | 说明缺什么、影响什么、下一步是什么；不能包含密钥/任意本机路径 |
| error.request_id | ID，R | 本次调用的关联 ID，不代替幂等键；每次网络重试可有新的 request_id |
| error.field_errors | FieldError[]，D=[] | 类型/字段校验时给具体位置，不回显完整敏感输入 |
| error.resource_refs | ObjectRef[]，D=[] | 仅包含当前用户可访问的任务、问题、版本或文件 ID |
| error.retry_policy | RetryPolicy，R | 提供允许的动作，不承诺供应商请求恰好一次 |
| error.actions | RecoveryAction[]，R | 至少一个可执行或查看结果的动作；由后台固定映射，不由模型自造 URL/命令 |
| error.retry_after_seconds | int?，D=null | 只有有依据的服务限流/可恢复等待才填写，>=0；不是估计运行完成时间 |
| error.details | ErrorDetails，R | 结构化诊断引用与版本信息，空项显式 null/[] |

`FieldError={path:string,code:string,message:string}`，全字段 R；path 为请求字段路径，code 为 required/type/enum/range/reference/conflict/unsupported_field 之一。

`ErrorDetails={expected_revision:Revision?,current_revision:Revision?,current_resource_ref:ObjectRef?,replacement_resource_ref:ObjectRef?,call_id:ID?,saved_result_refs:ObjectRef[]}`，全字段 R。无法确定的值为 null，不在 details 塞未经脱敏的 traceback、响应体、HTTP 认证头或文件绝对路径。

| RetryPolicy | 含义 |
|---|---|
| none | 原请求不可重试；先改输入或按 actions 修复 |
| same_submission | 可重送同 key、同内容；先读取可定位结果，不能旋转 key |
| refresh_then_new_submission | 读取新版本/问题并核对，用户更正内容后用新 key 提交 |
| repair_then_new_submission | 修复配置/文件/服务后显式创建新逻辑提交；不冒用原任务成功 |
| resume_as_new_task | 查看成果/检查点后用新 key 与明确预算创建 parent 关联任务 |
| verify_external_outcome | 先核验未知外部调用，禁止自动重发模型请求；没有核验能力时保持未知 |

`RecoveryAction={kind:RecoveryActionKind,target_ref:ObjectRef?,label:string}`，全字段 R。target_ref 可空仅限修改输入/重新配置等不指特定资源的动作。

RecoveryActionKind 的合法值为 edit_input/read_task/read_information_request/review_configuration/view_saved_results/reupload_file/configure_provider/resume_task/create_new_task/verify_external_call/restart_local_application。动作由可信页面路由/CLI 处理器解释，不包含可执行脚本。create_new_task 与 resume_task 只引导明确的新提交，不由错误处理器自动执行。

### 2.2 公共调用约定

所有创建及状态变更都携带 MutationContext；HTTP 使用 Idempotency-Key，访问保护会话标识单独校验。同源/会话校验、报文解析、基本字段校验先于持久注册；未知字段/不合法键不得触发模型或文件解析。

短事务操作不排入重队列，包括回答、取消控制标记和当前指针校验。HTTP 超时是客户端未收到响应，不是后台取消；客户端恢复后读原任务或以原 key 重试。客户端不能通过超时后换 key 自动启动新采集/模型调用。

GET 只查询已存在资源，不生成持久比较或导出文件；比较/导出创建使用幂等变更操作，具体路由在 C1/07 定义。分页、轮询和超时参数也在 C 中定义；本轮不凭空确定第三方工具/接口时限。

## 3. 共享错误码

表中 HTTP 是网页形态的建议映射；所有资源错误须先校验用户可见性。对不可访问对象不泄露其路径/存在性。

| ErrorCode / 分类 | HTTP | 触发与副作用边界 | retry_policy；主要动作 |
|---|---|---|---|
| MALFORMED_REQUEST / validation | 400 | JSON/参数报文无法解析，未注册任务 | none；edit_input |
| INPUT_VALIDATION_FAILED / validation | 422 | 缺字段、类型/枚举/单位/范围错误、答案与跳过字段交叉；无业务写入 | none；edit_input |
| IDEMPOTENCY_KEY_REQUIRED / validation | 400 | 缺少或不合法 key；无业务写入 | none；edit_input |
| SESSION_CONTEXT_INVALID / security | 403 | 同源/应用会话校验失败；无业务调用 | same_submission；restart_local_application，恢复会话后保持原 key/内容 |
| RESOURCE_NOT_FOUND / not_found | 404 | 目标不可见或不存在；不返回敏感目标细节 | none；edit_input/view_saved_results |
| RESOURCE_GONE / not_found | 410 | 资源受控清理且保留墓碑；旧键不复活结果 | none；view_saved_results/create_new_task |
| IDEMPOTENCY_CONFLICT / conflict | 409 | 同作用域 key、不同规范请求 hash；不执行新输入 | refresh_then_new_submission；edit_input |
| CURRENT_CONFIGURATION_CONFLICT / conflict | 409 | expected_current_applied_id 不等于当前；不创建采用版本/声明、不推进指针 | refresh_then_new_submission；review_configuration |
| PREFERENCE_VERSION_CONFLICT / conflict | 409 | 偏好族 expected_current_id 已过时；不创建偏好版本/声明，返回当前 Preference 引用 | refresh_then_new_submission；view_saved_results/edit_input |
| TARGET_ACCEPTANCE_CONFLICT / conflict | 409 | 同会话/目标的 supersedes_id 不是当前接受声明；不覆盖或新增冲突声明，返回当前 TargetAcceptance 引用 | refresh_then_new_submission；view_saved_results/edit_input |
| BINDING_REVISION_CONFLICT / conflict | 409 | expected_segment_revision、expected_binding_revision、历史登记 segment_ref 或04日志关联 expected_session_revision 中相应修订旧；field_errors 标明具体维度，不登记历史配置、关联artifact或追加绑定；先前独立提交的历史事实仍保留 | refresh_then_new_submission；view_saved_results |
| INFORMATION_REQUEST_VERSION_CONFLICT / conflict | 409 | 仅请求 version 旧，上下文仍有效；无状态变更 | refresh_then_new_submission；read_information_request |
| INFORMATION_CONTEXT_CHANGED / conflict | 409 | 上下文已失效；必要时原子保存 superseded/替代问题或解除等待 | refresh_then_new_submission；read_information_request/read_task |
| TASK_STATE_CONFLICT / conflict | 409 | 对不允许状态执行恢复/绑定等；终态取消另按幂等无操作返回当前终态 | none；read_task/view_saved_results |
| ACTIVE_CAPTURE_EXISTS / conflict | 409 | 同会话已有 queued/running 采集，即使 key 不同也不另建 | none；read_task，返回已有 task 引用 |
| REFERENCE_INVALID / dependency | 422 | 引用错类型/错游戏/错安装/不属于该包，依赖记录隔离，或恢复检查点不属于父任务/输入不兼容 | none；edit_input/view_saved_results |
| BASELINE_REQUIRED / dependency | 409 | 请求正式 ChangeSet/采用时没有有效实际基线或无法恢复必要原值 | repair_then_new_submission；review_configuration |
| PRECONDITION_UNVERIFIED / dependency | 409 | 操作要求的关键适用/生效前提缺证据、历史配置没有适用于目标时段的依据、分析引用开放段/绑定，或恢复时隐去父任务已有检查点；无伪造结果写入 | repair_then_new_submission；read_information_request/view_saved_results |
| UNSUPPORTED_ARTIFACT_FORMAT / validation | 422 | 未验证 CSV Schema/单位/时钟；已上传原件可保留但不假解析 | repair_then_new_submission；reupload_file/view_saved_results |
| IMPORT_SNAPSHOT_INCONSISTENT / storage | 409 | 源快照变动、缺文件/坏引用/重复键，不发布导入 | repair_then_new_submission；view_saved_results |
| ARTIFACT_UNAVAILABLE / storage | 409 | 登记资源对应文件缺失/损坏/未发布；不返回空文件成功 | repair_then_new_submission；view_saved_results/reupload_file |
| PROVIDER_NOT_CONFIGURED / external | 503 | 本机服务配置缺失；已有成果保留，不调用空模型 | repair_then_new_submission；configure_provider |
| PROVIDER_AUTH_FAILED / external | 503 | 供应商认证/权限被拒，脱敏保存；不自动换模型/钥匙 | repair_then_new_submission；configure_provider/view_saved_results |
| PROVIDER_RATE_LIMITED / external | 503 | 供应商明确限流且执行结果已知；Retry-After 有依据才传 | resume_as_new_task；view_saved_results/resume_task |
| PROVIDER_FAILED / external | 502 | 已知供应商失败；若不能证明执行结果已知则改 CALL_OUTCOME_UNKNOWN | resume_as_new_task；view_saved_results/resume_task |
| CALL_OUTCOME_UNKNOWN / external | 409 | 可能已送达/计费，结果未知；保留审计，不自动重发 | verify_external_outcome；verify_external_call/view_saved_results |
| STORAGE_COMMIT_FAILED / storage | 503 | 文件/库写失败；若提交结果未知先查幂等记录，不能补建同操作 | same_submission；read_task/view_saved_results |
| INTERNAL_ERROR / internal | 500 | 非预期错误，保留关联 ID；不泄露栈或把失败改成空成功 | same_submission；read_task/view_saved_results |

PRECONDITION_UNVERIFIED 用于尝试本身不成立的操作，例如请求受控比较但缺生效条件；正常“生成推荐”产 provisional/blocked 则保留为领域结果。BASELINE_REQUIRED 不阻止未知配置会话、日志或文字反馈。

后台 provider 错误出现在任务记录中时，任务 GET 仍为 200；表中 502/503 只用于相应同步操作直接失败的情况。预算耗尽/玩家明确跳过不是供应商故障：Task/领域结果按 state 规则保存，不额外伪造上述错误。

## 4. 幂等契约与规范请求

### 4.1 作用域、hash 与前端持久保存

1. 前端在一次逻辑提交前保存 key、规范业务内容及提交目标；双击、刷新、超时/断开后沿用原 key 与内容。提交下一批回答、改内容重新确认、明确新建一轮/追加预算才使用新 key。
2. 后台作用域为 `(user_scope_id, HTTP method, canonical_route, key)`；user_scope_id 是本机用户持久身份，不因后台重启或会话保护令牌更新而变化。CLI 使用稳定操作名作为 canonical_route 参与同一规则。
3. canonical_route 来自可信路由器解析后的资源路径，含实际目标 ID；不使用只有 `/tasks/{id}` 的模板，不含 host/临时会话令牌，不按 Windows 路径规则折叠大小写。影响业务的查询参数也进入规范请求 hash。
4. 校验请求 Schema 后展开明确默认，按键名稳定排序对象；列表保持语义顺序，仅对契约声明为集合的项按确定规则归一。数字保留 int/float 类型区别，字符串/原话不压空白、不改大小写，不把未知/null/未提交混同。
5. hash 覆盖请求 Schema 版本、所有业务字段/预期版本/预算、文件 ID 及内容 hash；不含 request_id、会话保护令牌、幂等键本身或后台生成的时间/ID。文件 hash 从后台已验证注册读取，不能相信客户端自报。
6. IdempotencyRecord 保存 request_schema_version；Schema 升级后旧键必须按旧规则核对或明确拒绝兼容输入，不能因使用新规则查不到而把旧提交重跑。

hash 针对经校验规范化的外部业务请求；不针对本次重新构造的 Provenance、声明记录 ID 或其他后台生成字段。先查重再生成新事实，避免相同玩家提交因来源包装的登记时间不同被误判为不同请求。内部 DTO 的可信包装由已登记声明/观测导出，不允许客户端通过传入“已观测/检查通过”绕过来源核验。

### 4.2 原子注册与重复请求

| 情况 | 必须行为 |
|---|---|
| 同 key、同 hash，原记录 registered | 返回同 task/result 与原 response_status/response_body；不重入领域副作用或外部调用；当前状态另 GET 查询 |
| 同 key、不同 hash | IDEMPOTENCY_CONFLICT；不悄然忽略参数差异，也不自动生成 key |
| 同 key 并发首次提交 | 数据库唯一约束＋同事务只登记一次；竞争方等短事务提交后读原响应，不能先起外部调用再去重 |
| 不同 key、同会话活动采集 | ACTIVE_CAPTURE_EXISTS，包含原 task_id，不能并行启动第二采集 |
| 任务已结束/失败，再重试原提交 | 返回原任务/响应；不自动建立“重试任务”；显式恢复用新 key、parent_id 与新预算 |
| 数据已清理但 key 墓碑保留 | RESOURCE_GONE，HTTP 410；不让旧 key 成为新提交 |
| 进程退出发生在 DB 提交前 | 事务回滚，没有可消费的任务/结果或调用；用原 key 可重新登记同操作 |
| 提交成功、响应丢失 | 读已提交的幂等记录，返回原 ID；不能靠“客户端没见成功”重做 |
| 无法判断 DB 是否提交 | 查询同 key 后再决定；存储仍不可用则返回 STORAGE_COMMIT_FAILED，不改 key、不启动任何外部调用 |

任务创建事务包含 TaskRun、初始 TaskInputRevision、注册事件与 IdempotencyRecord；提交后入队。同步采用/回答等事务包含领域事实、所有受影响投影/事件和同一幂等原响应。历史配置登记仅提交其声明/历史版本/幂等响应，不写当前指针或绑定；后续补绑是另一逻辑提交，使用新 key。文件创建/发布另遵守 state §8，不能先返回有效文件引用再慢慢写原件。

报文、类型、访问校验失败且尚无业务写入时不消耗 key；纯并发前置冲突也不产生假任务。业务校验在事务内复核。INFORMATION_CONTEXT_CHANGED 如确实提交了 superseded/替代问题等维护事件，必须在同事务保存该 409 响应的幂等记录；同 key 重试返回原 409，不重复造替代问题。若没有提交任何变化，则可不登记 key；客户端更新内容后必须用新 key，不能借此偷改已注册提交。

幂等记录首版不自动过期。清理资源时保留作用域、key、hash、原响应语义/墓碑与结果定位；不能随表清理把去重历史删掉。具体存储/压缩机制在 C/07 定义。

## 5. 超时、重试与恢复矩阵

| 层/失败 | 自动动作边界 | 玩家/调用者动作 |
|---|---|---|
| 本地 HTTP 丢响应/断开 | 可用相同提交重试去重；不得新建逻辑请求 | 先读 task/已有结果，再用原 key/内容重送 |
| 问题/配置版本冲突 | 不自动选最新、不合并声明 | 读取新版本/差异，核对后新提交；不要丢仍有效回答 |
| 公共只读读取失败 | 仅由任务层在预算内按明确已知失败策略重读；每次尝试计数 | 看部分结果/访问限制；不是要求玩家逐页搜集 |
| 搜索/模型调用明确未发送 | 可按 C 模块的有界策略重新尝试，仍须预算与审计；默认不隐式重试模型 | 修复配置/明确恢复；每个新尝试有独立调用记录 |
| 供应商明确失败响应 | 不把整个任务从头重跑；认证失败停止该服务继续调用；已知限流按真实依据处理 | 保存部分成果，修复后新恢复任务 |
| 外部请求超时/连接断开/进程退出，可能已送 | outcome_unknown；绝不把一般异常当 failed_known 后自动再试 | 先核验结果；无法核验则保留未知，独立新调用需明确新预算与可能重复说明 |
| 文件写入/发布失败 | 未发布资源不被消费，保留暂存/操作日志，核对原件与 DB | 原 key 重试核对；修复或受管理重新上传，不传任意路径 |
| 后台重启，旧 queued/running | 标 interrupted，保留原状态历史，不自动继续外部调用 | 有检查点先校验兼容性；确无检查点可显式传 null，核对父任务全部调用审计后以新预算创建 parent 关联任务 |
| 后台重启，waiting_input | 保留问题与等待，不占队列 | 可继续按版本回答/明确跳过 |
| 终态任务后补信息 | 不修改旧任务成果/输入、不复活 | 新事实保存后显式新建相关分析任务 |

超时秒数、最大重试次数、轮询间隔由 C 根据已核验依赖给出，不由模型动态生成。没有某项参数时默认不自动重试有副作用的外部请求；本地幂等重送与供应商请求重发必须分开实现。

## 6. 结构化示例

以下为虚构的“当前采用版本已变化”错误，代码/字段与上述契约一致；两个配置 ID 仅为示例，不表示 FH6 真值。HTTP 状态为 409。

```json
{
  "error": {
    "code": "CURRENT_CONFIGURATION_CONFLICT",
    "category": "conflict",
    "message": "当前配置已更新。请核对新版本，再确认本轮调整。",
    "request_id": "00000000000000000000000000000011",
    "field_errors": [{
      "path": "expected_current_applied_id",
      "code": "conflict",
      "message": "该基础版本已不是当前采用版本。"
    }],
    "resource_refs": [{
      "entity_type": "AppliedConfiguration",
      "entity_id": "00000000000000000000000000000012"
    }],
    "retry_policy": "refresh_then_new_submission",
    "actions": [{
      "kind": "review_configuration",
      "target_ref": {
        "entity_type": "AppliedConfiguration",
        "entity_id": "00000000000000000000000000000012"
      },
      "label": "核对当前配置"
    }],
    "retry_after_seconds": null,
    "details": {
      "expected_revision": null,
      "current_revision": null,
      "current_resource_ref": {
        "entity_type": "AppliedConfiguration",
        "entity_id": "00000000000000000000000000000012"
      },
      "replacement_resource_ref": null,
      "call_id": null,
      "saved_result_refs": []
    }
  }
}
```

expected_revision/current_revision 为 null 是因为本操作按不可变 applied_id 比较，不编造与其对应的整数 revision。字段冲突不应返回“已成功采用”的部分结果。

## 7. 契约验收与设计检查

以下为未来实施用例，非本轮运行结果；均需原输入、动作计数及持久响应断言。

| 编号/关联要求/目的 | 初态与输入 | 操作步骤 | 预期输出/状态与副作用 | Mock / 真实环境 |
|---|---|---|---|---|
| B-ERR-01 / D02：提交去重 | 空库，同 key 同业务输入，模型调用桩计数=0 | 并发创建、丢初响应、重启后原 key 重试 | 同 task_id/原响应；仅一次注册；无重复调用；重启任务按 interrupted 保留，不自动重发 | 供应商桩、临时 DB；不需网络 |
| B-ERR-02 / D02：hash 不混同 | 已注册 key，分别改预算/expected ID/上传内容 | 复用 key 再提交；另只更新会话令牌/request_id | 业务变化 409 且无副作用；仅访问令牌/request_id 变化仍指同提交 | 请求/文件夹具；不需真实环境 |
| B-ERR-03 / D02：墓碑与不同键 | 已清理结果保留 key；另有同会话活动采集 | 重试旧 key，再用不同 key 启采集 | 旧 key 410 不重建；活动采集 409 返回原 task；调用计数不增 | 临时 DB/采集桩；不需游戏 |
| B-ERR-04 / D04：错误也去重 | 上下文失效，需 supersede 并建替代问题 | 同 key 重试该提交 | 返回同一 409、同替代问题；只追加一次维护事件 | 请求/上下文夹具；不需真实环境 |
| B-ERR-05 / A06：未知调用 | 外部桩已可能执行但超时 | 返回错误、客户端重试、尝试恢复 | CALL_OUTCOME_UNKNOWN+verify_external_outcome；本地重试不增加供应商计数 | 可计数供应商桩；不通过付费调用验证 |
| B-ERR-06 / A05：提交不确定 | DB 提交后模拟响应丢失，另一次文件校验失败 | 原 key 重试，读取错误资源 | 前者回原 ID；后者不发布/不空成功，给保存成果定位 | 故障注入/临时目录；不需网络 |
| B-ERR-07 / 安全边界：脱敏 | 供应商异常含密钥/请求体，本机路径含用户名 | 构造错误包、日志和导出 | 页面/可分享数据不含敏感原文，只有稳定码/关联 ID/可见资源引用 | 异常/路径夹具；不需真实环境 |
| B-ERR-08 / 领域与错误分离 | 任务产 blocked 推荐与 failed 任务各一例 | 查询两个已有任务，再查不存在 ID | 已有任务均 200 且保留不同领域/执行结果；不存在 404；不隐式重跑 | 任务夹具；不需真实环境 |
| B-ERR-09 / D03、D04：历史登记去重 | 当前 B，旧段 A 的登记已提交但响应丢失；另有过时 segment_ref | 原 key 重送历史登记，再以新 key 补绑；提交过时登记 | 原 key 返回同 A/声明，当前 B 不变；补绑独立去重；过时登记返回 BINDING_REVISION_CONFLICT，无部分写入 | 历史/版本夹具与临时 DB；不需游戏 |
| B-ERR-10 / A06、D02：无检查点恢复边界 | interrupted 父任务 checkpoint=null；另有外来检查点和父任务未知调用 | 分别提交恢复，再重试原 key | 合法恢复返回同新 task/parent；外来检查点 REFERENCE_INVALID；未知调用仍适用 CALL_OUTCOME_UNKNOWN，不因 null 自动重发 | 调用桩/临时 DB；不需网络 |
| B-ERR-11 / 偏好与目标并发 | 两页同偏好当前ID/同目标supersedes | 并发更正/撤回或接受/拒绝，再以原key重试 | 仅一方推进当前意向，另一方对应专用409；历史不覆盖，成功响应同key去重 | 声明夹具/临时DB；不需真实环境 |

静态检查须同时核对 data 的 MutationContext/IdempotencyRecord、state 的终态/未知窗口以及本篇 retry_policy。完整页面文案、错误到按钮的映射和逐操作接口验收在 C5/06/07/08 展开。
