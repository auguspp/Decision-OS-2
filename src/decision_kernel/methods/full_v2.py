"""Full execution discipline distilled from the adopted Kernel methods, not a new agent."""
from decision_kernel.domain.records import digest, encoded
from decision_kernel.methods.research_v1 import research_method as method_v1

# These are instructions for the authorized researcher. No keyword test, number of
# passes, work-product count or author statement can certify substantive execution.
DEPTH = (
    ("execution-loop", """一次明确Full委托包含必要的内部迭代，不是只写初稿。先恢复真实问题、已有证据/计算和未决项，按对结论的影响选择下一项最有判别力且当前可推进的工作：取得或重读证据→计算/检验机制→构造最强可行替代解释→据新证据修订→再次检查重大缺口。已取得未分析的资料优先消化，已披露未核的关键附注/合同优先获取；发现这些缺口本身不是Full完成。不要等用户再说“查现金”“看合同”“继续深挖”。没有固定轮数/角色数、没有后台模型worker；当前工具或权限真实中断时保存部分成果，说明具体未完成项，不承诺对话外自动续作。问题已回答、被证据否定/重构，或当前合理获准途径已不能有效区分主要解释时可以停止；不消灭所有未知，也不以臆测预算停止。"""),
    ("economic-architecture", """先判断这家公司在经济上是什么，后选情景格式。只有收入形成、毛利、资本占用、客户议价权、持续期或融资归属差异足以改变股东结果时才拆经济引擎；披露分部与分析引擎须映射，单引擎足够时不硬拆。逐引擎回答量/用户/项目/产能×价格/抽成如何到收入、毛利、费用、现金、再投资和每股归属。检查共同客户、替代、共享固定成本、滞留成本、交叉补贴、资本迁移和融资约束。独立变化不等于统计独立，不能相乘未经建立的概率。检验有经济依据的反向组合，例如旧业务修复而新业务失败、收入上升但毛利/现金下降、新业务成功却需要增发；不是强制熊/中/牛三格。经营事实支持到何层就止于何层，产品发布/产能目标/播放量不升级为可持续盈利。"""),
    ("financial-diagnostics", """对影响判断的业务取得足以识别周期和会计变化的跨期资料。通常争取约5年与最近8季度，按存续、披露和问题调整，不把年限/表格数当门槛。优先原始年报/半年报/附注/合同与合格财务表，转载可定位但不是已读原件。比较量价/收入、产品组合、成本毛利、应收/存货/应付/合同资产负债、非经常损益、信用/资产减值、经营现金、资本开支、融资和股本；累计转单季保留同口径差分，重述/并购/汇率/重分类先调节。要回答利润表故事能否被资产负债与现金支持，不止计算几个比率。每个重大矛盾形成具体问题、竞争解释、可区分证据和相依价值影响。负现金不直接等于破产，应收增长不直接等于造假；剔除减值只可作有边界反事实，不能永久免除真实信用损失。"""),
    ("contract-and-project-economics", """对内容、工程、研发里程碑、授权或其他项目型业务，沿实际公开合同和项目附注检查：参与主体/权益、项目状态、承诺与实际投入、收入确认条件、应收与到账、发行/推广/履约成本、税和收益分配、后续资本及失败回收。在手订单须区分已签/意向、金额/收入确认/现金、客户与期间，不能把订单全额变成当期高毛利收入。净额/总额确认、委托制作/联合投资/IP许可/平台分账可有完全不同的经济性；关联出资和制作成本冲减不当作额外收入或利润。签约≠到账≠收入≠归母利润≠可分配现金。无法逐项目量化时给覆盖范围、代表性限制和有依据的上下界，不以披露不足跳过能核到的合同。一般轻资产公司也可能大量占用存货、应收和研发资本。"""),
    ("owner-cash-and-capital", """把经济引擎结果勾稽到集团、普通股股东和同期间每股结果：抵销/总部费用、利息税、少数股东、权益法/持股归属、现金分类、维护及必要增长投入、债务到期与稀释都按实际口径处理。普通非金融企业可从NOPAT+已扣非现金费用-资本投入-非现金营运资金增加分析FCFF，或从净利润按同口径到FCFE；从已报CFO出发时营运资金通常已在其中，不再扣一次。先核利息、税、租赁、资本化研发与并购分类。CFO-capex不是自动等于可分给普通股的现金。融资不是经营创造现金，增发需进入股数；已纳入增长的资本不能免费。检查现金/理财/受限资金、债務、期后变动和治理分配能力。用期初现金+经营/投资/融资/汇率变动=期末现金验证资金桥；少投项目改善现金与持续再投仍赚钱分别说明。未知分部资本/现金不分摊造数，增量ROIC只在可比且可归因时解释。"""),
    ("competition-and-reference-class", """行业需求不是公司的份额与定价权证据。具体检查谁控制客户、渠道、IP/技术/牌照、采购预算和分账规则；成本节省归公司还是客户，新增供给/买方自制/替代品如何改变单价、获客、毛利和持续期。按同经济机制、阶段、起点、期限选择外部参照，纳入失败和未成熟对象，解释异同及选择范围。参照必须约束某项参数或机制，而非列同行名单/一条买方支出证明公司受益。少量案例只作类比，不报成功率/基准概率；公开范围没有合格参照就记录检索途径与识别限制，并采用其他有据约束。"""),
    ("substantive-challenge", """在预期对照和结论修订前，留下可定位的经营版本/计算及已知股价、卖方预期和用户反馈暴露，不冒称市场盲。自查攻击能够改变结果的假设：遗漏引擎、共同约束、无法兑现的现金桥、坏账/稀释/失败成本、持续期与终值、重复计价。每条挑战须指原假设和来源/公式，提出能区分的证据；实际取得所需证据或作合理获准尝试后，记录改变了哪些变量、计算或结论，或为什么有证据维持；无法解决则保留条件分析。没有新资料也可对已有材料补算和纠错。不用通用宏观风险凑数，不用多个角色投票；同上下文只能称自查，工具算术也不是独立经济审阅。"""),
    ("valuation-and-reverse-closure", """委托涉及投资价值时，在取得尽可能充分的经营/归属桥后独立论证适用估值：盈利口径、持续期、再投资回报、期末状态、折现/倍数、价值日期、币种及股数。使用相关可比、历史表达、基本面回报/资产现金等互补依据，不为贴近股价调参。经营问题不强制DCF；金融、周期、转型、网络和研发项目采用相应资本/期限/权利镜头。先建立有据的基础业务价值，再比较有机构/日期/期间的外部预期和价格要求。重大剩余价值不是分析终点：残余→未来利润/现金要求→可支持这些结果的收入、份额/项目数、单价、净收益率、资本及融资→折回同一当前价格单位；反推再正算勾稽。现金收益率或利润×倍数尺度检验不能替代这条经营链。基础已含的新业务不能全额再加期权价值；现金本金/利息、股权/公允价值收益、分红/期末留存、增长/倍数溢价均检查重复计价。没有参数依据则限定范围，不强给目标价；没有概率保留条件世界，不伪造期望收益或胜率。"""),
    ("stop-and-delivery", """交付前逐项检查真实研究成果而非标题：经济结构及披露映射；关键事实→机制→现金影响；跨期财务矛盾；集团/现金/资本/每股桥；最强挑战及实际处置；适用估值参数、正反算与完整下行。每项只按本委托相关性判定充分/有限/未完成/不适用，说明理由与相依结论，不做总分或关键词PASS。对每个重大未知说明已尝试何种材料/范围、取得什么、仍缺什么、现在能否缩小以及下一项区分证据；“待补核”“没披露”标签不证明合理查证已尽。已取得未分析/已披露未核通常继续当前工作，未来事件列具体观察条件，访问拒绝只限受影响途径。保存失败、公开授权未具备、Relay价格不合格不自动阻断其他已授权经营分析。部分成果可正常保存且清楚未完成；不等同用户接受。首次正常研究的质量由实际成果及人工反馈验收，不以方法加载、CI、自动字段或宿主I/O签收。"""),
)


def full_method() -> dict:
    # Retain stable shared safeguards, enrich the actual returned method (not a
    # link the host may never read), and identify the changed content as v2.
    method = method_v1("full")
    method.pop("sha256")
    method["id"] = "decision-os-research-full-v2"
    method["steps"] += [{"topic": topic, "instruction": text} for topic, text in DEPTH]
    method["execution_owner"] = "authorized-researcher-in-current-session-not-actions-io"
    method["completion_rule"] = "material-question-closure-or-evidenced-local-limits-not-format-pass"
    method["basis"] += [
        "auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8:docs/full-research-economic-architecture-and-causal-state-space-v1.md",
        "auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8:docs/full-research-review-gate-v1.md",
        "auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8:docs/full-research-price-implied-economics-closure-v1.md",
        "auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8:docs/economic-species-underwriting-playbook-v0-2026-09-03.md",
    ]
    return {**method, "sha256": digest(encoded(method))}
