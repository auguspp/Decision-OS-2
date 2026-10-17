# A股因子日期核验与补充读取

责任：#10市场读取，来源/capture仍由#12拥有，保管仍复用#6。
复用[历史读取](ashare-history-continuity.md)、原 `_factors`/`_view`/`_adjust`；
不新增来源、HTTP/SDK、复权算法、存储或重试策略。

## 为什么不能把分页结束当作完整历史

一次完整请求证明捕获按该请求的分页合同结束，不证明每个市场交易日都有因子。
本入口按原完整交易日历对齐日期，不用收到的行数反推交易日，也不沿用邻日因子。
官方 [adj_factor](https://tushare.pro/document/2?doc_id=28) 支持 `ts_code`、
`trade_date`、`start_date`和`end_date`；[trade_cal](https://tushare.pro/document/2?doc_id=26)
提供交易日标记。2026-10-17核读；参数可用不保证响应完整，实际行仍逐项验证。
两种请求形状是同一供应商，不是两个独立来源，也不认证公司行动或总回报。

## 原读取载荷的可选补充

`relay-security-stock-reading-v1`及three-surface的stock部分可增加：

```json
{"factor_supplements": {"600000.SH": {"20261016": "原捕获对象，不是字符串或手填因子"}}}
```

此片段仅说明映射位置，不是可执行的完整输入。每个值必须是现役`capture_registered`
或原保管恢复的完整结果，`api=adj_factor`，请求恰为该代码和`trade_date`，并包含原必需字段。
每个证券最多16个补充，所有引用仍受原32-query、页数及2MiB保管输入上限约束。

原区间因子必须完整捕获且通过原严格行校验。点查询只能补原区间的缺失市场日，
不能替换任何已观察值，即使值相同。每个点只能返回0或1条同代码、同日、正有限因子；
重复、错码、错日、非法值、不完整来源都不能填缺口。原区间缺失/错误时不得只凭点查询
冒称完整历史。无补充载荷时保留原读取合同和来源版本。

合法补充组合产生新的qfq来源版本，保留`baseline`和各点的来源链、原始缺失日、
实际补入日与残余缺失日；不改原捕获、原回执或原失败。按原公式再次读取新组合，
不是在原数据里补0或插值。局部点失败只留下该日缺口，不抹掉原有效因子和独立补充；
原始价格不受影响。补充映射结构错误仍明确拒绝相关因子计算。

原 `market_relay_cli` 和 `market_custody_cli save/read` 均使用此载荷；权利预检、
CAS/幂等、query-root SourceRef与独立进程读取不变。丢失一个已保存点来源后，
该日重新成为缺口，其他输入继续读取，无网络回退。

## 有限现场诊断

原`market-source-check.yml`选择`factor-audit`、`exchange=CN`、明确`start/end`，
`stock_codes`为1—4个唯一SH/SZ代码；`access_confirmed`和`request_context_confirmed`
须明确true，`explain_quotes=false`，member/resume留空。

先登记所有原区间请求：仅相关交易所完整日历及各代码区间因子，不抓主表/报价/停复牌。
日期缺口明确后，在同次输入中一次性登记全部缺失日的点查询，再开始执行。总点数最多16；
超出时不任取前16个冒称完成，而是保留缺口数量和`point_budget_exceeded=true`，不发点查询。
最多22个逻辑查询，仍使用原600秒、10k行/8MiB、5000×2页、初次+3、至少30秒及更长
Retry-After；权限/限流/全局停止保持。没有后台循环、跨运行resumer或自动第二次dispatch。

回执仅计数、状态与实际生成请求（包括补查日期），不导出返回因子值、价格、证券名单、
源行、原页行定位或来源摘要。明确请求元数据不授权公开供应商正文。未取得/非法区间
是未知缺口数量，不是0。对照只针对本次实际输入，不声称重放以前未保管的源行。

`checked-factor-audit`只表示本次声明日历范围内因子组合完整且必要查询合格；
不签上市历史、日行情/qfq收益、全市场覆盖、源稳定性、研究采用或长期保管。
`original_batch_revalidated=false`始终保留。原区间与点查询的不同结果应交原来源owner
查语义/上游处理，不能提前断言供应商错误，也不能用补查成功覆盖旧缺口证据。

## 验证与退出

测试直接走合成Relay响应、原capture、普通raw/qfq reader、有限宿主、原临时Git库和
删除输入后的新进程根引用恢复；包括原缺口/锚点、非法点、局部失败、权限/等待/总预算、
不可替换原值、来源版本与完整分母、缺源恢复。合成恢复不替代真实来源保管验收。

退出时普通反向PR移除本可选stock补充映射/遍历、`reconcile_adj_factors`、
`market_factor_check`、factor-audit选项、专属测试与文档引用。原复权算法、history/session、
共享来源、保管与CI继续。不得因代码退出删除旧调用、源记录或失败证据。
