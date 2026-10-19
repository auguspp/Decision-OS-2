# 有界 A 股日常更新

责任：#10 的日线来源与 #13 的正常阅读；批次归 [#147](https://github.com/auguspp/Decision-OS-2/issues/147)。用户在 2026-10-19 要求登记 Kernel / OS2 差距、分批实现并先实现第一批，其中包含常态更新。本合同把这项新授权落实到已有 GitHub Actions、同仓 private `records` 和现役 FTShare v1 日线集合，不改变旧一次性 study、旧失败额度或研究委托。

## 真实范围与周期

可信 [daily-market.json](../../config/daily-market.json) 声明六只已有真实历史样本：`000001.SZ`、`002384.SZ`、`002714.SZ`、`300750.SZ`、`600000.SH`、`688981.SH`。保留原市场 5226 只分母；六只日常更新不能代签全市场完成。

[daily-market.yml](../../.github/workflows/daily-market.yml) 使用 GitHub 原生 `schedule`：`35 10 * * 1-5`，即北京时间工作日 18:35 一次。时区换算固定 UTC+8，没有自建时钟服务。手动 `update` 与自动调用使用同一合同；18:35 之前只生成 `not-due` 读取回执，不取日历、不取价格、不写入日更意图或消耗当日来源额度。当前日的合格价格还须满足原采集与读取时钟合同，不能让提前取得的正文在收盘后自动获得新资格。

一天至多推进一个正常日更周期，每周期最多 **2 个实际交易日历请求 + 12 个 FTShare 单股整段请求**。两所 `trade_cal` 返回全部开闭市日，按实际完整日历决定最后已完成交易日，不凭星期推定开市。休市且没有新增交易日时只保留实际日历与原市场引用，不重抓价格。新开市日使用最长 119 自然日整段窗口；是否足够 5/20/60 交易日比较由实际会话及价格点判定，119 天不是保证，缺点继续显示 partial。

日历只复用既有 Relay 元数据通路；价格只使用固定 FTShare v1 `stock-candlesticks`、`close`、`none/forward`。没有 v2 批量资格、供应商自动切换、盘中行情、隐式全市场回填、新的 Full / Brief 生成、主动通知、公开部署、第三方保管或旧 Kernel 停用。

## 继承、预留和停止

首次从配置内确切的六股终态 FTShare SourceRef 接续，以后使用前一成功日更引用。原 `plan_ftshare_single_collection` / `run_collection` 继续持有价格计划、单股预留、原文、资格、累计旧额度及每个 generation 的唯一前驱。只在实际新增完成交易日时生成确定的 `daily-YYYYMMDD`；换 run、重跑、换日期或更新配置不能把耗尽请求再变成 pending。

新日历的两次预留使用同一个 `VersionedStore.put_source` 写成小型 `SourceInput / SourceRef`。`daily-market-cycle-YYYYMMDD-rNNN` 是原 Store 内不可变的来源工作记录：先独立保管一次预留，再发一次 HTTP，再保管实际成功或失败。它不保存另一套价格，不是新的业务主库、后台任务队列或手工同步状态看板。真实日历查询自身仍通过原 `retain_market_query` 保管和独立恢复。

同日已经完成时再调用只读，来源请求为 0。未完成的安全 pending 周期只接续原引用和剩余额度；即使到了下一天，也先完成这一个旧周期，不另立日期绕过。以下情况停止新的来源请求并保留原问题：

- 原 FTShare 基线或当前周期有 source hold、failed 或未知 in-flight；日历来源失败、历史开闭市被改写、两所最后交易日冲突。
- 一个来源请求已预留但传输或 Store 效果不明；按原引用对账，不能借新 run 或新日期重发。
- 原生 private / main / 当前 CI 不再成立、原生额度不足、Store API 准入或容量不足。
- 发现另一个后继、重复序号、控制链缺口、旧根或配置范围变化。读取失败不会被解释为没有历史，重新开一份预算。

只有安全的未取源 pending 可以继续。已失败或未知的周期没有自动解锁入口；来源 owner 核清真实条件与原副作用后，按现行有限修复合同处理，不能只改 ID、删控制记录或换 Seed。日历 one-shot `ftshare-calendar-continuity-20261018` 的原两次预算和终态保持，本模块不调用它。

## 宿主与正常消费者

入口：

```bash
python -m decision_kernel.interfaces.daily_market_cli runtime \
  --out "$RUNNER_TEMP/dos2-daily-market"
```

自动 `schedule` 的代码 SHA 来自受信 `GITHUB_SHA`；手动输入只接受 `operation=update|status` 和确切 `code_sha`。宿主重新核原生仓库 ID / private、当前 main、自己的工作流与 run / attempt 1，以及该代码最新一次合格 main CI。不能伪装成旧手动核验宿主。原生运行创建时刻控制当次周期日期，实际采集和正常读取继续记录各自真实时刻；延迟超过 24 小时的运行不取得新来源额度。

日更与现役市场来源共同使用 `decision-os-relay-source` 串行组，`cancel-in-progress: false`。GitHub 这个组不是分布式原子锁或无限 FIFO 队列；原 CAS、唯一前驱、独立读回继续防止混写。每个有限单元使用同一原 API / writer / reader，沿用来源前 240 次准入、单元 512 次硬上限及跨单元共享的原生 quota 观察。每个单元的真实容量须预留可保管和读回空间，日更不扩大根、8192 版本、Git 下载或树响应边界。

### 同次运行内的原生额度等待

工作流沿用自身 `GITHUB_TOKEN`。GitHub 官方的普通每仓令牌额度为每小时 1000 REST 请求，GraphQL 单独每小时 1000 点；代码不假设工程凭据的较高余量也适用于工作流。两次日历与十二次价格的独立保管和读回会跨越一个普通小时窗口，因此在同一个原 run 内、下一个尚未预留的单元前保留 512 次原生额度。首次 FTShare 单元、后续单股单元和最终周期完成保存均使用这个边界。

只有主动检查发现余量不足、未出现 `throttled`、且最近实际 core 响应提供有效的未来一小时内 `reset` 时，才能等待该原生窗口。每次睡眠最多 60 秒；初始窗口之后最多 **5 次实际刷新等待**，累计等待不超过 **5 小时**，工作流作业上限 **330 分钟**。这是有限的同次运行等待，不重新进入 `run_daily`，不重建来源 client、不重新开始 receipt、不把单股槽位、预留或累计来源额度置零。等待期间持续更新安全回执，记录等待次数、累计时间和各原生资源的最近观察。

到达 reset 之后，沿用原 API、令牌和共享 `GitHubRateLimits`，实际读取仓库 private、当前 main、本次 run 和最新 main CI 四个原生响应。只有响应证明 core 的 reset 已前进，且所有已观察资源仍满足 512 预留，才开始下一个单元。GraphQL 余量不足时直接暂停：普通 GET 不能刷新 GraphQL，墙钟经过旧 reset 也不能补写额度。缺少、过期或未前进的 headers、超出等待边界，以及真实 403 / 429 / 权限或 CI 变化都保留暂停或失败；不把这些情况改成普通重试。刚好跨过 reset 而最后观察仍是旧窗口时允许保守暂停，不保证每次额度不足都自动恢复。

完成周期的独立保存随后仍执行正常市场读取、工作台读取和输出前原生检查。工程门禁以实际 native API wire 的 core / GraphQL 分开计数，核完整 2 + 12 原始请求和尾部消费者余量，不以新建 API 对象的局部计数代替令牌额度。新窗口等待不能重放来源 failed、source hold 或未知 in-flight；这些状态继续按原引用核对。

输出分为：

| 输出 | 含义 |
|---|---|
| `result.json` | 安全计数、周期状态、实际请求与失败、原生代码身份、正常 reader 的 freshness / coverage；`store_quota_wait` 保留同次运行的原生等待和额度观察。没有研究执行或通知。 |
| `control.json` | 同 private 产物内的确切 `cycle_reference`、`market_reference`、generation 和已保管日历引用，供现役正常消费者使用。 |
| `reading-room.html` | 原统一阅读室的离线页面，显示本次更新状态、自动发现并固定的已存 Brief、研究、待复核问题与实际市场覆盖。 |
| `reading-room.json` | 与 HTML 对应的只读模型及实际日更状态，保留原精确引用、内容截止与失败。 |
| `reading-summary.md` | 同模型派生的人用摘要，经发布前私有核验后写入本次 Actions Summary。 |

完成或失败之后均尝试原 `read_ftshare_collection_version`，采用最新真实尝试及其缺口，不安静换成较早的成功快照。工作台可以调用 `daily_market_reference(store, registry, policy(...))` 取得相同精确引用及日更状态，再进入自己的正常读取；此 resolver 不调用来源、不写 records。正式报告、研究采用和人工决定仍按原版本独立进行。

`status` 作业只有 contents / actions 读取权限，没有来源 Secret。`update` 作业用现有来源 Secret 和原 `records` 写入权限，Secret 不交给后续阅读步骤。产物发布前再核一次原生 private / main。读取和操作失败保留本次 failure / partial，不能用上一份成功报表代替。Actions 产物有到期日；持久真源仍是原 records。

正常阅读步骤运行 `python -m decision_kernel.interfaces.daily_reading_cli --out "$RUNNER_TEMP/dos2-daily-market"`，只取得 Store 读取凭据，复用原 `reading_commands.perform` 的单次权限/预算作用域。它核对同目录 `result.json` 的原生本次 run、代码与事件身份，通过 `daily_market_reference` 取得最新实际尝试，再使用 `brief={"select":"latest-saved"}` 从原发行目录发现并固定简报。读失败、坏控制链或旧内容继续显示原缺口，不借重新生成 HTML 声称今日已发行新 Brief。该步骤不取得新闻或行情，不写 records，也不自动更新研究判断。

人用入口为[本工作流页面](https://github.com/auguspp/Decision-OS-2/actions/workflows/daily-market.yml)最近一次运行的 Summary。完整页面与精确引用在该次私有产物中；没有另建网站、自动发布或第二个当前状态文件。工作流的 schedule 路由不依赖未保证存在的事件 repository.private 字段，所有来源与 Store 副作用前及输出释放前仍由原生 API 严格核私有仓库身份。

## 暂停、回退与证明边界

可以在 GitHub 原生界面停用本工作流，或经普通 PR 将本专属配置 `enabled` 置为 false。暂停不删除历史、不清除失败、不取消已有在途操作；在途结果仍按原引用核对。恢复 enabled 只恢复原合同，不能解锁已失败周期。代码回退使用普通反向 PR；新增 records 原件与它们的现役只读支持继续保留，不强制回退业务分支。

工程测试包括同日零重放、真实日历休市、新日期继承累计额度、日历拒绝 / 历史冲突 / FTShare hold 跨日阻断、未知预留以及独立 Git 恢复；原生 Git/API 模型另核普通 1000 core / 1000 GraphQL 额度下的同 run 完整 2 + 12、独立新窗口响应、五次刷新上限、陈旧 headers 与拒绝停止、完成后的消费者余量。合成数据和合成时间测试只证明工程语义，不是已经等待未来五小时或已经连续更新多日的证据。首次真实运行、原生 schedule 启用、每个后续自然交易日、人工工作台体验和长期持续性分别记录；一轮成功不能签未来 uptime 或全市场可用。

## 组件复用与退出

2026-10-19 核读 GitHub 官方 [schedule 事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) 和 [workflow concurrency](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency)。原生 schedule 只从默认分支触发，可能延迟或丢弃；因此本合同使用实际运行 / 日历 / 来源时钟，而不把 cron 字符串称作准点 SLA。选择已有 Actions 配置与 Python 标准库窄适配，不另装通用调度、监控或备份平台。

普通令牌的独立额度和真实响应等待依据同日核读的官方 [REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) 与 [GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)。继续复用已有 `GitHubRateLimits.observe` / `admits`；宿主只添加有界等待与原生复核，没有通用重试中间件、第二个令牌或手工额度表。

本责任维护 `application/daily_market.py`、`interfaces/daily_market_cli.py`、`interfaces/daily_reading_cli.py`、专属配置、工作流及日更/阅读入口测试。退役执行能力时同步退工作流与采集入口，保留尚有已存消费者依赖的日历控制读取、FTShare 原件及独立恢复支持；共享 Store、来源、CI 与正常阅读不因本模块退出而删除。
