# 收盘价格结构：原来源的有限只读消费

本组让现有 `decision-os store` 与统一阅读室消费行业、概念或独立个股的确切已存来源，显示前 N 个已完成交易日的收盘区间，以及须右侧相邻收盘确认的局部高低点。它属于 #147 的有限价格结构责任组；不把集合竞价、分钟 K、缠论、Wyckoff 或完整盘中链路改写为已完成。

## 复用与边界

`application/close_structure.py` 复用 `MarketDiscovery` 的原 core：THS 行业/概念仍由原 catalog、calendar、index query 恢复；个股仍由 FTShare collection、原 master、calendar、原始或前复权 capture 恢复。Whole SourceRef、同一 repository、原读预算、来源 hold、完成时钟、缺数与隔离身份继续由这些已有合同约束。未新增 source provider、最新版本选择器、来源请求或保管库。

纯观察复用 `observations/market_daily.py` 的日历、价格校验与独立 Decimal context；区间、严格三点的直接计算不需要新的第三方依赖。规则参考已经核过的 [旧 Kernel 收盘结构合同](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/d-market-expression-v1.md)，只继承本节相应数学定义与可复现边界，不沿旧工程队列施工。

没有改动原 `close-path20-positive5-accel5-v1` 策略、Watch、议程的判断或写入流程。观察没有投资执行权，不写新 Artifact、资料、研究或正式 Decision，也不发通知。

## 有限计算口径

对于最后一个已完成交易日 D 与窗口 N，使用原日历选择 D 之前的 **N 个交易日**，求这 N 个收盘价的最小值与最大值，再比较 D 的收盘。D 不参加原区间的计算；需要的 N+1 个日期缺少任何一个收盘时，此窗口为 `missing-market-sessions`。日历不足时保留 `insufficient-calendar-history`。不按收到的 N 条价格倒推日期，不填前值、不拼接、也不从端点虚构中间路径。

相等边界分别显示 `at-high`、`at-low` 或 `at-flat-range`；严格超出才显示 `above-range` / `below-range`。相对高低边界的百分比只是收盘比值，不是预期收益或突破交易信号。

三点规则只比较原日历中相邻的三个已完成交易日。中间收盘严格高于左右两点才是局部高点，严格低于左右两点才是局部低点。相等平台不形成严格转折。每个确认保留 `turn_session`、`confirmed_session` 与三点收盘；任意缺点均不跳过后拼成相邻三点。末端的候选没有右侧收盘，`confirmed_session` 永远为 null。窗口边界之外的历史不作完整性承诺。

确认日是右侧原收盘所属的交易日，**不是系统首次得知时间**。从已存来源重算不会产生事前观察、提醒或 PIT 证明。相同已存价格前缀得到相同的已确认三点；更长窗口内新增的右点只确认尚未确认的中间点。

## 来源资格、历史长度与重述分开

现役 core 为诊断可能保留尚未合格的 `prices`。本消费者明确区分以下状态，不能因为价格非空就直接计算：

| 输入事实 | 本组行为 |
|---|---|
| Source hold、捕获晚于读取时钟、未完成收盘、隔离身份、原请求失败等来源原因 | `source_qualification.status=unavailable`，`observation=null`，保留原 reason、引用与 hold；不会把诊断价格输出为数值判断。 |
| 原发现策略的 20/5 日历史不足或缺口 | 来源资格不因该策略门槛降级；分别检查本次区间与三点所需的原日历与价格。短而完整的窗口仍可读。 |
| 本次 N+1 日窗口缺少日期或日历短于 N+1 | 只限制该窗口；其他完整窗口和不跨缺口的三点可以保留。 |
| 原 catalog 中未请求、缺失、隔离的身份 | 保留实体行与 reason，仍计入原分母；不消失、不充当零变化。 |
| 原根不能恢复，或选错来源族 | 原分母、完整数为 null，视图 `unavailable`；不改读别的旧根。 |

可选 `previous` 必须是明确的旧 SourceRef 与旧读取时钟。每个当前实体和其确切旧实体使用现役 `market_state.comparable` 检查：原口径或身份范围改变、日历改变、任一重叠历史价格重述、数据日期倒退，以及中间未观察交易日分别显示。比较不足不补造连续事件；当前版本自身合格的区间仍可以展示，但不会和旧版本拼接为一条历史。前版范围和离开当前范围的身份数量保留在 `previous` 中。

本组检查的是指定来源的截止日与完成资格，不认证它已覆盖今天的市场、长期运行情况或全市场。页面同时显示数据截止日和声明读取时钟；#10 的当前源运行与 freshness 验收仍走其原 owner。

## 读取合同

`CloseStructure(store, registry=None).read(plan)` 与 `open_structure(store, plan, registry=...)` 返回同一个只读面板。`registry=None` 使用受信宿主代码 checkout 中的 `docs/sources/relay-registry.json`，与现役 `read_market` 的方式相同；来源正文不能指定 registry 路径。离开 checkout 的本地 CLI 应显式传受信 `--market-registry`。

计划字段：

| 字段 | 约束 |
|---|---|
| `schema` | `close-structure-plan-v1`。 |
| `kind` | `sector`、`concept` 或 `stock`；一个确切原来源范围。 |
| `reference` | 已存在的完整 SourceRef；不接受行/列选择或 latest 替代。 |
| `as_of` | 含时区的声明读取时钟；来源捕获与完成资格按此核。阅读室内不得晚于外层读取时钟。 |
| `adjustment` | 仅 stock 支持 `raw` / `forward`，默认 `forward`。不转换原价或生成复权因子。 |
| `windows` | 1–4 个不重复正整数，各不超过 250，默认 `[5,20,60]`。 |
| `turn_sessions` | 3–64 个已完成交易日，默认 20；只展示这个有限范围内的严格相邻三点。 |
| `previous` | 可省略；若提供，包含原 `reference` 与 `as_of`，stock 可显式给 `adjustment`，否则继承本次口径。旧时钟不得晚于本次时钟。 |
| `entity_id` | 可省略的精确实体筛选；沿来源 namespace，不按公司名称或相似代码猜测。 |
| `offset` / `limit` | offset 为 0–10000；limit 为 1–50，默认 20。 |

原分母、来源资格、每个 N 窗口的完整/缺失计数及历史比较计数都先覆盖原目录，再作查询与分页。缺失实体和普通实体使用同一个按 `entity_id` 排序的分页序列；只读一条可计算实体不会把整组变成完整。`view_sha256` 绑定全范围观察、精确输入和每个实体行摘要，不随页码或实体筛选改变；它是重算摘要，不是另一个保存回执。

面板最多 768 KiB。超出时明确返回阅读室的 `not-included / STRUCTURE_DISPLAY_BOUND`，保留确切读取计划和原根请求；不交付截断后冒充完整的结果。可通过较小的 limit 或 turn_sessions 重新请求，原分母仍保持。

## 正常消费者

在已有安装环境中，用主 CLI 读取保存到本地的计划：

```bash
decision-os store --path /absolute/synthetic.git read-close-structure \
  --input /absolute/structure-plan.json \
  --market-registry /absolute/checkout/docs/sources/relay-registry.json
```

生产读取沿已有受信 `--binding`，不使用 `--path` 建第二个生产库。此命令不创建输出档案或远端记录，只返回该确切计划的 JSON；部分结果以面板的 `partial`、视图 status 和全范围计数表达。

统一阅读室在原计划中添加 `structure: <上述计划>`，继续使用原 `research-room` CLI 或宿主 `operation=reading-room`。**没有注册新的宿主 operation**，也没有改 workflow 权限、调度或 writes 列表。

原宿主在生成输入 artifact 之前核当前原生可见性。private 的一方向升级可沿已有保管权限读取；public 必须另有 `public_research_custody_confirmed=true` 与 `public_sources_custody_confirmed=true`，不是把 `rights_confirmed`、私有状态或历史保管许可充作公开再分发许可。原 reading scope 和宿主在所有组件读完后复查可见性与可达性；private 撤回为 public 时不释放输入、模型、价格正文或 HTML。

本次来源读取遇到原生 HTTP 拒绝、限流、原读预算耗尽、绑定或 scope 失效时，立即终止整个读取输出；不降格为局部缺口后继续请求。窄适配只在原 VersionedStore/backend 上锁存这类失败，保留同一个 API 预算。普通原件内容校验失败或数据不足仍按本组件 partial/unknown 表达，显示容量限制仍为 not-included。

页面只下载原 `roomRequest({...D.plan, structure: ...})`，原件使用宿主既有 `read-source` 的裸 SourceRef payload。页面不自动补公开权利、不提交任务。不存在 structure 计划时不触发本组件 I/O，不添加面板或改变原阅读模型。

## 验证与仍需实际消费

`tests/test_close_structure.py` 保护区间边界、严格确认日、缺口不移窗、前缀不回写、Decimal 精度隔离、原范围分页、确切历史重述，以及真实保管形状的 FTShare 原价、前复权、source hold、晚到 capture 和 master 隔离。

`tests/test_close_structure_room.py` 在真实临时 Git 图与原 GitHub API 适配器模型上验证正常 native private 阅读室、default registry、同一读预算与单次来源快照、公开权利前置拒绝、撤权末次拒绝、无面板兼容、HTML 文本隔离和原主 CLI 的新进程消费。传输和来源数据为合成输入，测试不调用真实源、不产生业务资料。

这些是本责任组的团队自查。main 上适用 CI、真实 #10 当期来源的正常阅读室/CLI 消费、真实来源范围与数据充分性、以及用户对经济解释的接受各自需要对应证据，不能用合成测试替代。#147 的其他盘中、全球资产、商品、IMA 与实质研究目标继续保留其原验收范围。

维护归属是价格结构消费者。能力存续期间保留本组观察、主 CLI、Room 权限与来源资格回归；真正退役时同步移除新增命令、面板、文档与专属测试，原行情、Watch、阅读室和共享权限检查继续有效。
