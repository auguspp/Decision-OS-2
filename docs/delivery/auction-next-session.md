# 原竞价名单的下一开市日观察

责任：#147 的有限 T+1 完整组。输入是已保管的确切原竞价根、明确的 SSE
日历和同源 daily 原件；输出是完整原名单在准确目标日的 raw open / close
及各自资格。保管、恢复、正常 Room、主 CLI、离线页面及退出范围在本组一起维护。
本组不发起来源请求、不建立 standing 任务，不计算跨日收益或真实交易效果。

语义依据是固定旧 Kernel 的 [下一开市日合同][old-contract]、
[seed / target / project / retain 实现][old-next] 与 [日历和价格输入][old-inputs]。
原代码只作语义依据；没有恢复旧 workflow、Collector、current-state 或 ZIP。
OS2 原竞价身份和五组沿 [现役竞价合同](opening-auction.md)，
来源准入和 SourceRef 沿 [市场保管合同](../architecture/market-custody.md)。

## 原名单、目标日与完成资格

原命名条件保持 `registered-317-prior-turnover10-30-auction-gap3-9-v1`。
种子必须从确切 `relay-auction-reading-v1` 的标准保管根完整恢复，核原
`cohort_version`、全部 `cohort_rows`、成员身份和五组计数。没有独立名单输入。
`cohort_version` 在原组内已经排除可选同日 close；其有无或修订不改变本 cohort。
在读原查询前先核根内 recipe 的 auction 类型；另一个 T+1 根不能递归充当原种子。

独立 U 分母与原 U/D/Z 背景分别保留。`MATCHED`、`NOT_MATCHED`、
`AUCTION_UNRESOLVED`、`STATIC_EXCLUDED`、`STATIC_UNRESOLVED` 全部接续；
静态排除、未决或无证券身份的原成员不会被删除。无身份行不能连接任意价格。
原分母未知仍为 null；没有把未取得原池改成零结果。页面只显示一页或一个证券，
也不能改变完整成员、分母、原组、原时钟或覆盖缺口。

目标日从原 T 后一天开始，逐自然日检查明确 `trade_cal` 的 SSE open/closed
记录。首次缺日停止并记录 gap；首个明确 open 固定为目标。不同日历原件重叠处
必须一致，已保留前缀不能改写。旧目标不得因报价缺失、停牌、来源失败或新表有值
而顺延。只含开市日的列表不能补出中间 closed 日，周末不能凭星期猜测。

**目标选择与“已完成”资格分离。** 日历不具有原 request_clock，不能单独证明
目标已经完成。缺 daily 或旧 daily 缺时钟时仍固定准确目标，并将完成资格标为
`NOT_ESTABLISHED`；新阅读时钟或 caller 的 completed 标志不能补签。

已完成资格复用原 `close_timeliness`：所选 daily 每页有限投影中的原 request
和 receive 都不得早于目标日 **15:30 +08:00**，包含 15:30 端点。沿既有 capture /
checkpoint 边界及普通 inspector 的形状和顺序约束，不重新依赖已剔除的 attempts。
读回证明的是这个确切 SourceRef 保存的请求时钟，不是独立原 HTTP、交易所时钟
或人当时看到了数据。原 captured_at / request / receive 不得晚于本次 checked_at。
这个记录时钟只检查顺序，不能证明源及时性或完成日。

目标日完成之后、当日结束之前取得标为 `SAME_SESSION_AFTER_CLOSE`；后日取得
标为 `LATER_ACQUISITION_NOT_HISTORICAL_KNOWLEDGE`。两者均不能倒填为早先已知。
资格用 aware instants 比较，不把极端偏移的源日期转换后造成上下溢。
不能表达 T 后一天或超出有限日历窗口时按输入错误拒绝，不修改通用 trade_day。

## 有限输入与独立原字段

新增 recipe `relay-auction-next-session-reading-v1` 严格只收六个字段：

| 字段 | 精确用途 |
|---|---|
| `schema` | 上述固定 schema |
| `auction_reference` | 标准原竞价整根 SourceRef，或 null |
| `previous_reference` | 本用途一个确切前版整根 SourceRef，或 null；与 auction 至少一个非 null |
| `checked_at` | 有时区的本次记录时间；不作为行情原接收或完成证明 |
| `calendar_result` | 原完整 SSE `trade_cal` result，或 null；前版可提供已存的必要原日历 |
| `quote_results` | `YYYY-MM-DD` 到原 Relay daily 完整 result 的有限 map；无表是空 map，不能放 null 或复制旧 model |

SourceRef 的 selection 必须为 all。可变 URL、counter 回执、复制的旧模型/hash、
caller 的 matched / qualified / completed 值均不是输入。所有原件复用现役 registry、
`inspect_result`、原 query 参数、来源 snapshot、完整页序列和精确 query identity。
直接 reading dispatcher 对这一个确切引用用途需要显式传入已有 VersionedStore；
它没有来源 HTTP 或第二存储。

日历请求固定为 SSE、明确 start/end、`exchange,cal_date,is_open`，每个返回日期唯一，
值只能是原明确 0/1。缺行不补零；错误身份、日期、重叠修订整体拒绝。
daily 必须是准确目标日的 marketwide `trade_date` 请求，声明字段只能为
`ts_code,trade_date,close` 或加上 `open`。每个原实体/日期符合原 A 股身份模式；
错日期、供应商或请求范围不拼成正常表。所有 quote map 日期必须是当前 cohort
已建立的目标，不能拿另一个有值日代替目标。

`research_sources._table` 仅对上述明确八位 trade_date、无 ts_code/日期范围的
close+open 请求允许缺 open 列。原 close 和身份列的必需性保持。未请求 open、
请求过但未返回、无证券行、重复证券、无效值、原身份未决、日历 gap 和完成未知
分别表示；不以一个默认 null 抹平原因。**daily 没有加入竞价 coverage 元数据 API**，
原一般 daily reader 的必需时钟和覆盖语义均不改变。

每个字段独立解释原正有限数，沿原有限数值精度约束。close 坏值不消去好 open，
open 缺失不消去好 close；同证券重复行则该证券两字段都未决。保留
`readable_open/readable_close`；仅当完成资格成立时进入 `next_open/next_close`。
不把 raw 价格拼成复权收益、隔夜成交回报、排名或策略效果，也不引入另一供应商补位。
泛用 capture complete 只能说明原读取流程状态，独立市场穷尽仍 `not-established`。

## 整份前版保留、修订与归档

第一次部分表直接保留各自合格的原字段，不要求先有一个完美结果。
若后继尝试使任何以前可读的字段消失，或丢失已成立的完成资格，则保留该 cohort
**整份**确切旧 result：原目标、所有行、结果摘要、首次记录时间、quote key、lineage、
覆盖和完成时钟都保持。新好字段不能与旧好字段逐 cell 拼接。

`current_attempt` 单独记录本次 quote 来源、完成状态、row coverage，以及每个丢失
字段和原因；页面明确显示旧观察被保留，不称为本次重新取得。完整兼容的后继表
替换整份结果。相同目标及整行结果 hash 维持原 first_recorded_at；变化才记录新首次
时间。checked_at、来源或当前 gap 可以变化，view_version 随完整模型变化。
first_recorded_at 是不可变记录声明，并不证明原行情当时已到达某个人。

pending cohort 遇到新 auction 继续保留。完成资格已经成立的旧 cohort，只有当新
auction 的 T 已超过其目标日时才退出活动集合，记录准确前版 SourceRef、原 cohort
身份和结果 hash。报价字段不全仍明确保留 gaps；“完成”在此指目标日完成资格，
不偷换成所有证券有价。归档入口按明确前版打开，不用动态 latest 猜测。

恢复当前根最多打开一份确切前版。前版的有效原 seed、日历前缀、所用原报价和
完整结果摘要均重新资格化；不能只信前版存在、保存的 hash 或调用方复制的模型。
前版当前尝试、保留缺口也从其具体来源重建。恢复不会递归展开前版的前版；
更早历史是可打开的确切引用，不声称本次审计了无限历史。原来源资格、成员、
日期或时钟错误直接拒绝，不能用保留旧结果掩盖。

## 容量、保管与整次终态

继续使用 `market-reading-custody-v1` 外根、原 SourceInput、root → query → page
引用、摘要、submission journal、exporter 和 fresh reader。新根的 reading recipe
保存具体来源 keys、前版关系和完整有限 snapshot；query/page 不复制成新私有数据库。
先检查输入和前版，再沿原保管流程保存；最后必须由不同 reader/backend 完整恢复
并逐模型核对，随后复核原授权。UnknownWrite 不补写、不切目标，保持原状态。

| 范围 | 本用途有限上界 |
|---|---|
| 直接 payload / 模型 | 各 2 MiB；保管根检查整个序列化 SourceInput，而非只检查 text |
| 活动 cohort / 本次归档项 | 各最多 16；pending 超限拒绝，不用展示分页隐藏或静默删除 |
| 明确日历窗口 | 单原查询最多 512 个自然日；每个 T 后前缀最多 512 日 |
| quote map / query | 输入 calendar+quote 最多 32；本次 current/previous 有效查询去重后最多 32，读取缓存也按原确切身份限 32 |
| 原 auction 根 | 每次最多 17 个不同原根（16 个活动身份和至多一个当前种子）；仅一份确切 T+1 前版，不递归历史 |
| daily 行数 | 每个完整已准入 daily 最多 10000 行；仍受原页、字节和总预算约束 |
| 原分页及宿主预算 | 每源页上限 5000、原 bounded 分页、query/page 限制与 GitHub 整次 512 次保持；不换 owner、不重置额度 |
| 展示 | Room 本面板 2 MiB，原 Room/HTML 总边界保持；导航 1–50 项，直接 reader 1–100 项 |

逻辑上限不是可成功塞满的承诺。多个完整原 auction、query 或 SourceInput 可先触及
原实际 GET/字节预算；失败保留原旧根，不宣称新结果已保管。保管过程中已经按原
journal 写入的独立 sources 不回删。旧查询只保留能证明本次有效结果、日历前缀和
当前尝试的确切原件；冗余日历可退出新根，但不能更改历史根或删 pending 证据。

SourceReads / operation_failure 复用共享小 facade 和现役 market / reading scope。
权限、scope 关闭、可见性反转、传输和整次预算终态原对象传播，后续读取立即停止。
最终核验失败不输出成功模型/HTML。普通 MissingObject 仅使本组 partial，其他独立
面板可以继续；它不成为寻找别的成功版本的指令。公开源权利仍用现役确认和可信
可撤销部署授权，与研究/材料权利分开；模型或页面按钮不填授权。

## 正常消费者与有限验证

Room 增加可选 `auction_next_session` 槽，严格只收整根 `reference`、非负整数
`offset`、1–50 的 `limit`、可省略/null 的精确 A 股 `entity_id`。无该计划不加载
本用途 registry、不读来源、不显示空面板。先核整个原 scope 和所有 cohort，
再生成独立 page。原 A 股 panel 的 `ROOM_MARKET_ASHARE_ROOT_REQUIRED` 保持。

原 `reading-room` 宿主 operation 只使用 `{"plan": <完整 Room 计划>}`，不新增
host operation/workflow。已安装包的正常本地入口：

```console
decision-os store --path /approved/existing-synthetic.git research-room \
  --input /approved/room-plan.json --out /approved/new-room.html \
  --market-registry /trusted/checkout/docs/sources/relay-registry.json

decision-os store --path /approved/existing-synthetic.git read-auction-next-session \
  --input /approved/next-session-plan.json \
  --market-registry /trusted/checkout/docs/sources/relay-registry.json
```

第二个输入只是 `auction_next_session` 槽内计划。原 --path 只表示已初始化合成
存储；实际目标继续沿已获准 binding，来源文字不能指定存储或 registry。

页面显示原 U 分母、U/D/Z 背景、五组、全部目标及完成状态、逐字段资格、旧结果保留
与当前缺口。完整模型可下载；原根、query、原 seed、前版和归档按钮导出
`{operation:"read-source",payload:<裸 SourceRef>}`；翻页/筛选/前版阅读导出原 Room
请求。没有自动联网、自动授权、innerHTML 或 localStorage。源文本沿 textContent、
JSON 转义和 CSP 保持字面值，窄屏表格在自身容器滚动。

专属合成测试覆盖原分母、精确目标、独立字段、旧时钟、整份后继保留与修订、
具体原来源反例、pending/归档、fresh restore、临时输入删除后的独立主 CLI 进程。
宿主测试执行完整交付脚本，把实际按钮产出的裸请求交回现役宿主；一次 403 后恢复
正常响应仍不能继续 GET，原 512 额度实际耗尽也同异常零后续请求，最终 private→public
反转拒绝输出。此处不是浏览器视觉验收声明。

`exercise_installed_auction_next_session` 只扩原唯一 wheel 安装 helper 链，checkout
外以 `-I` 和已安装包完成保存、删除合成 capture、两次重读及 direct CLI。没有新增
安装门或源码旁路。正式安装 gate、Ready CI 与最终 main 由集成者绑定实际新 tree
执行；本组局部测试不冒充它们。

## 尚未建立的事实与退出

本组没有读取/写入真实业务 records，没有新来源 GET、真实自然更新、旧 workflow
或远端操作。尚未证明当前 Relay 账号实际返回 open、真实原时钟、来源公开权利、
首个真实 T+1 消费或持续样本。登记字段与固定旧代码不能替代这些原件。
所有原 hold、失败预算、来源访问和权益继续归现役 owner，不因新 schema、日期或
case_id 重开。09:40 分钟是独立后继用途，不引用或依赖本组新 schema。

退役本用途时同一责任变更移除专属 pure/input/custody/reader/CLI/page、所有薄分派、
专属测试和唯一安装 helper 接点及本文。daily 可选 open 若已有别的真实消费者，
由其 owner 接续；标准源保管、历史引用、A 股/Global/auction、共同 scope 和其预算
按各自现役合同继续维护，不能随页面删除而破坏原件恢复。

[old-contract]: https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/d-auction-next-session-v1.md
[old-next]: https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/src/decision_kernel/runtime/d_auction_next_session.py
[old-inputs]: https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/src/decision_kernel/runtime/stock_market_inputs.py
