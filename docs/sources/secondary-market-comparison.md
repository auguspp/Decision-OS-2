# FTShare / Tushare Relay 有界双源验收（#128）

## 旧 Kernel 已交事实

- [旧 #501](https://github.com/auguspp/decision-kernel/pull/501) 已使用实际 cnequity 0.11.0 运行本地 Parquet/PIT/修订与严格复权测试；CNEquity 是读取/存储实现，不是第二个独立数据来源，OS2 本轮不建新 lake。
- [旧 #725](https://github.com/auguspp/decision-kernel/pull/725) 实际 FTShare 全页 5904 唯一证券，原价与旧 HiThink 在 5560/5560 个可比 close 上相等，但全页缺 latest，原公司行动有 403/route_not_allowed，单日时钟不构成可靠日频资格。这是 **FTShare/HiThink** 证据，不能冒充本次 FTShare/Relay 资格。
- [旧 #795](https://github.com/auguspp/decision-kernel/pull/795#issuecomment-6051834248) 真实执行56次有限历史 K 线请求并离线恢复14份价格 CSV；原价采集可用，但复权/PIT/日常连续未证明；不能重放已消费的试点。
- [旧 #364](https://github.com/auguspp/decision-kernel/issues/364) TDX/eltdx 真实完成269个概念板块及其 K 线核验；不等于全市场个股行情合格源。
- [OS2 #10 终态](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6243033585) 两市 current-L 身份 5226 已保管；2026-10-15/16 daily 和 adj_factor 四原查询已耗尽各4次 timeout，不能重置或换客户端偷抓相同失败页。末次 ASHARE_REMOTE_RATE_LIMIT 是 GitHub API 额度安全暂停，并非供应商 HTTP 429。

## 第一组实际能力

本批复用现有 observations.market_daily 精确日期/Decimal 校验，只新增纯函数 compare_retained_raw_market 与显式本地 CLI。对两个**已由原 owner 各自验证且合法保管**的数据投影，比较最多32只沪深股票同一交易日的原 OHLC 与可核的成交量；不发网络/供应商请求，不写 records，不改 Market 原源、失败检查点、Brief、Research、自动调度或任何投资行为。

使用：

    python -m decision_kernel.interfaces.secondary_market_comparison_cli --relay path/to/relay.json --ftshare path/to/ftshare.json --session 2026-09-30

每端对象必须包含 provider/source_ref/acquired_at/session/rows；locator 的 repository/commit/path/sha256 **只检查格式，不做 GitHub 权限/原件认证**。来源版本和取得时间必须由调用者沿现役 SourceRef、原数据/返回证明先核，单凭这些字段不能自签 source_authentication。每行基础字段为 symbol/trade_date/close/currency/adjustment/trade_status；已证明存在完整 OHLCV 时可**整组**提供 open/high/low/volume/volume_unit，不能提供残缺 OHLCV 然后假装 close-only。因为现有 OS2 日分区正常只要求 close，基础诊断只比较相同市场日的原收盘价，不虚构其余字段；只有两侧均完整提供时才比较 OHLC/成交量，并逐项报告 fields_compared。只能同日、SH/SZ、CNY、NONE 原价、Decimal 字符串；扩展读数还要有严格 OHLC 和合法成交量。Relay 的来源单位 LOTS_100_SHARES 和 FTShare 的 SHARES 必须先由其原合同证明，比较器只在明确声明后归一成交量。交易状态 UNKNOWN / SUSPENDED 仅保留身份，不通过价格比较。未上市、过期、缺因子、公司行动证据不能通过填1或推断恢复。

比较结果仅给出证券和不同 OHLC 字段名、覆盖数/差异数，无源价格/原Body/凭据/SourceRef 文本。无可比配对 CLI 以退出码2返回 NO_COMPARABLE_PAIRS，成功仍明确 common_upstream_independence=NOT_ESTABLISHED、ftshare_backup_admission=NOT_ESTABLISHED、market_canonical_changed=false。两家值相等不能证明真正上游独立、PIT 或合法备用准入。

## 下一真实双源来源资格

现有旧 FTShare/HiThink 真实对照与 OS2 无完整 daily 的 Relay 主表 **无法组成同日双源原价真实配对**；本批只有纯函数合成反例，不宣称实际双源比价已经完成。下一步须由 #10 当前owner 协同固定最多32个具名沪深证券、交易日、状态与来源读取；在两端具合法原件/受信元数据/权限、独立读回后才进行现实对比。FTShare 的最新交易日时钟、上市/停牌、企业行为/复权权限另验，不从旧 5904 个报价自动晋级。新来源调用需要明确额度/许可/现役runner 的源密钥归属并留下原生run和受限原件证据；无凭据不换主机或身份绕过。

当前阶段判定是：已有可运行的**离线比较工具**，**真实 FTShare/Relay 同日对照=尚未验收**，**FTShare 正式备用准入=尚未成立**。#10 保持独立采集主施工，本组不得重启其4条 terminal 查询。维护人 #12 来源资格；不再需要此源时连同本地CLI、纯函数、专属测试和本文一同退出，保留历史数据引用及失败记录。
