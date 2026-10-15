# 东山精密 Full Research：v1.1 正式修订与研究接续

本研究在原 v1.0 后继追加，不覆盖已有原始研究。2026-10-15 用户现已明确授权将本案**原创研究内容**公开存入 Decision OS 2.0 records；第三方付费报告/PDF原件及个人信息未获许可，不在此修订正文内。下方保留授权前形成的历史补充底稿，其中“尚未授权、尚未存档”的旧状态记载仅描述当时情况，**不再代表本次存档后的状态**。本次研究仍为阶段工作，未经完成券商原件验证与重新估值，不得标为 concluded。

---

# 东山精密（002384.SZ）Full Research 补充研究及存档检查

**日期**：2026-10-15（与原 Full Research 研究截止日期一致）  
**原案例**：`DSBJ-002384SZ-20261015`  
**原交付**：v1.0，本文件为补充/质疑，未改写原版正文、原模型及原有研究结论的历史版本  
**后继版本状态**：v1.1 补证与挑战草稿，尚未完成重估、未完成原版模型重算、不构成新的 `concluded` Full  
**存档状态**：仅对话文件目录的待保管材料；未通过 Decision OS 2.0 `checkpoint` 写入公开 `records`，无远端 `ResearchRevisionRef`，无独立读回回执。

## 一、为何必须把卖方研报纳入 Full 研究

在最新仓库文档中，Relay 历史清册确有 `report_rc`（结构化预测期记录）和 `research_report`（历史研报定位）；2026-10-15 第一轮**已经真实通过 Relay 密钥采集**的，是 `income/balancesheet/cashflow` 等八类财务 API，范围 32 次查询、25 正常、19 非空、7 失败，**并不是**这两个研报 API。现有 `relay-source-runtime.yml` 目前仅接入八类财务接口，故不能把“Relay 财务已在线”直接说成“已使用 Relay 研报”。

- Source: https://github.com/auguspp/Decision-OS-2/docs/sources/relay-source-opportunities.md
- Current batch route: https://github.com/auguspp/Decision-OS-2/blob/main/.github/workflows/relay-source-runtime.yml
- Actual live run receipt: https://github.com/auguspp/Decision-OS-2/issues/12#issuecomment-6195254834

## 二、公开的具体券商预测（尚未通过 Relay 读回）

下面是**公开网页中的机构预测／报告摘要**，不是已经获取并核验的付费 PDF，也不应直接解释为独立计算后的合理价值。不同网页的报告发布日期可能是研报日或转载日；正式版本须逐机构核原报告、研究日、合并口径与股数。

| 机构及网页所示日期 | 2026E归母利润（亿元） | 2027E（亿元） | 2028E（亿元） | 证据层级 |
|---|---:|---:|---:|---|
| 上海证券，东方财富2026-09-02刊载（其他汇总页标注2026-08-28） | 72.31 | 152.14 | 239.55 | 公开研报正文转载/摘要 |
| 东方证券，2026-09-02 | 72.41 | 159.00 | 282.28 | 证券之星机构预测汇总 |
| 东北证券，2026-08-28 | 36.28 | 61.43 | 95.10 | 证券之星机构预测汇总 |
| 国盛证券，2026-08-23 | 75.23 | 239.54 | 399.56 | 公开预测汇总（需核原文） |

公开聚合页面的搜索返回显示一个“截至2026-10-15”的15机构样本，2026E/27E/28E归母利润平均分别约68.50/143.35/248.23亿元（2028E样本11家）；但网页重新打开曾返回更早的动态快照，这些聚合数值**仍需通过 Relay `report_rc` 对同一日期和样本按报告版本重建**，不视为经独立核实的现时一致预期。个别机构高低值也可能含同机构不同日期版本，须先去重。

Sources:
- https://data.eastmoney.com/report/zw_stock.jshtml?infocode=AP202609021828932204 （上海证券研报转载，公开显示收入预测2026E 629.91、2027E 950.79、2028E 1271.43亿元）
- https://stock.quote.stockstar.com/finance/predict_002384.shtml （机构与EPS索引）
- https://basic.10jqka.com.cn/002384/worth.html （聚合预测，仅以检索时刻对应截图/字段为准）
- https://finance.sina.com.cn/stock/bxjj/2026-09-03/doc-iniqpvfw2097464.shtml （东方证券观点与目标价转载）

## 三、挑战原研究的实质修订要求

1. **原研究的场景轴显著偏低**：v1.0 对2028年集团股东收益的条件世界为48.07／71.47／107.98亿元；而多家已列出的券商预测明显更高，个别机构高达约400亿元。尤其不能把107.98亿元称为覆盖市场的“最强情景”，而不告诉读者它距离主流卖方基线有多远。
2. **价格含义需双重条件化**：v1.0 的2026-10-15收盘价124.20元、市值约2274.86亿元若沿用，按公开汇总2028E利润248.23亿元作*简单利润尺度*，约为9.16倍2028E P/E；这一尺度不表示便宜已经成立，因为它高度依赖2027—2028连续兑现、扩产现金回报、债务及融资稀释。不能继续只展示基于保守情景的27.27／65.88／142.75元，而没有同页展示同日期的外部盈利预期。
3. **最有判别力的机理核对**：上海证券公开摘要给出2028E集团收入1271.43亿元、归母利润239.55亿元，相当于假设集团归母净利率约18.84%；同期2026H1已披露归母约29.57亿元/收入277.98亿元，即约10.64%。要解释净利率持续跃升、分部利润、归属、资本投入、融资和现金桥，不能把机构评级或“供不应求”当成答案。
4. **期限和日期基准**：把机构的2026E、2027E、2028E分别映射到同一合并主体、现有与发行后股数；区分报告预测发布日期、研报转载/更新日期、公司公告日期、定价日。
5. **竞争性解释**：券商预测激进可能因未来高端光模块占比急增；研究原模型可能保守，因为未取得这些分部构成数据；或双方都混淆了有机/并表、增长资本和最终归属。这是待核的竞争解释，不可直接以多数观点胜出。

上述1—3项已触发 **挑战修订：原版估值完整性由“可看条件世界”下调为“缺卖方预测桥，不能代表覆盖市场预期的完整投资估值”**；这并非独立审阅或已经完成新的估值重算。

## 四、原始半年报遗漏纠错

已额外定位公司2026年半年度报告全文 PDF URL：

https://static.cninfo.com.cn/finalpage/2026-08-22/1225491182.PDF

公开索引同时显示2026-08-22的原始报告和证券公告转载。**本次通过网页 PDF 直接打开和容器下载均失败，尚未完成原PDF字节与页码核验**。因此“未找到原始全文”不再准确；正确状态是**已定位但未成功获取/逐页核验**，仍不把摘要当原始附注。需要通过合格的来源接入/下载路径读取原件后重开 v1.0 的 `Q-CASH-H1`。

## 五、存档合同与可操作边界

当前 `main` 中 `config/store-binding.json` 指向 **公开仓库的公开 `records` 分支**，其中 `write_enabled=false` 是仓内默认只读绑定。正式研究写入需使用 `research-runtime.yml` 的显式 `checkpoint`、受信写入配置、CAS `expected_head`、稳定 `submission_id`、准确 `method_sha256` 和授权数据；获得实际 `ResearchRevisionRef` 后用 `resume` 精确读回。

普通研究可纳入现有用户委托，不需要每个阶段重新请求研究授权；**公开发表/保管版权许可与研究授权不同**。当前用户并未明确授权将东山精密研究正文和包含其委托的报告公开在 `records`，故不写公开分支。私有版自动持久化目前没有在该公开仓证实的合格真源，暂不能承诺。原报告HTML、Excel、Markdown和manifest所在的会话文件目录不是远端版本合同，不能称为完成 OS2 归档。

- Runtime write contract: https://github.com/auguspp/Decision-OS-2/blob/main/docs/research/runtime.md
- Store contract: https://github.com/auguspp/Decision-OS-2/blob/main/docs/architecture/github-store.md
- Decision-OS-2 records binding: https://github.com/auguspp/Decision-OS-2/blob/main/config/store-binding.json

## 六、继续同一委托的优先顺序

1. #12 原 owner 沿原来源责任接线 `report_rc` 与 `research_report`，用已有 Relay Secret 做精确金融研究请求及来源/时间/权利验证，优先取得东山精密过去两季主要报告，并检验不同机构的预测版本；不复建并行接口。
2. #7 在原 `DSBJ-002384SZ-20261015` 研究底稿中保留已有结论及挑战，分析券商预测差异与模型关系、补全H1原件现金附注，按财务和可分配现金再修订，不重建第一份报告。
3. 如果授权公开本案例**原创研究**，排除受限PDF/原文及隐私资料后，再用现成受信研究宿主提交并独立读回；否则保持未公开、等合格的私有真源，不用 GitHub main/Issue/PR/Actions 产物变相公开。

本补充文件本身也**不是**保存回执或新版本结论。

## Research workpaper (author declarations, not acceptance)
<!-- decision-os-research-workpaper-v1 -->
```json
{"method":{"id":"decision-os-research-full-v2","sha256":"6509212be4fa216b6d2333ffa1a2568b4b7e556070d390d19939eb382ededc62"},"progress":{"challenge_note":"","challenges":[{"argument":"扩产收入增长可能伴随融资和营运资金侵蚀股东现金","disposition":"unresolved","evidence":[],"question_id":"q-capital","response":"继承v1.0未解质疑；新发现机构大额利润预测并未补齐逐产线ROIC、供应商信用、最终融资股数与现金回报证据，仍需约束结论。"},{"argument":"原版标作最强的2028集团收益107.98亿元仍显著低于多家公开卖方2028E预测，原条件估值缺少同期外部预期闭合","disposition":"changed","evidence":[],"question_id":"q-valuation","response":"下调估值完整性评价：原27.27/65.88/142.75元只作为内部条件世界，不代表覆盖市场预期；保留2028年公开卖方95—400亿元数量级差异并明确需要重估及反向模型。"},{"argument":"聚合页面的预测均值可能混入不同报告版本、转载日期或证券股本口径，不能未经Relay重建就称机构共识","disposition":"unresolved","evidence":[],"question_id":"q-sellside","response":"四家机构表格仅标公开网页摘要与待核原件；多家机构的预测值不提供独立概率，等待真实SourceRef和版本验证。"}],"findings":[{"evidence":[],"finding":"保留v1.0多引擎经济结构、AI PCB与索尔思内部价值防重算判断；本次没有新的分部原件推翻此分析。","material":true,"next_action":"","question_id":"q-economics","remaining_kind":null,"status":"answered"},{"evidence":[],"finding":"保留v1.0关于并购并表、负商誉与索尔思一次性条款修改收益的核算；新增研报预测不改变已知历史会计事实。","material":true,"next_action":"","question_id":"q-earnings","remaining_kind":null,"status":"answered"},{"evidence":[],"finding":"现已定位2026H1原始全文PDF网址，但PDF实际读回失败；仍无法核2026H1纯capex、租赁/利息、受限资金及准确融资后现金。","material":true,"next_action":"通过已有合格资料通路读取2026H1原始全文并逐页核现金流量表附注与资产负债细目，然后修订股东现金桥。","question_id":"q-cash","remaining_kind":"disclosed-unverified","status":"open"},{"evidence":[],"finding":"17亿美元扩产规模与H股备案上限已知，最终项目投资、融资股数及增量ROIC缺可核分部现金与客户承诺。","material":true,"next_action":"核真实投产和订单/良率等；取得发行文件与资本开支批次后更新融资后归属和股东收益。","question_id":"q-capital","remaining_kind":"currently-unidentifiable","status":"open"},{"evidence":[],"finding":"保留v1.0条件估值算术，但公开券商2028E利润约95亿至400亿元，与原48/71/108亿场景差距重大，原“最强情景”标签不足以代表机构预期范围；完整估值比较尚未完成。","material":true,"next_action":"将个别机构同口径2026–2028年预测拆成收入、利润率、产能与融资要求并逆算，不把旧价位当经检验目标价。","question_id":"q-valuation","remaining_kind":null,"status":"reframed"},{"evidence":[],"finding":"公开页面识别到上海证券、东方证券、东北证券、国盛证券2026–2028E预测，但研报版本/发布日期/各机构原文与聚合样本未由Relay取得；不能声称一致预期核验完成。","material":true,"next_action":"由#12现有owner在完成当前能力目录后接入report_rc/research_report，核机构报告日期、目标年、数据版本、单位股数与许可，再由#7独立分析预测差异。","question_id":"q-sellside","remaining_kind":"disclosed-unverified","status":"open"}],"limitations":["本次只保管原创中文研究文本与原报告的可读后继引用；第三方报告PDF/付费原文未保存。","公开网页预测尚未从TuShare Relay report_rc/research_report正式取得；外部URL是可追溯定位符，不是可验证的确切 SourceRef。","2026H1原始PDF已定位但读取失败，不能声称已核相关附注；原模型及Excel附件仍在先前对话文件中，不在records研究正文里。","v1.1是挑战与补证阶段，不是基于机构预测重新计算出的目标价格，也不是人工Decision。"],"source_changes":"","sources":[],"stop":"continue","stop_reason":"机构预测与内部条件世界差异已经触发估值完整性修订，但最有判别力的机构原文/数据版本、2026H1附注、经营资本及发行后稀释未闭合；当前保存的是增量版本。"},"request":{"case_id":"dsbj-002384sz-20261015","kind":"full","mandate":"用户于2026-10-15直接委托东山精密（002384.SZ）Full Research，研究经济结构、跨期财务、并购与现金、竞争和扩张、估值、关键反证与合理接续。2026-10-15已明确授权将该案例原创研究成果公开保存到Decision OS 2.0 records；第三方付费原文、私密材料与正式人工Decision不在授权范围。研究截止2026-10-15；后续补证属于同一委托。旧本地案例标签DSBJ-002384SZ-20261015映射为本存储ID。","predecessor":{"commit":"91645376fbed15c2ef81d6feb8c434b781ad3580","path":"research/cases/dsbj-002384sz-20261015/revisions/dsbj-20261015-v1-0/record.json","repository":"auguspp/Decision-OS-2","sha256":"ba891b266e3292151be3dc555c6026bea177228aa89bbd2c3f86f777da545fd3"},"questions":[{"question_id":"q-economics","text":"传统FPC、AI PCB、光模块和精密组件分别通过什么经济机制形成可持续利润？"},{"question_id":"q-earnings","text":"并购与经营成长如何区分；财务利润、非经常损益与持续经营利润能否跨期闭合？"},{"question_id":"q-cash","text":"经营现金、营运资金、资本开支、债务与少数权益之后，普通股股东可得的现金到底是多少？"},{"question_id":"q-capital","text":"索尔思与AI PCB扩产需要多少可归因资本、客户订单和收益率；融资及稀释风险是否改变每股经济性？"},{"question_id":"q-valuation","text":"截至2026-10-15价格隐含怎样的收入、利润、资本和持续期要求，是否存在可靠安全边际？"},{"question_id":"q-sellside","text":"券商及外部经营预期与研究假设差在哪里，哪些证据能验证或否定最强竞争解释？"}],"scope_change_reason":""},"schema":"research-workpaper-v1"}
```
