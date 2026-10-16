# Odds：显式概率、风险政策与留存收盘输入

责任 #60；继承[条件Odds](conditional-odds.md)和[目标第14章](../decision-os-2.0-design.md#chapter-14)。本组把**有依据的显式研究概率、风险政策、已保管市场资料**接到同一规格、算术和保管层。不是拟合概率的模型、通用估值语言、行情采集器或投资决定器；不能把参数登记成功当作经济判断已被独立批准。

## 1. 正常消费者顺序

1. 正常Full保管正文、来源与必要未来权益/分配现金，沿原`freeze-valuation`得到确切`spec_ref`。原无概率路径不变。已经折回今日的公允价值仍只适用估值距离，不能用它计算未来概率收益。
2. 研究者对**同一组世界**说明概率依据、互斥/完备覆盖、最大模型风险、环境不确定性及每个原未决项怎样限制用途，主动读取原SourceRef。尚不能形成有据概率时继续使用无概率条件结果，不能默认等权或把券商数量当概率。
3. `freeze-odds-assessment`把明确政策与概率评估保存为原生JSON附件，返回确切引用与新reader读回结果。它不修改研究current、不替研究者补参数、不自动成为人工接受。
4. `calculate-probability-odds`从现役市场保管引用恢复日历、原始日线与证券主数据，按声明评价时点/新鲜度政策核查，再计算结果；`save-probability-odds`保存同一计算；`read-probability-odds`按原输入重算并逐项比较。

同一概率评估可用新的合格价格输入重算，目标日、股本、情景、概率与政策不变。若这些输入需要变更，先保留原评估并提交新的明确评估或研究/估值规格；不把改变假设叫做只换价格。新资料事实、机械核对、经济充分性和人工Decision分别验证。

## 2. 概率评估 `odds-assessment-v1`

必须包含`assessment_id/spec_ref/as_of/author_note`以及下列对象；as_of带时区，政策不得晚于评估，评估不得早于原研究/所用来源的实际保存时间。调用时也不得写未来评估。

| 对象 | 内容和限制 |
|---|---|
| `probabilities` | 每行`world_id/probability/basis_note/source_indices`；世界ID与冻结规格完全一致、无重复；概率为有限十进制字符串[0,1]，总和**精确等于1**，不归一化、不补尾项。 |
| `coverage` | `mutually_exclusive/exhaustive`均须明确true，另含`basis_note/source_indices`。这是可审阅的作者判断，不是程序证明真实世界完整。 |
| `model_risk`、`uncertainty` | `level/basis_note/source_indices`；模型风险low/medium/high/very-high，环境baseline/elevated/high，无隐含默认值。 |
| `unresolved_treatment` | 每行`item/effect`，精确覆盖原冻结研究的全部unresolved，不静默删除问题；文字处置本身不证明风险已经消失。 |
| `policy` | 见下节，整份政策随评估冻结。 |
| `limitations` | 非空限制列表，一直随结果保留。 |

每个概率/覆盖/风险依据至少指向该精确研究sources列表中的一个实际可读取SourceRef；原文仅外部定位或不可读时拒绝该概率用途，不阻断原Full保存。程序核引用与读取，不能判断引用是否真的足以支持概率、推断是否正确、评估人是否独立。`author_note`也不是经认证的Human角色。

## 3. 风险政策不是系统默认投资偏好

`policy_id/as_of/rationale`标明本次研究使用的政策依据。`minimum_holding_period_days/maximum_holding_period_days`覆盖价格日至固定退出日；`max_quote_age_days/max_master_age_days/max_capital_age_days`是显式的可接受资料年龄，均为正整数、最大36525日。上限仅为有界输入，不是推荐以陈旧资料分析；真实用途须论证相称时效。

`thresholds`按ACCEPTABLE_ODDS、ATTRACTIVE_ODDS、EXCEPTIONAL_ODDS顺序提供三组`minimum_return/minimum_positive_probability`，回报门槛严格递增、正收益概率门槛不递减。`model_risk_addons`四档及`uncertainty_addons`三档非负且单调，baseline环境加成必须为0。没有复制旧仓任何数值阈值作为当前默认。

复用旧`odds.py`语义：**所需回报 = 基础门槛 + 模型风险加成 + 环境加成**；风险不暗改情景概率。只有回报与概率两个条件同时满足才进入相应最高区间；否则INSUFFICIENT_ODDS。区间只是给定研究政策下的分类，不是参与/交易许可。

## 4. 本组支持的市场资格是有限的

计算输入形状：`{assessment_ref, market, evaluated_at}`。market为`odds-retained-close-v1`，包含`price_id/calendar_ref/daily_ref/master_ref/exchange/start/end`。三个引用均来自现役`market_custody`，不是自行写一个`qualified=true`；start/end是相同查询边界的YYYYMMDD。实际采用现役Registry、`restore_market_query`、`qualified_trade_calendar`、`qualified_daily`与`qualified_security_master`，没有新的HTTP提供方或自动抓取。

本组只支持SSE/SZSE、CNY普通A股（`ordinary-a`或原`A`标签）、所选范围**末日确切未复权收盘价**。末日必须是日历中的开市日且有唯一对应日线，上市日期/源证券/币种/交易所匹配；不前填缺日、不把复权历史价当入场价格。日期型源无法证明盘中/收盘分钟，因此保守要求报价本地日已经结束、日线捕获也在该日之后；输出23:59:59+08:00仅是日期边界标记，不冒充提供方tick或交易所关市时刻。

数据捕获不得晚于evaluated_at，eval不得在调用者实际运行时间之后。报价、证券参考和研究资本结构日期分别受明确政策约束。结果标为`qualified-retained-close-for-declared-as-of`，**只说明这项留存收盘用途**，不认证最新市场、可成交价格、实时停牌、历史PIT、永久发行人身份或公司行动。股数与净债务等仍来自冻结研究，证券主数据不会替它证明；发生拆股/增发/现金路径变化需要研究者新规格，不能靠价格标签自动修复。

概率与原未决项可能在所用历史报价之后形成，这只支持明确评价时点的条件分析，不是当时就能做出的历史投资判断。旧结果读回保留原evaluated_at，`current_use`只另返研究当前指针对照，不把旧结果标为今日新Odds。

## 5. 确定性计算与保管

每世界沿既有80位ROUND_HALF_EVEN条件算术，输出普通股每股终值、分配现金与`(终值+现金)/价格-1`。加权算术局部提炼旧`calculation.py`：概率加权终值/分配/总回收/累计收益；正收益情景概率只计收益**大于0**的世界，保本不算正收益。输出不是经验胜率；最小/最大情景收益不代表完整尾部损失边界。没有自动年化、折现、税费、汇率、期间现金再投资或负资本调用支持。

评估与结果使用原`ResearchArtifacts(application/json)`及日志/CAS/备份，不造新真源或current。`read-probability-odds`重建并比较规范JSON，即使人为重算了文件hash，也不能掩盖篡改后的结果。同意图同载荷恢复原引用、同键异载荷冲突；保存成功与独立读回失败分别返回。引用数据、源码文本和政策理由均不执行。

Registry仍属于来源owner。CLI对计算及结果重算必须提供匹配的`--registry`（连同它引用的原手册/能力文件）；原snapshot不符会拒绝，不静默套新登记。宿主使用当前checkout的Registry；未来升级时旧完整重算需恢复匹配的原工程资源，原生`read-artifact`始终可读原字节。此组不新建Registry备份服务、不声称任意历史注册表自动迁移。

## 6. CLI与现役宿主

```bash
# 公开权限声明不能代替本案真实授权。先准备真实的完整评估JSON。
decision-os store --binding approved.json freeze-odds-assessment --input assessment-publication.json --submission-id original-assessment
# assessment-publication.json: {"assessment": <上文评估>, "rights_confirmed": true, "rights_basis": "实际授权依据"}
decision-os store --binding readonly.json read-odds-assessment --ref saved-assessment-ref.json
decision-os store --binding readonly.json calculate-probability-odds --input calculation.json --registry docs/sources/relay-registry.json
decision-os store --binding approved.json save-probability-odds --input result-publication.json --submission-id original-result --registry docs/sources/relay-registry.json
# result-publication.json: {"calculation": <计算输入>, "rights_confirmed": true, "rights_basis": "实际授权依据"}
decision-os store --binding readonly.json read-probability-odds --ref saved-result-ref.json --registry docs/sources/relay-registry.json
```

原research-runtime宿主新增相同五个操作，加只读`reconcile-odds-assessment/reconcile-probability-odds`。读取payload为`{reference:<确切引用>}`，其余同CLI。不新增凭据或Actions权限；只有freeze/save有写权限。保留现役code_sha/expected_head/稳定submission_id/大小与请求预算。

未知写使用原payload/意图只读对账；没找到意图明确拒绝，不能顺便新写。只有恢复事实后才继续原意图。不得为了凑“成功运行”给东山历史稿硬填概率；它的当前价值规格及无概率限制仍有效。

## 7. 复用与维护边界

旧源固定`decision-kernel@cb3bbc87`的`calculation.py`加权投影和`odds.py`风险加成/分类；新输入不用旧ResearchSnapshot/信息hash/人工价伪装。仅一处提炼既有条件world算术供两条路共用，原结果字节与正反例保持。概率总和从旧容差1e-6收紧为精确1是新显式合同，不把容差当金融政策；使用标准库Decimal及原既有来源/保管，无新金融包或并行引擎。

验证包含显式概率/风险、来源与世界一致、陈旧/错期/残缺资料拒绝、真Git保管及历史读回、宿主原意图对账与512请求上限、原唯一cold-wheel消费者。测试不是经济审阅，不证明任何真实公司的概率已经可用。Watch/自动刷新、实盘/其他股类行情、公司行动自动适配和正式Decision仍是后继责任。本能力退出时一并退专属入口/模块/测试，原附件和历史方法读取责任保留，不删除业务数据。

## 显式Watch与按需刷新

已有评估可沿[Watch入口](odds-watch.md)绑定有限条件与事件窗，再显式检查/保存/重演。每次复用原概率/政策和留存行情资格，研究、股本、现金或期限变化另行复核；缺资料不写条件false。该入口不创建定时、通知或投资决定，不能把工程接线代签真实概率政策采用。
