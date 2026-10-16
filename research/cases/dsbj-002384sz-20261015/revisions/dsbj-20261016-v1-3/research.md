# 东山精密 Full v1.3：新接口实际取证与现金桥补核

本版是既有委托的取证增量。行情与经营判断基准仍为2026年10月15日，接口获取时间为2026年10月16日；没有把本轮数据后填为历史PIT证据，没有重新签发目标价。v1.2的两份原文件引用及v1.1/v1.0历史保留。

## 实际调用结果

调用现役relay-source-runtime.yml的family=research，执行11个明确查询。run38838698248使用代码8da25186cb4cc01150887313598887a8a1148946：9项完整非空，38条原返回行；2项anns_d未取得数据。38不是独立报告数：Q1现金流和H1资产负债表各有仅update_flag不同的一对记录，保管原样、核算不重复相加。

| 查询 | 返回行 | 证据性质 |
|---|---:|---|
| cashflow，2026H1 | 1 | 现金流量表选定字段 |
| cashflow，2026Q1 | 2 | 同金额、不同update_flag，非两期现金 |
| balancesheet，2026H1 | 2 | 同金额、不同update_flag |
| income，2026H1 | 1 | 合并利润及归属字段 |
| fina_mainbz，2026H1 | 5 | 产品主营构成，原分类不扩成AI业务 |
| daily，2026-10-15 | 1 | 收盘价观察 |
| daily_basic，2026-10-15 | 1 | 总股本/总市值观察 |
| report_rc，2026-08-01至10-15 | 23 | 8家机构的预测期记录；20行np非空 |
| research_report，同区间 | 2 | 上海证券、开源证券目录及PDF URL |
| anns_d，2026-08-21及10-12 | 未取得 | 各4次504/upstream_pool_exhausted；不能当作合法空数据 |

全部成功页在所选limit=100下到终页；检查了证券代码、期间、查询参数、38条所选字段及空值和原HTTP摘要。这里只证明本次范围的返回与分页，不证明所有历史机构或公告都被覆盖。原生ZIP SHA256为bc9492bdc85138e3f84ae9336d19bf5e66a0ae28804abfd1050700aefd50b332。存储中的SourceRef合辑是9个成功查询的完整字段投影，不是完整HTTP原文或发行人原件。

## 半年现金问题由待取数推进为可复算

按现役财务接口金额口径人民币元读取，以下展示为亿元，均为2026H1集团口径：经营现金23.8389；现金购建长期资产52.5336；投资活动净流量-54.8253；筹资活动净流量41.3967；汇率影响-1.9822；期初现金等价物61.0472；期末69.4753。

按未舍入元值计算：6104722626.15+2383887276.10-5482528738.38+4139667280.73-198222834.12=6947525610.48。与接口期末现金相差0.00元。经营现金减现金资本开支为-2869470684.35元，即-28.6947068435亿元。这不是FCFE，也不能由负差额直接认定扩产失败；但原稿“未取得H1纯现金资本开支和现金桥”的取数缺口已有实质进展。54.83亿元投资净流出仍不能被当作52.53亿元现金资本开支。

Q1未舍入现金桥也完全相等，期末7008115772.42元。原稿显示数字相加70.09、报告期末70.08的0.01亿元差异与这组未舍入金额的舍入相容；原文件不事后修改。

H1货币资金8792751795.06元，与现金等价物相差1845226184.58元。差额不是全部受限资金的证明，仍需附注拆解。资产=负债+归母权益+少数权益、集团净利=归母+少数损益，以及5产品收入/成本对利润表收入/成本均按接口未舍入数勾稽为0差额。勾稽不能代替源资料真实性或会计分类审核。

## 卖方预测证据不再只有网页提及

report_rc的np/op_rt按Tushare官方doc292为万元；np名称是“预测净利润”，不能在未核原PDF时统一改称归母。6家2028Q4有非空np：东方282.28、上海239.55、开源210.05、中泰285.67、招商249.50、国盛399.56亿元。原v1.0的最强集团股东收益约107.98亿元，仍须与这些预测统一口径并解释收入、利润率、资本/融资及归属差异，不能直接把原模型抬高至卖方数字。

长江证券3行np和收入为空，只返回EPS/PE等字段，没有用EPS乘当前股数替造预测利润；中金本批只有2026/2027预测期。23行既不是23家机构，也不是23份独立报告，本次未计算共识或概率。

两个接口的日期不一致：上海report_rc为20260828、research_report目录为20260902；开源分别为20260825和20260826。可能涉及报告/目录日期或版本，但未读到PDF前不作原因定论，也不强行join。通过接口实际返回两条公开PDF定位，随后用网页工具打开原URL及去除时间戳查询的同路径都失败；本轮没有读到这两份原PDF，不能声称获得了研报全文。

## 行情和仍需保留的限制

daily与daily_basic均返回20261015收盘124.20元；total_share为183160.7532万股，即1831607532股；total_mv为22748565.15万元，即2274.856515亿元。价格乘返回股数为227485655474.4元，比接口total_mv换算多3974.4元，保留该小差异，不伪称精确相等。两者在原稿亿元两位小数尺度均为2274.86；该通路观察不自动代签全套合格行情、Odds或研究质量。

H1财务接口的ann_date/f_ann_date为20260822，与原稿转载日期20260821不同。原两次公告接口失败是上游504，不能据此确定公告不存在或认定日期错误；下一次公告定位应核8月22日及实际披露记录。

后续不应重抓已保存且时效适用的9项数据，先复用确切SourceRef，继续受限资金/融资条款、资本批次和经营预测分歧。原PDF附注、最终H股发行、订单与ROIC证据、完整估值返工仍未完成。期后事件不回写研究截止日。

官方字段依据：Tushare doc292（预测万元及预测期）、doc44（现金流字段）、doc32（总股本万股/总市值万元）；金额口径声明与来源真实性、是否归母分别核实。本次数值附录只检查声明的7个H1输入与3项复算，其余计算见保留脚本，不冒称全文自动审计。


<!-- decision-os-numbers-v1:start -->
Declared numerical scope only; stored-value consistency is not source authenticity, economic sufficiency, qualified Market/Odds or human acceptance. Unmarked prose is not audited.

| ID | Computed display | Status |
|---|---|---|
| h1_cash_bridge | [UNAVAILABLE:h1_cash_bridge:OPERAND_NOT_AVAILABLE] | blocked |
| h1_cash_bridge_residual | [UNAVAILABLE:h1_cash_bridge_residual:OPERAND_NOT_AVAILABLE] | blocked |
| h1_cfo_minus_capex | [UNAVAILABLE:h1_cfo_minus_capex:OPERAND_NOT_AVAILABLE] | blocked |

本次接口投影的H1现金桥期末为[UNAVAILABLE:h1_cash_bridge:OPERAND_NOT_AVAILABLE]；与接口期末现金的差额[UNAVAILABLE:h1_cash_bridge_residual:OPERAND_NOT_AVAILABLE]；经营现金减现金资本开支[UNAVAILABLE:h1_cfo_minus_capex:OPERAND_NOT_AVAILABLE]。未将其等同FCFE，币种/期间/归属由研究者声明，未获原PDF。

<!-- decision-os-numbers-v1:input -->
```json
{"calculations":[{"id":"h1_cash_bridge","operands":["c_cash_equ_beg_period","n_cashflow_act","n_cashflow_inv_act","n_cash_flows_fnc_act","eff_fx_flu_cash"],"operation":"sum","period":"2026H1","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"0.00","id":"h1_cash_bridge_residual","operands":["h1_cash_bridge","c_cash_equ_end_period"],"operation":"difference","period":"2026H1","places":2,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY"},{"id":"h1_cfo_minus_capex","operands":["n_cashflow_act","c_pay_acq_const_fiolta"],"operation":"difference","period":"2026H1","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"}],"inputs":[{"id":"c_cash_equ_beg_period","locator":{"end":869,"start":856},"period":"2026H1","scope":"consolidated-cashflow","source":0,"subject":"002384.SZ","unit":"CNY","value":"6104722626.15"},{"id":"n_cashflow_act","locator":{"end":785,"start":773},"period":"2026H1","scope":"consolidated-cashflow","source":0,"subject":"002384.SZ","unit":"CNY","value":"2383887276.1"},{"id":"n_cashflow_inv_act","locator":{"end":827,"start":813},"period":"2026H1","scope":"consolidated-cashflow","source":0,"subject":"002384.SZ","unit":"CNY","value":"-5482528738.38"},{"id":"n_cash_flows_fnc_act","locator":{"end":841,"start":828},"period":"2026H1","scope":"consolidated-cashflow","source":0,"subject":"002384.SZ","unit":"CNY","value":"4139667280.73"},{"id":"eff_fx_flu_cash","locator":{"end":855,"start":842},"period":"2026H1","scope":"consolidated-cashflow","source":0,"subject":"002384.SZ","unit":"CNY","value":"-198222834.12"},{"id":"c_cash_equ_end_period","locator":{"end":883,"start":870},"period":"2026H1","scope":"consolidated-cashflow","source":0,"subject":"002384.SZ","unit":"CNY","value":"6947525610.48"},{"id":"c_pay_acq_const_fiolta","locator":{"end":799,"start":786},"period":"2026H1","scope":"consolidated-cashflow","source":0,"subject":"002384.SZ","unit":"CNY","value":"5253357960.45"}],"schema":"research-numbers-v1","sources":[{"manifest":{"commit":"4afac97dd5fd0bfcb506e7b52af95f6b3f46fdb8","path":"source-views/dsbj-relay-38838698248/08d9e84324249f6389274b465aa1900c36184ebe02f5c46836f4d0ee9819fa7d/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"07c908d3092fb15efc0b8f4b1dc9120eba5a93ac4b79b4d49d7148e835ecf2bd"},"selection":{"columns":[],"end":null,"kind":"all","start":null}}],"template":"本次接口投影的H1现金桥期末为{{number:h1_cash_bridge}}；与接口期末现金的差额{{number:h1_cash_bridge_residual}}；经营现金减现金资本开支{{number:h1_cfo_minus_capex}}。未将其等同FCFE，币种/期间/归属由研究者声明，未获原PDF。"}
```
<!-- decision-os-numbers-v1:end -->


<!-- decision-os-delivery-v1:start -->
```json
{"base_revision":{"commit":"ff37cf294f2f8a667efc4efc1a51ad27216b6c87","path":"research/cases/dsbj-002384sz-20261015/revisions/dsbj-20261016-v1-2/record.json","repository":"auguspp/Decision-OS-2","sha256":"14190918dda1f52d924b1f10b0dd615979be5645b365738d67581e8edbadf48f"},"body_mode":"delta","input_sha256":"fd8bc0ec9ccc825ca6052fead431e4e070f7cb08e8704e7fa5bddb18c7d32020","intent_reference":{"commit":"257cb9b40fb57dd028865c2f02ea797337b471a9","path":"research/artifacts/delivery-932b8596712cb1591304452be5f96bc44d675d04d274df80958a6987b5511111/a715f340070b68248d0e65b3baf3884e41b691742806053de9d71c46e5a468d9/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"d45aebcfd5ab1cfad4f4e238a3efbc5c5a6fe817eea8fea7bfcbd27fa99f3684"},"items":[{"id":"report-v1-0-html","kind":"artifact","metadata":{"artifact_id":"dsbj-20261015-v1-0-html","media_type":"text/html","name":"DSBJ_Full_Research_20261015(1).html","sha256":"660fb9f4be11de8bdbe1d397d7cb8509276b33996ff921e4598a2b26562f7c98","size":62524},"reason":"实际提供的v1.0完整HTML原件，样式、导航及历史文字不改写。","reference":{"commit":"ffe7723f390dd6d27e492bb73c312d1124e9b9ea","path":"research/artifacts/dsbj-20261015-v1-0-html/8c51b8b736af5594c23e9dec189076c57e20c554e75acfd5a66e063800384e02/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"fe03c4faacd91223a6fbdaf37486d6656d1e2cf46464f5ae5105b2a7cf7ae862"},"required":true,"role":"report-original","status":"retained"},{"id":"calculation-v1-0-xlsx","kind":"artifact","metadata":{"artifact_id":"dsbj-20261015-v1-0-xlsx","media_type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","name":"东山精密_FullResearch_计算底稿(1).xlsx","sha256":"9882335cbd3c87a49f889dde27b5ab93479b4f34a8d1f39bfb737482e9bf002b","size":44960},"reason":"实际提供的v1.0十表XLSX原件，按原始ZIP字节保管，不重算或转换。","reference":{"commit":"9470a394cb14d908d8fc1d5af8709040f4e9cddb","path":"research/artifacts/dsbj-20261015-v1-0-xlsx/b8ee6753402c49f15904a2e52a598eedbdfede40e181c7814b49598fc69dd09a/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"25d3dbfa4a83bffe57afdf4796d567fe7abc1144e6b5f37bf477720d68ff0386"},"required":true,"role":"calculation-file","status":"retained"},{"id":"external-source-originals","reason":"接口已经取得结构化数值及研报URL，但公司财报PDF及券商原PDF仍未读到；原件与派生SourceRef分别记账，不用数值返回冒充全文。","required":true,"role":"source-evidence","status":"missing"},{"id":"relay-evidence-20261016","kind":"source","reason":"本轮9项真实查询的全部选定字段合辑，已核对38行与原生产物及请求身份，原生PDF仍缺失。","reference":{"manifest":{"commit":"4afac97dd5fd0bfcb506e7b52af95f6b3f46fdb8","path":"source-views/dsbj-relay-38838698248/08d9e84324249f6389274b465aa1900c36184ebe02f5c46836f4d0ee9819fa7d/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"07c908d3092fb15efc0b8f4b1dc9120eba5a93ac4b79b4d49d7148e835ecf2bd"},"selection":{"columns":[],"end":null,"kind":"all","start":null}},"required":true,"role":"source-evidence","source_index":0,"status":"retained"}],"schema":"research-delivery-manifest-v1"}
```
<!-- decision-os-delivery-v1:end -->


## Research workpaper (author declarations, not acceptance)
<!-- decision-os-research-workpaper-v1 -->
```json
{"method":{"id":"decision-os-research-full-v2","sha256":"6509212be4fa216b6d2333ffa1a2568b4b7e556070d390d19939eb382ededc62"},"progress":{"challenge_note":"","challenges":[{"argument":"扩产收入增长可能伴随融资和营运资金侵蚀股东现金","disposition":"unresolved","evidence":[0],"question_id":"q-capital","response":"H1接口CFO减现金资本开支为负28.6947亿元，强化资本消耗必须建模的约束；现金回报、订单和增量ROIC仍未证明。"},{"argument":"原版标作最强的2028集团收益107.98亿元仍显著低于多家公开卖方2028E预测，原条件估值缺少同期外部预期闭合","disposition":"changed","evidence":[0],"question_id":"q-valuation","response":"真实接口取得6家非空2028Q4预测净利润210.05至399.56亿元，但与原107.98亿元股东收益口径尚未统一；原价位继续只是内部条件演算，不签完整估值。"},{"argument":"聚合页面的预测均值可能混入不同报告版本、转载日期或证券股本口径，不能未经Relay重建就称机构共识","disposition":"unresolved","evidence":[0],"question_id":"q-sellside","response":"保留质疑：两个接口报告/目录日期不一致，8机构23行不等于23份独立研报，np有空值且原PDF未取得；本轮不计算共识、不伪造概率。"}],"findings":[{"evidence":[],"finding":"保留v1.0多引擎经济结构、AI PCB与索尔思内部价值防重算判断；本次没有新的分部原件推翻此分析。","material":true,"next_action":"","question_id":"q-economics","remaining_kind":null,"status":"answered"},{"evidence":[],"finding":"保留v1.0关于并购并表、负商誉与索尔思一次性条款修改收益的核算；新增研报预测不改变已知历史会计事实。","material":true,"next_action":"","question_id":"q-earnings","remaining_kind":null,"status":"answered"},{"evidence":[0],"finding":"已通过现役Full接口取得H1现金流和资产负债原值：现金资本开支52.5335796045亿元，CFO减现金资本开支-28.6947068435亿元，现金等价物桥未舍入差额0元；仍未取得PDF附注和受限资金等细目。","material":true,"next_action":"复用本轮SourceRef核PDF附注、货币资金与现金等价物18.4522618458亿元差额、债务期限及利息/租赁，再修订股东现金而非把CFO-capex当FCFE。","question_id":"q-cash","remaining_kind":"disclosed-unverified","status":"open"},{"evidence":[0],"finding":"已取得H1表内债务和现金数据，为资本桥提供实数，但17亿美元投入分期、客户承诺、最终发行和增量ROIC仍不可由三表行值单独识别。","material":true,"next_action":"结合已得现金数据核投入批次与客户承诺，取得最终发行资料再做融资后每股桥；不重复抓已保存数据。","question_id":"q-capital","remaining_kind":"currently-unidentifiable","status":"open"},{"evidence":[0],"finding":"取得原估值日Relay价格/股本，并取得6家非空2028Q4预测净利润210.05至399.56亿元；原107.98亿元较强集团股东收益与外部预测仍需同口径求解，未重新签发目标价。","material":true,"next_action":"将外部预测按报告版本、归属、收入/利润率和再投资要求与原模型逐项对照，处理日期差异后重估并反算。","question_id":"q-valuation","remaining_kind":null,"status":"reframed"},{"evidence":[0],"finding":"report_rc返回23条、8机构预测期记录，research_report返回上海和开源两条PDF定位；日期分别8/28对9/2、8/25对8/26，原PDF打开失败，不能称一致预期或原文已核。","material":true,"next_action":"复用已保存预测表，沿两条原PDF定位取原件并核真正报告日期、版本、净利归属和预测假设；继续查缺失机构，不再等待首次接口建设。","question_id":"q-sellside","remaining_kind":"disclosed-unverified","status":"open"}],"limitations":["两份v1.0原创原文件已保管并沿清单保留；第三方公司财报和券商PDF仍未获得。","本轮SourceRef为9项选定字段合辑，38行包含2对update_flag重复版本，不能重复求和；不是完整HTTP原件或历史PIT证明。","接口返回值与原报告时点存在日期差异，np的归母属性及完整融资、受限资金、净投资口径仍需原件审核。","新计算不改写原HTML/Excel，不新增正式目标价、概率、人工接受或Decision；公开元数据和数值不等于受限原文可再分发。","数值审计单位/期间/主体/范围为研究者声明，未捏造来源context字段，不将conditional升级为全部checked。"],"source_changes":"新增本轮真实接口9项完整字段投影的合辑SourceRef；不删除旧来源，不把PDF定位升级为原件，原8/21与新ann_date8/22差异单列。","sources":[{"manifest":{"commit":"4afac97dd5fd0bfcb506e7b52af95f6b3f46fdb8","path":"source-views/dsbj-relay-38838698248/08d9e84324249f6389274b465aa1900c36184ebe02f5c46836f4d0ee9819fa7d/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"07c908d3092fb15efc0b8f4b1dc9120eba5a93ac4b79b4d49d7148e835ecf2bd"},"selection":{"columns":[],"end":null,"kind":"all","start":null}}],"stop":"continue","stop_reason":"实际接口数值已取得并可复算H1现金桥，研报日期/原PDF与资本回报、融资和估值分歧仍需继续；取证及保管不签完整Full。"},"request":{"case_id":"dsbj-002384sz-20261015","kind":"full","mandate":"用户于2026-10-15直接委托东山精密（002384.SZ）Full Research，研究经济结构、跨期财务、并购与现金、竞争和扩张、估值、关键反证与合理接续。2026-10-15已明确授权将该案例原创研究成果公开保存到Decision OS 2.0 records；第三方付费原文、私密材料与正式人工Decision不在授权范围。研究截止2026-10-15；后续补证属于同一委托。旧本地案例标签DSBJ-002384SZ-20261015映射为本存储ID。","predecessor":{"commit":"ff37cf294f2f8a667efc4efc1a51ad27216b6c87","path":"research/cases/dsbj-002384sz-20261015/revisions/dsbj-20261016-v1-2/record.json","repository":"auguspp/Decision-OS-2","sha256":"14190918dda1f52d924b1f10b0dd615979be5645b365738d67581e8edbadf48f"},"questions":[{"question_id":"q-economics","text":"传统FPC、AI PCB、光模块和精密组件分别通过什么经济机制形成可持续利润？"},{"question_id":"q-earnings","text":"并购与经营成长如何区分；财务利润、非经常损益与持续经营利润能否跨期闭合？"},{"question_id":"q-cash","text":"经营现金、营运资金、资本开支、债务与少数权益之后，普通股股东可得的现金到底是多少？"},{"question_id":"q-capital","text":"索尔思与AI PCB扩产需要多少可归因资本、客户订单和收益率；融资及稀释风险是否改变每股经济性？"},{"question_id":"q-valuation","text":"截至2026-10-15价格隐含怎样的收入、利润、资本和持续期要求，是否存在可靠安全边际？"},{"question_id":"q-sellside","text":"券商及外部经营预期与研究假设差在哪里，哪些证据能验证或否定最强竞争解释？"}],"scope_change_reason":""},"schema":"research-workpaper-v1"}
```
