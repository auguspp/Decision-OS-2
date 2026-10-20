# 原池竞价观察与同日收盘接续

责任：#147 的有限竞价完整组。既有 Relay 原件、保管、读取作用域和正常
Room 承担各自责任；本消费者保留固定观察条件、完整原池与同日结果。
它不选择交易、不启动采集、不签策略效果，也不把有限返回范围称作全市场。
价格与名称示例只在专属合成测试中出现，不迁入旧真实运行值。

## 固定条件和原范围

命名条件保持 `registered-317-prior-turnover10-30-auction-gap3-9-v1`。
依据是原 [#317 需求][requirement]、[固定竞价实现][probe]、
[同日接续实现][follow]，以及原 [完成日选择与请求时钟][daily]。
旧 Kernel 只作确切语义参考，不恢复其 workflow、运行包或任务队列。

| 项目 | 本用途保留的确切条件 |
|---|---|
| 日历 | SSE 显式 `trade_cal`，T 前 31 日至 T；取 T 之前最大的 open 日。该日至 T 的逐自然日不能缺失；更早无关缺行不取消本用途。T 必须显式开市 |
| 原池 | 前一开市日 `limit_list_d` 的整个返回 U/D/Z 范围；不先把请求改为 U 子集 |
| 独立 U 分母 | 可识别的 U 身份、含 U 的冲突身份、身份不明的 U 原行都保留。相同重复证券不增加人数；原重复行和位置继续可读 |
| 主板条件 | `(?:60[0135][0-9]{3}\.SH\|00[0123][0-9]{3}\.SZ)`；这是原代码模式，不认证发行人或交易资格 |
| 名称 | 昨日合格名称的大写不含 `ST`；不是当天 ST、上市或停复牌证明 |
| 昨日换手 | 有限非负的 `turnover_ratio`，10%–30%，包含两端 |
| 竞价 | T 日 `stk_auction` 且 `ts_type=STK`；同证券、同一行语义的正有限 `price` / `pre_close` |
| 竞价涨幅 | `(price/pre_close-1)*100`，3%–9%，包含两端；条件比较不依赖显示舍入，不用昨日 daily.close 代替原 pre_close |
| 连板背景 | 独立解释 `limit_times`；2、2.0、"2.0" 可表达同一正整数，2.5、bool、非有限值不合格。缺失或冲突不取消已合格价格 |

原池全行位置、U/D/Z 唯一计数、类型冲突、未知类型和无效行分别保存。
`pool_rows` 是已检查的完整原返回投影，`prior_temperature` 是背景计数；
`cohort_rows` 是独立 U 观察分母。静态排除和静态未决不能从分母中消失。
错误日期或来源上下文在整查询资格层失败时，依赖分母未知，不填 0。
空的完整返回与没有取得/无法恢复的原池也分别表示。

五个原组为 `MATCHED`、`NOT_MATCHED`、`AUCTION_UNRESOLVED`、
`STATIC_EXCLUDED`、`STATIC_UNRESOLVED`。新 close 不改原成员、状态或原组。
没有匹配、没有可比和没有数据是不同状态；零可比时中位比值为 null。

## 原输入与来源资格

新增本地 reading recipe `relay-auction-reading-v1`，只接受：

| 字段 | 形状与用途 |
|---|---|
| `schema` | `relay-auction-reading-v1` |
| `profile` | 上述固定名称 |
| `market_session` | 目标 T，规范 ISO 日期 |
| `calendar_result` | 原完整 capture result；精确 SSE、T−31 至 T，必需请求 `exchange,cal_date,is_open` |
| `pool_result` | 可省略或 null；原前开市日 `limit_list_d`，必需身份 `ts_code,trade_date,limit` |
| `auction_result` | 可省略或 null；原 T 日 `stk_auction` + `ts_type=STK`，必需身份 `ts_code,trade_date` |
| `close_result` | 可省略或 null；原 T 日 Relay `daily`，请求 `ts_code,trade_date,close` |

每个 result 必须通过现役 Registry、query 参数、源身份、snapshot、完整页序列和
原 `inspect_result`；未保管 result 与由确切 query SourceRef 恢复的 envelope
都走同一资格。不能输入 `matched=true`、`preopen=true`、可变名单或新的供应商地址。
原通用 `_view` 要求查询从 offset 0 起、日期业务范围确切；错误上下文不拼凑成成功表。

请求仍由现役 `research-source-plan-v1` / Relay 执行入口拥有。本组仅为
`stk_auction` 接入原固定 `ts_type=STK` 参数；ETF、其他值、别的 API 携该参数均拒绝。
原 registry 手册与已存 capabilities 证明登记及当时声明，不证明当前账号字段和权益。
原手册未完整列出的字段语义以固定旧代码为依据，未新访问官方字段页或数据来源。

`limit_list_d` 的 `name,close,turnover_ratio,limit_times` 和 `stk_auction` 的
`price,pre_close` 可独立缺列。只在原请求确实请求过并实际返回时使用；必需身份列
不能缺失。一般 API 的必需字段、字段安全检查和研究用途不放宽。

### 页终止与覆盖

现役每页上限保持 5000，使用原 bounded 分页；不复制旧竞价 8000 的上限。
恰好一整页不能证明结束，仍须正常后继终止页。达到页数、字节、运行或来源预算
都保留原未完成/终态，不为竞价补一次请求。

上述两个 API 的新投影只额外保留 `relay-page-coverage-v1`：body/data 的
`count,total` 与有限的继续/截断标记。游标文字不存为新执行地址，只保存是否存在。
同页冲突计数、跨页变更的 total、已收行超过 total、显式 truncated、末页仍声明更多，
不能成为成功完成。完整 capture 保管门槛保持；不完整页只能沿原执行 checkpoint 接续。

旧投影没有这份字段时继续恢复，`coverage.status=unknown`；不会因为旧
`complete=true` 或短页而升级成本用途的来源覆盖完成。新证据完整且原分页终止后，
最多是 `returned-query-complete`；独立市场穷尽仍未建立，`unreturned_count=null`。
不以降低 5000 上限或只读取一个好子集规避容量或覆盖问题。

### 请求时钟与完成日

`limit_list_d`、`stk_auction` 和同日 close 所需 `daily` 可保留同一有限
`transport.request_clock={requested_at,received_at}`。新 capture 只从最后成功的
原 attempt 取这两个字段，核原 base transport 一致、时区明确且
`requested_at <= received_at <= captured_at`；原 checkpoint 执行 owner 在校验完整
attempts 后再次逐字段绑定。普通 inspector/exporter 只检查有限投影的形状与时间顺序，
沿原合同剔除可编辑的执行诊断，不以诊断作为准入条件或补齐时钟。
其他 API 不能携此字段。daily 的其他消费者不增加必需门槛，也不改变其数值资格。

原保管有意不保存 attempts。恢复后这份有限投影只能证明该确切
SourceRef 所存的源请求时钟，不成为独立原 HTTP、交易所时间或人当时收到的证明。
旧投影缺字段时为 unknown；即使原内存 attempts、较晚 captured_at 或本次阅读钟存在，
也不补签被丢弃的投影。

竞价盘前资格要求所有原页的 request/receive 同在 T 的沪市时间
`09:26 <= request <= receive < 09:30`。否则明确为历史/迟到，缺任一原时钟为
`NOT_ESTABLISHED`。`plan.as_of` 不参与此判断。

同日 close 复用原 `session_quotes` 做同源、同证券、T 日期和原价字段资格，
然后在本用途单独保留旧 `stock_market_inputs.calendar` 的 **15:30 Asia/Shanghai**
已完成日边界：每页原请求与接收均不得早于 T 15:30，15:30 本身可用。
同日盘前或尚未完成日的数字仅进入 `readable_close` / `close_readable`，
不进入 `same_session_close` / `close_available` 或两端比较。
缺原时钟是完成时点未知；晚日读取同一 T 原件不会用新 read clock 将其变为已完成。
该规则不扩大为其他日线 reader 的准入合同。

## 同日接续和版本

五组均左连接所选 close，保留各自 `denominator`、原值可读数、已完成 close 可用数、
两端可比数、高/低/等于竞价数量和中位比值。比较方向用未舍入原价；
`auction_to_close_change = same_session_close / auction_price - 1`，输出小数比值并
保留 12 位小数，不称成交收益、复权回报或策略效果。价格单位 CNY，换手与 gap 为 percent。
单证券缺值或重复冲突只限制该证券；其他日期/供应商的 close 不补位，坏的新引用不倒找旧成功。

`cohort_version` 在 close 左连接前绑定固定条件、T/前开市日、原分母、静态/可比/匹配数、
非 close 查询 lineage、完整原成员及原五组。可选 close 的有无、修订和时点资格不改变它。
`view_version` 绑定整个观察模型，包含 close、来源与缺口，仍与分页/筛选无关。
后继组可复用这个确切 cohort；它不是新的可写候选库或默认 T+1 任务。

原 [市场保管合同](../architecture/market-custody.md) 继续拥有 root → query → page
SourceRef 链、submission journal、原 exporter、摘要、独立恢复与容量。这里没有新存储格式、
后台任务或第二 reader。新 close 使用明确的新 recipe/root，旧根仍精确可读；临时 capture
文件删除后只给原根也能重开。复用 readback 不证明未保留的原 HTTP 正文可恢复。

## 正常 Room、CLI 和离线页面

原 Room 计划增加可选 `auction`，严格只收整根 `reference` 和导航
`offset`（非负整数，默认 0）、`limit`（1–50，默认 20）、`entity_id`（可省略/null）。
reference 是 `market-reading-custody-v1` 中上述竞价 recipe 的完整 SourceRef，
selection 为 all；错族根、计数回执、collection 根和来源 URL 不替代它。
无 auction 计划不加载本用途 registry、不增加来源读取或空面板。

原宿主 `reading-room` 的 payload 是 `{"plan": <原 Room 计划>}`；`plan.auction`
可写为 `{"reference": <完整 SourceRef 对象>, "limit": 20}`。
沿原只读宿主的代码版本、权限和实际字段调用，不新建 host operation/workflow。
源公开确认继续使用原 `public_sources_custody_confirmed` 及可信可撤销部署授权，
并与研究/材料权利分开；读取失败不产生追加来源授权。

本地已安装包的正常入口为：

```console
decision-os store --path /approved/existing-synthetic.git research-room \
  --input /approved/room-plan.json --out /approved/new-room.html \
  --market-registry /trusted/checkout/docs/sources/relay-registry.json

decision-os store --path /approved/existing-synthetic.git read-opening-auction \
  --input /approved/auction-plan.json \
  --market-registry /trusted/checkout/docs/sources/relay-registry.json
```

第二条输入只有 `auction` 槽内计划。`--path` 沿原约束只代表已初始化合成存储，
真实目标继续使用获准的原只读 binding；不能从来源文本指定 registry 或目标。

应用先恢复原全 scope，保留完整 `view`、原 `cohort_rows/pool_rows/groups`，再把
当前页放入单独 `page`。下一页/精确证券筛选不会改分母、资格、源日、版本或未选中项缺口。
A 股原 panel 的 `ROOM_MARKET_ASHARE_ROOT_REQUIRED` 保持；auction 不挤进该用途。
页面显示整个原池背景、五组、可读值与最终收盘区别、时钟/覆盖及各原引用。
整模型可下载；原根和查询按钮只导出
`{operation:"read-source",payload:<裸 SourceRef>}`，导航按钮导出原 reading-room 请求。
它们不直接联网、不填公开权利、不改其他面板计划。

源文字使用原 textContent、JSON/HTML 边界转义和 CSP；无 innerHTML、localStorage
或自动网络。窄视口表格在自身容器滚动。脚本的实际导出请求由正常宿主复读验证，
不以手造按钮 payload 代替消费者证据；这不是浏览器视觉验收声明。

## 整次终态、边界和维护

Global 和 auction 现在都消费 `application/market_reading_scope.py` 的小 facade。
它从原 Global 实现原样提炼，只把 private class 命名为共享 `SourceReads`；
没有新 API、预算 owner、缓存或 scope。原全局异常对象一旦出现，后续 backend
读取立即传播该对象。权限、可见性、scope 关闭、传输与整次预算终态不转为 partial；
普通 MissingObject 仍局部缺失。原外层 market/reading scope 和最终宿主检查保持。

读取后 private→public 反转、最终 scope 失败或预算终态都拒绝成功正文/HTML。
获准 Public 宿主可能先保留已授权 inputs.json；private 路径在最终核验之前不释放输入。
本组模型上限 2 MiB；超出整组 not-included，最终检查仍执行。原 Room 6 MiB/HTML
8 MiB、32 query/100 page、直接输入 2 MiB、原 GitHub 512 次和其他字节限制不变。
5000 行 source 页不是保证所有完整市场数据都可塞进某个有限 Room。

专属测试绑定纯条件、输入/覆盖/时钟、原保管恢复、新进程、正常 Room/CLI、原宿主与
实际整页按钮。一次 403 恢复正常响应后仍不得继续 GET；原 512 allowance 实际耗尽
保持同异常且零后续请求。原唯一 `test_distribution` 的同一次 wheel 安装链扩展
`exercise_installed_auction_room`，checkout 外删除临时 capture 后按引用重读两次；
不增加第二安装、源码旁路或 CI 豁免。正式 gate 由集成者按实际交付版本执行。

已有失败/hold/耗尽预算保持，不通过新日期、API、case_id 或本消费者恢复。#10 当前
FTShare daily 不自动成为 Relay 同源 close；旧 Relay 日线也不会为本用途补抓。
实际竞价/close SourceRef、来源权利、首次真实正常消费、盘前自然及时性和连续样本
仍需原范围真实证据，本组全为合成工程验证。

09:40 的[给定原件分钟观察](opening-auction-minute.md)复用本原 cohort，保留严格桶时间、
字段、原回执和完整分母，不启动来源请求；真实分钟采集仍须原来源 owner 的实际资格。
下一开市日须冻结本 cohort、T 后首个显式开市日和该日原 open/close 用途。
二者不互相强迫依赖，也不从本组推断日内路径、下日结果或真实交易效果。

本用途退出时同一责任变更移除专属模块、route/Room/CLI/页面、专属测试、安装 helper
及本文链接；两 API 的额外字段/证据和 daily 时钟若已被别的真实消费者使用，按其 owner
接续或另行退役，不能随页面删除历史引用支持。共享 facade、来源、保管、A 股、Global
和原 Room 仍随各自现役消费者维护。

[requirement]: https://github.com/auguspp/decision-kernel/issues/317
[probe]: https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/src/decision_kernel/runtime/d_auction_probe.py
[follow]: https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/src/decision_kernel/runtime/d_auction_follow_through.py
[daily]: https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/src/decision_kernel/runtime/stock_market_inputs.py
