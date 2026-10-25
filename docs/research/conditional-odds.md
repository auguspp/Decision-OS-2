# Odds：冻结研究、条件估值与价格重算

责任 [#60](https://github.com/auguspp/Decision-OS-2/issues/60)，落实[设计第14章](../decision-os-2.0-design.md#chapter-14)及[实施基线§7.1](../implementation-baseline.md#contracts)。本入口负责无概率条件计算及其明确价格输入，包含后文FTShare与Relay留存收盘接线；[概率Odds](probability-odds.md)、[Watch](odds-watch.md)各有独立合同，不代签Decision。原Full、SourceRef、原字节附件、提交日志及Git备份继续承担各自责任；不建立新数据库、研究current或行情提供方。

## 能做什么，不能做什么

| 输入含义 | 方法 | 输出 |
|---|---|---|
| 指定估值日的普通股公允价值（已折回当日） | `equity-value-distance-v1` | `value_to_price_distance = value / price - 1`，不是未来收益 |
| 固定未来退出日的普通股权益价值，另列期间分配现金 | `nominal-holding-return-v1` | 每股权益价值、分配现金、合计回收、名义未贴现累计收益 `(exit + cash) / price - 1` |

两者都不自动年化、不折现、不换币、不计算费用/税费、现金再投资、期望收益或胜率。当前价值载荷不得有未来目标日和另加分配现金，不能把Full已经贴现的每股估值塞进未来收益。目标日是退出价值日期，不是盈利预测年份的任意简称。

`probability_status=not-established`，只允许明确声明世界，不默认等权；加权输出为null。`odds-price-v1`不接受显式概率、所谓qualified-market或未知方法；合格留存价必须沿后文市场输入恢复真实来源。**这不阻止无概率条件分析。** 概率Odds沿独立入口建立真实概率/风险/行情资格，不把无概率限制当作产品取消，也不能从机构预测数或后来涨幅补概率。

## 有限输入与精确绑定

`valuation-spec-v1` 必填：

- `spec_id`；确切 `research_ref`（原BlobRef四字段）；`security`（code/share_class）；`currency`。
- `value_kind`、对应 `method`、`valuation_date`、`target_date`（当前价值必须null）。
- `share_count`（原始普通股股数，不是万股/亿股）；`capital_structure:{as_of,assumption}`。数量、债务、少数权益、募资用途和经济归属由研究先求解；输入只能是对应股类的**权益**，不接未转权益的EV。
- `cash_policy`：当前价值用`included-in-fair-value`；未来用`terminal-excludes-distributions`。标记是作者声明，不是自动审核终值是否真的排除了分红。
- `worlds`（1—12）、`probability_status=not-established`、`allowed_changes=["price-context"]`及非空`limitations`。

每个world：`id/name/value/basis/conditions/basis_note/source_indices/distributions`。`value`是普通股权益，`basis`为`per-share`或`total-equity`；总额按固定股数转换。`source_indices`引用该精确研究的sources列表，不允许借新来源静默改变原依据；没有可量化来源时用空列表并在basis_note/conditions明确假设。系统读取所引SourceRef、保存实际保管等级；它不从正文自动提炼估值，也不核准假设或研究充分性。无关未引用来源不全局阻塞。

未来每项分配现金包括`id/date/amount/basis/currency/kind`，kind仅dividend/capital-return。现金币种相同、金额非负、日期在估值日之后且不晚于退出日；ID不重复。资本追加不能伪装成负分红，本组需新规格明确经济处理。

金额和股数都是有限十进制**字符串**，不用float；最多48有效位、数量级±32。输入256KiB、每世界64笔现金流、64个来源下标；原存储/宿主限额仍生效。未支持模型仍可通过原Full保存，不执行任意脚本或表达式。

### 价格上下文

`odds-price-v1` 必填`price_id/kind/security/currency/share_count/price/as_of/note/evidence`。`kind`仅：

- `declared-reference`：真实用户参考价或明确的分析假设，note如实记录谁提供、用于什么；模型给的价格不冒称用户给价。无来源时evidence=null，结果只标声明价格。**新的 `save-odds` 不再接受 evidence=null 直接落库**：必须先固定被引用的价格切片（`relay-observation` 或合格 `market` 路径）。确实要以纯声明价存档时，必须显式承认 `--allow-declared-only`，回执记 `price_evidence=declared-only-acknowledged`。已有的仅声明历史结果照旧可读、可重算，不受此收紧影响。
- `relay-observation`：必须有确切来源与数值、证券、报价日期定位；核原origin为现有Relay。evidence为`{source, value_locator, security_locator, date_locator}`，locator沿现役数值工具使用CSV行/列或文本start/end。保管原值匹配、日期代码匹配**仍不是合格Market或发行人认证**；币种、股类和股本仍由作者声明，PIT/复权/交易资格没有建立。

as_of须含时区；其日历日期对应所选报价日，调用者应采用来源本地时区，不自动推断时区/收盘时间。价格必须正数，证券/股类/币种/股数匹配冻结规格。可替换price-id/来源引用/日期/数值等价格上下文，但不得改变规格；新报价不滚动目标日。若分配日已过去或价格日不早于退出日，拒绝计算，要求显式调整规格，不能静默删除旧现金流。历史价配后续研究、当前估值配新日期价格均带限制，不能称历史PIT或新研究。

### 已留存 FTShare 原始收盘价

现役`calculate-odds/save-odds/read-odds`另支持`{spec_ref, market, market_policy, evaluated_at}`，使无概率条件分析直接消费[FTShare日线](../sources/ftshare-daily.md)原集合。原`{spec_ref,price}`、`odds-price-v1`与历史结果保持不变；FTShare价格不包装成声明价或Relay观察。

| 输入 | 内容 |
|---|---|
| `market` | `{schema:"odds-ftshare-close-v1",price_id,collection_ref,exchange,quote_date}`。`collection_ref`必须是原集合的确切SourceRef；`exchange`为SSE/SZSE；`quote_date`为明确的YYYY-MM-DD，不自动寻找最新日期。 |
| `market_policy` | `policy_id/as_of/rationale`及`max_quote_age_days/max_master_age_days/max_capital_age_days`，年龄均为显式正整数、最大36525日。日期与容忍度的依据由本次用途说明，程序不提供投资默认值。 |
| `evaluated_at` | 带时区的本次评价时点；不早于政策，不晚于实际运行时间。 |

复用`restore_ftshare_collection`取得原日历、证券主表及请求身份，只恢复该证券对应的`adjust_kind=none`原始查询，再逐日定位`close`。不接受调用者自填价格、复权选择或`qualified=true`；不回退另一提供方、另一日期或未保管查询。引用/原件损坏直接失败，来源hold不授予新资格。缺失的其他证券或前复权序列不抹去该只证券已成立的原始收盘用途，也不因此证明全市场完整。

所选日必须在集合范围和实际开市日历内；证券、交易所、上市期和CNY普通A股匹配。新FTShare路径采用固定政策`ashare-eod-1835-v1`：评价时点与原始响应取得时点都不得早于报价日的北京时间18:35，全部采用资料的捕获时点不得晚于评价。该规则允许正常收盘后日更当晚被消费；18:35之前取得的原件，即使次日读取也不自动取得这一资格。原Relay次日规则不改写。

FTShare输出`as_of`的18:35:00+08:00是**保守的收盘后观察政策截止**，不是供应商tick、交易所关市证明或独立最终价认证；原响应实际取得时间另外保留。报价年龄按本地日历日计，同日为0；证券主表和固定资本结构各自检查年龄。`freshness=within-declared-age-policy`只说明原明确容忍度，`latest_market_close=not-certified`不改称最新价、可成交价或历史PIT。该截止只服务现有沪深普通A股日线消费，盘中/竞价及其他市场不套用。

新结果使用`conditional-odds-retained-close-v1`，保留原80位算术及当前价值/未来退出价值区分，连同`market_policy`、全部实际采用引用/取得时间/原请求摘要、原`calculation_input`保存。`read-odds`按原输入重新恢复、重算；原v1仍按原price重算。没有新存储、研究current、概率参数或行情请求。算术相同不表示研究估值随新价更新。

CLI原命令可给`--registry docs/sources/relay-registry.json`，因为FTShare集合仍引用原Relay日历与主表。不给时，新市场路径使用当前checkout登记；重放历史登记变化必须提供原匹配资源。旧声明价/Relay观察结果不新增Registry依赖。宿主沿原Odds路由消费相同输入，权限与稳定提交意图不变。

原生GitHub宿主在恢复FTShare集合时复用现役MarketRadar的`GitMarketReader`，研究规格和结果仍走原Store。来源快照只读、沿同一绑定和同一个GitHub API预算，退出前重查可见性和可达性；保存后的独立reader重新取得原始字节，不继承写入者的来源快照。Watch的操作内引用缓存只透传这项原底层能力，不放宽512请求上限。

### 已留存 Relay 原始收盘价与同一条件计算

同一组 `calculate-odds/save-odds/read-odds` 也接收既有 `odds-retained-close-v1` 市场输入，复用概率Odds已经使用的原Relay适配器。外层仍为 `{spec_ref,market,market_policy,evaluated_at}`，政策与评价时点沿上节；`market` 必须提供 `schema/price_id/calendar_ref/daily_ref/master_ref/exchange/start/end`。三个引用是原日历、原始日线和证券主表的完整SourceRef，`start/end`为原查询的八位日期，明确使用`end`当天收盘；不接受调用者直接填收盘数值或资格布尔值。

两种已支持来源的边界分别保留：

| 市场输入 | 恢复的价格类型 | 原时点资格 |
|---|---|---|
| `odds-ftshare-close-v1` | `retained-ftshare-raw-close` | 报价日北京时间18:35以后实际取得并评价；18:35是观察政策截止，不是供应商tick |
| `odds-retained-close-v1` | `retained-relay-raw-close` | 原日线捕获和评价均在报价日结束后的本地日期；输出23:59:59是保守日期边界，不是供应商tick |

Relay路径逐一恢复并校验原查询、登记快照、日历、证券、CNY普通A股、上市状态、实际捕获时刻及明确的报价/主表/资本结构年龄政策。错期、缺失或损坏原件直接失败；不换日期、来源或提交键重试。两条适配均为已保管的沪深原始日收盘用途，`source_requests=0`；不能据此认证最新价、实时可交易价、前复权完整历史或历史PIT，也不改变现役FTShare主源和采集预算。

同一冻结规格、同一有效价格数值通过同一80位Decimal算术，逐世界结果相同；原来源、引用、捕获时刻和两种截止语义仍分别保留。相同数值不证明两个独立上游，不更新经营判断、概率或人工接受。新增Relay结果沿现有`conditional-odds-retained-close-v1`保管，保存、未知写对账、独立读回、Odds Book和Room使用同一原Store。Book核市场schema与价格kind/price_id的对应，只呈现已存声明；打开`read-odds`才按原输入重新恢复和重算。

本项加性接线不改变已冻结Watch的来源政策：条件Watch的合格留存政策仍为`retained-ftshare-raw-close`，传入Relay市场输入继续拒绝，不自动换源或启用暂停Watch。概率Watch已有的Relay支持沿其原合同。真实连接器取证、来源公开权利、研究采用和生产迁移仍各自验收。

旧FTShare、`odds-price-v1`声明价和Relay观察结果保持原重演。新reader与writer同组交付；一旦保存Relay条件结果，回退时先停止新增该类写入，并保留支持该类型的reader，不能只退回旧二进制使新档案失读。没有第二套存储、自动回退来源或新的后台运行。

## 保存与真正的价格刷新

先提交原研究，再冻结其受支持估值产出；一份规格绑定一个真实研究版本和其正文摘要，不模拟旧ResearchSnapshot。冻结对象与各价格结果沿原ResearchArtifacts保存为application/json，路径仍是`research/artifacts/.../manifest.json + content.bin`。结果内含确切规格引用、规格摘要、研究引用、价格时钟与证据、原未解问题、方法版本及逐世界计算。没有第二current、删除接口或后台行情请求。

同一规格的两价格结果分别保存；新研究版本不会覆盖旧结果。`read-odds`从确切规格和价格重算并逐字段比较；`current_use`单独提示原研究是否还是current，不参与不可变计算摘要，也不宣称“current所以经济判断有效”。旧结果可读不等于推荐现在使用；无法读取某个确切引用时不会改用最新来源。

冻结和保存均使用原稳定submission_id及原生CAS。同意图同输入返回原回执；同键不同内容冲突。CLI和宿主创建另一个只读实例核验；成功回执与读回状态分别返回，读回失败保留已知保存引用。读回只是机械验证，不是第二研究者批准。

### CLI

所有写入均须已有具体公开权利，冻结输入包装为`{spec,rights_confirmed:true,rights_basis}`，保存包装为`{calculation:{spec_ref,price},rights_confirmed:true,rights_basis}`。rights字段是调用者声明，不是认证凭据；不因此新增发布权。

```bash
# 路径/输入来自本次实际保管的回执，示例不是假造生产引用。
decision-os store --binding approved.json freeze-valuation --input freeze.json --submission-id original-spec-intent
decision-os store --binding readonly.json read-valuation --ref saved-spec-ref.json
# calculation.json: {"spec_ref": <原保存回执>, "price": <新的合法价格上下文>}
decision-os store --binding readonly.json calculate-odds --input calculation.json
decision-os store --binding approved.json save-odds --input save.json --submission-id original-result-intent
decision-os store --binding readonly.json read-odds --ref saved-result-ref.json
```

第二个价格仍使用同一个spec_ref；相同spec SHA可验证股数、目标日、价值/现金和假设都未改变。返回值保留小数收益（0.25=25%），不把其误显示为0.25%。本组不创建价格订阅或自动刷新；结果引用由实际调用者保留，按需可用原deliver的supporting-file/artifact_ref携带，不能把绑定旧研究的spec称为新修订已经重估。

FTShare合格收盘也可作为第二个价格输入：保存包装仍是`{calculation:<上节市场计算输入>,rights_confirmed:true,rights_basis:<真实依据>}`。这些字段不由工程示例授予实际保管权。

### 现役显式宿主

`research-runtime.yml` 新增`freeze-valuation/read-valuation/calculate-odds/save-odds/read-odds`，写操作只有freeze/save。读spec/result的payload为`{reference:<exact ref>}`；calculate与CLI相同。原code_sha、稳定submission_id、expected_head与公开产物约束保持，代码变化明确重新核验，读入口无写权限。无需第二个Secret或工作流。

未知写先按完全相同payload/submission_id调用`reconcile-valuation`或`reconcile-odds`（只读job）；缺意图返回SUBMISSION_NOT_FOUND_NO_WRITE，不顺便补写。确认状态后再恢复原意图。不是先换提交编号再试。公开Actions参数与输出不适合私密材料；原研究绑定仍只读配置。

## 复用、检验与退出

旧固定源为`auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8`：`calculation.py`的80位HALF_EVEN Context与_normalized；`conditional_odds.py::_calculate_world`的现金排序、归一化、名义回报。旧pydantic对象依赖旧COMMITTED ResearchSnapshot、信息bundle/hash和HumanPriceContext，不能直接套新来源，故仅提炼其算术并以原80位上下文运行。总权益/股数转换是新边界的明确单位映射；没有重新实现自由DCF、公式DSL或概率引擎。

外部复用检查：Python 3.12官方decimal文档（https://docs.python.org/3.12/library/decimal.html）支持显式Context/localcontext及可控精度；现成标准库与旧算术足够，不引入金融建模包或第二数值栈。原固定方法不修改；未来方法变动采用新版本，历史读取责任继续。

测试覆盖原两价格正例、无概率边界、当前价值非回报、股类币种/股本/时点/分红/来源定位、真Git保存读回、旧研究变化提示、同意图冲突/未知写只读对账、宿主权限和原同一次cold-wheel安装。自查不是独立批准；CI不是经济研究。没有为工程构造真实公司判断或补概率。

FTShare接线继续直接用现役集合、单股原始响应恢复与Decimal，不引入新的市场reader或第三方计算包。专属回归`tests/test_ftshare_odds.py`用真实Store保管合成的两个原始查询，实际进入条件/概率Odds和两类Watch、独立Git读回及原命令；检查同日18:35截止前拒绝、截止后消费、次日原输入重演、陈旧、错期、来源hold、未保管、伪装复权、损坏和不匹配Registry。合成工程验证不代签真实研究概率或全市场可靠性。退出FTShare采用时退该窄适配、对应输入/消费者与专属回归，继续保留旧结果和原资料的历史读取义务。

Relay条件接线直接复用原`qualify_close`和两个有限适配，无新依赖。`tests/test_retained_conditional_odds.py`保护两个已保管输入的同规格数值一致、确切恢复与正常Book/Room消费，并保留错源/时点限制和Watch拒绝换源；现役宿主与同一次包外安装消费覆盖原保存、独立读取及未知写对账。该组测试随此加性能力维护，退出新增写入时仍须保留已存结果的reader及其恢复验证，不为测试重新调用真实来源。

退出本能力时同步处理独立domain/application/odds_commands、CLI/宿主路由和专属测试；原artifact/store及历史方法/引用继续可读，不清理用户资料。普通反向PR回退工程，数据副作用不随代码回退消失。

## 后继概率与风险入口

首组无概率接口保持原语义；确实形成显式概率、风险及政策依据的未来权益/现金规格，可按[概率Odds入口](probability-odds.md)继续冻结评估、采用留存收盘输入并保存加权结果。新入口不把原无概率档案自动升级，不给历史当前价值稿补概率，也不代签实盘行情、研究质量或Decision。
