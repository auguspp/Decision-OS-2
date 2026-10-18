# FTShare A 股日线接入

## 当前责任组

用户在 2026-10-18 的 #10 接管委托中明确要求将日线数据源切换为 FTShare，并已在同仓 Secret 设置 `FTSHARE_API_KEY`。这项委托修改日线来源选择；不会把既有 Relay 记录、失败、重试额度或复权因子改名成 FTShare，也不改变同仓 `main` / `records` 保管路线。

本责任组提供固定来源传输、严格日 K 资格检查、有限账户验证，以及同一 Store 上的生产集合与正常读取接线。`ftshare-check` / `ftshare-check-continue` 不写 records；生产采集另由 `ftshare-sync` 执行。配置的 `qualification: null` 明确表示来源尚未获准激活：真实协议、容量、重复读取和价格对照通过后，才能在审阅过的配置中填写确切资格引用。单次校验成功不代表持续可用率或完整市场覆盖。

## 可核对的上游合同

实现依据是 FTShare 官方 [FTShare-skill 固定版本](https://github.com/FTShare-Lab/FTShare-skill/tree/d2233464fc26b6d3c885f60543e316565361bd83) 中的 [stock-candlesticks-batch](https://github.com/FTShare-Lab/FTShare-skill/blob/d2233464fc26b6d3c885f60543e316565361bd83/ftshare-market-data/sub-skills/stock-candlesticks-batch/SKILL.md) 及其 handler；不能用旧 v1 或 MCP 包装器的限额代替当前 REST 合同。

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

每次至多一个 HTTP 请求，没有隐含 retry；遇到错误停止后续来源请求，保留真实失败，native exit 2 不改写成成功。空响应不能满足本轮预定非空样本的验收。返回体受字节上限、JSON 重复键 / 非有限数以及凭据反射检查约束。错误仅保留稳定代码和白名单元数据，不打印正文、凭据或任意供应商错误文本。

原始 HTTP 正文逐个保留字节数和 SHA-256，另有请求、基准引用及计数回执；release 阶段在不含来源 Secret 的环境重新核验原生 private / main，再上传同一私有仓库的诊断 artifact。SourceInput 的原始正文与 normalized excerpt 明确分开，哈希只证明字节身份，不代替来源认证或保管授权。日配额 1000 仅是待核账户假设；没有上游计量证据时不得把 HTTP 次数等同计费单位或宣称每日余额。

## 收口所需后续证据

### 2026-10-18 首次实际运行

[运行 39141799035/1](https://github.com/auguspp/Decision-OS-2/actions/runs/39141799035) 使用 main `fd60f1f41e3d311132a1694704d12cda0d605899`，在第一个双股原价请求收到 HTTP 403 后停止：实际请求 1/6，已合格 candle checks 0/5，其余五项未请求。原 artifact `12141618961` 为 34,673 字节，SHA-256 为 `c51d2ea9ddc5167b9de0056716338252fcfd8bad1e9172121c425e5aee59a02f`；原生身份、ZIP 及 Source10 基准已独立复原。该运行不写 records，也没有可用于跨源、重复或历史覆盖验收的 FTShare 行情正文。

对照官方固定版本 handler，发现当前请求遗漏 `X-Client-Name: ft-claw`。补齐该头是已知的代码修正，403 的具体原因仍未确定。不能把这次拒绝直接解释为无效密钥，也不能声称修正后必定可用。

`ftshare-check-continue` 是同一 study 的一次有限接续，不是新的六次预算。prepare 必须验证原失败 run 和原始 artifact，核实际已用 1 次，并验证唯一 continuation、当前 main CI 与 private。接续最多五次：双股 raw、同请求重复、100 股单日、50 股整段 raw、同 50 股整段 forward；可选因子查询取消。总计最多 `1 + 5 = 6` 次。任一失败即停止余项；原失败继续保留，不能 rerun 或另开 continuation 重置额度。只有五项 candle 实际合格才可能形成日线来源资格，因子语义仍未验证。

### 补齐请求头后的真实结果

[唯一 continuation 39145109553/1](https://github.com/auguspp/Decision-OS-2/actions/runs/39145109553) 使用 main `64b4d5f6bfe0f9373a8e7f33f590979178de22e3`，在 2026-10-18 16:28 UTC 首个双股原价请求仍收到 HTTP 403，耗时 1.014542 秒。本次实际 1 次、两轮累计 **2/6**，合格 candle checks **0/5**；后四项未请求，可选因子继续取消。原 artifact `12143465795` 为 69,318 字节，SHA-256 `92bd239a077025ef24633b9050d8b859cc9233bfb650379a2be4c59eb85cd725`，原生身份与原 ZIP 已独立核验。

结果没有行情正文、Retry-After 或可核额度余额，不能形成价格一致性、历史缺失、实际批次容量或可用率结论。该运行不写 records，前驱仍为 `3fcaa33e745afa0084ff20136538a06ce8c38877`。补齐客户端标识没有消除拒绝；HTTP 403 的具体账户权限、密钥配置或访问策略原因仍未知，不能将 transport 的 `auth-or-entitlement` 分类当作上游诊断。

因此生产配置保持 `qualification: null`。这次 one-shot 宿主已终止，不能用剩余四次数字重新派发它。继续真实验证需要先取得 FTShare 侧访问配置已修正的具体证据，再在 #10 内保留原累计记录、界定必要检查和有限续接；本次不增加来源调用，也不执行依赖 FTShare 获准的日历扩展。

#10 仍需真实运行原始 artifact、跨源同日原价对照、全声明股票范围的缺失分母、严格 5 / 20 / 60 日窗口、独立 Store 读回和正常 CLI / room 消费。生产切换应使用独立 FTShare 身份，引用旧 Relay 集合为前驱，保留旧失败账，不复制一套 Store 或用有限样本签全市场完成。未来自然日的更新稳定性，只按实际观察积累。

## 同一 Store 的生产接线

可信配置为 [config/ftshare-daily.json](../../config/ftshare-daily.json)，固定集合 `ftshare-cn-eod` / 世代 `g20261016`，引用上述 Source10 的完整原集合。当前有界价格区间为 2026-07-01 至 2026-10-16；它不自动延长日期，也不新增定时任务。

| 手动 operation | 输入与来源访问 | 结果 |
|---|---|---|
| `ftshare-status`（默认） | 固定集合与世代；来源 HTTP 为 0 | 从既存 records 恢复最新 FTShare 集合、预留次数、缺口与暂停原因 |
| `ftshare-sync` | 固定集合与世代，`work_units` 为 1–32；每单元最多一个 FTShare HTTP | 先保管预留，再取一次原始正文并保管结果；未完成槽保持真实状态 |
| `ftshare-calendar` | 固定 study `ftshare-calendar-continuity-20261018`、`work_units=1` | 仅两次 Relay `trade_cal` 元数据请求，独立保管 SSE / SZSE 截至 10/18 的日历；不取 Relay 日线价格 |

上述入口仍在 `ashare-data.yml` 的原有来源互斥组内。写入只允许原生 private 的同仓、当前 main、合格 main CI、attempt 1 和既有明确私有保管授权；公开权利输入为 false。`ftshare-status` 需要 contents / actions 只读权限。FTShare Secret 仅进入实际 FTShare capture / sync 步骤；日历只使用既有 Relay Secret，发布产物前另一步重新核验 private 与 main。

典型 selection 为 `{"collection_id":"ftshare-cn-eod","generation":"g20261016"}`。首次 sync 可额外携带日历运行保管的 `calendar_references: {"SSE": SourceRef, "SZSE": SourceRef}`，必须是本仓独立可恢复的完整引用，覆盖原区间且不能改变既有历史开闭市记录。后续运行省略时继承已保管日历，显式提供时必须与原计划相同。`ftshare-status` 不接受日历覆盖输入。

### 来源资格与未激活状态

`qualification` 只能为空或含 `run_id`、`artifact_id`、`sha256`、`code_sha` 四个字段。填写前核对成功 continuation 的原生身份、原始 ZIP、实际已耗预算、重复响应、Decimal 同日原价比较、样本缺失和真实响应容量；其中 code SHA 是该次资格运行的代码，不能用后来的生产 main SHA 冒充。五项 candle 通过仅代表这五项结构、非空样本和重复检查，历史个别空证券仍须在独立分析中披露。

为空时 sync 在创建 Store 或 FTShare 客户端之前返回 `FTSHARE_RUNTIME_QUALIFICATION_PENDING`，不预留、写 records 或请求行情。status 可以继续只读恢复已有集合；尚无集合时返回 `not-retained` / `FTSHARE_COLLECTION_NOT_RETAINED`、来源请求 0、退出码 2。这表示尚未保管新源数据。

当前有限世代的 sync 仍依赖原 continuation artifact 未过期；工作流保留期为 30 天。过期会阻止后续来源访问，只读恢复不依赖该临时 artifact。长期资格证明保管与跨世代自动更新尚未交付，不能将这个有界入口解释为持续更新服务。

### 范围、保管与不可重置的账

范围继承原生主表的 5,226 项声明；5,224 项可请求，2 项主表隔离继续留在完整缺失分母。默认每批 50 股，每批分别一次 `none` 与一次整段 `forward`，对应 210 个价格槽。不得以删除隔离项、覆盖旧来源身份或拼接不同 forward 请求来增加覆盖。

原 Source10 的 220 槽、原始终态、维修 grant 与预算随明确前驱保留。FTShare 使用独立 SourceInput 和集合身份，在相同 repository / records / store_id / anchor 上保管，不建立第二套数据仓。每槽先保管 `inflight` 与一次不可退回的预留；再执行至多一次 FTShare 请求。成功正文按原始字节独立读回，随后才保管完成根；错误保留 failed 及稳定原因。未知写按原键对账，未核清的 inflight 禁止重发。

鉴权失败 401/403、429、明确剩余额度 0、不可解释或超出当前有界等待的 Retry-After，会写入持久 `source_hold` 并停止下一次来源请求。有效正文与“此后暂停”可以同时保留。短等待在结算前执行；中断不会重置已预留次数。新 run、换输出目录或重复读取不能清除 hold、重开失败槽或领取新额度；当前宿主没有自动解除 hold 的接口。

每次来源准入继续核原生 GitHub 配额和 Store 容量；不等待额度重置刷完任务。单响应 2 MiB、集合正文 512 KiB 及既有 native Git 读取容量上限继续生效，真实价格正文和完整消费者模型仍按实际测量验收。合成测试的体积或行数只证明相应边界行为。

已用 Source10 的原 bundle 独立恢复真实范围，执行无网络、无 Store 写入的初始计划预检：计划 304,532 字节、初始状态 323,067 字节、根正文 323,839 / 524,288 字节、编码 SourceInput 375,077 / 2,097,152 字节，均通过现有结构与容量门槛。该预检确认 210 槽的初始包装可保管；最终完成根、真实 FTShare 正文、完整生产 bundle 和阅读室体积仍未获实际数据验证。

### 正常消费者与维护义务

正常 `open_market`、CLI 和阅读室识别 FTShare 集合引用，独立恢复主表、日历及保管的 none / forward 正文，计算原有 5 / 20 / 60 日严格逐日窗口与独立端点结果。分页和实体筛选保留完整范围哈希、缺失分母和隔离项。缺失 counterpart、坏行、交易日缺口及 source_hold 均可见；forward 缺失不回填 raw。来源请求数必须为 0，展示数据来自 Store 的确切引用。

日历覆盖至少包含读取时已完成的上海自然日，才可判定 freshness；陈旧交易日只从已保管的开市日计算，不猜测周末休市。FTShare 原始响应保留与旧 Relay 主表 / 日历摘录的证据等级分别说明，整体模型不能声称每个依赖都保有完整原始 HTTP。

固定 check、continuation 和 calendar 的过渡责任完成后，按实际依赖同步退出专属 workflow 选项、CLI、配置和测试。仍服务正常集合、严格窗口、读取与历史 exact-reference 恢复的共享能力继续保留；不会仅因 #10 工程合并而自动退役或签数据验收。
