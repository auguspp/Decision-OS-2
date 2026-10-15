# #12 来源候选：旧 Kernel 的 Tushare Relay 已登记能力

**事实层次必须分开：** 首版采用第三方 Relay `pcd.mobcvb.cn/tushare/pro` 的历史登记，并非官方 Tushare API 直连或本会话已经完成实际运行。旧仓2026-10-14静态手册核对结果为：导语称257已启用、标题称298注册，逐行实际为**271个具名路由：258标记可用、13标记不可用**；原Kernel `runtime/tushare_relay.py` 只允许**29**个已逐个审过用途的API。这三种数字口径不能互替，也不把标记可用视作当前有权限/有数据。

参考来源：
- [2026-10-14 静态索引与一次机器目录未成功证据](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/readings/relay-source-catalog-review-2026-10-14.md)：capabilities的一次请求 `ConnectionError`，没有服务器目录读回，业务调用零次。
- [原 Relay 白名单与Transport](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/src/decision_kernel/runtime/tushare_relay.py)：只对既定命名/参数、明确失败、时间/原始行、请求身份进行约束。
- [已取得资料类型/来源顺序](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/data-source-orchestration-v1.md)：结构化报表与原件不等价，Relay 补充现有来源，而不是覆盖原PDF。

| 接口族 | #12用途 | 旧证据 | 仍须核什么 |
|---|---|---|---|
| `disclosure_date` | 按公司与报告期给出预约日期、实际/改期字段 | 2026-09-29有有限真实返回（`pre_date/actual_date/modify_date`），不能推断历史未改期或所有公司覆盖 | 当次Host/权限/独立响应与report period绑定、计划≠实际、时间时钟与历史版本 |
| `income`、`balancesheet`、`cashflow`、`fina_indicator` | 结构化财务表供 #7 计算前核依据；与预告/快报/审计/主营构成共用[批量入口](relay-financial-batch.md) | 旧Relay客户端白名单；本批工程接入不代签live | 财报期、首次披露及更新、合并口径、币种/单位、原PDF/PIT核验；不要以数字表替代原件 |
| `report_rc` | 券商预测期记录和同报告期机构比较 | 旧项目曾出现排队后恢复与正常响应；属于Smart Money补充 | 报告日、机构与目标年/季度、重复版本、来源身份；不称机构共识和PDF |
| `research_report` | 选定机构的历史报告定位与同机构前后报告 | 原客户端白名单，旧项目有有限研究报告locator记录 | `trade_date/ts_code/inst_csname` 参数、分页与报告locator资格、实际PDF权限 |
| `stk_surv` | 公司机构调研的公共参与者记录 | 旧项目有限可用记录 | 活动日与披露日、参与行与活动分组、机构身份与名单不完整 |
| `hm_list/hm_detail/top_list/top_inst` | 游资/机构席位、龙虎榜补充 | 旧Smart Money运行/已有验证 | 标签不等于自然人、不同天/窗口重叠、原交易所证据与公开权利 |
| `news/major_news/anns_d` | 新闻与公告线索候选 | 2026-10-14静态表标记，不在29 API既有白名单 | 权限/版权/真实时间与原始发布；未经审查不得直接调用或进入公开正文 |
| `forecast`、`express`、`fina_audit`、`fina_mainbz` | 业绩预告、快报、审计意见与主营构成 | 本批按用户批量授权及官方字段33/36/44/79/45/46/80/81做八接口工程；不冒称旧29接口已包含或今日live通过 | 预告≠实绩、审计元数据≠审计报告、主营各维度不相加；财报期/单位/来源资格仍逐结果验证 |
| `fina_mainbz_vip` | 全市场分部资料候选 | 仍未接入；不是本批普通接口的自动替代 | 实际用途/许可/分页及源版号另验 |

**选择规则：** 用户于2026-10-15明确允许接口批量接入、不用考虑用量；不再把逐接口小额试点或费用当作批量工程前置。本批八类财务资料共用[财务批量入口](relay-financial-batch.md)，支持多证券、多报告期、主营构成不同维度的显式计划。分页/响应体/内存/超时是技术边界，真实401/403/429仍须停止相应请求、不换目的地绕过；批量授权不表示全部258个路由或组合写/删接口都可调用。读取范围与公开保管权利仍分开。

`disclosure_date` 已由PR #30交付，继续复用其字段与来源边界，不重新当成下一待建功能；当前财务批次在现役Relay HTTP上提炼共同传输，保留旧接口兼容。尚无本批真实Relay调用回执时只写“工程已接入”，不能写“线上八接口全部取数成功”。后继优先在已有获准宿主实际使用批量入口，或按原#12范围接入 `report_rc/research_report`、调研和其他参与者资料；不新建第二内容池、不重新铺行情fallback。
