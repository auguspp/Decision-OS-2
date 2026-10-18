# FTShare A 股日线接入

## 当前责任组

用户在 2026-10-18 的 #10 接管委托中明确要求将日线数据源切换为 FTShare，并已在同仓 Secret 设置 `FTSHARE_API_KEY`。这项委托修改日线来源选择；不会把既有 Relay 记录、失败、重试额度或复权因子改名成 FTShare，也不改变同仓 `main` / `records` 保管路线。

本责任组提供固定来源传输、严格日 K 资格检查和真实账户的有限验证宿主。`ftshare-check` **不写 records、不更换正常消费者的活动引用**；真实协议、容量、重复读取和价格对照通过后，生产切换沿 #10 接续。单次校验成功不代表持续可用率或完整市场覆盖。

## 可核对的上游合同

实现依据是 FTShare 官方 [FTShare-skill 固定版本](https://github.com/FTShare-Lab/FTShare-skill/tree/d2233464fc26b6d3c885f60543e316565361bd83) 中的 [stock-candlesticks-batch](https://github.com/FTShare-Lab/FTShare-skill/blob/d2233464fc26b6d3c885f60543e316565361bd83/ftshare-market-data/sub-skills/stock-candlesticks-batch/SKILL.md) 及其 handler；不能用旧 v1 或 MCP 包装器的限额代替当前 REST 合同。

- 固定 HTTPS origin 为 `https://market.ft.tech`，前缀 `/gateway`；凭据仅发送到该 origin 的已列举接口，不接受任意 URL、代理、跳转或调用方请求头。
- 日 K 使用 `GET /api/v2/market/data/stock-candlesticks/batch`；`symbols` 为重复 query 参数，`interval_unit=day`，`adjust_kind=none|forward`。
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

#10 仍需真实运行原始 artifact、跨源同日原价对照、全声明股票范围的缺失分母、严格 5 / 20 / 60 日窗口、独立 Store 读回和正常 CLI / room 消费。生产切换应使用独立 FTShare 身份，引用旧 Relay 集合为前驱，保留旧失败账，不复制一套 Store 或用有限样本签全市场完成。未来自然日的更新稳定性，只按实际观察积累。
