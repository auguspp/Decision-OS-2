# FTShare A 股日线接入

## 当前责任组

用户在 2026-10-18 的 #10 接管委托中明确要求将日线数据源切换为 FTShare，并已在同仓 Secret 设置 `FTSHARE_API_KEY`。这项委托修改日线来源选择；不会把既有 Relay 记录、失败、重试额度或复权因子改名成 FTShare，也不改变同仓 `main` / `records` 保管路线。

本责任组提供固定来源传输、严格日 K 资格检查、有限账户验证，以及同一 Store 上的生产集合与正常读取接线。`ftshare-check` / `ftshare-check-continue` / `ftshare-check-kernel` 不写 records，原核验已经结束。已验证的 Kernel v1 原文通过 `ftshare-adopt-kernel` 直接采用，新增行情 HTTP 为 0。正常 `ftshare-sync` 现在由可信配置的 `kernel_sync` 选择 v1 单股整段更新，直接继承已保管的资格和价格；旧批量配置 `qualification: null` 保留，不用单股证据填写批量资格。有限输入和有界更新不代表长期可用率或完整市场覆盖。

## 可核对的上游合同

原批量实现依据是 FTShare 官方 [FTShare-skill 固定版本](https://github.com/FTShare-Lab/FTShare-skill/tree/d2233464fc26b6d3c885f60543e316565361bd83) 中的 [stock-candlesticks-batch](https://github.com/FTShare-Lab/FTShare-skill/blob/d2233464fc26b6d3c885f60543e316565361bd83/ftshare-market-data/sub-skills/stock-candlesticks-batch/SKILL.md) 及其 handler；以下保留它的独立协议。当前采用的 v1 合同与真实结果见「复用 Kernel v1 整段日线」，两种入口不能混用请求身份、成功 envelope 或账户资格。

- 固定 HTTPS origin 为 `https://market.ft.tech`，前缀 `/gateway`；凭据仅发送到该 origin 的已列举接口，不接受任意 URL、代理、跳转或调用方请求头。
- 日 K 使用 `GET /api/v2/market/data/stock-candlesticks/batch`；`symbols` 为重复 query 参数，`interval_unit=day`，`adjust_kind=none|forward`。
- 请求头沿官方 handler 提供 `FTSHARE_API_KEY`、`Content-Type: application/json` 及 `X-Client-Name: ft-claw`；本项目仍使用自己的 User-Agent。客户端标识是官方请求兼容性的一部分，不能据此保证账户有接口权限。
- `since_ts_millis` / `until_ts_millis` 按 UTC+8 完整自然日首尾限定。本地最多 100 个 SH/SZ A 股标识、119 个自然日；同时给出时间上下界，不发送可能截断整段的 `limit`。
- 业务成功外层为 `code=200`、`message=success` 和 `[symbol, bars]` 列表。每个请求证券必须出现一次；明确空列表和遗漏证券分别处理。遗漏证券、重复日、错误日期及时间越界拒绝整批；可定位的 OHLC / 成交字段错误按证券和日期隔离。
- 价格和成交额用 Decimal 精确字符串保留，禁止浮点改值。成交额单位为 CNY；成交量保存原整数，单位尚未证明，不能直接与另一来源不同单位的 volume 相等比较。
- `none` 是原价。`forward` 保存供应商同一整段请求的前复权价格；当前官方说明未给出可复演的因子锚点，不能生成因子 1、从价格比值倒推因子、拼接不同请求的前复权序列或声称 PIT / 总回报。

## 有限真实校验

现有 `ashare-data.yml` 中新增手动 `ftshare-check`，沿用现有来源互斥组。只允许原生 private 的指定仓库、当前 main、成功 main CI、attempt 1，以及明确来源访问和私有保管授权。公开权利输入必须为 false。固定 study 为 `ftshare-daily-qualification-20261018`，原生 Actions 历史中只允许本 study 的首次运行，不能重跑或另开 run 重置六次预算。

先独立读回 `records` 上 Source10 的确切集合 `3fcaa33e745afa0084ff20136538a06ce8c38877`，核现有 master / 原价引用；prepare 阶段没有 FTShare Secret。capture 阶段才注入 `FTSHARE_API_KEY`，顺序进行：

| 次序 | 检查 | 目的 |
|---|---|---|
| 1–2 | 000001.SZ、600000.SH 的 2026-10-16 原价，重复读取 | 原始协议及同一请求的稳定性 |
| 3 | 已独立恢复 master 中的 100 股，2026-10-16 原价 | 实际账户批量响应 |
| 4 | master 中的 50 股，2026-07-01 至 2026-10-16 原价 | 一次请求覆盖 60 日窗口所需历史的容量 |
| 5 | 同 50 股、同时间范围的 forward | 供应商前复权响应与缺失情况 |
| 6 | 000001.SZ 同段 adjustment-factor 查询 | 仅保留实际语义证据，未签因素资格 |

每次至多一个 HTTP 请求，没有隐含 retry；遇到错误停止后续来源请求，保留真实失败，native exit 2 不改写成成功。空响应不能满足本轮预定非空样本的验收。返回体受字节上限、JSON 重复键 / 非有限数以及凭据反射检查约束。HTTP 错误最多读取 4,097 字节，用其中至多 4,096 字节的完整 JSON 提取白名单 MIME、短数字或固定符号业务码、实际读取字节数、截断状态与有限分类；正文、message、个人标识和凭据不进入诊断回执。正文业务码不能覆盖实际 HTTP 状态，分类也不代签账户拒绝根因。

原始 HTTP 正文逐个保留字节数和 SHA-256，另有请求、基准引用及计数回执；release 阶段在不含来源 Secret 的环境重新核验原生 private / main，再上传同一私有仓库的诊断 artifact。SourceInput 的原始正文与 normalized excerpt 明确分开，哈希只证明字节身份，不代替来源认证或保管授权。日配额 1000 仅是待核账户假设；没有上游计量证据时不得把 HTTP 次数等同计费单位或宣称每日余额。

## 收口所需后续证据

### 2026-10-18 首次实际运行

[运行 39141799035/1](https://github.com/auguspp/Decision-OS-2/actions/runs/39141799035) 使用 main `fd60f1f41e3d311132a1694704d12cda0d605899`，在第一个双股原价请求收到 HTTP 403 后停止：实际请求 1/6，已合格 candle checks 0/5，其余五项未请求。原 artifact `12141618961` 为 34,673 字节，SHA-256 为 `c51d2ea9ddc5167b9de0056716338252fcfd8bad1e9172121c425e5aee59a02f`；原生身份、ZIP 及 Source10 基准已独立复原。该运行不写 records，也没有可用于跨源、重复或历史覆盖验收的 FTShare 行情正文。

对照官方固定版本 handler，发现当前请求遗漏 `X-Client-Name: ft-claw`。补齐该头是已知的代码修正，403 的具体原因仍未确定。不能把这次拒绝直接解释为无效密钥，也不能声称修正后必定可用。

`ftshare-check-continue` 是同一 study 的一次有限接续，不是新的六次预算。prepare 必须验证原失败 run 和原始 artifact，核实际已用 1 次，并验证唯一 continuation、当前 main CI 与 private。接续最多五次：双股 raw、同请求重复、100 股单日、50 股整段 raw、同 50 股整段 forward；可选因子查询取消。总计最多 `1 + 5 = 6` 次。任一失败即停止余项；原失败继续保留，不能 rerun 或另开 continuation 重置额度。只有五项 candle 实际合格才可能形成日线来源资格，因子语义仍未验证。

### 补齐请求头后的真实结果

[唯一 continuation 39145109553/1](https://github.com/auguspp/Decision-OS-2/actions/runs/39145109553) 使用 main `64b4d5f6bfe0f9373a8e7f33f590979178de22e3`，在 2026-10-18 16:28 UTC 首个双股原价请求仍收到 HTTP 403，耗时 1.014542 秒。本次实际 1 次、两轮累计 **2/6**，合格 candle checks **0/5**；后四项未请求，可选因子继续取消。原 artifact `12143465795` 为 69,318 字节，SHA-256 `92bd239a077025ef24633b9050d8b859cc9233bfb650379a2be4c59eb85cd725`，原生身份与原 ZIP 已独立核验。

结果没有行情正文、Retry-After 或可核额度余额，不能形成价格一致性、历史缺失、实际批次容量或可用率结论。该运行不写 records，前驱仍为 `3fcaa33e745afa0084ff20136538a06ce8c38877`。补齐客户端标识没有消除拒绝；HTTP 403 的具体账户权限、密钥配置或访问策略原因仍未知，不能将 transport 的 `auth-or-entitlement` 分类当作上游诊断。

因此生产配置保持 `qualification: null`。这次 v2 one-shot 宿主已终止，不能重新派发。用户随后明确授权采用可行的旧 Kernel 方式；对应 v1 具名验证及原累计额度见下一节。它不要求先升级批量权限，不是自动 fallback，也不假定 Key 权限已经修复。原两次 v2 失败和相依日历未启用的事实保留。

#10 仍需真实运行原始 artifact、跨源同日原价对照、全声明股票范围的缺失分母、严格 5 / 20 / 60 日窗口、独立 Store 读回和正常 CLI / room 消费。生产切换应使用独立 FTShare 身份，引用旧 Relay 集合为前驱，保留旧失败账，不复制一套 Store 或用有限样本签全市场完成。未来自然日的更新稳定性，只按实际观察积累。

## 复用 Kernel v1 整段日线

用户在 2026-10-19 明确要求「如果原来 KERNEL 的方式可以实现，那就用 KERNEL 的方式实现吧」，并确认 `close` 已验证。[#10 认领6257049505](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6257049505)记录该路线选择和有限执行范围，替代此前等待用户选择批量权限升级的下一步。

参考旧 Kernel `cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` 的 `ftshare_stock_history_comparison.py`、`ftshare_market_inputs.py`、`yinquan_ftshare_56.py`。历史 [37720503804/1](https://github.com/auguspp/decision-kernel/actions/runs/37720503804) 在代码 `f732e0ef6f1c63c30ad05ff3f1c27663a649ef12` 完成 14 股 × 4 段历史的 56 次单股请求，原运行离线核过 56 份正文。这证明旧单股整段入口实际成功过，不等于本仓当前 Key 已获准，也不代表 Kernel 正常全市场日线已经默认 FTShare。

官方同一固定版本的 [stock-candlesticks](https://github.com/FTShare-Lab/FTShare-skill/blob/d2233464fc26b6d3c885f60543e316565361bd83/ftshare-market-data/sub-skills/stock-candlesticks/SKILL.md) 明确：`GET /api/v1/market/data/stock-candlesticks`，单个 `symbol`，`interval_unit=day`，`adjust_kind=none|forward`，起止毫秒参数，不传 `limit` 返回整个请求区间。`close` 是价格字段；本次沿用现有 Decimal、时间、OHLC 隔离及原价 / 前复权边界，不重新解释为 `latest`。

`ftshare_single.py` 是该入口的窄适配，复用 `ftshare_daily` 的日线验证核心，接受官方裸数组和旧 Kernel 已观察的整数 `code=0|200`、`data` 数组外层。其 request、原始 body、hash、schema 和 SourceInput origin 都保存真实 v1 身份；用于共享行校验的内存结构不是供应商原件。`source_for_single_capture` / `restore_single` 使用既有 Store、完整同仓 SourceRef 和独立重建；normalized excerpt 与原始正文分开。当前仍限定 119 个自然日、SH/SZ、每次至多一个 HTTP，无自动分页、重试或接口切换。

### 四次有界核验

现有 `ashare-data.yml` 的手动 `ftshare-check-kernel` 使用固定 selection：

```json
{"study":"ftshare-daily-qualification-20261018","route":"kernel-v1","predecessor_run":39145109553}
```

仍要求当前 private 主仓、精确 main、成功 main CI、attempt 1、原生唯一同名运行、明确访问及私有保管授权，public rights 为 false。原 check 与 continuation 原生身份、两个 ZIP digest、原请求和实际失败必须独立恢复；不能由 caller 填一个「已用2次」数字。任何输出目录共享本 run 的一次性准入标记，换目录 / 新 run / rerun 不恢复额度。

| 次序 | 请求 | 实际需要证明的差异 |
|---|---|---|
| 1 | 000001.SZ，2026-07-01..10-16，none | v1 实际访问、全部既有交易日、末日 `close` 对照原存储原价 |
| 2 | 同一请求重复 | 整段归一化序列一致 |
| 3 | 600000.SH，同段 none | 另一交易所的真实日期覆盖及末日原价 |
| 4 | 000001.SZ，同段 forward | 同一供应商整段前复权及实际日期覆盖 |

本次上限 4、原两次 v2 已耗用继续计入，累计上限为 **2 + 4 = 6 次 HTTP**。前置阶段从原 Store 正文按十进制 token 恢复对照 `close`、两所日历和引用；capture / release 重新读回，不能通过一起改 verification 和本地摘要修改基准。样本须没有坏行、具备至少 61 个收盘价且日期轴与原生交易日历完全一致。任一请求或检查失败即停止后项；有效响应后的 quota / Retry-After 也约束下一次访问。来源 Secret 只进入 capture，release 重新核 private / main 后才上传原始 bytes 与 capture。没有 records 写入、全市场激活或因子资格。

### 当前 Key 的实际 v1 结果

[唯一运行 39187632132/1](https://github.com/auguspp/Decision-OS-2/actions/runs/39187632132) 在实际 main `23cbac467af01c943f5a52d2dbd5331001536f15` 完成四项检查，均为 HTTP 200 / `qualified-response`，结果 `validated`。原 artifact `12158493623` 为 146,928 字节，SHA-256 `f1c15e0712a46b4c17e96739562f3b3c294d39edc3338033c96e34af1fd9f147`；原生身份、ZIP、13 个原成员与全部正文已独立核验。从原 records bundle 恢复两所主表、日历及原价后，直接从原始 bytes 重建四份 capture，离线分析新增来源请求为 0。

| 本次请求 | 已取得的证据 |
|---|---|
| `000001.SZ` 整段 none | 72 个交易日与原 SZSE 日历逐日一致，末日 `close` 精确匹配原 Store 基准，无隔离行 |
| 同请求重复 | 整段归一化序列相同，原始正文 SHA-256 也相同 |
| `600000.SH` 整段 none | 72 个交易日与原 SSE 日历逐日一致，末日 `close` 精确匹配原 Store 基准，无隔离行 |
| `000001.SZ` 整段 forward | 同一请求内的完整 72 日前复权序列，无隔离行；未签因子、PIT 或总回报资格 |

本次实际 **4 次**、与原两次 v2 HTTP 403 累计 **6 次**；每次观测耗时约 1.48–1.65 秒。返回头没有可核额度余额、request-id 或 Retry-After，不能据此推算计费额度。当前 Key 可访问这条 v1 路线已经实证；旧 v2 拒绝的具体账户原因仍不能由此代签。检查没有写 records，也没有改变活动全市场数据覆盖。

### 请求量与覆盖

单股「一次请求」取得一整段历史，并非每个交易日一次。`5224 × 2 = 10448` 是逐股分别拉 raw / forward 全段的一种完整回填估算，**不是旧 Kernel 每日固定调用量，也不是本次执行计划**。现有 210 槽批量集合的根容量和 Store 容量不支持仅将 batch size 改为 1；本轮不派发这个朴素计划。

旧 Kernel 的 `daec/stocks` 按 `page` / `page_size` 抓当前全市场快照，按实际空尾终止，不能信带 filter 的 `total_items`；官方 `close` 字段可用于当期收盘截面。这个入口没有历史日期参数。另一个 `stock-history-list` 可不传 code、按指定日分页，但官方只承诺主板且明确累计复权因子不可用。两者均不能直接补出全沪深的 60 日前复权序列。后续全市场采用应利用真实可用能力及已保管历史，保留完整分母和 raw / forward 各自缺口；有限 v1 样本通过不能填写现有 v2 qualification 或签 #10 完成。

维护责任仍为 #10 的 FTShare 日线入口。临时 Kernel 核验完成并有持久接续依据后，随旧 check / continuation 的真实依赖同步退出专属运行入口；已保管 v1 原件的只读恢复、共享 close 核心和正常业务所需能力继续保留。

## 同一 Store 的生产接线

### 显式收盘后观察合同

第一批常态更新的接续范围和运行验收由 [#147](https://github.com/auguspp/Decision-OS-2/issues/147) 及原来源责任 #10 维护。single plan 与可信 `kernel_sync` 可以显式声明 `completion_policy: "ashare-eod-1835-v1"`：北京时间 18:35 是本系统采用当日已收盘观察的保守截止点，**不是 FTShare 逐笔时间、供应商准点承诺或交易所行情认证**。只有该字段明确存在的新计划，才允许 `created_at` / 正常读取使用当日日期；原始 capture 的最大实际价格日期也必须在真实 `acquired_at` 时已经达到该截止点。提前取得的原件不能因为以后读取而被升级为合格当日收盘。

没有该字段的全部既存 plan 与旧手动配置继续原来的“前一北京时间自然日”规则；原 `market_collection_inputs._reading_clock` 不改。新 reader 按保存的可选字段重建原计划；不回填旧字段、不改旧原件或旧 view 的时间语义。未知 policy、显式 null、续取时改 policy 均拒绝。`application/market_completion.py` 提供共用的政策名称、时区、截止点和完成日期函数，FTShare 计划、正常阅读及价格消费者复用；日期是否真实开市仍来自完整原生交易日历。

工程兼容与常态运行分别验收；下面固定 study / 旧 generation 的描述保持其原范围，不能据此重新派发已结束的来源核验。

### 当前有限配置：四个独立历史候选

可信配置为 [config/ftshare-daily.json](../../config/ftshare-daily.json)，固定集合 `ftshare-cn-eod`，当前手动更新世代为 `g20261020-independent-v1`。其 `kernel_sync.base_reference` 精确指向 [v4 原运行 39521263796/1](https://github.com/auguspp/Decision-OS-2/actions/runs/39521263796) 已保管的 `daily-20261020` FT r024 根：records commit `d8ce218889ff19fd85a1ef9dbd3d68d7d5e1fbd6`，完整 manifest path、SHA256 与 all selection 见配置。原 Source10 `legacy_reference`、Kernel adoption 资格和 `qualification: null` 继续保留。

本组沿用该 base 的 `2026-07-01..2026-10-20` 整段、显式 `ashare-eod-1835-v1` 及两所已保管的完整 current-L 主表和日历。四个候选来自原 5,226 范围的 2026-10-09 至 10-16 qfq5 端点筛选：5,208 项可比较、18 项缺失保留，排除既有日更六股后按 Decimal 涨幅排序，等值才按代码排序；第 4／5 名无并列。该历史筛选不重跑，也不代表候选在 10/20 的强度或及时发现。

|历史顺序|候选|本组新增目标|
|---:|---|---|
|1|`300006.SZ` 莱美药业|`none`、`forward`|
|2|`002343.SZ` 慈文传媒|`none`、`forward`|
|3|`301052.SZ` 果麦文化|`none`、`forward`|
|4|`000530.SZ` 冰山冷热|`none`、`forward`|

这八个目标与 base 的十二个 owner 完全不重叠。真实 v4 主表分母为 5,224（SSE 2,318、SZSE 2,906），四股均 eligible；原十二个请求、状态、引用和原因完整继承，历史累计预留保留为 33，本代新增预算为 8。正常纯计划得到 12 个继承 owner 与 8 个 pending owner，最多形成 20 个活动 owner；配置和纯计划不证明八个新请求已经执行或窗口已经合格。

现役入口仍是 `ashare-data.yml / ftshare-sync`，selection 为 `{"collection_id":"ftshare-cn-eod","generation":"g20261020-independent-v1"}`。本组不传 master／calendar override，不新增 Relay 请求。执行前仍核当前 main、完整 CI、实际写前沿、原额度和容量；若其他世代已经接续这个 base，必须先对账，不能改 ID 或回指旧版本领取预算。成功后分别核八个原 FT 正文、正常 raw／qfq 5／20／60 日读取和 daily 扩展前沿，再纳入新备份与独立恢复。

[daily-market.json](../../config/daily-market.json) 继续原六股、每次最多 12 个价格请求、原 seed 和原 schedule。四股的这次有限扩展不使其加入日更池；以后缺少新交易日价格时保持真实陈旧／缺失状态。完整主表其余证券仍留在分母，有限扩展不签全市场覆盖或长期可用率。

| 手动 operation | 输入与来源访问 | 结果 |
|---|---|---|
| `ftshare-status`（默认） | 固定集合与世代；来源 HTTP 为 0 | 从既存 records 恢复最新 FTShare 集合、预留次数、缺口与暂停原因 |
| `ftshare-sync` | 可信有限集合与世代，`work_units` 为 1–32；每个新目标最多一个 v1 单股整段 HTTP | 先保管预留，再取一次原始正文并保管结果；继承 owner 不重新请求，未完成槽保持真实状态 |
| `ftshare-adopt-kernel` | 原采用世代的历史入口，`work_units=1`，新增来源 HTTP 为 0 | 原四份正文及证据已经保管；启用 `kernel_sync` 的配置拒绝再次采用，不向新世代复制同一原核验 |
| `ftshare-calendar` | 固定 study `ftshare-calendar-continuity-20261018`、`work_units=1` | 仅两次 Relay `trade_cal` 元数据请求，独立保管 SSE / SZSE 截至 10/18 的日历；不取 Relay 日线价格 |

上述入口仍在 `ashare-data.yml` 的原有来源互斥组内。正常 Public `ftshare-sync` 沿 [#164 固定部署授权](../engineering/public-runtime.md)，要求同仓精确绑定、当前 main、合格 main CI、attempt 1 和原访问／保管范围；输入 `access_and_custody_confirmed=true`、`public_rights_confirmed=false`。控制根与原文保管核有效 `sources` grant，发布产物前另核 `market` grant、实际可见性和 main。`ftshare-status` 只读；FTShare Secret 只进入实际 FTShare capture／sync 步骤。历史 check、adoption、calendar 的原身份、权限与终态继续按各自合同保留，不因本配置重新派发。

其他明确授权的首次 sync 可携带已保管的 `calendar_references: {"SSE": SourceRef, "SZSE": SourceRef}`，必须是本仓独立可恢复的完整引用，覆盖计划区间且不能改变既有历史开闭市记录。本组省略该字段，直接继承 v4 原日历；续取时显式提供则必须与原计划相同。`ftshare-status` 不接受日历覆盖输入；历史根继续通过原精确 SourceRef 正常读取。

### 批量来源资格与未激活状态

`qualification` 只能为空或含 `run_id`、`artifact_id`、`sha256`、`code_sha` 四个字段。填写前核对成功 continuation 的原生身份、原始 ZIP、实际已耗预算、重复响应、Decimal 同日原价比较、样本缺失和真实响应容量；其中 code SHA 是该次资格运行的代码，不能用后来的生产 main SHA 冒充。五项 candle 通过仅代表这五项结构、非空样本和重复检查，历史个别空证券仍须在独立分析中披露。

未配置 `kernel_sync` 且批量资格为空时，sync 在创建 Store 或 FTShare 客户端之前返回 `FTSHARE_RUNTIME_QUALIFICATION_PENDING`，不预留、写 records 或请求行情。status 可以继续只读恢复已有集合；尚无集合时返回 `not-retained` / `FTSHARE_COLLECTION_NOT_RETAINED`、来源请求 0、退出码 2。这表示该世代尚未保管新源数据。

旧批量分支仍依赖合格 continuation 的临时 artifact；实际 v2 运行没有取得该资格。当前 v1 分支从同一 Store 恢复原 adoption、原始 ZIP 与四份 capture，匹配可信 `kernel_adoption` 身份后才创建来源客户端，不重新下载临时 study artifact，也不重复四次资格请求。自动跨世代更新仍未启用。

### 采用已经验证的 Kernel v1 原文

`kernel_adoption` 单独固定本次成功运行的 `run_id`、`artifact_id`、`sha256` 与实际 `code_sha`。采用宿主重新核原生运行、原 artifact 及完整正文，再使用现有 `source_for_single_capture` / `restore_single` 和同一 Store 保管。保存原始 ZIP、原生归属及四份 capture 的完整引用，使正常读取在已采用后独立于临时 Actions artifact 的保留期。原文和共享 `close` / Decimal 验证继续由 FTShare 日线入口维护，不把本地摘要当成原始响应。

正常价格输入固定为 `raw-sz-history`、`raw-sh-history`、`forward-sz-history` 三个角色。`raw-sz-repeat` 保留为重复核验证据，不增加证券覆盖或第二个价格 owner。集合采用的是已保管输入，采用阶段的来源预算、预留和实际 HTTP 均为 0；原检查的 4 次与累计 6 次属于历史证据，不能伪造 `begin → request → finish` 来导入。重复采用应按既存精确引用对账；未知写入先恢复原逻辑版本，不能改键覆盖或生成第二份集合。

集合仍继承完整 5,226 项主表、原两项隔离、两所日历和 Relay 前驱。三个价格输入齐全只表示这次有限输入已保管：它不能令整个市场变为 `current-complete`，也不能替代当前用途的日历覆盖。原价与前复权各自计算原有 5 / 20 / 60 日严格窗口和独立端点，缺失保持可见；`600000.SH` 没有本次 forward 输入，不能以 raw 补上。

正常 CLI / `open_market` / room 消费同一个 collection 根，仍以全部主表为分母。对本次两个证券分别读取，并读取一个无 FTShare 输入的证券，才能验证分页没有隐藏缺口。源检查、原文采用、独立 Store 恢复与正常产品读取分别按实际回执成立；本次有限采用不关闭 #10 的全市场覆盖与持续更新责任。

### 已保管 v1 的有限后继更新

[原采用组交接](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6258970328)已经核验实际采用、正常 status / room 和新备份的离线恢复。后继按[6259072790](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6259072790)接通同一个 `ftshare-sync`，使用 `ftshare-single-collection-plan-v1`，外层 custody、状态、预留及未知写 journal 保持原合同。

`kernel_sync` 要求 `base_reference`、`start`、`end`、`targets`、`source_budget` 五项，可额外声明已实现的 `completion_policy`；当前有限组显式继承 base 的 `ashare-eod-1835-v1`。每个 target 是唯一的 `symbol / adjust_kind`；预算严格等于新目标数。新请求以该股、该复权类型及完整日期区间为身份，不按交易日拆请求。各世代预算与已终止来源核验分别记账；不把预留次数当作每次都已发出 HTTP 或账户计费。

新世代以精确 FTShare base 为 genesis 前驱，必须是该集合可写前沿，不能改指旧 revision 或另开 child 绕过失败、inflight、hold 或已预留请求。上一有限计划未终结时不能开新世代。继承 complete / failed owner 的原请求、结果引用和原因，本代计数为 0；同一失败请求不会因世代名变化重新获得访问次数。历史根仍可独立恢复，不受最新写入前沿限制。

后继可推进日期，但起止必须单调，单次区间最多 119 个自然日，覆盖新日期的两所合格日历须已经保管。原 Relay 日线仅用于原资格证据，不随每次更新重抓。替换的 owner 使用一次新的完整整段请求；旧版本留在前驱中，不把两次 forward 的价格拼成一条序列。未更新 owner 可以继续读取旧价格，但其请求没有覆盖新开市日时，完整状态和 freshness 都保持缺口；日历已经证明休市的日期不强制重抓价格。

所有继承与新建的活动 owner 合计最多 212，state 不超过 `512 KiB - 16 KiB`，根正文仍最多 512 KiB。有限目标不缩小已采用完整主表的分母；当前组沿真实 v4 的 5,224 项，旧历史根仍按其原 5,226／5,224／2 范围恢复。新增八个 owner 即使完成，也只扩展四只股票的 raw／forward 输入；实际窗口与缺失状态以正常 reader 为准。

首个 `g20261016-v1` 补缺口组曾从采用后的 `ca6477ccdb6de562a0764499b81876c50a3be737` 原 records bundle 执行只读计划预检：5,226 / 5,224 / 2，3 继承 owner、1 新槽、预算 1、预留 0。计划 136,792 字节，状态 138,547 字节，初始根 139,308 / 524,288 字节，SourceInput 162,348 / 2,097,152 字节。该历史预检新增来源请求和 Store 写入均为 0，不代替后继世代各自的原生容量、预算与真实行情验收。

### 范围、保管与不可重置的账

正常 `ftshare-sync` 的Public保管沿[#164固定部署授权](../engineering/public-runtime.md)，包括集合初始根、预留、原文与结算。集合控制写入要求原custody委托、原生真实Public可见性和精确数据绑定的有效 `sources` 授权；调用者公开确认字段不能单独放行任意公开目标。缺失或撤销授权仍拒绝，历史check/continuation/adoption的原资格及终态不变。

历史采用范围继承原主表的 5,226 项声明，其中 5,224 项可请求、2 项隔离；该历史版本的分母保持。后继采用真实完整 current-L 主表时，范围由该版本的完整原页决定，保留当期全部隔离和缺失项。原批量计划每批 50 股，分别一次 `none` 与一次整段 `forward`，对应 210 个价格槽；当前 v1 计划只采用显式有限目标，不将该批量列表展开成逐股全回填。不得以删除隔离项、覆盖旧来源身份或拼接不同 forward 请求来增加覆盖。

原 Source10 的 220 槽、原始终态、维修 grant 与预算随明确前驱保留。FTShare 使用独立 SourceInput 和集合身份，在相同 repository / records / store_id / anchor 上保管，不建立第二套数据仓。每槽先保管 `inflight` 与一次不可退回的预留；再执行至多一次 FTShare 请求。成功正文按原始字节独立读回，随后才保管完成根；错误保留 failed 及稳定原因。未知写按原键对账，未核清的 inflight 禁止重发。

鉴权失败 401/403、429、明确剩余额度 0、不可解释或超出当前有界等待的 Retry-After，会写入持久 `source_hold` 并停止下一次来源请求。有效正文与“此后暂停”可以同时保留。短等待在结算前执行；中断不会重置已预留次数。新 run、换输出目录或重复读取不能清除 hold、重开失败槽或领取新额度；当前宿主没有自动解除 hold 的接口。

每次来源准入继续核原生 GitHub 配额和 Store 容量；不等待额度重置刷完任务。单响应 2 MiB、集合正文 512 KiB 及既有 native Git 读取容量上限继续生效，真实价格正文和完整消费者模型仍按实际测量验收。合成测试的体积或行数只证明相应边界行为。

已用 Source10 的原 bundle 独立恢复真实范围，执行无网络、无 Store 写入的初始计划预检：计划 304,532 字节、初始状态 323,067 字节、根正文 323,839 / 524,288 字节、编码 SourceInput 375,077 / 2,097,152 字节，均通过现有结构与容量门槛。该预检确认 210 槽的初始包装可保管；最终完成根、真实 FTShare 正文、完整生产 bundle 和阅读室体积仍未获实际数据验证。

### 正常消费者与维护义务

正常 `open_market`、CLI 和阅读室识别 FTShare 集合引用，独立恢复主表、日历及保管的 none / forward 正文，计算原有 5 / 20 / 60 日严格逐日窗口与独立端点结果。分页和实体筛选保留完整范围哈希、缺失分母和隔离项。缺失 counterpart、坏行、交易日缺口及 source_hold 均可见；forward 缺失不回填 raw。来源请求数必须为 0，展示数据来自 Store 的确切引用。

日历覆盖至少包含读取时已完成的上海自然日，才可判定 freshness；陈旧交易日只从已保管的开市日计算，不猜测周末休市。FTShare 原始响应保留与旧 Relay 主表 / 日历摘录的证据等级分别说明，整体模型不能声称每个依赖都保有完整原始 HTTP。

固定 check、continuation 和 calendar 的过渡责任完成后，按实际依赖同步退出专属 workflow 选项、CLI、配置和测试。仍服务正常集合、严格窗口、读取与历史 exact-reference 恢复的共享能力继续保留；不会仅因 #10 工程合并而自动退役或签数据验收。

## 2026-10-19 跨板块有界扩样（历史配置）

以下保留原组的冻结范围和验证义务；现役配置见上面的四个独立历史候选，不能按本节重新派发旧八次请求。

前组 [#10 回执 6260063834](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6260063834) 已将 `g20261016-v1` 的沪深两只股票、两种口径与全部历史原始响应完成保管和独立读回；当前 records 正常可写前沿为 `2649d19995f28399d3b3fced12ba4f86b1d5a5a9`。本组由 [#10 新认领](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6260406580) 接续，**沿同一 Store 复用现役 `ftshare-sync`，不新增行情采集器或放宽安全门**。

新受信配置 `g20261016-v2` 的 `kernel_sync.base_reference` 是上组实际终态 SourceRef，包含原 records commit、manifest path、sha256 与完整 all selection，非推测路径。区间继续固定 `2026-07-01..2026-10-16`；截至本计划冻结时 2026-10-19 交易日还未收盘，故不将当天作为可确认日收盘价，不重新派发原已保管的两所日历。新目标是 `002384.SZ`（东山精密）、`002714.SZ`（牧原股份）、`300750.SZ`（创业板）和 `688981.SH`（科创板），每只分别独立要求供应商 `none` 和 `forward` **整段**，合计最多 **8 个新请求/8 个永久预留**。原 `000001.SZ` 与 `600000.SH` 两口径的 4 个终态 owner 完全继承，原研究 6 次与已执行正常生产 1 次不重复。

这是一个有意义但有限的跨板块来源样本，不是全市场采集承诺。执行前必须由真实已存主表验证四只证券均 eligible，且确认上代完整终态、唯一可写前沿、private/main/CI、实际 Store/API 配额、512KiB 集合根及完整响应容量；任何一项不满足即停止，不以修改 ID、切换旧基线或隐式重试获取额度。每笔新来源请求先由既有 journal 持久记录 in-flight 与一次预留；401/403/429、source_hold、错误及未知写均保留真实状态并停止按原合同继续访问。建议先小批 `work_units` 观察，逐次记录真实供应商 HTTP、Store API 消耗及终态；8 仅为任务硬上限，**不是已花费 HTTP、供应商计费单位或账户真实余额**。

工程完整 CI 只证明现有入口兼容这个配置；来源实际成功后，仍须验正常 `ftshare-status`、至少两个不同板块实体及无输入证券的 reading-room、原始 `close` 5/20/60 Decimal 窗口、固定 records 备份和独立恢复。原 5,226 / 5,224 / 2 分母、缺失、停牌与复权不确定性不会被小样本隐藏。若真实请求缺项，只报告真实完成、失败和待处理，不将本组或整体 #10 冒称全市场收口。此计划不引入定时器、通知、新研究、第二 Store 或任何公开再分发权。
