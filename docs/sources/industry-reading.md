# 产业商品变化与公司双向阅读

`decision_kernel.application.industry_reading.read_industry` 消费现有版本保管中的确切
SourceRef。它不采集、不保存、不刷新来源，不执行研究或通知。
本合同只证明声明范围内的保留输入、比较与查询；真实来源资格、周期采用和覆盖数量在原任务验收。

实际取得生产资料旬度价格、有限涨跌检测和原响应离线恢复，见[生产资料价格接入](material-prices.md)。

工业累计利润、成本、应收/产成品余额及回款天数的同版派生阅读，见
[工业经营周期阅读](industrial-operating-cycle.md)，仍使用本页同一阅读包。

汽车生产、厂商批发、国内零售与出口的实物量分通道阅读，见
[汽车实物产销](vehicle-flows.md)，月度、累计与发布批次不混算。

主营构成的收入、成本、负数抵销及同期间财表代数对账，见
[Relay 主营构成阅读](main-business.md)。它消费已有捕获，不从业务标签自动创建采购角色。

畜禽、鸡蛋和配合饲料的采集日价格及公布同比/环比核对，见
[畜禽饲料周价](livestock-weekly.md)，不把采集日值当周均价或把公布基数当独立观察。

原包上的有限商品身份归并、两期资格及明确评估时刻的四组集合，见
[商品资格读取](industry-goods.md)。候选、系列与统计总体不能作为独立商品相加；
有限批次始终保留全市场数量未知，不据此宣称 500 目标完成。

## 使用入口

先通过既有 `put-source` 保存获准资料，再保存引用这些资料的阅读包；不新增写入通道。
远端保管仍按[版本保管合同](../architecture/github-store.md)核目标、可见性、公开权利和唯一 writer。
阅读入口强制将绑定设为只读；凭据自身可能更宽，不能称为角色隔离。

```bash
python -m decision_kernel.interfaces.industry_cli --binding config/store-binding.json --ref packet-ref.json --subject-id copper-cn
python -m decision_kernel.interfaces.industry_cli --binding config/store-binding.json --ref packet-ref.json --company-id company-example
```

本地合成测试使用 `--path dedicated-synthetic.git` 替代 `--binding`。
`packet-ref.json` 是接收器真实返回的 SourceRef，不是 URL 或临时输入文件。
不传查询条件则读取整包；两个方向互斥，都过滤同一份已通过输入核验的关系。
无结果表示此包未覆盖该联系；`scope` 和 `relation_outcomes` 始终保留整包分母。
退出码 2 表示输入失败或部分恢复，成功分支仍在输出中。

## 原生宿主的整包只读入口

没有本地凭据时可使用现役 `research-runtime.yml` 的 `industry-read` 操作。
先核当前 `main` 并填确切 `code_sha`；`submission_id/expected_head` 留空。
`payload` 是下列 JSON，对应的 `reference` 必须替换成实际已保存阅读包的完整 SourceRef：

```json
{"reference": "替换为实际完整 SourceRef 对象", "view": "vehicle-flows", "company_id": "company-example", "public_sources_custody_confirmed": true}
```

`view` 必须显式选 `industry`、`operating-cycle`、`vehicle-flows`、`livestock-weekly`、
`industry-uses` 或 `industry-goods`。具名用途沿同一阅读室合同；商品资格视图另须
`evaluation_at`，带时区且不早于该原包截止，其他视图不接受该字段。
`subject_id/company_id` 可都省略，但不可同时非空；经营周期不支持公司过滤，
应改用原 `industry` 或 `vehicle-flows`，不能把宏观总量映射成个股暴露。
完整原根、行/段引用、期间、限制、比较拒绝和整包分母均由原读取器返回，
不将网址、临时 JSON 或前次 CI 文件当成已保管根，也不自动改用最新数据。

公开来源表示确认与研究/反馈授权不同：调用者须核本包及实际依赖可在公开运行产物中保管。
未确认、选择不合法、额外 URL/代码字段或非整包引用在 Store/网络和公开产物创建前拒绝。
原 read 作业继续使用只读权限；原 write 条件、CAS、512 请求及结果大小上限均不变。
只访问绑定资料库，不请求来源网页、Relay 或 AI。结果中的 `source_requests=0`
是没有原始来源采集，不代表没有 GitHub 资料库 GET。

检查 `report.json` 而不只看作业名称：完整可读为 `completed`；部分恢复保留
`result` 内成功分支与缺失原因，报告 `industry-reading-partial` 且宿主退出码 2。
无法打开根、完整性/权限/作用域退出检查失败不返回中间结果；超出原产物上限也不截断求绿。
已可读但不可比（例如累计相减或初步批次混比）沿原比较 reason_code 展示，
不把数据缺失与不适用的算术混成同一种失败。

维护者同步维护 `interfaces/industry_commands.py` 和 `tests/test_industry_runtime.py`。
退出此宿主接线时移除专属操作选项、分派/预检/部分状态及测试；原 CLI、三种读取器、
历史 SourceRef 和共享权限/保管仍保留。线上读取、来源原件真实性、研究采用和人工接受分别验收。

## 阅读包

阅读包以 `SourceInput(kind="content")` 的 JSON 正文保存：

| 字段 | 含义与边界 |
|---|---|
| `schema` | `industry-reading-v1` |
| `prepared_at`、`as_of` | 带时区的整理时间与输入截止，前者不晚于后者 |
| `coverage_note`、`use` | 有限来源范围和本次周期用途；不认证真实使用 |
| `series` | 1—32 条，含唯一 `series_id`、commodity/industry 类型 `subject` Entity、2—64 个 `observations` SourceRef |
| `relations` | 0—64 条，唯一 `relation_id`；不以数量宣称覆盖 |

正文重复 JSON key、非有限 JSON 数值、重复系列/关系 ID、同主体冲突身份均拒绝。
所有依赖固定 commit、路径、摘要及选择，不替换为最新来源。
来源版本仍按既有 SourceInput 上限，整次恢复限制为 16 MiB；不截断列表假装完整。

## 同口径序列

每个 observation 必须选择现有 dataset 的**单行及下列确切列集合**。
值从所选 CSV 行读取，包内不再复制一份可自行改写的价格。

```text
subject_id,metric,specification,region,currency,unit,price_kind,market,contract,tax_basis,frequency,period_kind,assessment,period_start,period_end,value
```

主体须匹配来源元数据 Entity 和行内 ID。`metric/specification/region/currency/unit/tax_basis/frequency/assessment`
保存实际指标、规格、地区、币种、计量、税费、频率和平均/期末等方法；不得为方便比较填通用名称。
非适用项显式写 `not-applicable`；未知口径写 `unknown/unspecified` 时不计算变化。

- `price_kind`：`quote/transaction/survey/not-applicable`，报价、成交、调查统计不互换。
- `market`：`spot/futures/not-applicable`；期货须指定 `contract`，非期货写 `not-applicable`，换月不暗接连续价。
- `period_kind`：`price-level/index-level/stock/period-flow/ytd-flow`。
- 期间为 ISO 日期起止，起止有序且不晚于 cutoff 或资料发布/取得日期；YTD 必须从当年 1 月 1 日开始。
- `value` 为最多 64 字符的有限十进制字符串，空单元格表示缺失；不把它填 0。

来源的 `acquired_at/published_at` 必须存在且不晚于整理时间。
仅有日期就保留日期，不证明该日盘中已可用。来源标签/时间均为保留元数据，不是历史 PIT 认证。

按包给定顺序逐相邻观察比较，不跨缺失点补连。全部口径及来源 `source_id/origin` 相同才算同系列；
来源替换须重新核基线。完全相同期间区分不变与修订；回退、重叠期间拒绝。
不同期间的 YTD 不当月度流量；期间流量及期间均价要求相同长度或完整自然月。
变化保留正负号，百分比以旧值绝对值为分母，零基数只返回绝对变化。
读取成功不等于可比，须检查每个 `changes/latest_change.status` 和 `reason_code`。

## 公司经营角色

每条 relation 需要：

| 字段 | 核验或输出 |
|---|---|
| `subject_id`、`company` | 包内商品/产业主体和 company 类型 Entity，不用证券代码冒充公司 |
| `role` | `producer/purchaser/processor/transporter/substitute/inventory` |
| `business_scope`、`report_period` | 实际业务范围、发行人资料期间 |
| `applicable_start/end` | 声明适用期间；截止时已过期则标 `historical`，不自动续到今天 |
| `evidence` | `reference/source_role/quote/issuer_binding_note` |
| `exposure_size_note` | 量级缺口说明；首版数值 business_share/elasticity/pass_through/lag/net_benefit 均为 null |
| `transmission` | `revenue/cost/profit/cash_flow` 四项，每项保留 `hypothesis` 与 `counterevidence` |
| `unknowns` | 至少一条仍须核实的未知，不默认零暴露或 100% 传导 |

evidence 必须是可恢复的 content **lines** SourceRef，`quote` 须出现在所选原有文本，
保留元数据含该公司 ID 且 report_period 一致。只允许声明来源角色 `issuer-filing/issuer-ir`，
并记录发行人绑定依据；研报、关键词、行业成员、只有外部定位均不能补齐关系。
程序核引用、文本和元数据一致性，**不以 source_role 标签认证发行人或自动判定引文语义**。
经营角色和传导由获准整理者核对；输出固定说明 curated、真实性未认证、当前业务未重验。
涨价可以提高下游成本，合同、库存、套保及一体化可抵消影响；不返回受益股评分或净受益结论。

## 复用与维护

局部提炼旧
[`economic_company_context.py@cb3bbc8`](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/src/decision_kernel/runtime/economic_company_context.py)
的发行人绑定、取得/整理/cutoff 顺序、机制假设与未知量级约束；沿用旧产业统计合同的期间/修订/回退语义。
直接使用本仓 Entity、SourceRef、VersionedStore、GitStore/GitHubStore 和标准库 Decimal/CSV/JSON，
不引回旧 evidence schema、业务状态或组件依赖，不建图数据库。

本能力 owner 维护 `tests/test_industry_reading.py` 的口径、缺失、双向、证据失效和独立进程恢复测试。
能力退役时同步退出本模块、CLI、文档和专属测试；既有保管及历史引用继续由原 owner 维护。
真实同口径基线、获准资料的独立保存/读回、至少一次有限周期消费仍须各自留证，合成测试不能代签。
