# 固定范围的跨批商品覆盖

责任为 #127 的商品覆盖读取组。单个 [商品资格包](industry-goods.md) 最多选择
32 个系列，多个包中的合格数字不能直接相加。本能力固定一组确切原包，
在同一个显式评估时刻重新运行每个原商品读取器，再核跨批身份和来源冲突、
取共同商品身份集合的并集。它不接收调用者提交的合格数字或先前结果缓存。

实现为 `application/industry_goods_coverage.py`，复用现役 SourceInput/SourceRef、
`read_industry_goods`、`industry_source_scope`、原宿主与同一阅读室。
没有新来源目录、数据库、采集器、业务 writer、定时任务或 Brief 采用身份。
沿用现有读取器能保留原比较、来源资格、失败和预算；引入另一套批处理框架或
只把每批结果相加，都不能完成这项具体核验，因此仅增加这一层有界组合。

## 固定声明

根仍是 `SourceInput(kind="content")`，正文严格使用以下字段。
示例中的引用文字不是有效 SourceRef，必须替换为实际保存回执中的完整引用：

```json
{
  "schema": "industry-goods-coverage-v1",
  "prepared_at": "2026-10-21T10:00:00+08:00",
  "as_of": "2026-10-21T10:00:00+08:00",
  "coverage_note": "本次明确声明的商品批次、未决范围和原来源停止条件。",
  "batches": [
    {
      "batch_id": "retained-batch-one",
      "reference": "替换为完整 industry-goods-v1 包的实际 SourceRef"
    }
  ],
  "aliases": []
}
```

| 字段 | 合同 |
|---|---|
| `prepared_at/as_of` | 带时区，整理不晚于截止；每个子包的整理和截止不晚于对应根时刻。 |
| `coverage_note` | 最多4096字符的实际范围说明；不得用数量宣称代替边界。 |
| `batches` | 1–32 个确切原商品包，稳定且唯一的 `batch_id`；不递归引用覆盖包。 |
| `reference` | 完整原 SourceRef；同一内容仅换可达 commit 不构成另一批，重复声明拒绝。 |
| `aliases` | 最多128项有据直接归并；没有必要归并时使用空数组。 |

每个叶包仍保持原8输入、32候选、32系列选择等全部限制。批次不是商品；
32批也不是保证能够容纳任意1024商品，原提交数、读取字节、请求和结果上限仍会限制实际范围。
新范围通过普通 `put-source` 保存新版本，旧根与旧失败不被覆盖。

## 同一商品与跨批冲突

每个叶包先独立核身份、至少两期可比物理观察及原来源资格。
覆盖读取再检查所有恢复候选，包括不在当前显示筛选中的候选：

- 相同原主体不能归属于不同的共同商品身份。
- 同一保留行（忽略等价可达 commit）和同一上游族、完整口径及期间的事实，
  不能被不同商品重复签名。口径键沿原叶包 `_fact_keys`，不另作价格比较。
- 同一 Entity ID 的定义不应在批次之间冲突。
- 相同 `source_id + origin` 的上游族、权利或时效声明不能相互冲突；
  同一上游族的时效规则也须一致。

冲突保留在 `conflicts` 和受影响候选的原因中，相关候选不贡献本次合格或新鲜集合。
已有叶包的 `leaf_status` 保留，覆盖结果的状态另列，不能改写原叶包的结论。
其他无冲突商品仍可以进入明确范围的集合，整组保持 partial。

跨批不同已有商品 ID 需要归并时，`aliases` 每项只允许
`commodity_id`、`canonical_id`、`evidence` 三个字段。两端均须是在本次原叶包
实际恢复且有观察绑定的 commodity Entity；证据用1–4个确切 content lines Ref，
实际引文和证据元数据须绑定两端。其格式沿原商品包的
`{reference, quote, note}`，不接受目录 `record_id` 投影代替别名证据。
只允许直接指向最终根；A→B→C 与环保留为错误，不推断或修补。

名称相近、共同发布者、地区、品种、等级、报价带或统计族本身都不证明同物。
程序检查引用和声明的一致性，不能独立认证整理语义或来源权利。
需要人工或责任人的具体裁决仍在原任务留证；未知归并保留未决，不能为达到数量而补别名。

## 时效、集合与500门槛

调用者必须提供带时区的 `evaluation_at`，不早于本包 `as_of`。
所有叶包使用同一时刻；它不由 Room 议程时钟或本机“现在”自动代入。
原观察期末、发布、取得、整理和读取时刻分别保留。
新整理和重新读取不会令旧观察变新；来源日龄上限仍为1–20天，未知时效仍为 unknown。

| 输出 | 含义 |
|---|---|
| `scope.declared_batches/read_batches/unavailable_batches` | 完整固定批次分母与实际恢复情况；缺包不算零商品。 |
| `scope.unavailable_input_packets` | 已恢复商品叶包中仍不可用的原阅读包总数；叶根存在不代表所有依赖已恢复。 |
| `scope.candidate_records` | 已恢复叶包的候选记录数；若有缺包，它不是所有声明批次的完整候选总数。 |
| `sets.candidate_ids` | `[batch_id, candidate_id]` 二元身份，避免不同包中的同名候选互相覆盖。 |
| `sets.comparable_series` | 原读取器恢复的唯一可比系列身份；不是商品数量。 |
| `sets.qualified_goods` | 通过原叶资格和本次跨批核验的共同商品 ID 并集。 |
| `sets.fresh_goods` | 合格商品中至少有一条原合格系列在规则时效内的并集。 |
| `sets.freshness_unknown_goods` | 合格但时效未成立且没有其他新鲜系列的商品集合。 |
| `global_qualified_goods/global_fresh_goods` | 始终 null；这组固定包不能证明全市场完整覆盖。 |

`quantity_milestone.status` 仅在全部声明批次及其原阅读包恢复、跨批冲突为零、
未决候选为零且至少500个不同商品合格并在规则时效内时，成为
`established-in-declared-scope`。否则为 `not-established`，逐项给出原因。
明确排除和具名阻塞仍在分母与结果中，不以其数量补充合格商品。

这个状态只证明当前声明范围的数量条件，不证明新期间已自然取得、
来源持续运行、全市场覆盖、研究/Brief实际采用、人审或 #127 可以关闭。
真实增量、正常消费者和运维交接仍须按原任务分别验收。
原单个 `industry-goods` 的数量门槛保持未成立，不改变其既有合同。

## 紧凑结果与正常消费者

覆盖结果保留每批根、原 scope、状态和 `view_version`，每项候选的完整
紧凑 outcomes、来源声明、别名依据、冲突和恢复条件。它不重复嵌入每个原数值与公司角色。
每批的 `open_request` 使用原 `industry-read`、`view=industry-goods`、确切引用与
相同评估时刻；从该请求可恢复全部原价、规格、比较、引用和失败。
没有截断叶包、用摘要冒充原数值或从另一个来源补齐缺项。

正常 CLI：

```bash
python -m decision_kernel.interfaces.industry_cli --binding config/store-binding.json --ref coverage-ref.json --industry-goods-coverage --evaluation-at 2026-10-21T10:00:00+08:00
```

原 `research-runtime.yml/industry-read` payload 或 `reading-room` 的 `plan.industry`
选择 `view=industry-goods-coverage`，提供完整根和 `evaluation_at`。
可用 `subject_id` 按原主体、叶商品或共同商品 ID 筛选显示；所有计数、outcomes、
集合和 `view_version` 在筛选前形成，不会因零命中变成零覆盖。
不支持跨批 `company_id` 查询：需要公司角色时打开对应原叶包，沿其原证据方向读取，
不能把同物归并升级为跨包公司暴露。错误在 Store I/O 前拒绝。

同一阅读室显示范围计数、数量条件、完整冲突和缺项、每批原请求，支持本地文字与
资格状态筛选。下载 JSON 保留本次完整结果；导出的请求不预填公开许可或代码 SHA，
导出也不执行来源请求。公开权利与最终可见性检查继续由现役宿主负责。

## 共享预算、终态与维护

整次读取只开一个原 `industry_source_scope`，共享确切 SourceRef 缓存。
子读取器不新建后端会话或重置原32个 commit／512请求预算；
累计读回最多16MiB，完整结果最多2MiB，均在筛选前核验。
普通缺包保留 partial 和全部声明批次；原绑定/权限/作用域终态、预算耗尽、
StoreUnavailable 或作用域退出失败阻止整次结果导出。
首次终态即停止后续读取，即使叶读取器暂将其记作普通不可用也不能解除停止。

本组维护领域模块、原产业CLI/宿主/Room入口与页面，专属领域和宿主测试为
`tests/test_industry_goods_coverage.py`、`tests/test_industry_goods_coverage_room.py`。
安装验证只扩展原唯一 cold-wheel 消费者，不建第二安装环境或新的CI责任。
合成测试用于证明去重、冲突、缺失、20日边界、共同预算和消费者合同；
合成500商品规模不是真实500商品验收。

退出时用普通PR移除这个可选视图、模块、页面及专属测试/文档接线；
保留原商品读取器、历史 SourceInput/Ref、共享保护、来源停止条件与已保存结果。
不修改依赖、工作流、保管绑定、部署授权或原来源预算。
