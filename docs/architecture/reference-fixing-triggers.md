# 引用即固定的触发点与口径

本文件回答 #221 工作包 C 的第二条：**Research / Odds / Watch / Brief 各自的「引用即固定」触发点、最小价格切片、raw/qfq/因子口径与失败处置**。它不新造机制，只把既有机制的责任边界写死，并标明哪些消费者目前无法履行。

## 1. 为什么需要这条规则

代码已经写出这个问题的边界。`conditional_odds.compute` 的每个结果都追加：

```
PRICE_CONTEXT_DOES_NOT_ESTABLISH_HISTORICAL_PIT
```

即：**价格上下文不构成历史时点事实**。原因是页面与索引读的都是 `latest-saved`、`latest-ai-brief`、`latest-hosted-handoff` 这类「最新」指针。今天读今天没问题；一旦要回答「当时那个判断基于什么」，指针返回的是**后来修订的值**，而且不报错。

由此得到的唯一有效规则是时序约束，不是存储约束：

> **固定必须发生在写入判断之前。事后补固定，取回的是「现在的历史」，不是「当时的历史」。两者字节不同，且事后无法分辨。**

## 2. 现状实测（按消费者）

不假设，逐项读代码确认。

| 消费者 | 现有绑定机制 | 强制级别 | 真实缺口 |
|---|---|---|---|
| **Odds** | `price.evidence.source`（SourceRef）+ `value_locator`/`security_locator`/`date_locator`，`_price_evidence()` 实际 `read_source` 并逐项比对 | 证据**可选**；缺失时降级为 `declared-reference-only` | `save-odds` 允许仅声明路径落库 |
| **Odds（合格路径）** | `market` + `qualify_close()`（`odds_market.py`），要求 `FTSHARE_CLOSE_SCHEMA`、`RelayRegistry`、`market_policy`，产出 `source_references` | 走该路径时**强制** | 与仅声明路径并存，调用方可自行选择弱的那个 |
| **Watch** | 复用 `FTSHARE_CLOSE_SCHEMA` 与 `retained_close_read_scope`，`odds_watch` 有 exact-source 记忆 | 走合格路径时强制 | `result["price"]["as_of"]` 本身只验时序（`<= activation`） |
| **Research** | `ResearchProgress.sources: tuple[SourceRef, ...]`，且 `UNSOURCED_LIMITATION_REQUIRED` 要求无来源必须给 limitation | **强制有引用或明确 limitation** | `sources` 只证明「带了哪些引用」，**不校验被引用的价格日期是否落在这些引用的覆盖范围内**；L1 之后该判定仍然不可做（见 6.2） |
| **Brief** | 无 | **无** | `hosted_handoff.py` 没有任何 SourceRef 字段 |

结论：**Odds / Watch 的机制已经存在且会真实回读**，不需要新建；缺的是「弱路径是否允许落库」。Research 是「有引用但不绑定切片」。Brief 是完全缺失。

## 3. 触发点定义

固定操作（`put-source`）必须在下列写入操作**之前**完成。写入操作本身不得触发取源。

| 消费者 | 写入操作 | 触发点 | 说明 |
|---|---|---|---|
| Research | `checkpoint` | 早于 `checkpoint` | `checkpoint` 携带 `progress.sources`，引用须已存在且可读；`deliver` 只发布既有 revision，不新取源 |
| Odds | `save-odds` | 早于 `freeze-valuation` | 冻结研究时即确定其价格证据；`save-odds` 落库前 `_price_evidence` 必须已执行 |
| Watch | `freeze-odds-watch` / `save-odds-watch-check` | 早于 `freeze-odds-watch` | 条件冻结时固定基准；**每次 check 的两个时点各自固定**，否则「变化」无参照 |
| Brief | `save-hosted-handoff` | 早于 `save-hosted-handoff` | 保存前其正文实际引用的行情/简报须已在 records 固定 |

一条补充：**跨事务补固定不算固定**。若判断已写入，之后才补固定，须按新历史写入并在回执中标注「引用晚于判断」，不得回填成似乎当时已获得。

## 4. 最小价格切片

固定的对象是**被引用的那一段**，不是整份响应，也不是全市场。

| 项 | 内容 |
|---|---|
| 标的 | 判断实际引用的 security 集合，不是 catalog 分母 |
| 日期 | 判断实际引用的 trade_date 集合，不是计划窗口全集 |
| 口径 | raw 与 qfq 按实际使用者分别固定；同一判断内不得混用两种口径 |
| **因子** | **qfq 必须同时固定当日 `adj_factor`** |

因子的要求不是可选的。视图层 `ftshare_collection_inputs.py` 硬编码

```
factor: {status: "not-provided", reconstructed: false}
anchor_status: "not-established-by-provider-response"
```

含义是「该视图载荷不带因子行、单次全区间查询无端锚」，**不是供应商不提供**——`market_collection.py` 的 `APIS = ("daily", "adj_factor", "suspend_d")` 表明主采集本来就在拉 `adj_factor`。因此：没有同时固定的因子，前复权结论不可复算，事后无法解释同一段代码为何算出另一条线。

不固定：未被引用的标的与日期、可随时重取的整市场分页、当日未进入任何判断的一次性查询结果。

## 5. 失败处置

按已批准的四条执行：

1. **只对模式 2 强制**。研究期取证（模式 1）与机械 Radar（模式 3）不进入此约束。
2. **固定失败即阻断写入**，不降级、不静默跳过。宁可写不进去，也不写入一份不能自证的东西。
3. **只存被引用切片与来源回执**，不存整份响应。
4. **跨事务补固定视为新历史**，并在回执中显式标注。

对既有的弱路径，处置是**收紧而非删除**：`price.evidence = None` 保留可读（历史结果仍需可读），但新的 `save-odds` 不得只走该路径落库。

## 6. 实施分级

按 Reuse First 与单一 writer 约束，分四级，不一次性大改。

| 级别 | 内容 | 是否改 schema | 风险 | 状态 |
|---|---|---|---|---|
| **L0** | 本文件；`checkpoint` / `save-hosted-handoff` 回执中加性输出 `reference_fixing` | 否（回执不入库） | 最低 | **已实现** |
| **L1** | Research：让「引用即固定」可审计（`verify-reference-fixing`） | 否 | 低 | **已实现，范围见 6.2** |
| **L2** | Brief：`save-hosted-handoff` 增加来源绑定 | 是（需版本升级 + 兼容期） | 中 | 未开始 |
| **L3** | 收窄 `save-odds` 的仅声明路径 | 否（`compute()` 与记录格式均未动，见 6.3） | 中，见 6.3 | **已实现** |

### 6.1 Odds 结果的加性字段不成立

原计划在 `save-odds` 回执里加自检字段，按「加性可选」处理。读代码后确认**不成立**：

```python
def read(self, reference):
    result = self._open(reference, RESULT_RECORD)
    ...
    rebuilt = self.compute(...)
    require(encoded(rebuilt) == encoded(result), "ODDS_RECALCULATION_MISMATCH")
```

`read()` 要求重算结果与已存结果**逐字节相等**。若在 `compute()` 内新增字段，所有历史 `conditional-odds-record-v1` 都会因缺少该字段而读不出来。因此 Odds 的任何字段改动都不是「加性」，而是版本升级：须新增 schema 版本并保留对 `RETAINED_VERSION` 的读取分支，旧结果不得重算成新形态。

同理适用于任何按「重算即校验」保存的消费者。判定方法：写入后是否存在 `encoded(rebuilt) == encoded(stored)` 类比对，有则不得按加性处理。

### 6.2 L0/L1 实际交付了什么，以及它证明不了什么

实现在 `application/reference_fixing.py`，报告 schema `reference-fixing-report-v1`，两档故意分开：

| 档 | 是否读源 | 用在哪 | 依据 |
|---|---|---|---|
| `listing` | **不读** | `checkpoint` 与 `save-hosted-handoff` 的常规回执 | `SourceRef` 是内容寻址：`manifest.commit` + `path` + `sha256` 已经钉死被引用的字节，无需再读一次来"固定" |
| `inspect` | 逐条 `read_source` | 只读命令 `verify-reference-fixing --ref`，审计已存 revision | 事后审计，不发生在写入路径上，也不取源 |

两档分家的原因是预算，不是洁癖：写入路径本身已经由 `submit_revision` 对每个 `SourceRef` 做了一次 `read_source`，回执若再读一次，每次判断的源读取请求就翻倍。因此**回执只做零读取清单**，`listing` 的签名里根本没有 `store` 参数——它没有能力读，也就不会偷偷读。

**一条必须写明的更正。** 本节原先把 L1 描述为「校验 `progress.sources` 覆盖被引用的价格日期（字段已存在，不改 schema）」。实现时确认这个说法不成立：现有 Research 字段里**没有任何可机读的「本次判断用了哪个标的、哪一天的价格」声明**。`findings[].evidence` 只指向 source 下标，不携带日期；正文是自由文本。因此：

- 在**不改 schema** 的前提下，L1 能交付的是「每个引用是否被显式列出、字节是否被钉死、事后是否可复算」，**不能**交付「被引用的价格日期是否落在切片覆盖内」。
- 后者需要先有声明字段，属 L2 的版本升级范围。在此之前，任何人说「日期已校验」都是超出实现的声称。

`inspect` 报告的 `observed_dates` / `observed_securities` / `observed_markers` 是从已存载荷中**按固定键名**抽出的值，边界是 `MAX_NODES` / `MAX_VALUES` / `MAX_DEPTH`，触发边界即置 `harvest_truncated: true`。它们回答「这份切片里出现过什么」，**不回答**「这份切片覆盖了什么」——切片是请求范围的结果，不是历史完整性证明（与 `REQUESTED_PAGE_TERMINATION_NOT_HISTORY_COMPLETENESS` 同义）。

报告固定携带 `coverage_assertion: "not-declared-in-existing-fields"`，这是个显式拒绝，不是占位符。

### 6.3 L3 只收窄写入，不动结果形态

最初把 L3 估为「改 schema」是过度估计。实际做法不需要动记录：

- 阻断点放在 `ConditionalOdds.save()`，在 `compute()` 之后、`_put()` 之前。`compute()` 不变，因此 `read()` 的 `encoded(rebuilt) == encoded(stored)` 仍对历史结果成立——**历史仅声明结果照旧可读，只有新的写入被收窄**。
- 显式承认通道是构造参数 `allow_declared_only`，CLI 上是 `save-odds --allow-declared-only`；承认后回执记 `price_evidence=declared-only-acknowledged`，未承认而通过则记 `fixed-slice-verified`。回执不入库，因此不影响重算比对。
- 宿主运行时通道（`research-runtime` 的 `save-odds`）**不提供**该开关：自动化不得自行承认一个没有固定切片的估值。确需此类记录时由本地显式命令完成。
- 未承认即抛 `ODDS_PRICE_EVIDENCE_REQUIRED`，且发生在写入之前——拒绝之后存储 head 不变，没有半份记录。

如果哪天要把「仅声明」也写进结果本身（例如加一条 limitation），那就回到 6.1 的结论：属版本升级，不是加性。

## 7. 不做什么

- 不改 `qualify_close` 的合格路径判定，不重开 #10 的 FTShare 主源裁决。
- 不因容量节省取消 Radar 扫描范围证据或 PIT/复权约束。
- 不把「最新指针」改写为「当时指针」——两者语义不同，页面消费的是前者。
- 不用当前历史补成当时已获得的价格。
- 不要求 Brief 之外尚未进入判断的取证材料入库。
- 不在写入路径上为「固定」再读一次源：预算只有一份，`listing` 档因此连 `store` 参数都没有。
- 不把 `observed_dates` 说成覆盖或历史 PIT 证明；在 L2 落地前，不声称「被引用价格日期已校验」。
