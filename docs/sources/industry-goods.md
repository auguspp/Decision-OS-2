# 有限商品身份、资格与时效读取

责任为 #127 的有限 G1 工程组。此能力在同一 records 中恢复原产业阅读包，
把候选记录、可比系列、去重合格商品和评估时刻仍在时效内的商品分别输出。
它不建立全市场商品库；任何有限批次的结果都保持
`global_qualified_goods/global_fresh_goods = null` 和
`quantity_milestone.status = not-established`。500+ 以及真实增量运行仍须在原事项单独验收。

调用 `decision_kernel.application.industry_goods.read_industry_goods`。
输入和证据沿现有 SourceInput / SourceRef 保管，原数值、口径、来源与公司角色
均由 [原产业读取器](industry-reading.md) 实际恢复。不接受整理者直接提交数值、
角色或 `qualified: true`。不创建采集器、数据库、业务记录 writer、研究或 Brief 采用通路。

## 有限声明及证据

声明是 `SourceInput(kind="content")` 中的严格 JSON 正文；新增或未知字段拒绝，
引用必须是实际接收器返回的完整对象。下例中的文字占位不是有效 SourceRef，
也不是可以直接写入的真实资料：

```json
{
  "schema": "industry-goods-v1",
  "prepared_at": "2026-10-16T10:00:00+08:00",
  "as_of": "2026-10-16T10:00:00+08:00",
  "coverage_note": "本批有据范围；尚未核全市场，未授权新来源请求。",
  "inputs": [
    {
      "input_id": "retained-price",
      "reference": "替换为完整原 industry-reading-v1 包的 SourceRef",
      "view": "industry"
    }
  ],
  "sources": [
    {
      "source_id": "original-source-id",
      "origin": "与原观察 source.origin 完全相同的定位",
      "family_id": "curated-upstream-family",
      "rights": "unknown",
      "freshness": null,
      "evidence": [
        {
          "reference": "替换为确切 content lines SourceRef",
          "quote": "该行实际包含的资格引文",
          "note": "解释与原来源绑定的依据、适用范围和限制。"
        }
      ],
      "reopen_when": "原权利缺口实际解除并取得适用证据后，由原责任接续。"
    }
  ],
  "candidates": [
    {
      "candidate_id": "candidate-one",
      "title": "本批待核商品定义",
      "subject_id": "original-subject-id",
      "commodity_id": null,
      "disposition": "identity-unresolved",
      "identity_evidence": [
        {
          "reference": "替换为确切身份说明的 content lines SourceRef",
          "quote": "该行实际包含的身份、规格或归并引文",
          "note": "说明尚不能确定独立商品及继承关系的原因。"
        }
      ],
      "selections": [{"input_id": "retained-price", "series_id": "original-series-id"}],
      "missing": ["真实身份归并、适用来源权利和时效规则尚未核定。"],
      "reopen_when": "原身份与同源关系有可恢复证据后重新整理。"
    }
  ]
}
```

| 声明部分 | 含义与边界 |
|---|---|
| `prepared_at/as_of` | 带时区，整理时间不晚于包截止；子包准备和截止不晚于本包对应时刻。 |
| `inputs` | 最多 8 个完整原阅读包；仅 `view=industry`，不递归消费 goods/uses，不用目录代替原包。每个输入至少被一个候选选择。同一内容仅换可达 commit 不增加输入。 |
| `sources` | 最多 32 项，按原观察的 `source_id + origin` 精确绑定；`family_id` 声明共同上游，而不是独立来源计数。 |
| `candidates` | 1–32 条，有稳定 `candidate_id`、明确分类及 1–16 条缺项说明；全批最多 32 个系列选择槽。槽、记录、地区或系列的数量均不是商品数量。 |
| `identity_evidence`、来源 `evidence` | 每处 1–4 项。普通证据是精确 lines Ref，实际文本必须包含引文；保留来源日期和解释，单项投影最多 32 KiB。 |
| `missing/reopen_when` | 缺项与恢复条件属于原记录。身份未决、来源受阻，以及 unknown/restricted 权利必须写明恢复条件；读取不会解除条件。 |

可复用原 `industry_uses` 的窄资格记录投影：确切的
`source-qualification-inventory-part-v1` 整包 SourceRef，加 `record_id`，
仅恢复原记录内相同 source_id 的唯一条目，并验证引文。它不接受任意 JSON 路径，
不重新保存旧目录，不把其他记录的权利移入本项。
用于来源资格时，record_id 和实际恢复记录的 source_id 必须同时等于本项声明的 source_id；
不同来源的已许可记录不能移作本项依据。错配保留原证据引用并标为不可用，不贡献合格或新鲜商品。

## 归并、分类与资格的计算

分类由有据整理声明。程序核引用、元数据、可比事实与声明的一致性，
**不会仅从名字或 kind 标签判断物理商品，也不独立认证整理语义、来源原件或第三方权利**。
这与原产业公司角色的有据整理边界相同；结果固定
`qualification=evidenced-curation-and-retained-comparison-not-independent-authentication`。
`original_verified` 仍为 false。

| `disposition` | 处理 |
|---|---|
| `physical-good` | 原 `subject_id` 与归并 `commodity_id` 都必须是实际恢复且有原观察绑定的已有 commodity Entity；须由身份证据元数据同时绑定两者。满足后再核原比较与来源资格。 |
| `statistical-aggregate` | 明确排除统计总体、总量或统计族；保留原系列，不贡献合格商品。 |
| `service` | 明确排除服务费率；commodity 标签不能把运费变成物理货物。 |
| `identity-unresolved` | 归并未决，commodity_id 必须为 null；原观察与重开条件保留。 |
| `source-blocked` | 没有系列选择，不提供或代填报价；具名证据、缺项和原停止条件保留。 |

`identity_resolution` 分开显示原 Entity、归并 Entity、同 ID / 有据别名继承、
处置状态与身份原因。因此身份有据而值缺失，与身份本身未决是不同情况。
多地区、规格、指标、库存及发布时间可能属于同一商品；整理者必须用有证据的
共同 commodity_id，不能因新名称或 Entity ID 增加独立商品。硫酸不同折算口径、
能源总项/分项等未决关系应保持未决；程序不猜测合并，也不自动拆分。

同一原主体不能映射到多个 commodity_id。相同保留行（忽略等价 commit），以及
同一上游族、除主体 ID 外完整 BASIS 相同、相同期间的事实，不能被不同候选签成
不同独立商品；冲突双方保持 unresolved。归并目标必须直接指向最终根，
A→B→C 或环不能把中间身份当新商品。共同上游可以发布不同真实商品；
规格等完整事实不同且有独立身份依据，不因共用发布者而合并。

此判断只覆盖本次声明并实际恢复的有限输入。来源 family 与商品归并关系的语义仍需
整理者核实；代码不能证明未提供的镜像、统计族、别名或其他批次都已去重。
跨批完整集合可以用[固定范围的跨批读取](industry-goods-coverage.md)重新核验；
它重新消费每个原包并检查跨批冲突，不能把若干批次数简单相加。
实际身份、权利、真实增量和500商品验收仍在原事项继续。

## 原始比较、来源权利与时效

每个选择沿原 reader 从实际单行数值计算，保留原 `series`、
`changes/latest_change`。资格比较另列 `comparison.from_index/to_index`，
不覆盖原比较结果：

- 至少有两个不同且实际可比的观察期间。仅同一期的多个修订不构成两期；
  最末期间的连续有效修订可与紧接在前的独立期间比较，仍调用原 `_change`。
  不跨过缺值、不可比点或中间失败寻找漂亮基线。
- 原缺值、未知税基/口径、规格或来源改变、期间回退/重叠、累计代流量等拒绝仍生效。
  NBS 生产资料另保留原相邻统计期要求。
- 指数水平、期货合约及 assessment 中明确标示
  forecast/projection/prediction/scenario 的系列保留可比结果，但不作为本组物理观察资格。
  这些是已知结构/标记的保守拒绝，不是能够识别所有语言、预测或统计语义的分类器；
  其他统计或非观察性质仍须有据排除。
- 必须有与末期实际 source_id/origin 完全对应且证据可恢复的来源声明。
  rights 为 `retained-use-permitted` 才贡献合格系列；`restricted/unknown`
  保留原值、理由和恢复条件。此声明不授予公开保管、传播或导出权。

同一候选有身份证据且至少一条系列满足上述资格，才进入本批
distinct-qualified-goods。另一选择失败仍逐项保留并使整批 partial。
商品合格与新鲜分开；公司查询只裁剪显示，不重新为公司签商品资格或净暴露。

调用者另提供带时区的 `evaluation_at`，不得早于本包 as_of。
不会自动使用系统“现在”或 Room 议程时钟，因此同一确切引用和同一评估时刻可独立复算。
原 prepared_at、发布/取得日、截止和评估时刻各自保留，重读/重新整理不刷新观察期间。

来源 `freshness` 为 null 时，新鲜资格为 unknown；不将其填为零日或记成已过期。
有规则时必须有 `max_period_age_days`（1–20）和明确
`utc_offset_minutes`（−720 到 +840），同一上游族不能有冲突规则。
NBS `nbs-material-prices` 固定中国日期偏移 +480。以评估时刻在该时区的日期
减原最新观察期末日期，年龄为 0 到阈值（含边界）才在规则时效内。
例如合成测试的 9 月 30 日期末，在中国 10 月 20 日年龄 20，
10 月 21 日年龄 21；改整理日期不会改变结果。

`fresh` 为 true/false/null；null 明示资格已满足但时效未知。
同一 canonical 商品只要有一条合格且在时效内的选择才进 fresh-goods；
没有在时效内的选择而存在时效未知选择时进 freshness-unknown-goods。
时效规则只核保存的期末年龄，不证明该发布者没有更新、更晚公告已经取得或任何自然运行窗口完成。

## 输出集合与完整范围

| 输出 | 计数身份 |
|---|---|
| `sets.candidate_ids` | 声明的候选记录；包括未决、排除及具名阻塞。 |
| `sets.comparable_series` | 实际恢复且有两期原可比基线的唯一原选择身份，沿原 `version_id`；可以包含服务/统计等已排除商品类别。它不是独立上游或商品数。 |
| `sets.qualified_goods` | 有据身份、原物理观察基线和可恢复适用来源声明均成立的 canonical commodity_id 去重集合。 |
| `sets.fresh_goods` | 合格商品中在当前明确规则时效内的去重集合。 |
| `sets.freshness_unknown_goods` | 合格但时效没有成立或否定依据的去重集合；与 fresh-goods 不重叠。 |

`scope` 同时列候选、唯一选择、可比系列、合格/新鲜/时效未知商品、未决/排除/阻塞、
不可用原输入数，以及两个全范围 null。每项的 `outcomes`、
`input_outcomes`、原整包 scope、原关系失败与来源资格全部保留。
明确排除本身可以是已完成的有限处置；partial 不等于零商品，read 也不证明全市场达标。

先恢复每个原包完整 scope 和原 reader 结果，再选择系列并核全批身份/容量。
主体/公司查询只改变 `entries` 中返回的显示内容；全部集合、outcomes、
原输入范围和 `view_version` 在查询前形成并保持不变。公司查询只沿同一个 input_id
中实际恢复的公司角色选择系列，不把另一个包的同名观察移给该公司。
历史报告期、传导反证与未知业务份额/弹性/净受益沿原结果，不填零。

## 原正常消费者

同一产业 CLI 加入互斥视图 `--industry-goods` 和该视图必需的时刻：

```bash
python -m decision_kernel.interfaces.industry_cli --path dedicated-synthetic.git --ref goods-ref.json --industry-goods --evaluation-at 2026-10-20T09:00:00+08:00
```

正常绑定用原 `--binding` 替换合成 `--path`；不增加确认接口。
`--subject-id/--company-id` 沿原互斥规则，partial 保留 JSON 并返回原退出码 2。

原 `industry-read` payload 或 Room 的 `plan.industry` 使用
`view: industry-goods`、根 `reference` 和必需的 `evaluation_at`；
跨批覆盖视图也须提供此时刻，其他视图不接受该字段。实际读取准入及公开来源输出授权全部复用现役宿主，
本声明的 retained-use 权利不能替代它。Private 历史产物也不是 Public 资料权利。

[同一阅读室](../delivery/reading-industry.md) 显示四组数、时效未知、全范围未知、
原数值/期间/比较、身份依据、原公司角色及完整缺项。
本地搜索、主体/公司和资格状态筛选不会修改结果；空命中不能把全市场或公司暴露记零。
下载 JSON 保留完整本次结果，导出查询保留确切根、评估时刻与主体/公司方向。
文字搜索与状态仅为本地显示，不伪装成后端查询。
逐来源按钮导出 `operation=read-source`，payload 直接是完整
`{manifest, selection}`，不再包一层 reference，不预填新的公开许可或代码 SHA。

## 原预算、失败和维护

同一原 industry_source_scope、VersionedStore、GitStore/GitHubStore 与 API 会话。
最多 32 个原 commit scope、512 请求及宿主总限额不增改、不重置。
本组合最多读回 16 MiB，单项资格投影 32 KiB，结果 2 MiB；
完整结果在查询前检查容量。缓存仅复用相同确切 SourceRef 的同次读取，
不请求新来源网页或产生业务写入。

普通缺包/不可比允许有限 partial，原失败分支仍在。StoreUnavailable、
现役绑定/权限/scope 终态 RecordError、整次读取/结果预算以及所有 scope 退出失败，
均阻止结果导出。子 reader 暂时把失败记成不可用也不能解除整次终态，
首次终态后不再请求其他来源。原 Room 的终态传播属于共享维护边界。
原 READING_SCOPE_CLOSED 与 SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED 也按整次终态保留同一错误；
普通 MissingObject 仍是局部缺失，不因此升级为整次作用域失败。

维护本模块、四个原产业入口/页面接线与专属
`tests/test_industry_goods.py`、`tests/test_industry_goods_room.py`；
正常安装消费继续扩展原唯一 wheel 安装 helper，不建第二安装试点。
合成验收包括身份/同源冲突、两期/修订、NBS 20 日边界、缺值/排除/未知、
完整 scope、公司原输入隔离、裸来源按钮、原生一次 403/退出失败无导出和独立进程恢复。
这些是工程验收，不是已取得真实值、权利认证、人审、运行窗口或 500 商品完成证据。

退役通过普通 PR 退出此可选视图、专属页面和测试接线；保留原 reader、
SourceInput/Ref、历史记录、共享宿主保护及原来源停止条件。
DDR5、SCFI、NOAA 原资格/失败预算不因本能力、新批号、换 Public 或插件可用而重开。
