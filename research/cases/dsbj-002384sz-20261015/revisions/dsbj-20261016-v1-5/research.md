# 东山精密 Full v1.5：不再等待原件，把分歧推进到利润与资本要求

**研究与估值基准：2026年10月15日；本次分析：2026年10月16日。** 承接 v1.4，不重做 v1.0 的五年/八季基线，也不修改已归档的 HTML 和 Excel。本版是同一委托的经济分析增量，不是全新 Full、正式目标价或投资决定。接口原值来自已保管的 run 38838698248；本轮未重复发起成功查询，没有将10月16日的取数时间回填成历史首次可得时间。

## 一、这次真正改变了什么判断

**我撤回“现价主要只能靠很长的高倍数持续期解释”这一过宽表述。** 它仅适用于原模型较低的利润路径。若上海证券接口预测的2028年239.55亿元利润，能够转成同口径、可持续的普通股股东收益，那么在原有基础业务价值、股数和10%要求收益率不变的条件下，剩余成长业务只需约 **11.79倍** 即可正算回124.20元，并不需要35倍。

因此，真正的分歧不是先争论“应给多少倍”，而是：**利润能否从原较强情景107.98亿元提升至200多亿元甚至更高；为得到这些利润需要多少资本；利润留下多少现金，以及能持续多久。** 不能用旧模型未覆盖乐观路径来证明股票必然贵，也不能用机构远期预测对应的低市盈率来证明便宜。所有下述机构数值均为预测，不是公司已实现结果。

v1.0原参数与边界见本案确切历史版本：非光收益30亿元、光收入320亿元、光调整后净收益率25%、索尔思归属97.479%；原报告第九至十章。保留这些参数用于对照，不把它们升级为事实。

## 二、现金矛盾具体落在营运资金与扩产，而不只是净利现金比

以下是原接口人民币元值的复算，展示单位为亿元；半年累计与一季度累计相减得到第二季度。

| 项目 | 2026Q1 | 2026Q2（差分） | 2026H1 |
|---|---:|---:|---:|
| 经营活动现金净额 | 11.2667 | 12.5721 | 23.8389 |
| 购建长期资产支付现金 | 21.5997 | 30.9339 | 52.5336 |
| 前两项差额 | -10.3330 | -18.3617 | -28.6947 |

第二季度经营现金增加，但现金资本开支增加得更多，缺口扩大。半年存货增加的经营现金影响为-26.8718亿元，经营性应收影响为-14.5562亿元，应付增加提供14.3041亿元，合计吸收 **27.1238亿元现金**。这里的 `decr_oper_payable` 按Tushare字段说明实际是“经营性应收项目的减少”，不能望文生义当成应付。

这两组结果使挑战更具体：**光业务放量是否既要垫更多存货/应收，又要预先建设产能？** 合理成长与低回报扩张均可产生负现金，现有表格不能单独区分；区分证据是后续同口径回款、存货转化、增量经营收益与资本投放批次，而不是再算一次CFO/净利润。

从23.8389亿元CFO减52.5336亿元现金资本开支，再减5.3682亿元收购子公司等净支付、2.4648亿元“分配股利、利润或偿付利息”现金，得到选定用途后的差额 **-36.5277亿元**。该合并行不能拆成纯利息；租赁及其他融资支付尚未全部分类，因此这仍然**不是FCFE**。CFO已经包含营运资金变化，不能再扣27.1238亿元一遍。

同期取得借款109.3203亿元、偿债64.0258亿元，借款净流入45.2945亿元，说明新增融资对资金平衡重要；不声称每笔借款都被追踪至某项设备。去掉上述三项营运资金现金影响后的50.9627亿元余项也不是正常化盈利或可自由分配现金。

### 债务的可用尺度与不可用精度

短期借款、长期借款、一年内到期非流动负债、非流动租赁负债四行合计239.6303亿元；现金等价物69.4753亿元、货币资金87.9275亿元。分别抵扣后为170.1550和151.7027亿元。这只是**选定报表行与两种现金口径的机械对照**，不是已认证净债务区间：一年内到期项目未全部拆分，债券字段为null而非已证实为零，也尚未查明所有债务性负债及现金可用性。18.4523亿元货币资金差额不能整体标为受限资金。现有证据不足以给出精确研究日EV或偿债安全保证。

## 三、卖方比原模型乐观在哪里：先比较收入与利润率

`report_rc`的收入和预测净利润字段按官方文档为万元，下表换算亿元。日期为该接口的研报日期；原PDF未核，字段np不能对所有机构一律等同归母或调整后股东收益。23条预测期记录不是23篇独立报告，也不形成一个经过样本治理的“共识”。

| 机构 | 接口日期 | 2028收入 | 2028预测净利润 | 隐含净利率 |
|---|---|---:|---:|---:|
| 东方证券 | 2026-09-02 | 1336.86 | 282.28 | 21.12% |
| 上海证券 | 2026-08-28 | 1271.43 | 239.55 | 18.84% |
| 开源证券 | 2026-08-25 | 1299.56 | 210.05 | 16.16% |
| 中泰证券 | 2026-08-24 | 1400.23 | 285.67 | 20.40% |
| 招商证券 | 2026-08-23 | 1264.57 | 249.50 | 19.73% |
| 国盛证券 | 2026-08-23 | 1741.47 | 399.56 | 22.94% |

### 用上海这一条可观察路径做分解，不把它当共识

上海预测2026年收入629.91亿元、利润72.31亿元，2028年收入1271.43亿元、利润239.55亿元。半年实际收入277.9768亿元、归母29.5690亿元。**仅在利润口径可比的条件下**，要实现2026年预测，下半年须做到收入351.9332亿元、利润42.7410亿元、净利率12.14%；半年归母净利率为10.64%。这提供了比“AI需求很好”更可检验的近期尺度，但不是公司指引。

2028年若只把收入提高到1271.43亿元、净利率停留在半年10.64%，利润约135.24亿元；与239.55亿元之差约104.31亿元来自净利率提升这一条件。它可能来自光/AI PCB占比、更高毛利、费用杠杆等，**本轮尚无机构分部原模型能把这104.31亿元具体归因**。

与原较强世界107.9832亿元相比，差额为131.5668亿元。只改变一个变量的诊断如下：保留非光30亿元和光25%收益率，光收入需要约859.88亿元；保留光收入320亿元及25%收益率，非光收益要达到161.57亿元；只靠光利润率、其他都不变，则要约67.18%。三者不是上海证券真正采用的假设，而是说明**不能只在原模型上小幅上调利润率就补上差距**。需要更大的业务规模、额外引擎贡献、不同归属/利润口径或多项变化共同解释；不能直接断言机构预测不可能。

## 四、价格反推重算：利润路径与持续期存在替代，资本却不能免费

固定原估值日价格124.20元、股数18.31607532亿股，权益价值2274.8566亿元；到2028年末按10%回报、实际天数/365.25折算2.21218年，要求期末权益价值2808.8091亿元。仍保留基础业务2028年20亿元收益及原11.0357倍，对应220.7143亿元，不让成熟业务无依据分享成长倍数。

设成长部分2028年倍数为M、可持续集团股东收益为P：

`P = 20 + (2274.856554744 × 1.10^2.212183436 - 220.714285714) / M`

| 成长部分倍数M（分析输入） | 所需集团股东收益P（亿元） | 正算回基准价 |
|---:|---:|---:|
| 12 | 235.67 | 124.20元 |
| 18 | 163.78 | 124.20元 |
| 25 | 123.52 | 124.20元 |
| 35 | 93.95 | 124.20元 |

这不是新的四档目标价，更不是概率分布。它说明：原107.98亿元路径确实更依赖较高倍数，但239.55亿元路径只需成长部分11.79倍；六家2028预测在**可持续股东收益可比**条件下对应的成长部分门槛约6.82–13.62倍。供应商PE引用的价格时点可能不同，本表没有照抄它们；当前负债已在股东收益口径的融资成本假设中处理，不再机械重复扣一次净债务。

这些逆算还假设没有新增净股数、没有另加中间分红或现金资产，且2028年利润是可重复的经济收益而非暂时高点。H股发行、融资用途或收益归属变化，都要求更新整个权益/每股关系，不能只改一个股数。因原PDF和资本路径未核，本轮没有据此给出无条件买入价。

### 资本约束使“低远期PE”不自动等于便宜

上海路径2026到2028收入增加641.52亿元。若仅作压力测试，假设集团增量收入/增量经营资本为1.5、2或3倍，对应增量资本427.68、320.76或213.84亿元。**这些周转率是研究者假设，不是已披露ROIC或新厂效率；金额包含营运资本与净经营资产，不是现金资本开支，不能与光业务119亿元预算直接相减制造资金缺口。** 这是要求乐观路径交代资金与周转效率，而不是否定它。

另沿原股东现金近似式 `收益×(1−增长率/增量股本回报)` 检验：在239.55亿元收益基础上，增长15%、增量回报20%时现金近似仅59.89亿元；增长30%、增量回报30%时为零。若增长超过资本回报可内生支持的水平，须追加资本或降低增长。上述都是稳态约定下的敏感性，不是逐季融资预测；不能把预测利润全额当分红，也不能将负现金直接视作价值为零。

## 五、战略性停止：停止低收益取证，不删除风险，也不放弃已能完成的分析

用户明确允许难取证据战略性放弃。本轮不再重复运行已四次上游排队失败的anns_d/20260822；已成功的九项查询也不重抓。对既有两条研报PDF各作一次本轮直接读取，对已定位半年报PDF一次读取，并检查两个替代公告索引：原件仍未返回，索引打开却退到旧页面。**这些是当前通路的访问结果，不证明报告不存在。** 具体URL和返回类型保存在配套 `retrieval_limits.json`。

| 停止主动重复追索的对象 | 本阶段采用的替代与失去的结论 | 重新开启条件 |
|---|---|---|
| 2026H1原PDF及完整附注 | 复用已存现金流/资产负债字段，能算选定现金缺口；不能精确分清受限资金、全部租赁利息、到期和非经常项目 | 实际可读取原件、公司新增可核披露，或融资判断必须依赖这些细目 |
| 上海/开源原研报与所有机构版本 | 用保留日期的接口预测做条件比较；不认证一致预期、全部归母口径或具体分部预测；不强行合并日期不一致记录 | 原件或可信版本对照可读，或预测被修订并有可追溯依据 |
| 客户订单、良率、逐批资本回报 | 给出需要达到的收入/净利率/资本条件，不捏造客户承诺和项目IRR | 出现可归因的订单/产能/投产收益及资本批次披露 |
| 最终H股发行净款与实际股数 | 保留原条件融资分析，不把上限或申请状态当完成 | 取得对应日期的正式发行结果；期后事实另立时钟，不回填10月15日 |

**本阶段经济增量到此收口；上述相依问题保留未解决，暂停主动重复追索，等待新证据触发，不创建自动重试或后台监控。** 为不把真实未解挑战改成“已解决”，原合同中的研究状态保留blocked，并明确它表示本阶段取证边界，而不是其他已授权任务都不能做。没有承诺完整原件齐备，也没有通过改标签、改缺项或改公式来换取“Full已完成”。

目前可以形成有用但有限的判断：光业务改善与扩张资金压力同时存在；原模型偏低的利润上限不能排除更乐观价值路径；要把该路径升级为投资依据，需要验证可持续归属利润与资本回报，而非等待所有文件齐全才开始分析。若这些关键条件仍不能识别，可以放弃本案的强估值结论，把注意力移到更可验证的机会，而不是不断增加报告页数。

## 证据与复现

原值：已保管SourceRef合辑 `dsbj-relay-38838698248` 及H1现金CSV，确切版本随本修订保留；新标量CSV只为定位和复算，不是新的独立来源。原始捕获、`analyse.py`、Decimal计算结果和读取失败记录在增量计算包；旧HTML/XLSX原字节不变。当前数值入口仅核声明范围，不审计全文或经济正确。

字段定义：[Tushare现金流量表doc44](https://tushare.pro/document/2?doc_id=44)、[资产负债表doc36](https://tushare.pro/document/2?doc_id=36)、[盈利预测doc292](https://tushare.pro/document/2?doc_id=292)。网页定义用于解释字段，不等于发行人原件。上海预测公开转载与接口相符，但原PDF仍未核；本轮不以转载填补全部来源权利或版本认证。

阶段保存、声明清单完整性、数值一致性、经济充分性和人工接受分别记录。第三方原件缺失继续使档案显示partial，原文件与本次原创计算成功保存不能将其抹除。


## 计算脚本（本修订原文的一部分，不执行）

下面保留本次原计算脚本字节对应的UTF-8文本；输入为原run38838698248的capture，相关九个成功查询已有SourceRef合辑。原ZIP未通过类型校验，不声称其已成为远端附件。

```python
"""DSBJ v1.5: reproduce conditional economics from the retained capture, never fetch data.
Run: python analyse.py [directory containing previous_capture.jsonl]
Amounts are CNY 100m unless named otherwise. No probabilities, trade or target price.
"""
import json,sys
from pathlib import Path
from decimal import Decimal, getcontext
from datetime import date
getcontext().prec=32
D=Decimal
BASE=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent
raw=[json.loads(x,parse_float=D) for x in (BASE/'previous_capture.jsonl').read_text().splitlines()]
def table(api,period=None):
    chosen=[x for x in raw if x['use']['api']==api and (period is None or x['use']['parameters'].get('period')==period)]
    assert len(chosen)==1
    c=chosen[0]['capture']; assert c['complete'] and c['error'] is None
    out=[dict(zip(p['table']['fields'],r)) for p in c['pages'] for r in p['table']['items']]
    assert all(r.get('ts_code')=='002384.SZ' for r in out)
    return out

def latest_same(rows):
    keys=set(rows[0])-{'update_flag'}
    assert all(all(r[k]==rows[0][k] for k in keys) for r in rows)
    ones=[r for r in rows if r.get('update_flag')=='1']; assert len(ones)==1
    return ones[0]
def amt(v):
    assert v is not None
    return D(v)/D(10**8)
h1=latest_same(table('cashflow','20260630'));q1=latest_same(table('cashflow','20260331'))
bs=latest_same(table('balancesheet'));inc=latest_same(table('income'))
q2={k:h1[k]-q1[k] for k in ['n_cashflow_act','c_pay_acq_const_fiolta','n_cashflow_inv_act','n_cash_flows_fnc_act','eff_fx_flu_cash']}
cash={
 'h1_cfo':amt(h1['n_cashflow_act']),
 'h1_cash_capex':amt(h1['c_pay_acq_const_fiolta']),
 'h1_cfo_less_capex':amt(h1['n_cashflow_act']-h1['c_pay_acq_const_fiolta']),
 'h1_cfo_less_capex_less_acquisitions':amt(h1['n_cashflow_act']-h1['c_pay_acq_const_fiolta']-h1['n_disp_subs_oth_biz']),
 'h1_cfo_less_capex_less_acquisitions_less_dividends_interest':amt(h1['n_cashflow_act']-h1['c_pay_acq_const_fiolta']-h1['n_disp_subs_oth_biz']-h1['c_pay_dist_dpcp_int_exp']),
 'h1_operating_wc_absorption':-amt(h1['decr_inventories']+h1['decr_oper_payable']+h1['incr_oper_payable']),
 'h1_net_borrowing_receipts':amt(h1['c_recp_borrow']-h1['c_prepay_amt_borr']),
 'q1_cfo':amt(q1['n_cashflow_act']), 'q1_cash_capex':amt(q1['c_pay_acq_const_fiolta']),
 'q2_cfo':amt(q2['n_cashflow_act']), 'q2_cash_capex':amt(q2['c_pay_acq_const_fiolta']),
 'q2_cfo_less_capex':amt(q2['n_cashflow_act']-q2['c_pay_acq_const_fiolta']),
 'selected_liabilities_not_total_debt':sum(amt(bs[k]) for k in ['st_borr','lt_borr','non_cur_liab_due_1y','lease_liab']),
 'cash_equivalents':amt(h1['c_cash_equ_end_period']), 'money_cap':amt(bs['money_cap']),
 'money_cash_gap_not_proven_restricted':amt(bs['money_cap']-h1['c_cash_equ_end_period']),
 'st_borr_plus_current_noncurrent_liabilities':amt(bs['st_borr']+bs['non_cur_liab_due_1y']),
 'bond_payable':bs['bond_payable'],
}
cash['selected_liabilities_less_money']=cash['selected_liabilities_not_total_debt']-cash['money_cap']
cash['selected_liabilities_less_cash']=cash['selected_liabilities_not_total_debt']-cash['cash_equivalents']
# Explicit display assumption (not incremental ROIC, not evidence of cash availability).
cash['cfo_before_three_wc_adjustments']=cash['h1_cfo']+cash['h1_operating_wc_absorption']
assert h1['c_cash_equ_beg_period']+h1['n_cashflow_act']+h1['n_cashflow_inv_act']+h1['n_cash_flows_fnc_act']+h1['eff_fx_flu_cash']==h1['c_cash_equ_end_period']
market=table('daily_basic')[0];price=D(market['close']);shares=D(market['total_share'])/D(10000)
assert price==D('124.20') and shares==D('18.31607532')
mkt=price*shares
years=D((date(2028,12,31)-date(2026,10,15)).days)/D('365.25')
discount=(D(1)+D('.10'))**years
base_earnings=D(20)
base_multiple=D('1.03')*(1-D('.03')/D('.12'))/(D('.10')-D('.03'))
base_val=base_earnings*base_multiple
required_future=mkt*discount
forecasts=table('report_rc')
sellside=[]
for f in forecasts:
    if f['np'] is None or f['op_rt'] is None: continue
    np=D(f['np'])/10000; rev=D(f['op_rt'])/10000
    row={'institution':f['org_name'],'report_date':f['report_date'],'period':f['quarter'],
         'revenue':rev,'np_field_not_yet_verified_attributable':np,'net_margin':np/rev,
         'current_date_price_over_np_conditional':mkt/np,'vendor_pe_not_current':f['pe']}
    if f['quarter']=='2026Q4':
        row.update(h2_revenue_required=rev-amt(inc['revenue']),h2_np_required=np-amt(inc['n_income_attr_p']))
        row['h2_margin_conditional']=row['h2_np_required']/row['h2_revenue_required']
    if f['quarter']=='2028Q4':
        row['group_multiple_required_at_2028']=required_future/np
        row['growth_residual_multiple_required']=(required_future-base_val)/(np-base_earnings)
        y26=next(v for v in forecasts if v['org_name']==f['org_name'] and v['quarter']=='2026Q4')
        row['revenue_cagr_2026_2028']=(D(f['op_rt'])/D(y26['op_rt'])).sqrt()-1
        row['np_cagr_2026_2028']=(D(f['np'])/D(y26['np'])).sqrt()-1
    sellside.append(row)
# Shanghai is an illustrative external path, NOT consensus or verified segment guidance.
s26=next(x for x in sellside if x['institution']=='上海证券' and x['period']=='2026Q4')
s28=next(x for x in sellside if x['institution']=='上海证券' and x['period']=='2028Q4')
shanghai={'revenue_2028':s28['revenue'],'np_2028':s28['np_field_not_yet_verified_attributable'],
 'h1_parent_margin':D(inc['n_income_attr_p'])/D(inc['revenue']),
 'h2_revenue_required':s26['h2_revenue_required'],'h2_np_required':s26['h2_np_required'],'h2_margin_conditional':s26['h2_margin_conditional']}
shanghai['2028_profit_at_h1_margin']=shanghai['revenue_2028']*shanghai['h1_parent_margin']
shanghai['margin_uplift_profit_component']=shanghai['np_2028']-shanghai['2028_profit_at_h1_margin']
# Algebraic bridge from existing v1.0 strong case: 30 non-optics + 320 optics*25%*97.479%.
old_optical_revenue=D(320);old_optical_margin=D('.25');old_nonoptical=D(30);ownership=D('.97479')
old_strong=old_nonoptical+old_optical_revenue*old_optical_margin*ownership
shanghai['old_strong_owner_income']=old_strong
shanghai['np_to_old_strong_gap_conditional']=shanghai['np_2028']-old_strong
shanghai['required_optical_revenue_at_old_margin_nonoptics30']=(shanghai['np_2028']-old_nonoptical)/(old_optical_margin*ownership)
shanghai['required_nonoptics_profit_at_optics320_margin25']=shanghai['np_2028']-old_optical_revenue*old_optical_margin*ownership
shanghai['required_optical_margin_at_optics320_nonoptics30']=(shanghai['np_2028']-old_nonoptical)/(old_optical_revenue*ownership)
# Two orthogonal fixes to old target: solve thresholds, not arbitrary target price.
thresholds=[]
for multiple in (D(12),D(18),D(25),D(35)):
    earnings=base_earnings+(required_future-base_val)/multiple
    reconstructed=(base_val+(earnings-base_earnings)*multiple)/discount/shares
    assert abs(reconstructed-price)<D('1e-20')
    thresholds.append({'growth_multiple':multiple,'total_owner_earnings_required':earnings,
                       'reconstructed_price':reconstructed,'share_count_100m':shares})
# Explicit aggregate capital-turnover stress, NOT borrowed as a verified company ratio.
revenue_delta=s28['revenue']-s26['revenue']; capital=[]
for turnover in (D('1.5'),D(2),D(3)):
    for margin in (D('.15'),s28['net_margin']):
        capital.append({'turnover_assumption':turnover,'margin_assumption':margin,
         'incremental_operating_capital_2026_to_2028':revenue_delta/turnover,
         'earnings_at_2028':s28['revenue']*margin,
         'sales_growth_with_zero_owner_cash_before_new_financing':margin*turnover,
         'owner_cash_2028_for_30pct_revenue_growth_no_new_financing':s28['revenue']*margin-(s28['revenue']-s28['revenue']/D('1.30'))/turnover})
# Zero owner-cash condition is g/(1+g)=m*k when growth is defined as 2027→2028.
for x in capital:
    mk=x['margin_assumption']*x['turnover_assumption']
    x['maximum_growth_from_prior_year_at_zero_cash']=mk/(1-mk) if mk<1 else None
    del x['sales_growth_with_zero_owner_cash_before_new_financing']
# Cash conversion using explicit equity reinvestment return convention, matching old Full.
retention=[]
for growth in (D('.15'),D('.30'),D('.50')):
    for roe in (D('.20'),D('.30')):
        retention.append({'earnings_growth':growth,'incremental_roe':roe,
                          'owner_cash_fraction':1-growth/roe,
                          'cash_on_239_55_of_earnings':D('239.55')*(1-growth/roe)})
result={'basis':{'asof':'2026-10-15','captured':'2026-10-16','source_run':38838698248,
         'units':'CNY_100M unless declared','forecasts':'report_rc np NOT certified attributable',
         'not_a_forecast_or_target':True,'raw_inputs_unchanged':True},
        'cash':cash,'market':{'price':price,'shares_100m':shares,'equity_value':mkt,'years':years,'discount':discount,'base_value_2028':base_val,'required_total_value_2028':required_future},
        'sellside':sellside,'shanghai_gap':shanghai,'valuation_thresholds':thresholds,
        'capital_sensitivity':capital,'retention_sensitivity':retention}
def default(v):
    if isinstance(v,D):return str(v)
    raise TypeError(type(v).__name__)
(BASE/'calculations.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=default))
print(json.dumps({'cash':cash,'market':result['market'],'shanghai_gap':shanghai,'thresholds':thresholds,
                  '2028':[x for x in sellside if x['period']=='2028Q4']},ensure_ascii=False,indent=2,default=default))

```


<!-- decision-os-numbers-v1:start -->
Declared numerical scope only; stored-value consistency is not source authenticity, economic sufficiency, qualified Market/Odds or human acceptance. Unmarked prose is not audited.

| ID | Computed display | Status |
|---|---|---|
| h1_wc_cash | -27.1238 CNY_100M | conditional |
| h1_gap | -28.6947 CNY_100M | conditional |
| h1_selected_gap_acq | -34.0629 CNY_100M | conditional |
| h1_selected_gap | -36.5277 CNY_100M | conditional |
| q2_cfo | 12.5721 CNY_100M | conditional |
| q2_capex | 30.9339 CNY_100M | conditional |
| q2_gap | -18.3617 CNY_100M | conditional |
| sh_h2_revenue | 351.9332 CNY_100M | conditional |
| sh_h2_profit | 42.7410 CNY_100M | conditional |
| sh_h2_margin | 12.14 percent | conditional |
| equity_value | 2274.8566 CNY_100M | conditional |
| future_value | 2808.8091 CNY_100M | conditional |
| growth_value | 2588.0949 CNY_100M | conditional |
| growth_income | 219.5500 CNY_100M | conditional |
| required_growth_multiple | 11.79 ratio | conditional |

营运资金三项现金影响-27.1238 CNY_100M；Q2经营现金减现金资本开支-18.3617 CNY_100M；若上海预测符合可持续股东收益口径，成长部分门槛11.79 ratio。条件计算不认证原件、FCFE或目标价。

<!-- decision-os-numbers-v1:input -->
```json
{"calculations":[{"claimed":"-27.1238","id":"h1_wc_cash","operands":["inventory","receivables","payables"],"operation":"sum","period":"2026H1","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"-28.6947","id":"h1_gap","operands":["h1_cfo","h1_capex"],"operation":"difference","period":"2026H1","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"},{"id":"h1_selected_gap_acq","operands":["h1_gap","acquisitions"],"operation":"difference","period":"2026H1","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"-36.5277","id":"h1_selected_gap","operands":["h1_selected_gap_acq","distribution_interest"],"operation":"difference","period":"2026H1","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"12.5721","context_bridge":{"period":"同一证券、合并现金流、半年累计减一季度累计得到二季度；原接口更新标志重复行只选择同值的一行。"},"id":"q2_cfo","operands":["h1_cfo","q1_cfo"],"operation":"difference","period":"2026Q2","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"30.9339","context_bridge":{"period":"同一证券、合并现金流、半年累计减一季度累计得到二季度；原接口更新标志重复行只选择同值的一行。"},"id":"q2_capex","operands":["h1_capex","q1_capex"],"operation":"difference","period":"2026Q2","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"-18.3617","id":"q2_gap","operands":["q2_cfo","q2_capex"],"operation":"difference","period":"2026Q2","places":4,"scope":"consolidated-cashflow","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"351.9332","context_bridge":{"period":"全年2026预测减2026H1实际，得到实现预测所需H2，不是实际H2。","scope":"供应商集团收入预测与接口集团实际收入的条件比较。"},"id":"sh_h2_revenue","operands":["sh_rev26","h1_rev"],"operation":"difference","period":"2026H2","places":4,"scope":"conditional-revenue","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"42.7410","context_bridge":{"period":"全年预测减H1实际，非H2已发生数据。","scope":"仅假定供应商np与归母实际可比；原PDF未核，不认证净利归属。"},"id":"sh_h2_profit","operands":["sh_np26","h1_parent"],"operation":"difference","period":"2026H2","places":4,"scope":"conditional-owner-income","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"12.14","context_bridge":{"scope":"条件股东收益除集团收入为条件归母净利率，预测利润可比假设继续保留。"},"id":"sh_h2_margin","operands":["sh_h2_profit","sh_h2_revenue"],"operation":"ratio","period":"2026H2","places":2,"scope":"conditional-owner-margin","subject":"002384.SZ","unit":"percent"},{"claimed":"2274.8566","id":"equity_value","operands":["price","shares"],"operation":"product","period":"2026-10-15","places":4,"scope":"equity-quotation","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"2808.8091","context_bridge":{"period":"把基准日权益价值按假定10%收益率和确定天数折到2028年末，非未来实际市值。","scope":"原报价权益值转条件未来股东价值，无中间分红、无新增股数假设。"},"id":"future_value","operands":["equity_value","discount"],"operation":"product","period":"2028-12-31","places":4,"scope":"conditional-owner-value","subject":"002384.SZ","unit":"CNY_100M"},{"id":"growth_value","operands":["future_value","base_value"],"operation":"difference","period":"2028-12-31","places":4,"scope":"conditional-owner-value","subject":"002384.SZ","unit":"CNY_100M"},{"context_bridge":{"scope":"仅假定上海np可比持续股东收益，再减原20亿元基础收益；不认证其持续性或可分配性。"},"id":"growth_income","operands":["sh_np28","base_earnings"],"operation":"difference","period":"2028FY","places":4,"scope":"conditional-owner-income","subject":"002384.SZ","unit":"CNY_100M"},{"claimed":"11.79","context_bridge":{"period":"2028年末权益值除同年全年收益作条件收益倍数。","scope":"条件成长股权价值除对应可持续股东收益，不能将np未核归属消除。"},"id":"required_growth_multiple","operands":["growth_value","growth_income"],"operation":"ratio","period":"2028-12-31","places":2,"scope":"conditional-growth-multiple","subject":"002384.SZ","unit":"ratio"}],"inputs":[{"id":"h1_cfo","locator":{"column":"n_cashflow_act","row":1},"period":"2026H1","scope":"consolidated-cashflow","source":1,"subject":"002384.SZ","unit":"CNY","value":"2383887276.1"},{"id":"h1_capex","locator":{"column":"c_pay_acq_const_fiolta","row":1},"period":"2026H1","scope":"consolidated-cashflow","source":1,"subject":"002384.SZ","unit":"CNY","value":"5253357960.45"},{"id":"inventory","locator":{"column":"decr_inventories","row":1},"period":"2026H1","scope":"consolidated-cashflow","source":1,"subject":"002384.SZ","unit":"CNY","value":"-2687181590.55"},{"id":"receivables","locator":{"column":"decr_oper_payable","row":1},"period":"2026H1","scope":"consolidated-cashflow","source":1,"subject":"002384.SZ","unit":"CNY","value":"-1455615091.07"},{"id":"payables","locator":{"column":"incr_oper_payable","row":1},"period":"2026H1","scope":"consolidated-cashflow","source":1,"subject":"002384.SZ","unit":"CNY","value":"1430413825.17"},{"id":"acquisitions","locator":{"column":"n_disp_subs_oth_biz","row":1},"period":"2026H1","scope":"consolidated-cashflow","source":1,"subject":"002384.SZ","unit":"CNY","value":"536818618.87"},{"id":"distribution_interest","locator":{"column":"c_pay_dist_dpcp_int_exp","row":1},"period":"2026H1","scope":"consolidated-cashflow","source":1,"subject":"002384.SZ","unit":"CNY","value":"246477649.48"},{"id":"q1_cfo","locator":{"column":"value","row":1},"period":"2026Q1","scope":"consolidated-cashflow","source":2,"subject":"002384.SZ","unit":"CNY","value":"1126672373.29"},{"id":"q1_capex","locator":{"column":"value","row":2},"period":"2026Q1","scope":"consolidated-cashflow","source":2,"subject":"002384.SZ","unit":"CNY","value":"2159971473.19"},{"id":"h1_rev","locator":{"column":"value","row":3},"period":"2026H1","scope":"consolidated-revenue","source":2,"subject":"002384.SZ","unit":"CNY","value":"27797683972.88"},{"id":"h1_parent","locator":{"column":"value","row":4},"period":"2026H1","scope":"attributable-profit","source":2,"subject":"002384.SZ","unit":"CNY","value":"2956898811.04"},{"id":"price","locator":{"column":"value","row":5},"period":"2026-10-15","scope":"equity-quotation","source":2,"subject":"002384.SZ","unit":"CNY/share","value":"124.2"},{"id":"shares","locator":{"column":"value","row":6},"period":"2026-10-15","scope":"equity-quotation","source":2,"subject":"002384.SZ","unit":"share_10K","value":"183160.7532"},{"id":"sh_rev26","locator":{"column":"value","row":7},"period":"2026FY","scope":"supplier-forecast","source":2,"subject":"002384.SZ","unit":"CNY_10K","value":"6299100.0"},{"id":"sh_np26","locator":{"column":"value","row":8},"period":"2026FY","scope":"supplier-forecast","source":2,"subject":"002384.SZ","unit":"CNY_10K","value":"723100.0"},{"id":"sh_rev28","locator":{"column":"value","row":9},"period":"2028FY","scope":"supplier-forecast","source":2,"subject":"002384.SZ","unit":"CNY_10K","value":"12714300.0"},{"id":"sh_np28","locator":{"column":"value","row":10},"period":"2028FY","scope":"supplier-forecast","source":2,"subject":"002384.SZ","unit":"CNY_10K","value":"2395500.0"},{"assumption":"原模型10%要求收益率及2026-10-15到2028-12-31实际天数/365.25；1.10^2.212183436由配套Decimal脚本计算。不是市场事实。","id":"discount","period":"2028-12-31","scope":"conditional-owner-value","subject":"002384.SZ","unit":"ratio","value":"1.2347192311628318218192482093141"},{"assumption":"继承原20亿元基础收益、3%增长、12%增量股本回报和10%折现：20*1.03*(1-.03/.12)/(.10-.03)，不是公司承诺。","id":"base_value","period":"2028-12-31","scope":"conditional-owner-value","subject":"002384.SZ","unit":"CNY_100M","value":"220.71428571428571428571428571428"},{"assumption":"原基础业务2028收益假设20亿元，不是披露分部盈利或保底。","id":"base_earnings","period":"2028FY","scope":"conditional-owner-income","subject":"002384.SZ","unit":"CNY_100M","value":"20"}],"schema":"research-numbers-v1","sources":[{"manifest":{"commit":"4afac97dd5fd0bfcb506e7b52af95f6b3f46fdb8","path":"source-views/dsbj-relay-38838698248/08d9e84324249f6389274b465aa1900c36184ebe02f5c46836f4d0ee9819fa7d/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"07c908d3092fb15efc0b8f4b1dc9120eba5a93ac4b79b4d49d7148e835ecf2bd"},"selection":{"columns":[],"end":null,"kind":"all","start":null}},{"manifest":{"commit":"8053805ebdead8293c679594d869187005a9c549","path":"source-views/dsbj-h1cash-csv-38838698248/ca580c3d89fb0e7698b7d134af134cba8d68fe5e22f13a1bba22b3830b345655/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"a55f73ee9d705a6f14e7bfeea6e1f76afe587194093aeafb14ae085c20fb2e5a"},"selection":{"columns":[],"end":null,"kind":"all","start":null}},{"manifest":{"commit":"1027f41943c6bc5a9b58e64e4d9d7bf4f1c12538","path":"source-views/dsbj-v15-scalars-38838698248/a4b291683c84269ca653d5df09cf18c9ac5d088df35b1504b1c6ef3b895761a7/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"f7b9567f3972487a974c1d0f536d0da8357a2c4fa7da0f9e18b976e3464efb2f"},"selection":{"columns":[],"end":null,"kind":"all","start":null}}],"template":"营运资金三项现金影响{{number:h1_wc_cash}}；Q2经营现金减现金资本开支{{number:q2_gap}}；若上海预测符合可持续股东收益口径，成长部分门槛{{number:required_growth_multiple}}。条件计算不认证原件、FCFE或目标价。"}
```
<!-- decision-os-numbers-v1:end -->


<!-- decision-os-delivery-v1:start -->
```json
{"base_revision":{"commit":"401a1b84a3f52e3143c4b146f761339a1857e569","path":"research/cases/dsbj-002384sz-20261015/revisions/dsbj-20261016-v1-4/record.json","repository":"auguspp/Decision-OS-2","sha256":"d038bb6e586938a110408bd56875720fde0d2e328358a18ec76c47ab014bea5e"},"body_mode":"delta","input_sha256":"cb7daba61549adb51dc53ffc49cc9992abcfdda2110aee55b71c1d79a914d307","intent_reference":{"commit":"5cc341b13b3bb18d811a6eec179f0be80c253d2e","path":"research/artifacts/delivery-c417ca2e4780e8c7fe1770adff7439d719c66eed56eaa730889808e670e22b55/82d199ae9358c2970c5da1f6873691f115cfa3c475e7d54ed5e0cb315d789c02/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"b85f717be18d673d0e1f23cf79bba4e01d55130976c790a85ae37062ff8faff7"},"items":[{"id":"report-v1-0-html","kind":"artifact","metadata":{"artifact_id":"dsbj-20261015-v1-0-html","media_type":"text/html","name":"DSBJ_Full_Research_20261015(1).html","sha256":"660fb9f4be11de8bdbe1d397d7cb8509276b33996ff921e4598a2b26562f7c98","size":62524},"reason":"实际提供的v1.0完整HTML原件，样式、导航及历史文字不改写。","reference":{"commit":"ffe7723f390dd6d27e492bb73c312d1124e9b9ea","path":"research/artifacts/dsbj-20261015-v1-0-html/8c51b8b736af5594c23e9dec189076c57e20c554e75acfd5a66e063800384e02/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"fe03c4faacd91223a6fbdaf37486d6656d1e2cf46464f5ae5105b2a7cf7ae862"},"required":true,"role":"report-original","status":"retained"},{"id":"calculation-v1-0-xlsx","kind":"artifact","metadata":{"artifact_id":"dsbj-20261015-v1-0-xlsx","media_type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","name":"东山精密_FullResearch_计算底稿(1).xlsx","sha256":"9882335cbd3c87a49f889dde27b5ab93479b4f34a8d1f39bfb737482e9bf002b","size":44960},"reason":"实际提供的v1.0十表XLSX原件，按原始ZIP字节保管，不重算或转换。","reference":{"commit":"9470a394cb14d908d8fc1d5af8709040f4e9cddb","path":"research/artifacts/dsbj-20261015-v1-0-xlsx/b8ee6753402c49f15904a2e52a598eedbdfede40e181c7814b49598fc69dd09a/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"25d3dbfa4a83bffe57afdf4796d567fe7abc1144e6b5f37bf477720d68ff0386"},"required":true,"role":"calculation-file","status":"retained"},{"id":"external-source-originals","reason":"原PDF/完整附注和券商原文仍未取得；本阶段已有限尝试，战略性停止重复追索。现有结构化来源可用于有边界分析，缺项不改成不适用或已保管。","required":true,"role":"source-evidence","status":"missing"},{"id":"relay-evidence-20261016","kind":"source","reason":"本轮9项真实查询的全部选定字段合辑，已核对38行与原生产物及请求身份，原生PDF仍缺失。","reference":{"manifest":{"commit":"4afac97dd5fd0bfcb506e7b52af95f6b3f46fdb8","path":"source-views/dsbj-relay-38838698248/08d9e84324249f6389274b465aa1900c36184ebe02f5c46836f4d0ee9819fa7d/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"07c908d3092fb15efc0b8f4b1dc9120eba5a93ac4b79b4d49d7148e835ecf2bd"},"selection":{"columns":[],"end":null,"kind":"all","start":null}},"required":true,"role":"source-evidence","source_index":0,"status":"retained"},{"id":"relay-h1-cashflow-csv","kind":"source","reason":"同一H1接口原值的CSV单元格表示；保留原JSON来源和null说明，不是新的独立证据。","reference":{"manifest":{"commit":"8053805ebdead8293c679594d869187005a9c549","path":"source-views/dsbj-h1cash-csv-38838698248/ca580c3d89fb0e7698b7d134af134cba8d68fe5e22f13a1bba22b3830b345655/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"a55f73ee9d705a6f14e7bfeea6e1f76afe587194093aeafb14ae085c20fb2e5a"},"selection":{"columns":[],"end":null,"kind":"all","start":null}},"required":true,"role":"source-evidence","source_index":1,"status":"retained"},{"id":"v1-5-original-calculations","reason":"本次原创ZIP包含原始捕获、脚本、计算输出和取证日志；生产入口拒绝application/zip（UNSUPPORTED_ARTIFACT_TYPE），未保管此ZIP，不改名或修改类型白名单绕过。完整计算脚本另作为本修订正文附录保留，重要数值由原数值底稿保留；ZIP作为本轮附件交付，不冒充远端附件。","required":true,"role":"calculation-file","status":"unsupported"},{"id":"v1-5-source-scalars","kind":"source","reason":"同一原capture十个标量的精确CSV表示，保留旧SourceRef，供现役数字入口定位，不是新独立来源。","reference":{"manifest":{"commit":"1027f41943c6bc5a9b58e64e4d9d7bf4f1c12538","path":"source-views/dsbj-v15-scalars-38838698248/a4b291683c84269ca653d5df09cf18c9ac5d088df35b1504b1c6ef3b895761a7/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"f7b9567f3972487a974c1d0f536d0da8357a2c4fa7da0f9e18b976e3464efb2f"},"selection":{"columns":[],"end":null,"kind":"all","start":null}},"required":true,"role":"source-evidence","source_index":2,"status":"retained"}],"schema":"research-delivery-manifest-v1"}
```
<!-- decision-os-delivery-v1:end -->


## Research workpaper (author declarations, not acceptance)
<!-- decision-os-research-workpaper-v1 -->
```json
{"method":{"id":"decision-os-research-full-v2","sha256":"6509212be4fa216b6d2333ffa1a2568b4b7e556070d390d19939eb382ededc62"},"progress":{"challenge_note":"","challenges":[{"argument":"扩产收入增长可能伴随融资和营运资金侵蚀股东现金","disposition":"unresolved","evidence":[0,1,2],"question_id":"q-capital","response":"保留未解：Q2现金缺口扩大及营运资本吸收加强资本约束；对上海路径作资本/现金敏感性，仍无实际批次ROIC。停止无效追索不将挑战改为已解决。"},{"argument":"原版标作最强的2028集团收益107.98亿元仍显著低于多家公开卖方2028E预测，原条件估值缺少同期外部预期闭合","disposition":"changed","evidence":[0,2],"question_id":"q-valuation","response":"实质改变：旧107.98亿情景不再被称覆盖全部乐观解释；补六机构收入/利润率，上海路径三项单变量反推与现价正反算；若239.55亿为持续股东收益，只需成长部分11.79倍，不必35倍。仍不把供应商预测当事实。"},{"argument":"聚合页面的预测均值可能混入不同报告版本、转载日期或证券股本口径，不能未经Relay重建就称机构共识","disposition":"unresolved","evidence":[0,2],"question_id":"q-sellside","response":"保留未解：接口提供机构/期间原值但不能闭合所有原PDF日期与归属。已逐机构对照而不做共识，指定原件或可靠版本表作为重开条件。"},{"argument":"原稿从较低利润路径反推高倍数，可能错误排除高利润兑现、普通倍数即可解释价格的正面路径。","disposition":"changed","evidence":[0,2],"question_id":"q-valuation","response":"按原20亿基础收益独立价值保留，计算不同成长倍数的收益门槛并正算回124.20元；撤回现价只能依靠长期35倍这一泛化，改为收益规模/资本转化/持续期共同检验。"}],"findings":[{"evidence":[],"finding":"保留v1.0多引擎经济结构、AI PCB与索尔思内部价值防重算判断；本次没有新的分部原件推翻此分析。","material":true,"next_action":"","question_id":"q-economics","remaining_kind":null,"status":"answered"},{"evidence":[],"finding":"保留v1.0关于并购并表、负商誉与索尔思一次性条款修改收益的核算；新增研报预测不改变已知历史会计事实。","material":true,"next_action":"","question_id":"q-earnings","remaining_kind":null,"status":"answered"},{"evidence":[0,1,2],"finding":"已复用原值算出H1营运资金吸收27.1238亿元、Q2现金缺口18.3617亿元和选定用途差额36.5277亿元；选定债务/现金给出尺度，但FCFE、现金可用性及完整到期仍不可精确识别。","material":true,"next_action":"本阶段停止重复原PDF请求；仅在实际可读原件/新披露或必需融资决策触发时核附注，不把缺失填零。","question_id":"q-cash","remaining_kind":"currently-unidentifiable","status":"open"},{"evidence":[0,2],"finding":"上海2026至2028收入增641.52亿元；假定增量周转1.5/2/3倍的资本压力为427.68/320.76/213.84亿元，不是现金capex或已证资金缺口。客户/批次ROIC和最终H股事实仍缺。","material":true,"next_action":"仅有可归因产能/订单/资本批次或正式发行结果时重开资本与每股桥；暂不主动重复搜相同未披露项。","question_id":"q-capital","remaining_kind":"currently-unidentifiable","status":"open"},{"evidence":[0,2],"finding":"原低收益世界偏向高倍数解释被修订：保持基础业务、股数和10%回报，2028成长倍数12/18/25/35对应集团收益235.67/163.78/123.52/93.95亿元，均回算124.20元。上海239.55亿元若可比持续股东收益，只需成长部分11.79倍；未签目标价。","material":true,"next_action":"在更可靠归属利润、资本兑现或发行结果出现时重估；不重复原算术，也不把此条件闭合称已证明安全边际。","question_id":"q-valuation","remaining_kind":null,"status":"reframed"},{"evidence":[0,2],"finding":"已完成六机构收入/利润率对照与上海路径分解：2026H2条件需收入351.93亿、利润42.74亿；2028利润与原强景差131.57亿，需要规模/组合等实质改变。原PDF/日期/全部归母与分部模型仍未核，不计算共识。","material":true,"next_action":"本阶段停止同两PDF重复请求；出现可信原件、版本对照或预测修订才重新核。现有未核np只作明确条件比较。","question_id":"q-sellside","remaining_kind":"currently-unidentifiable","status":"open"}],"limitations":["研究基准仍2026-10-15，接口捕获10-16；新计算是同批资料深化，非新独立数据、原PDF或历史PIT认证。","六机构np未全部核归母/可持续收益，研报日期与目录不一；无共识、概率、无条件目标价。原光归属和基础业务等均是继承的模型条件。","FCFE、受限资金/全部到期、最终H股及客户订单/资本回报尚未解决；战略性停止本阶段重复取证，不宣称资料不存在、全路径穷尽或问题已证实。","增量经营资本不等于现金capex，选定负债净额不等于完整净债务，CFO不再次扣营运资本；原HTML/XLSX不重算或覆盖。","当前数值入口只检查本修订声明的20个输入与15项计算，全文其余结果由配套Decimal脚本复算；机械一致与经济充分、人类接受分开。","本轮application/zip附件被现役类型校验拒绝，ZIP只在本轮附件；原计算脚本及重要数值底稿已随本修订正文保留，不能称ZIP原件已远端保管。"],"source_changes":"保留原两来源；新增同批原值标量CSV仅用于明确定位，不新增独立上游证据。","sources":[{"manifest":{"commit":"4afac97dd5fd0bfcb506e7b52af95f6b3f46fdb8","path":"source-views/dsbj-relay-38838698248/08d9e84324249f6389274b465aa1900c36184ebe02f5c46836f4d0ee9819fa7d/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"07c908d3092fb15efc0b8f4b1dc9120eba5a93ac4b79b4d49d7148e835ecf2bd"},"selection":{"columns":[],"end":null,"kind":"all","start":null}},{"manifest":{"commit":"8053805ebdead8293c679594d869187005a9c549","path":"source-views/dsbj-h1cash-csv-38838698248/ca580c3d89fb0e7698b7d134af134cba8d68fe5e22f13a1bba22b3830b345655/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"a55f73ee9d705a6f14e7bfeea6e1f76afe587194093aeafb14ae085c20fb2e5a"},"selection":{"columns":[],"end":null,"kind":"all","start":null}},{"manifest":{"commit":"1027f41943c6bc5a9b58e64e4d9d7bf4f1c12538","path":"source-views/dsbj-v15-scalars-38838698248/a4b291683c84269ca653d5df09cf18c9ac5d088df35b1504b1c6ef3b895761a7/manifest.json","repository":"auguspp/Decision-OS-2","sha256":"f7b9567f3972487a974c1d0f536d0da8357a2c4fa7da0f9e18b976e3464efb2f"},"selection":{"columns":[],"end":null,"kind":"all","start":null}}],"stop":"blocked","stop_reason":"本阶段对现有资料的现金/资本/卖方预测及价格要求分析已完成；用户允许战略性放弃后，停止重复失败原件/公告通路。未来事件或确有新可读原件才重开相依问题，不循环抓取。保留未解资本回报与研报版本挑战，不以concluded或文件存齐假称完整Full/人工接受。"},"request":{"case_id":"dsbj-002384sz-20261015","kind":"full","mandate":"用户于2026-10-15直接委托东山精密（002384.SZ）Full Research，研究经济结构、跨期财务、并购与现金、竞争和扩张、估值、关键反证与合理接续。2026-10-15已明确授权将该案例原创研究成果公开保存到Decision OS 2.0 records；第三方付费原文、私密材料与正式人工Decision不在授权范围。研究截止2026-10-15；后续补证属于同一委托。旧本地案例标签DSBJ-002384SZ-20261015映射为本存储ID。","predecessor":{"commit":"401a1b84a3f52e3143c4b146f761339a1857e569","path":"research/cases/dsbj-002384sz-20261015/revisions/dsbj-20261016-v1-4/record.json","repository":"auguspp/Decision-OS-2","sha256":"d038bb6e586938a110408bd56875720fde0d2e328358a18ec76c47ab014bea5e"},"questions":[{"question_id":"q-economics","text":"传统FPC、AI PCB、光模块和精密组件分别通过什么经济机制形成可持续利润？"},{"question_id":"q-earnings","text":"并购与经营成长如何区分；财务利润、非经常损益与持续经营利润能否跨期闭合？"},{"question_id":"q-cash","text":"经营现金、营运资金、资本开支、债务与少数权益之后，普通股股东可得的现金到底是多少？"},{"question_id":"q-capital","text":"索尔思与AI PCB扩产需要多少可归因资本、客户订单和收益率；融资及稀释风险是否改变每股经济性？"},{"question_id":"q-valuation","text":"截至2026-10-15价格隐含怎样的收入、利润、资本和持续期要求，是否存在可靠安全边际？"},{"question_id":"q-sellside","text":"券商及外部经营预期与研究假设差在哪里，哪些证据能验证或否定最强竞争解释？"}],"scope_change_reason":""},"schema":"research-workpaper-v1"}
```
