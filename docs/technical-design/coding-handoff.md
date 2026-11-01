# 产品编码交接：从离线基础切片开始

> 日期：2026-11-02；新产品契约`product-contract/0.4`，公共知识0.1。
> 本文是下一轮编码的最终交接入口。当前轮只修订设计、整理交接和推送，未创建game_app业务源码。后续用户给出“开始首个切片”的指令即可按§4执行，无须重新进行阶段A—D。
> For agentic workers: 获得编码指令后按executing-plans逐任务实施；只有用户另行要求时才委派。复用现有[34任务实施计划](./implementation-plan.md)，不再创建平行计划或GSD/.planning结构。

**Goal:** 先在临时库、临时目录及供应商桩中证明不可变历史、持久幂等、任务等待/恢复与文件发布边界，再进入领域功能。

**Architecture:** 保留game_collector独立CLI，新增game_app单应用包；所有者写领域事实，应用层协调SQLite短事务、持久任务和受管理文件；网络不进入数据库事务。

**Tech Stack:** Python >=3.11、Pydantic 2、标准库SQLite，现有requests/BeautifulSoup；本机记录使用Python 3.13。网页/模板/图表依赖尚未固定，首个切片不安装它们。

**Spec:** [当前修订及反例](./12-phase-d-remediation.md)、[全局计划](./implementation-plan.md)、[数据](./shared/data-contracts.md)、[状态](./shared/state-conventions.md)、[错误](./shared/error-conventions.md)、[工程07](./07-data-and-architecture.md)、[验收08](./08-validation-and-delivery.md)。

## 1. 仓库事实、权威与状态

- 当前已有game_collector、45个既有测试函数、研究JSON和原始设计；game_app、tests/product、产品库/持久执行器/页面尚未实现。存在设计不表示存在对应函数。
- 09、10、11分别是初次D、0.3补全、修订前独立审计的历史；12和更新后的shared/模块/全局计划定义当前0.4。根目录design-handoff.md是阶段A原交接，不能把其中“只开始A”当作后续实施范围。
- 不重写公共知识0.1、冻结研究JSON或旧runs。研究映射使用副本；纯克隆缺runs时报告输入不可用，不造运行数据。
- Q1（产品形态）、Q2（首游戏/适配范围）、Q3（确认方式/玩家测试参与）仍待确认；R03仍为方案建议。首个离线基础切片不依赖这些选择，不需为UUID/表名/常规文件布局再次停下来确认。
- G-WIN/G-GAME/G-FORMAT/G-MODEL/G-WEB/G-IMAGE/G-EFFECT均只有门禁定义，没有新增真实通过证据。真实工具/模型/游戏操作在对应授权和门禁满足后另行执行；首个切片供应商发送计数应为0。

## 2. 实现时必须继承的四项修订

| 必须行为 | 唯一定义与实现落点 |
|---|---|
| 更正GPU后还可补根问题中的显存；前轮更正不回退 | 01 §5.3、data §8.2、state §7.3；ENG-T02先保留协调边界，DEV-T04实现具体创建/答案归一；facts_only各分支均禁止写源任务 |
| 路径未知的同名设置可独立保存；复用必须选定ID | 01 §5.2、07 §4；ENG-T01建已定位唯一槽/未知位置头/旧歧义桶，DEV-T02实现GET列表及auto/reuse/distinct；历史不合并 |
| 更正目录后换配置引用须确认旧值和新值 | data SettingRefReplacement、01 §5.2、state §4.2；DEV-T03实现manual_edit的显式映射事务；未改K/U/预设保留，旧A不变 |
| 带原件/声明文件的功能依赖真实文件服务 | ENG-T03完成后才按完整任务开始KNW-T01/DEV-T01，DEV-T02传递继承；01上下文可以桩，文件发布不能用桩冒充 |

0.4变更请求的Schema版本由X-Product-Contract-Version/CLI等价选项传递并写入幂等记录；原key先按原request_schema_version核对。禁止给旧0.1—0.3请求加新默认后重跑。0.4新字段定义和旧链/目录兼容一律读data §12.2。

## 3. 首个切片的文件与接口归属

所有下列game_app路径、测试路径均为拟建。不要先搭空服务/页面再补事实链。

| 任务 | 创建/修改文件 | 消费与产出 |
|---|---|---|
| ENG-T01 | game_app/__init__.py、contracts/__init__.py、contracts/primitives.py、enums.py、errors.py、common.py、repository.py、migrations/；tests/product/test_repository.py；修改既有pyproject.toml包发现 | 从shared转录严格ID/Revision/ObjectRef/VersionRef及头、基础包装/枚举/错误；提供固定版本读取、UnitOfWork、引用/唯一约束和旧视图；不提前实现领域算法 |
| VAL-T01 | tests/product/conftest.py、test_fixture_loader.py、tests/fixtures/product/及测试证据工具 | 消费08 FixtureManifest/CaseSpec/CaseResult/RunManifest；合法UUID映射、hash校验、追加报告，旧期望不覆盖 |
| ENG-T02 | game_app/tasks.py、application.py；tests/product/test_tasks.py | 消费ENG-T01事务，提供07 register_task/get_task/cancel_task/resume_task；持久输入/事件/幂等/预算、一个重槽和短控制；领域处理器先用桩 |
| ENG-T03 | game_app/files.py；tests/product/test_files.py | 消费UnitOfWork/幂等/任务审计，提供FileOperation、manifest、发布/恢复和一致备份；补ENG-T02响应原件窗口；不解释领域内容 |

公共服务函数输入/返回以07 §5及shared §9为准；内部repository方法可按模块职责命名，但必须由ENG-T01给出明确签名和对应测试再让后继调用，不由后继猜接口。VersionHeader、ObjectRef等基础类型放primitives.py；FieldValue/来源包装放common.py；领域具体DTO待对应任务创建到contracts/device.py等，避免把全部实体堆进primitives.py。

## 4. 可直接执行的首个切片步骤

以下复选框均为待做动作，本次没有勾选为已实现。精确依赖以全局计划为准：ENG-T01与VAL-T01可以先后独立完成，再ENG-T02→ENG-T03。

### 4.1 开始与边界证据

- [ ] 读取上方Spec链接及实施计划§1—5.1，执行git status --short、git log -1、git branch --show-current，记录开始提交和已有工作区差异。保护已有改动，不整库reset。
- [ ] 先运行既有离线基线；不安装依赖，不连接真实供应商。若环境缺依赖，报告具体缺项，按用户授权处理，不能把未运行写成通过。

```powershell
py -3.13 -B -m pytest -q -p no:cacheprovider
```

### 4.2 ENG-T01：类型/版本库

- [ ] 先创建有反例的测试：bool不充Revision、坏UUID/未知字段拒绝；同版本重复/跨类型引用拒绝；旧对象原字节及幂等响应可重读；事务中间异常无半事实；未知同名条目不误并，已定位槽唯一。
- [ ] 先运行目标测试确认因缺包/接口或行为失败，再最小实现。不能把测试文件不存在当作回归通过。

```powershell
py -3.13 -B -m pytest tests/product/test_repository.py -q -p no:cacheprovider
```

下面是primitives.py中VersionHeader的最小严格性反例，放进该测试文件；后续仍需加入版本库/引用/回滚的行为测试。

```python
import pytest
from pydantic import ValidationError
from game_app.contracts.primitives import VersionHeader


def test_revision_rejects_bool():
    with pytest.raises(ValidationError) as caught:
        VersionHeader.model_validate({
            "series_id": "00000000000000000000000000000001",
            "version": True,
        })
    assert any(error["loc"] == ("version",) for error in caught.value.errors())
```

实现该类型的最小形状如下；这里只落实shared定义，不意味着存储任务完成：

```python
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ID = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]


class VersionHeader(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    series_id: ID
    version: Revision
```

- [ ] 实现固定对象/聚合快照、references、版本唯一性和显式事务；每连接核对foreign_keys=ON。0.4根快照和目录索引按data §12.2/07 §4准备；事实载荷只追加，头/索引是投影。
- [ ] pyproject包发现同时包含game_collector*与game_app*，旧game-collector入口保留；在本地已有构建工具可用时做wheel内容检查，缺构建工具则明确该项受阻，不联网补装。记录构建产物位置，不能声称仅源码可导入就是分发通过。
- [ ] 目标测试及受影响既有回归通过后，只提交本任务实际文件与脱敏证据。

### 4.3 VAL-T01：冻结夹具和结果

- [ ] 用08模型实现manifest/label→UUID映射、hash损坏拒绝、run_id追加保留旧失败。夹具全部在tests/fixtures/product或临时副本，不改根目录金样本。先失败，再最小实现，再跑下列命令。

```powershell
py -3.13 -B -m pytest tests/product/test_fixture_loader.py -q -p no:cacheprovider
```

- [ ] 报告区分static_design/offline_fixture/integration/local_effect及passed/failed/not_run/blocked。本轮基础类型/本地规则只能证明其负责部分；完成后按任务提交。

### 4.4 ENG-T02：幂等、等待与恢复

- [ ] 先用临时库和SpyProvider写同key并发/异hash、提交后丢响应、旧版本key、墓碑、queued重启、等待释放槽、取消先提交及无checkpoint但有未知调用的测试。
- [ ] 最小实现07四个任务函数及输入/首事件/原响应同事务；外部调用在可能发送前持久in_flight与预算。终态不复活；恢复创建parent任务，核对父链全部调用，不因checkpoint=null重发未知调用。

```powershell
py -3.13 -B -m pytest tests/product/test_tasks.py -q -p no:cacheprovider
```

- [ ] facts_only协调边界保存根/直接源固定引用；可用领域桩核对零源任务写入，不提前声称DEV-T04答案归一/完整更正旅程通过。响应文件真实恢复窗口等ENG-T03补齐。目标断言通过后按任务提交。

### 4.5 ENG-T03：文件与恢复

- [ ] 在临时目录测试文件最终改名后DB前退出、DB后响应前退出、hash错误、缺失/占用/磁盘写失败及备份/迁移失败；只在指定临时资源注入。
- [ ] 最小实现FileOperation prepared/files_ready/committed/abandoned；文件先就绪，短事务原子发布资源/引用/原响应。原key恢复定位原操作，孤立文件不可消费；提交不确定先查key，不重建资源/调用供应商。

```powershell
py -3.13 -B -m pytest tests/product/test_files.py tests/product/test_tasks.py -q -p no:cacheprovider
```

- [ ] 使用SQLite一致备份机制及文件manifest，不能复制活动主库冒充一致备份；真实Windows占用行为单列integration。补完ENG-T02原件窗口后提交。

### 4.6 首切片结束条件

- [ ] 一次运行首切片目标测试及原型回归；没有新变化/失败不反复扩大测试。输出执行过的完整命令、exit code、对象/事件/发送数量、文件hash和未执行项。
- [ ] 同key同资源；事务失败零部分事实；waiting_input释放重槽；未知调用不重发；未发布文件不可消费；0.1—0.3对象/请求/原响应不被0.4改写。
- [ ] 将ENG-V01/02/04/05及B契约中本切片负责的断言标明覆盖部分，未来领域/页面/真实能力仍not_run；不要整条勾选63场景通过。
- [ ] 交付首切片提交/测试证据和剩余事项，再按全局计划进入KNW-T01/DEV-T01。需要新产品选择或真实环境证据时在对应影响点处理，不用它们阻塞独立离线基础工作。

## 5. 后续路线与停止条件

后续直接用全局计划的34任务表/任务卡：文件基础→公共身份/设备/安装/独立基线与导入→知识/推荐、性能两条领域分支→反馈→入口/页面→综合验证/分发。所有任务当前仍未开始；不依据本交接把未来checkbox勾完成。

遇到具体输入/来源/历史约束冲突，保存最小反例并修权威规格；不要用手填ID、预装游戏、默认平台/分辨率、当前值追填历史或自动重发外部请求让演示通过。对门禁未验能力保持禁用/受限，不扩大为全项目不能工作。是否启用真实模型/工具/游戏及预算，按用户指令和各门禁处理。

每个任务按全局计划§6.2保存task_id/spec_revision、开始提交、变更文件、夹具hash、命令/退出码、断言/计数、证据层级、not_run/blocked及提交。提交只纳入对应改动；推送遵从用户指令，不force、不覆盖远端工作。

## 6. 可复制给下一轮的编码指令

> 阅读docs/technical-design/coding-handoff.md、12-phase-d-remediation.md、implementation-plan.md和三份shared契约，按product-contract/0.4开始首个离线基础切片：ENG-T01＋VAL-T01→ENG-T02→ENG-T03。先读当前Git状态，保护已有成果；每任务先失败测试再最小实现，完成对应本地验证和提交。新增game_app并保留旧CLI/公共知识0.1/冻结样本/旧幂等响应；仅使用临时库、临时目录和供应商桩，首切片不联网安装、不调用真实模型、不探测硬件或启动游戏。Q1—Q3/R03保持原状态，不妨碍独立基础部分；不得将领域/浏览器/真实效果用例误记通过。不要重做阶段A—D，也不要新建GSD/.planning或另一套实施计划；首切片后报告实测证据和下一任务。
