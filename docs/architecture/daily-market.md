# 有界 A 股日常更新

责任：#10 的日线来源与 #13 的时钟接入、正常阅读；批次归 [#147](https://github.com/auguspp/Decision-OS-2/issues/147)。用户在 2026-10-19 要求登记 Kernel / OS2 差距、分批实现并先实现第一批，其中包含常态更新。本合同把这项新授权落实到已有 GitHub Actions、同仓 private `records` 和现役 FTShare v1 日线集合，不改变旧一次性 study、旧失败额度或研究委托。

## 真实范围与周期

日历传输失败的一次显式恢复见[日历恢复合同](daily-calendar-repair.md)；原写入实际已保管而宿主回执未知的固定五笔对账，见[原写入接续合同](daily-write-reconciliation.md)。两者均保留旧失败与已用预算，普通调度不能自动启动恢复授权。

后续新周期的完整 current-L 主表刷新与正常 FT 后继采用，见[主表刷新合同](daily-master-refresh.md)。新 v4 周期在两所日历确认新增已完成交易日后、价格预留前执行主表阶段；旧 v1/v2/v3 周期仍按原意图恢复，不追加主表请求。当前仓库的公开部署与资料发布按[公开运行合同](../engineering/public-runtime.md)的既有可撤销授权判定，本文件历史 private 描述不另设公开授权障碍。

可信 [daily-market.json](../../config/daily-market.json) 声明六只已有真实历史样本：`000001.SZ`、`002384.SZ`、`002714.SZ`、`300750.SZ`、`600000.SH`、`688981.SH`。原完整主表的 5226 只分母留在旧版本；新完整主表采用时由实际 scope 重建分母，隔离项和未覆盖证券继续计数。六只日常更新不能代签全市场完成。

[daily-market.yml](../../.github/workflows/daily-market.yml) 的初始 GitHub 原生 `schedule` 是 UTC `35 10 * * 1-5`（北京时间工作日18:35），这是保留在业务policy与原生历史中的原定时槽，并非外部时钟的请求值。正式切换时同一 PR 将 [daily-market-clock.json](../../config/daily-market-clock.json) 选择为 `external-cron`，并从 workflow 中移除 `on.schedule`，仅保留 `workflow_dispatch`；已有历史 `schedule` 记录和运行解析继续可读。外部固定入口、零业务 probe、服务商禁用状态读回、单主时钟交接及自然运行的独立验收按[外部日更时钟合同](../engineering/external-daily-clock.md)执行；代码配置不代表服务商任务已经启用或日更准时完成。换钟不改变本文件的 source policy、周期或预算。普通手动 `update` 保留原生运行创建当日本地 18:35 门槛；提前调用只生成 `not-due / DAILY_MARKET_BEFORE_CLOSE` 读取回执，不取日历、不取价格、不写入日更意图或消耗当日来源额度。当前日的合格价格还须满足原采集与读取时钟合同，不能让提前取得的正文在收盘后自动获得新资格。

每个业务日期至多建立一个正常日更周期，一次调用最多推进一个既存或新周期。日历与价格原预算仍为 **2 个实际交易日历请求 + 12 个 FTShare 单股整段请求**；新 v4 周期另有两所完整主表查询，各最多20页、每页500行及原4次尝试/180秒等待，最多40页/160次实际主表HTTP，不能把两次完整查询写成只有两次HTTP。两所 `trade_cal` 返回全部开闭市日，按实际完整日历决定最后已完成交易日，不凭星期推定开市。休市且没有新增交易日时只保留实际日历与原市场引用，不重抓主表或价格。新开市日使用最长119自然日整段窗口；是否足够5/20/60交易日比较由实际会话及价格点判定，119天不是保证，缺点继续显示partial。

### 延迟调度与显式补更

被选中的自动时钟（原生 `schedule` 或 `external-clock`）和显式手动 `recover-late` 从该次运行真实 `created_at` 之前最近的工作日 18:35 选择业务槽，创建时刻距该槽必须严格小于 24 小时。选择只决定业务日期、日历窗口和确定的 generation，来源授权与旧预算不变。例如原生创建于 `2026-10-19T17:07:33Z`，北京时间已到 10/20 01:07:33，仍选择 10/19 18:35 的槽。周五迟到周六且不足 24 小时可以选择周五；到周六 18:35、周日或周一 18:35 前已没有符合窗口的旧槽，返回 `not-due / DAILY_MARKET_NO_RECENT_SCHEDULE`，不取来源、不写新周期。

| 原生调用 | 日期准入与记录 |
|---|---|
| 手动 `status` | 只读原状态，无来源凭据；不选择新周期。 |
| 手动 `update` | 保留当日本地 18:35 门槛，不自动补上一日。 |
| 自动 `schedule` | primary=github-schedule 时准入，按原生创建时刻选择最近且不足 24 小时的已到期工作日槽。 |
| `external-clock` | primary=external-cron 时进入同一 update，采用同一个原生创建时钟和业务槽；event 仍为 workflow_dispatch，不单凭入口标识证明外部自然触发。 |
| `clock-probe` | 独立工程元数据检查；不选择或读取业务周期，不进入日更、发现或阅读消费者。 |
| 手动 `recover-late` | 显式补同一有限窗口内的最近槽；执行行为为 `update`，回执保留 `invocation=recover-late`。 |

`recover-late` 没有任意日期、旧 run、额外来源额度或强制跳过状态的输入。已有同一业务日完成记录时只读；旧周期未结束时先恢复原链，安全 pending 只继续剩余工作，失败、hold 和未知预留仍停止。该入口可补一个仍在最近槽窗口内的实际遗漏，不能选择更早历史日，也不能把后来的新周期或包含旧日的整段行情称作已恢复那个遗漏周期。

新周期的 `cycle.date` 与 `calendar_end` 固定为选中的业务日；接续旧周期时保留旧日期与窗口。实际 `created_at`、`updated_at`、来源 `acquired_at` 和正常读取时间各自照实记录。日历不能扩大到凌晨或盘中的当前自然日，否则其最后开市日可能尚未收盘。价格截止日继续由业务日内的两所真实日历决定；当前读取所需日期缺少日历或价格资格时，freshness 保留 `unknown`。补更成功不构成此前按自然日连续运行、准点更新或及时发现的证据。

日历只复用既有 Relay 元数据通路；价格只使用固定 FTShare v1 `stock-candlesticks`、`close`、`none/forward`。没有 v2 批量资格、供应商自动切换、盘中行情、隐式全市场回填、新的 Full / Brief 生成、主动通知、公开部署、第三方保管或旧 Kernel 停用。

## 继承、预留和停止

首次以配置内确切的六股终态 FTShare SourceRef 为起点，以后以前一成功日更引用为起点。建立新周期之前，可以沿原 records 中唯一、可完整恢复的 FTShare 手工扩面后继接续到实际当前引用。这里的扩面只允许在原市场分母内增加六股日更范围之外的新 symbol / adjust-kind owner；每条边必须保留 collection / adoption、价格起止日、日历窗口与确切引用、收盘资格，以及所有旧 owner 的请求、成功原件或失败状态。手工提前推进价格截止日、替换日历、改写旧 owner 请求，或将另一个 `daily-*` generation 当成手工扩面，都不能取得新周期来源资格。沿途任一 pending、failed、hold 或 in-flight 均阻断新来源，即使更后面的另一个新增 owner 已经成功。

原 `plan_ftshare_single_collection` / `run_collection` 继续持有价格计划、单股预留、原文、资格、累计旧额度及每个 generation 的唯一前驱。合格扩面后继的实际累计预留全部继承；本周期仍只请求配置六股的 `none/forward`，不因继承更多已有 owner 而增加十二次价格预算。周期使用业务日期确定 `daily-YYYYMMDD`，只有实际新增完成交易日时才生成该 FTShare 后继计划；换 run、重跑、换日期、选择 `recover-late` 或更新配置不能把耗尽请求再变成 pending。

新周期 r000 保存前，从实际原件再核当前后继是否仍是刚选定的 base，并把同一已核快照的 head 传给原 `VersionedStore.put_source` / backend CAS。无论新扩面到达 Store 取 head 之前还是原提交 CAS 之前，旧候选都停止，不留下过时意图。已保管的同一 submission 可以按确切原内容只读对账；即使调用者仍持旧 expected head，也不重新写入或消耗来源。独立读回历史控制链时，跨日的“前周期市场引用 → 本周期 base”只证明这两个已记录端点之间的确切 FTShare 扩面链；端点之后的新增或失败不反过来改写历史。当前来源准入另外核实际当前链，历史可恢复本身不授权越过新阻断。该适配使用原不可变 genesis 记录查找后继，没有新增手工同步的当前引用文件。

新日历的两次预留使用同一个 `VersionedStore.put_source` 写成小型 `SourceInput / SourceRef`。`daily-market-cycle-YYYYMMDD-rNNN` 是原 Store 内不可变的来源工作记录：先独立保管一次预留，再发一次 HTTP，再保管实际成功或失败。它不保存另一套价格，不是新的业务主库、后台任务队列或手工同步状态看板。真实日历查询自身仍通过原 `retain_market_query` 保管和独立恢复。

同日已经完成时再调用只读，来源请求为 0；期间手工扩面不会重定旧周期的 base、日期、市场引用或额度。未完成的安全 pending 周期只接续原引用和剩余额度；即使到了下一天，也先完成这一个旧周期，不另立日期绕过。每个尚未请求的日历和价格阶段之前再核原 FTShare 后继：自己的既存 `daily-YYYYMMDD` 只能由原执行器恢复剩余工作，外来后继使固定的旧周期停止，不能自动 rebase。日历由 pending 转 in-flight 的准入检查与预留写入绑定同一个已核快照；检查前到达的外来后继停止准入，检查后到达的写入由原 CAS 拒绝，均不发出该次日历 HTTP。已有来源响应的 complete / failed 落账继续保留真实效果，不套用新请求准入去丢弃已经取得的结果。以下情况停止新的来源请求并保留原问题：

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

自动 `schedule` 的代码 SHA 来自受信 `GITHUB_SHA`；原手动 `operation=update|status|recover-late|repair-calendar|reconcile-write` 继续要求确切 `code_sha`。两个恢复入口各自加载固定获审配置。宿主重新核原生仓库 ID / 可见性与部署授权、当前 main、自己的工作流与 run / attempt 1，以及该代码最新一次合格 main CI。不能伪装成旧手动核验宿主。业务回执分别记录真实 `invocation=schedule|external-clock|update|status|recover-late|repair-calendar|reconcile-write` 和供执行及消费者使用的 `operation=update|status`；下游核相同的原生运行、代码、事件及 invocation，不能把普通更新回执换标签当成补更。

只有 `external-clock` / `clock-probe` 接受固定 `code_sha=current-main`，并立即绑定该次原生 `GITHUB_SHA`；旧手动入口不接受这个标记。真实宿主明确加载固定 clock 配置，缺失或损坏即停止。被选中的自动生产在 visibility 阶段、注入来源密钥前先核当前 main 和 CI，实际宿主与消费者再次检查。`clock-probe` 只进入独立只读 job，输出 `probe.json` / `probe.md`，0 来源请求、0 records 读写、0 dispatch/通知；不得把它转入普通 status 后读取业务。配置、实际启用、自然来源和消费者采用仍按外部时钟合同分别验收。

周期准入固定使用原生运行创建时刻，不能因队列或同次安全 quota 等待跨过午夜、甚至下一次 18:35，就领取另一天的预算。进入宿主时，真实当前时间必须不早于该原生创建时刻，且相差严格小于 24 小时；这个队列年龄上限与原生创建时刻距业务槽的不足 24 小时上限分别成立。新建周期的实际创建时间不早于其原生准入时间；采集和正常读取仍使用当时真实时钟，不回填成槽的时刻。

日更与现役市场来源共同使用 `decision-os-relay-source` 串行组，`cancel-in-progress: false`。GitHub 这个组不是分布式原子锁或无限 FIFO 队列；原 CAS、唯一前驱、独立读回继续防止混写。每个有限单元使用同一原 API / writer / reader，沿用来源前 240 次准入、单元 512 次硬上限及跨单元共享的原生 quota 观察。每个单元的真实容量须预留可保管和读回空间，日更不扩大根、8192 版本、Git 下载或树响应边界。

### 同次运行内的原生额度等待

工作流沿用自身 `GITHUB_TOKEN`。GitHub 官方的普通每仓令牌额度为每小时 1000 REST 请求，GraphQL 单独每小时 1000 点；代码不假设工程凭据的较高余量也适用于工作流。两次日历与十二次价格的独立保管和读回会跨越一个普通小时窗口，因此在同一个原 run 内、下一个尚未预留的单元前保留 512 次原生额度。首次 FTShare 单元、后续单股单元和最终周期完成保存均使用这个边界。

只有主动检查发现余量不足、未出现 `throttled`、且最近实际 core 响应提供有效的未来一小时内 `reset` 时，才能等待该原生窗口。每次睡眠最多 60 秒；初始窗口之后最多 **5 次实际刷新等待**，累计等待不超过 **5 小时**，工作流作业上限 **330 分钟**。这是有限的同次运行等待，不重新进入 `run_daily`，不重建来源 client、不重新开始 receipt、不把单股槽位、预留或累计来源额度置零。等待期间持续更新安全回执，记录等待次数、累计时间和各原生资源的最近观察。

到达 reset 之后，沿用原 API、令牌和共享 `GitHubRateLimits`，实际读取仓库 private、当前 main、本次 run 和最新 main CI 四个原生响应。只有响应证明 core 的 reset 已前进，且所有已观察资源仍满足 512 预留，才开始下一个单元。GraphQL 余量不足时直接暂停：普通 GET 不能刷新 GraphQL，墙钟经过旧 reset 也不能补写额度。缺少、过期或未前进的 headers、超出等待边界，以及真实 403 / 429 / 权限或 CI 变化都保留暂停或失败；不把这些情况改成普通重试。刚好跨过 reset 而最后观察仍是旧窗口时允许保守暂停，不保证每次额度不足都自动恢复。

完成周期的独立保存随后仍执行正常市场读取、工作台读取和输出前原生检查。工程门禁以实际 native API wire 的 core / GraphQL 分开计数，核完整 2 + 12 原始请求和尾部消费者余量，不以新建 API 对象的局部计数代替令牌额度。新窗口等待不能重放来源 failed、source hold 或未知 in-flight；这些状态继续按原引用核对。

输出分为：

| 输出 | 含义 |
|---|---|
| `result.json` | 安全计数、周期状态、实际请求与失败、原生代码身份及 invocation、正常 reader 的 freshness / coverage；`admission` 保留准入模式、原生准入时刻、业务日、计划槽、延迟及适用上限，`store_quota_wait` 保留同次运行的原生等待和额度观察。没有研究执行或通知。 |
| `control.json` | 同产物内的确切 `cycle_reference`、当前实际 `market_reference`、周期 generation 和已保管日历/主表引用，供现役正常消费者使用。另保留 `frontier_reason_code`、首个 `frontier_blocking_reference` 和实际经过的 `frontier_successor_references`。 |
| `reading-room.html` | 原统一阅读室的离线页面，显示本次更新状态、自动发现并固定的已存 Brief、研究、待复核问题与实际市场覆盖。 |
| `reading-room.json` | 与 HTML 对应的只读模型及实际日更状态，保留原精确引用、内容截止与失败。 |
| `reading-summary.md` | 同模型派生的人用摘要，经发布前私有核验后写入本次 Actions Summary。 |

完成或失败之后均尝试原 `read_ftshare_collection_version`，采用最新真实尝试及其缺口，不安静换成较早的成功快照。工作台可以调用 `daily_market_reference(store, registry, policy(...))` 取得相同精确引用及日更状态，再进入自己的正常读取；此 resolver 不调用来源、不写 records。它沿唯一的真实结构读到当前 tip，并保留沿途首个用途或状态阻断。因此“新增 owner 失败 → 后续另一个 owner 成功”仍展示后续实际引用、前面的失败原件及首个阻断；后来的成功不会清除旧失败。结构分叉、坏引用或原件恢复失败则保留不可用诊断，不回退到一个较早的绿色引用。

当前阅读的 `market_reference` 可以晚于已完成周期内保存的 `market_reference`；`cycle_reference` 和周期 generation 仍标识原周期。旧周期完成与当前扩面失败分别显示，运行回执和正常阅读保留当前阻断 / partial，不把旧周期改写成失败，也不借旧周期的 complete 掩盖后续失败。正式报告、研究采用和人工决定仍按原版本独立进行。

`status` 作业只有 contents / actions 读取权限，没有来源 Secret。`update`、`recover-late` 与被选中的自动时钟（schedule/external-clock）沿用同一个更新作业，使用现有来源 Secret 和原 `records` 写入权限，Secret 不交给后续阅读步骤。产物发布前再核一次原生 private / main。读取和操作失败保留本次 failure / partial，不能用上一份成功报表代替。Actions 产物有到期日；持久真源仍是原 records。

正常阅读步骤运行 `python -m decision_kernel.interfaces.daily_reading_cli --out "$RUNNER_TEMP/dos2-daily-market"`，只取得 Store 读取凭据，复用原 `reading_commands.perform` 的单次权限/预算作用域。它核对同目录 `result.json` 的原生本次 run、代码与事件身份，通过 `daily_market_reference` 取得最新实际尝试，再使用 `brief={"select":"latest-saved"}` 从原发行目录发现并固定简报。读失败、坏控制链或旧内容继续显示原缺口，不借重新生成 HTML 声称今日已发行新 Brief。该步骤不取得新闻或行情，不写 records，也不自动更新研究判断。

人用入口为[本工作流页面](https://github.com/auguspp/Decision-OS-2/actions/workflows/daily-market.yml)最近一次运行的 Summary。完整页面与精确引用在该次私有产物中；没有另建网站、自动发布或第二个当前状态文件。工作流的 schedule 路由不依赖未保证存在的事件 repository.private 字段，所有来源与 Store 副作用前及输出释放前仍由原生 API 严格核私有仓库身份。

## 运行性能诊断

日更和相依 FT 运行的 `performance` 是同一次操作中的有界阶段诊断，随原运行回执保留。它使用标准库 monotonic 时钟观察现有调用边界，记录固定阶段标签、实际次数和有限时长样本；没有独立采集器、存储、网络探测或新的执行额度。历史 `result.json` 没有该字段仍按原合同读取。来源回执的完整摘要继续绑定新增诊断，产品页面的既有 `UPDATE_FIELDS` 不展开这些内部明细。

| 可观察边界 | 解释 |
|---|---|
| Relay / FT 客户端的实际传输、正文读取与解析 | 只计进入该边界的真实调用。HTTP状态、cache、原始字节和请求身份仍以各自原回执为准。 |
| 预留、原件与控制保存、CAS、正常独立读回 | 计时只观察原写入及读取过程；阶段返回或抛出不证明远端写入效果。未知写仍按原键对账。 |
| 原生准入、容量、快照获取与固定版本读取 | 只观察现有动作，共享原 API 实例及原512请求硬上限；不缓存live权限、main/records head或重新分配额度。 |
| 日历、完整主表单元、FT组、终态读取及实际等待 | 上层阶段包含其内部阶段，不能把嵌套时间相加当总耗时。跨进程以各自实际duration和原生job时钟关联。 |

诊断最多128KiB、512条有序阶段样本，每个固定阶段最多保留前32个可计时的完成样本。P50/P95按这组包含正常返回与抛出异常的样本计算nearest-rank，不能解释为整个运行的成功请求分位数。阶段总时长和计数都包含嵌套调用；有序日历/主表/FT单元另记其计数增量。`github_api_requests`沿原API owner的预算计数（包括超过硬上限而拒绝的调用），GET/POST另计真正获准进入dispatch的次数。Relay/FT分别记录原owner请求计数和进入opener的`wire_attempts`；进入传输边界不证明远端收到了请求、返回成功或确认写入。

计时缺失、无效或样本超限明确保留unknown / truncation；全部duration未知的阶段总时长为null，不补零。超过既有回执总字节上限时，诊断缩为显式省略标记；连标记也无法容纳时保持原回执，字段缺失表示未知。诊断内容不接受任意URL、路径、请求头、正文、供应商错误文本或凭据。诊断不进入SourceInput、query key、attempts、checkpoint、cycle、FT plan、journal或CAS意图；原严格schema与历史摘要保持。

source `result.json`在原来源入口结束时封存；Discovery和Reading分别在既有`discovery-result.json`与完整阅读模型中保留自己的诊断，不回写已被两者绑定摘要的source回执，也不增加第七个产物。Reading在构建模型后封存输出对象，JSON、HTML和summary消费同一模型。若附加诊断触发原8MiB HTML门，只回退原模型重渲染一次；原模型仍超限则按原门拒绝，诊断缺失仍表示unknown。渲染及最终原生资格检查不计入模型内duration，完整步骤耗时仍以原生job时钟为准。

两个旧计时字段继续采用原语义：Relay checkpoint 的 `requested_at` 在同步保存 in-flight reservation **之前**取得，至 `received_at` 的区间还包含保管、独立读回及请求前guard，不能解释为纯HTTP；FT原 `transport.elapsed_seconds` 包含客户端调用和capture转换，不能解释为纯socket时间。原Git提交时钟与页间差额可以定位未分解区间，不能替代各阶段实际计时。

终态 `daily_market_reference`、control与正常FT health读取在同一个现役 `market_read_scope` 中完成。这个无中间写入的只读组复用同一已验证snapshot及其有界immutable memo，减少重复clone和相同对象读取；原外层入口/退出live验证及最终guard继续执行。独立写后读回仍打开其自己的fresh scope，不能以本组旧快照确认新写入。

本能力由日更owner维护，与实际来源、保管和读取调用点一起退出；没有正常消费者后不保留一套空运行诊断框架。验证应覆盖计时边界、原失败和未知写停止、严格历史恢复、原预算、同快照出口失败及诊断容量。正式CI和合并仅证明工程合同；性能责任须在真实新业务日或自然窗口按同口径比较两所完整主表、全部页72小时、原FT12、Reading/Watch、失败恢复、新鲜度及总耗时。单次成功、合成计时或省去一次clone均不能代签持续生产性能。

## 暂停、回退与证明边界

可以在 GitHub 原生界面停用本工作流，或经普通 PR 将本专属配置 `enabled` 置为 false。暂停不删除历史、不清除失败、不取消已有在途操作；在途结果仍按原引用核对。恢复 enabled 只恢复原合同，不能解锁已失败周期。代码回退使用普通反向 PR；新增 records 原件与它们的现役只读支持继续保留，不强制回退业务分支。

工程测试包括同日零重放、真实日历休市、新日期继承累计额度、日历拒绝 / 历史冲突 / FTShare hold 跨日阻断、未知预留以及独立 Git 恢复。手工扩面集成回归复用原保管与正常 reader，核多个已存新增 owner 的继承、跨日确切桥接端点、后来的失败不破坏旧历史、实际最新尝试与首个阻断、r000 前竞争、外来后继在来源前停止、未知写后的原周期恢复，以及窗口 / 日历 / 旧 owner 变化拒绝。两股合成宿主的 2 + 4 请求只用于这些跨层语义；正式六股预算仍由完整 2 + 12 模型核验。

延迟回归另核原生午夜创建、显式补更与普通早盘更新的区别、周五至周六及严格 24 小时边界、周日和周一早盘无窗口、队列跨下一槽仍固定原日、真实创建与采集时钟、旧周期剩余预留及 hold、日历不包含当前未收盘日和下游 invocation 一致性。原生 Git/API 模型另核普通 1000 core / 1000 GraphQL 额度下的同 run 完整 2 + 12、独立新窗口响应、五次刷新上限、陈旧 headers 与拒绝停止、完成后的消费者余量。合成数据和合成时间测试只证明工程语义，不是已经等待未来五小时或已经连续更新多日的证据。首次真实运行、原生 schedule 启用、显式补更、每个后续自然交易日、人工工作台体验和长期持续性分别记录；一轮成功不能签未来 uptime 或全市场可用。

## 组件复用与退出

2026-10-19 核读 GitHub 官方 [schedule 事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) 和 [workflow concurrency](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency)。原生 schedule 只从默认分支触发，可能延迟或丢弃；因此本合同使用实际运行 / 日历 / 来源时钟，而不把 cron 字符串称作准点 SLA。选择已有 Actions 配置与 Python 标准库窄适配，不另装通用调度、监控或备份平台。

普通令牌的独立额度和真实响应等待依据同日核读的官方 [REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) 与 [GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)。继续复用已有 `GitHubRateLimits.observe` / `admits`；宿主只添加有界等待与原生复核，没有通用重试中间件、第二个令牌或手工额度表。

本责任维护 `application/daily_market.py`、`interfaces/daily_market_cli.py`、`interfaces/daily_reading_cli.py`、专属配置、工作流及日更/阅读入口测试。`ftshare_collection.resolve_ftshare_expansion_frontier` 的来源桥接合同由本市场接续能力维护；`VersionedStore.put_source(expected_head=...)` 是当前日更控制绑定已核快照的窄适配，继续使用原 writer 和 CAS。若日更写入退役且没有其他实际消费者，同一 PR 退出这项专属调用及无用入口。退役执行能力时同步退工作流与采集入口，保留尚有已存消费者依赖的日历控制读取、FTShare 原件及独立恢复支持；共享 Store、来源、CI 与正常阅读不因本模块退出而删除。
