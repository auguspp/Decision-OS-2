# Relay daily 单页与两日批量诊断

责任 #136，来源归 #12，结论交 #10。用户2026-10-18明确授权本次有限比较，
不是更改日常market collection或重启任何原失败页。复用现役RelayHTTP固定主机、
密钥/反射/禁止跳转/4MiB字节保护、原research_sources._table和market_daily._price。
不新建客户端、数据库或调度器，不修改共享5000上限、原220槽、原预算或records。

## 为什么单独验证

已保管2026-10-16能力快照的daily/adj_factor max_limit=null，只表示未声明；
suspend_d声明100000不能推广到daily。原手册“不超过5000”是建议；
当前research_sources.parameters_for却硬性拒绝更大的limit。
本实验只为固定三个请求使用原受信HTTP底层，不开放任意参数或来源URL。
成熟HTTP/安全/JSON/日期/Decimal实现继续复用，本批无需SDK、新依赖或第三方平台。

## 唯一实验与停止条件

现役relay-source-runtime.yml新增family=market-bulk-check、operation=check；
source_plan必须为 `{"study":"relay-bulk-20261018"}`；access_confirmed=true、
public_rights_confirmed=false；其他选择留默认/空，code_sha为核过的当前main。

| 次序 | 请求 | limit | 用途 |
|---|---|---:|---|
| 1 | capabilities/daily | 不适用 | 核当前声明，不签实际行数 |
| 2 | daily trade_date=20261016 | 6000 | 单日能否不用5000分页 |
| 3 | 同一市场日 | 12000 | 较大limit是否接受及结果是否一致 |
| 4 | daily start_date=20261015/end_date=20261016 | 12000 | 实际返回是否超过6000、两个日期是否完整 |

仅ts_code/trade_date/close三列，offset=0；总计最多4次，每项一次，无自动重试。
不同试验之间间隔30秒；拒绝/传输/业务/形状/身份/私有核验失败立即停止剩余项。
当前声明明确不支持更大请求时不继续挑战该限制。90秒仅socket操作超时；
原工作流15分钟上限仍可中断，不能承诺完整HTTP绝对耗时。
返回不足limit不自动叫完整：供应商可能截断或忽略大limit，须对下面原数据逐项比。

准备阶段没有源凭据，核当前同仓id1420041744/private/main、实际源run身份、
同SHA最近main push CI成功、原工作流全部有界历史中唯一固定实验标题/attempt1。
真实capture再次核同一准备输入；执行中重核main/private，结束后再次核才释放原件。
同一workflow的decision-os-relay-source串行组与原A股任务互斥；源任务不持contents:write。
私有化不授予其他数据用途或公开再分发许可。此实验只沿本次用户授权，不推定下一次试验许可。

## 冻结的历史对照（不是本次新HTTP结果）

原source9的records090e3a8a83234fa7549ea7bf6254e88945a57f04。
原backup run39116457357/1 artifact12132263580：4081098字节，
SHA256 `13fd9482e39fe4d09149363f9ed9162f695712deba42a5d67bd21a44d684f43c`。
原bundle3468807字节，SHA256 `17b01f02904863daeb66aaccaabe6b1e6ec7f3124c0b9a24e54524900a3b2f72`。
使用该包原508 reader的GitSnapshot/restore_market_collection/restore_market_query恢复两份
有效daily；206个reader文件核大小/SHA256/Git blob，不把两个查询恢复称全库存再审。

| 日期 | 原完整行数 | 原分页 | 规范化行集合SHA256 |
|---|---:|---|---|
| 20261015 | 5565 | 5000+565 | a4f27a15926ec6b039615f3fc1b2d139246565f990140b2c0d6906876dd7c20b |
| 20261016 | 5567 | 5000+567 | 2d42493a85d0628776b5851e7c50a65c9385ffb4dc7b6b9da9ab76356c76f469 |

query manifest提交/摘要分别为882110c6b336f178c92776d008362526ef6b1d88 /
74ba98ddf8e5b16e255702383333067db2ed6ce8d10d6194ccf6a1398001c9fc，及
0c2c7354b8bec6cf4e7540dd1c1365a3420d687f /
612950ff5b7446442f3ccf234f1ce1600ab587d8e8928da8a368d78af6bb3e06。
完整来源链沿#10原source9回执，不造逐证券SourceRef。

规范化：由原HTTP JSON直接Decimal读取close（原基准为保管投影），沿_price验正数、
有限值和指数边界；格式化为非指数表示，仅去末尾小数零，不受外部Decimal精度影响。
每行 `[ts_code, trade_date, close_text]`，排序、UTF-8紧凑JSON，计算SHA256。
两日应核合计11132证券日；不是当前上市分母或无停牌承诺，也不保证供应商不修订。
先比身份集合/缺失/新增/重复，再比较每个相同证券日的原价；差异不平均、不覆盖原数据。

## 原件、结果与退出

原生artifact `dos2-relay-bulk-RUN-ATTEMPT` 含request.json、result.json及成功获得且经
原密钥反射检查的HTTP原字节。失败在发送前先记request-started；没有正文不伪造原件。
result逐项保留参数、请求/返回钟、耗时、字节、哈希、实际观测、未执行与停止；
其中source_requests是尝试意图数，transport_requests为原client真实计数。
最终权限不能再次确认时不释放供应商正文。未执行项不会成为零行成功。

正文只留同仓private Actions七天，不进入代码、Issue或公共日志，不等于永久保管。
下载须核native run/attempt/head/artifact大小/digest/CRC和输入，再用纯inspect_quote
从原件独立重算，比较原分页集合。参数接受、单日完整、单次超过6000、多日完整及
长期稳定五件事分别结论。没有新研究、自动备用、市场资格、日更或生产切换授权。
退役时撤本family/job、两个专属模块、专属测试及操作说明；原HTTP/研究/市场/Store保护继续。
