# 有序条件、价格边界与经营证据

责任 #60；复用[原 Watch](odds-watch.md)、[活动管理](watch-activity.md)、原 Odds 算术与来源/Artifact 保管。`odds-watch-v2` 在同一冻结研究、同一窗口和同一政策下检查多个条件。原 `odds-watch-v1` 的定义、独立结果和活动历史继续按原语义重演；不自动升级任何已存 Watch。

## 1. 同一 Watch 中保留每个条件的真值

v2 沿原 `watch_id/basis/activation/window/data_policy/effect/limitations`，将单个 `condition` 改为 `conditions` 数组，并必须提供 `combination: "any" | "all"`。数组限 1—16 项，各有唯一 `condition_id`；顺序是冻结时的声明顺序，不表示投资优先级。

同一次合格价格/概率恢复只计算一次，随后比较各条件。`any` 在有一项明确 true 时为 true，全部明确 false 时为 false，否则未知；`all` 在有一项明确 false 时为 false，全部明确 true 时为 true，否则未知。任一前提须复核时，组合转为 `review-required`。逐行未知仍显示，不能由组合的 true/false 抹掉。

原 `window.end_after_trigger` 的 true 在 v2 指**组合曾明确满足后结束**，按显式 `combination` 决定。它不自动变成最后一个层级、通知一次或完成经营事件。原启用、窗口、最小间隔、固定目标日、现金日期及暂停/退役规则继续适用；修改定义需要新 Watch。

## 2. 数字条件和下一道边界

普通数字行使用原指标及单位：

```json
{
  "condition_id": "declared-price-boundary",
  "metric": "price",
  "world_id": null,
  "comparison": "lte",
  "threshold": "100",
  "basis_note": "仅为合成工程示例，不是任何公司的投资门槛"
}
```

`comparison` 为 `lte/gte`，阈值保持有限十进制字符串。原世界指标必须绑定同一冻结世界；当前公允价值仍不能作为未来回报。概率/回报使用原小数单位，`0.2` 表示20%，不自动年化。

区间使用 `comparison: "between"`，以 `lower/upper/lower_inclusive/upper_inclusive` 替代 `threshold`。端点均明确，不默认为开区间或闭区间；上下反向、空区间、重复ID和非法数值拒绝。不同条件允许重叠，它们各自检查。

结果 `next_boundaries` 按同一个 `metric/world_id` 分组，分别给出当前值以下、正好相等及以上的最近**已声明**边界，含原条件ID、端点角色、是否包含及精确距离。价格、各世界回报和概率不混排；未取得可用值时边界为未知，不沿用上一次的当前值。没有声明的方向返回 null，不编造下一档，也不据此给出买卖指令。

## 3. 经营条件及真实读取的证据

经营行形状为：

```json
{
  "condition_id": "operating-evidence",
  "metric": "evidence",
  "condition_text": "具名产线是否已在所述期间开始交付？",
  "required_evidence": "标明产线身份、期间和交付事实的发行人披露",
  "basis_note": "由研究者判断披露事实是否满足此条件"
}
```

`basis.kind: research` 可只做经营证据或原研究版本检查，不要求行情或 Registry。条件/概率基础也可把经营证据与价格放在同组；仍冻结原研究/spec/assessment，不能临时填概率。

一次 v2 检查仍用原 request 外层。`input` 改为：

```text
{schema: "odds-watch-input-v2",
 price: 原Odds价格/FTShare/留存行情输入或null,
 evidence: [{condition_id, observed_at, assessed_at,
             conclusion: "met"|"not-met"|"cannot-determine"|"review-required",
             reason, sources: [确切SourceRef]}]}
```

每个经营条件至多一个提交判断，每项最多8个确切来源，整次最多32个；不能给未知条件补数据。`observed_at <= assessed_at <= checked_at`，判读不早于 Watch 的启用声明。来源 manifest 与完整保管表示必须在所声明 `records_snapshot` 中存在且相同，再由 `VersionedStore.read_source` 实际读取原表示与选段。切面缺少任一部分时不读入后来的正文；摘要或原文被改变仍报完整性错误。

保管时间及已知取得/披露时刻均不得晚于判读。只有日期而没有来源时区的时间按保守区间判断，不填造午夜；同日等无法确定先后的情况保留未知。结果给出 `clock_qualification`、原取得/披露/报告期、选段字节数和摘要及来源限制。只有网址定位、无已存表示、缺来源或时点不合格时，明确 true/false 的声明也只能得到 `cannot-determine`；原件损坏、权限/传输及预算错误继续报错。切面成员和这些已知时间关系不代表历史PIT或最新信息已认证。

有合格来源的 `review-required` 记录证据触及原前提，需要研究复核；不更新原股数、现金、世界或概率。普通的 `review_reasons` 仍是原调用者说明。研究current已变化时不把旧价格模型继续算成现行判断；原检查的历史读取仍成立。

来源可读、判读有明确依据和经济判断正确是不同事实。程序证明保管表示及选择确实被读，并重演所提交的判断；**不会自行理解披露是否构成履约，也不代签经济充分性或人工接受**。观察、判读、保存是不同时间，重新判读旧材料不会把材料日期更新为今天。现版本不提供自动证据新鲜度或经营事实真值认证。

## 4. 逐条件状态与活动连续性

`odds-watch-check-v2` 保留原组合级 status/condition_met、时钟和 effects，新增 `condition_results`、`combination`、`next_boundaries`。每个条件独立保存首次/持续/不满足/无法判断/须复核/结束、最后明确真值、曾触发及未知间隔。组合持续成立时，另一条件仍可以是首次满足或重新进入；不能用组合状态覆盖逐项状态。

暂停不产生 false，不清空任何条件；恢复将组合及每一项的中间时段标为未知。管理摘要 `condition_summaries` 的各项检查/首次/持续等计数全部从原事件重演生成。导入旧独立 seed 只继承控制状态，不把原seed检查数伪加到新管理链。

继续使用原完整 Watch 摘要对应的唯一活动指针，事件、指针及日志同次原生CAS。已登记活动不能绕到独立入口执行，陈旧事件/快照冲突，退役不可恢复。历史变更/检查引用保持，原32前驱、128管理事件与512远端请求预算不提高。

## 5. 正常命令、议程与 Brief

原 `freeze/check/save/read-odds-watch` 和活动命令直接接受 v2，无第二套命令或调度器。显式 `research-runtime` 的原权限、稳定意图、unknown-write对账及全新reader验证继续；没有新增secret、schedule或通知效果。

[议程](../delivery/research-agenda.md)可显式选择已经保存的检查/活动事件：

```text
watch_evidence: [{reference: 原研究确切引用, question_id: 原问题ID或null,
                 kind: "check"|"activity", check_ref: 已存检查或活动事件引用}]
```

引用先经原 Watch reader 完整重演，再核确切研究和原问题。只保存冻结的检查时刻、输入和结果；现时 `current_use/current_activity` 不进入历史投影。未知、缺失、超出当时切面或显示容量的项继续有确切引用和状态，不冒称条件false；活动登记/暂停/退役本身没有新增检查。

不含该选择的旧议程与旧 Brief 输出保持原形状。显式新版议程把条件证据附在原研究卡，不改用户 choice/attention，也不自动开启研究。Brief 使用原议程消费路径，只有条件文字实际进入同一份 `brief_markdown` 时才记采用；未采用、重复或容量省略保留理由。确切新检查可与同一研究原文分别比较，不能把所有后继检查都当成同版本重复，去重范围仍是指定前版 Brief。

正常阅读室显示逐条件状态、原检查日期、上下边界和原证据读取请求。公开宿主对条件判断及来源分别核原feedback/sources导出许可，包括继承和省略引用；同仓私有阅读沿现役受信可见性检查。页面只读，不启用旧活动或发送通知。

## 6. 复用、验证及退出

数值继续使用原80位Decimal、时钟和 Odds 资格；来源、Artifact、活动日志和CAS继续由原owner维护。没有规则DSL、金融库、第二个活动注册表或资料库，已核标准库与现役原语足以支持这些有限比较。

专项保护多阈值/区间端点、异质指标分组、any/all与未知、逐项重入、暂停间隔、有据经营复核、原件损坏及伪造结果/摘要拒绝，并实际通过正常宿主保存/空reader、议程与Brief正文和原唯一cold-wheel。合成测试不为真实公司提供新阈值、概率或判断。

本组不扩大历史预算，不交付无限运行、通知或资本执行。退役时同步退出 v2 专属domain/application、议程/Brief和页面接线及对应测试；原v1、共享资料保管和已存v2历史读取责任明确转交，不删用户数据。
