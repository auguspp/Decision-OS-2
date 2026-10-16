# 汽车实物产销：分通道、期间和发布批次阅读

`application.vehicle_flows.read_vehicle_flows` 是原 `industry-reading-v1` 的用途消费者。
生产、厂商批发、国内零售、厂商出口各自可读；没有第二种资料保管格式、自动采集器、
股票名单或受益评分。整包核验和只读范围沿既有 `read_industry`、`VersionedStore`；
原 `industry_source_scope` 从经营周期入口窄提炼共享，进入和退出均核远端绑定/可达性，
解释执行仍在作用域内；退出失败不返回结果。不增加512请求预算或重置客户端。

## 原命令入口

```bash
python -m decision_kernel.interfaces.industry_cli --binding config/store-binding.json --ref packet-ref.json --vehicle-flows
python -m decision_kernel.interfaces.industry_cli --binding config/store-binding.json --ref packet-ref.json --vehicle-flows --company-id issuer-id
```

`--subject-id` 和 `--company-id` 仍互斥；`--vehicle-flows` 与 `--operating-cycle` 互斥。
不选用途时原价格/经营角色读取不变。资料先用既有获准 `put-source` 保管，返回实际
SourceRef后再组成阅读包；示例文件名不证明来源存在。CLI远端绑定强制只读，凭据可能更宽，
不是服务端角色隔离。失败或部分输入退出2；合格兄弟通道仍在结果中。

## 输入口径

仍是原16列单行CSV SourceRef，没有在阅读包重抄金额。主体为 `industry` Entity，
显式 attribute `observation_family=vehicle-flows` 只声明本次用途，不认证来源。
`specification` 必须保留真实产品边界，例如狭义乘用车/新能源乘用车、整车与CKD口径；
`region` 保存实际地域。不得把不同总体改成通用名称以求可比。

|metric|用途与不能混同的范围|
|---|---|
|vehicle-production|实物生产，不是销量、收入或库存|
|vehicle-wholesale|厂商批发；国内/出口覆盖按原发布，不是国内终端零售|
|vehicle-domestic-retail|国内终端零售，不是厂商全部销量或交付|
|vehicle-manufacturer-exports|厂商口径出口；整车/CKD见规格，不冒充海关或境外零售|

`unit` 只接 `vehicle`（辆，整数）或 `10k-vehicles`（万辆，保留公布精度）；
不同单位不自动换算并跨期拼接。值是原有限十进制字符串，0合法，空值缺失，负实物量拒绝。
`currency/price_kind/market/contract/tax_basis` 均为 `not-applicable`，频率为 `monthly`。
`period-flow` 须完整自然月；`ytd-flow` 须同年1月1日至月末。金额、指数、周度扫描的局部
期间、累计与单月不能混进一个系列。月扫描确实覆盖完整月时，须标其初步批次。

`assessment` 限 `monthly-release`、`preliminary-scan`、`revised-release`。月报不代表审计
终值，后续仍可修订；不推断“final”。同一主体/通道/期间类型只能声明一条系列，修订沿
该系列显式观察槽排列。来源发布、取得与整理时间沿原入口，不得编造盘中时间或历史PIT。

## 比较与不可用

每个系列按显式槽次比较相邻观察；不排序，不跨缺失点/缺月，不补数。先核实物用途，再用
原 `_change` 的全BASIS、来源id/origin、累计/重叠/零基数规则；额外限制相邻自然月及
发布时间不得倒退。单位、总体、来源或初步/月报批次变化时比较不可用。相同期间同口径
的新数值是修订，不是新月需求；YTD金额不相减为单月，跨年同样不例外。

返回原值、差额和旧值非零时的百分比，独立Decimal上下文保证外部精度/舍入/trap不改变
结果。不同月各取当时公布值时只是**跨发布版本的金额/数量对照**，不保证等于后一期
同版修订基数计算的官方环比；`publisher_reported_growth_verified=false`。没有季调、
同比基数重建、机械增长阈值或自动需求结论。

本用途故意不计算产销差、批零差、出口占比或市场份额：出口/进口、CKD、报告总体、
在途、渠道和不同修订基数会改变解释。差额不是库存，出口不是外国终端需求，产销增长
不是利润/现金增长；全乘用车和新能源子总体不相加。同行名称也不证明同一统计分母。

## 结果、角色与消费者

`entries` 是经过本用途核验的通道与期间；`retained` 保存原读取结果用于审计，不是
重新准入被汽车用途拒绝的值。`unclassified_series`、不可用点及原 `relation_outcomes`
保留故障。`coverage` 分别列未提供的月度、累计通道，不将缺项写零。查询只过滤同一
整包的合格series和原证据关系，`scope`、`coverage`、`view_version`保留整包范围；
无命中不是全市场零暴露。未分类主体的关系不进入汽车查询，原证据仍可审计。

公司角色沿原发行人lines引用、原报告期间、四项传导假设/反证和未知占比合同。公司
与总体不是同一个对象，不通过销量占比计算利润弹性。9月快报只证9月经营活动，10月
截止仍标历史，不自动延伸到今天。`source_role`或Entity标签不认证发行人。
#7在原委托中使用/挑战，#13可直接阅读；可读不代签Full/Brief已采用。

## 来源方法、复用和退出

方法依据为乘联分会月报对生产、国内外批发、国内零售及含整车/CKD出口的分别公布。
本组有界核对官方公开入口：
- 中国汽车流通协会乘联分会2026年7月月报： https://www.cada.cn/Trends/info_91_10550.html
- 2026年8月月报： https://www.cada.cn/Trends/info_91_10566.html
- 2026年9月月报： https://www.cada.cn/Trends/info_91_10580.html
- 8月初步扫描： https://www.cada.cn/Trends/info_91_10563.html

以上定位是方法/字段依据，不代表本工程提交已采集、已保管或持续运行。月报、协会
图表/全文的权利不由软件授予；实际资料只保管可合法使用的必要事实及原创说明，
保存原署名、版本、取得方法和限制。没有用另一行情提供方替代Relay。来源失败与
公开权限只限制相依工作，不修改共享Store/Relay/CI/依赖或提高权限。

复用现役行业读取、经营周期的既有只读作用域、Entity/SourceRef及标准库；既有组件
足以做四类用途检查，无需新SDK/统计引擎/框架。`tests/test_vehicle_flows.py` 由#11维护
实物/累计/批次/缺失/双向/原入口及GitAPI退出权限测试，旧经营周期测试保护提炼点。
退役时同步撤新用途模块、CLI选项、专属测试/文档和导航；共享作用域若尚由经营周期
使用则保留。既有资料版本、原行业入口及引用不删除，工程回退不撤销公开数据副作用。
