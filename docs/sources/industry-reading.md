# 产业商品变化与公司双向阅读

`decision_kernel.application.industry_reading.read_industry` 消费现有版本保管中的确切
SourceRef。它不采集、不保存、不刷新来源，不执行研究或通知。
本合同只证明声明范围内的保留输入、比较与查询；真实来源资格、周期采用和覆盖数量在原任务验收。

实际取得生产资料旬度价格、有限涨跌检测和原响应离线恢复，见[生产资料价格接入](material-prices.md)。

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
