# FTShare / Tushare Relay 有界双源验收

本责任由 #128 / PR #130 持有；复用旧 Kernel 技术与原始来源，不恢复旧业务兼容或启用全局主备切换。市场原采集、四个失败 checkpoint、预算、records 和 #10 owner 均不改变。

## 复用结论

旧 Kernel [#501](https://github.com/auguspp/decision-kernel/pull/501) 的 CNEquity 0.11.0 实际测试证明本地 Parquet/PIT/修订和严格复权能力，不证明第二独立上游。本次不建第二个 lake。旧 [#725](https://github.com/auguspp/decision-kernel/pull/725) 的 FTShare/HiThink 全页价格对照、latest 缺失与公司行动403保持原解释；不偷换为 FTShare/Relay。旧 [#795](https://github.com/auguspp/decision-kernel/pull/795) 的56请求历史原件可用于零来源请求重算；旧 [#364](https://github.com/auguspp/decision-kernel/issues/364) 的 TDX/eltdx 已验对象是概念板块，不是全部个股。

Reuse Decision: REUSE + THIN_ADAPTER。复用 observations.market_daily 日期/Decimal，新增纯比较器、本地CLI和专属反例；不复制SDK、客户端、调度、存储或原失败恢复程序。按真实用途检查：close-only 日分区可比较 close，无须编造 OHLCV。

## 离线工具合同

调用者先独立认证两份合法保管的原件，随后才投影为 provider/source_ref/acquired_at/session/rows。每端最多32行沪深证券；同日、CNY、NONE 原价，小数字符串保持精确。基础行是 symbol/trade_date/close/currency/adjustment/trade_status。完整提供 open/high/low/volume/volume_unit 时才检查和比较 OHLCV；残缺扩展不退化为合法基础行。成交量仅对已证明的 Relay LOTS_100_SHARES / FTShare SHARES 做十进制指数平移，不受调用方精度与traps影响。

source_ref 接受原 repository/commit/path/sha256 Git locator；也接受在其上明确添加 kind=github-actions-artifact、run_id、attempt、artifact_id、artifact_sha256 的原生归档 locator。后一类 commit 是 producer code commit，path 是 ZIP 内成员，不冒充Git内已存原件。**本函数只核locator格式，不查询GitHub、不认证归档字节或许可**；调用者的受信原件核验仍是前置。输入文件有128KiB有限读取、JSON重复键/非有限值与符号链接拒绝；输入错误仅输出固定拒绝码，不泄露异常、路径或供应商文本。

    python -m decision_kernel.interfaces.secondary_market_comparison_cli --relay relay.json --ftshare ftshare.json --session 2026-09-30

默认 purpose=trading-rows 只比较两侧都明确声明TRADING的值。显式 --purpose quoted-values 仅回答“已保存同日原价数值是否相等”，允许保留UNKNOWN交易状态；它不是放宽交易、停牌、连续性或复权资格。后者适用于本次历史档案。所有输出均声明 trading_eligibility_granted=false、ftshare_backup_admission=NOT_ESTABLISHED、common_upstream_independence=NOT_ESTABLISHED、market_canonical_changed=false。无可比对返回NO_COMPARABLE_PAIRS/exit2；DIAGNOSTIC_ONLY/exit0也不是备源准入。输出只含代码、字段名和计数，不含源价、源体或原件路径。

## 2026-10-18 实际历史双源核验

前序“尚未找到配对原件”由本次实际下载与核验纠正，不用后验结果改写过去失败。两个原生运行及其仓库/producer SHA/attempt=1已读；均为原已完成success的历史任务，没有重新派发：

| 来源 | 原生run / artifact | 原ZIP bytes / SHA256 |
|---|---|---|
| FTShare | [37720503804](https://github.com/auguspp/decision-kernel/actions/runs/37720503804) / 11525786543 | 974365 / `70247b75aca780faa4d5d4ca9d2f9564568c5376302e9e7cf3dfa35ebf10f59e` |
| Relay | [37125677265](https://github.com/auguspp/decision-kernel/actions/runs/37125677265) / 11275545350 | 407921 / `c6ba9fd435f9da4ebeaa862694479b3e5eb25c27cabf9d2c8d3bbb006801b2e3` |

FTShare producer `f732e0ef6f1c63c30ad05ff3f1c27663a649ef12`，Relay producer `ceee7e4ec53b474ad0d4f0d8dc76781f1cee3818`。下载ZIP大小/原生digest、CRC、无重复/越界/符号链接成员已核；FTShare全部56原响应及14CSV、Relay全部9原响应的内部bytes/SHA256逐项相等，原receipt工作流身份与native对应。JSON用拒绝重复键和Decimal直接解析，不经binary float。FTShare原plan与各请求一致，adjust_kind=none、interval_unit=day、上海时间毫秒边界、证券与日期唯一、正价与OHLC边界均核；Relay成功daily原fields、查询日/行日、证券唯一与正close均核。未运行旧producer代码或宣称已重验其所有日频/复权结论。

固定采用FTShare原研究全部14个证券，不按是否匹配选择样本。用其2026年原响应与Relay原四个daily日期相交，交易状态均保持UNKNOWN，以quoted-values比较：

| 原市场日 | 应核证券 | 有双方原报价 | 相等 | 不同 | Relay缺项 |
|---|---:|---:|---:|---:|---:|
| 2026-07-07 | 14 | 12 | 12 | 0 | 2 |
| 2026-09-01 | 14 | 14 | 14 | 0 | 0 |
| 2026-09-22 | 14 | 14 | 14 | 0 | 0 |
| 2026-09-30 | 14 | 14 | 14 | 0 | 0 |
| 合计 | 56证券×日期 | 54 | 54 | 0 | 2 |

这次**真实历史close比较成立**，不是只有合成测试。Relay原包只取close，因此没有双源OHLCV、成交量或因子一致性结论。2个缺项没有填充，没有从分母删除，也不猜停牌/新股原因。FTShare于10月8日取得，Relay于10月3日取得；不同回溯版本，不是历史当时可得/PIT证据，也不是10月16日新鲜行情或长期可用率。

### 复算步骤

沿表中两个native artifact ID下载原ZIP并核外层metadata/digest；按各receipt核全部原raw文件和身份。取FTShare receipt.coverage中全部14个symbol，遍历requests中period=2026的raw_file，以ts_millis按UTC+8转市场日；取Relay calls中api=daily的四个原response_file，以其params.trade_date对齐。Decimal读取原close，缺键保持缺项。双方构造close-only投影，附原receipt.json成员SHA及明确artifact locator，原finished_at分别作为取得时钟，trade_status=UNKNOWN。按四个日期分别调用上面的CLI并显式 --purpose quoted-values；检查每次fields_compared只包含close、总分母56/配对54/缺项2，以及所有不授权字段。原ZIP失效或digest不符时停止，不能找另一个“相近”包重命名为本结果。

## 决策与剩余边界

结论是：**FTShare值得作为原价交叉核验候选，但本次不启用自动备用采集或替换Relay。** 其独立底层上游、现役凭据/额度、新交易日连续性、上市/停牌、公司行动及复权仍分别未验。主源超时或GitHub配额不足不可通过重置原terminal解决；本组对#10无写入。

本次只有历史原件读回和比较，source HTTP=0、records写入=0；原始供应商数据保留于原私有仓，main仅记录原创工程说明与汇总。原Actions归档到期仍是保管限制：FTShare预计2026-11-07、Relay预计2026-11-02到期；不把下载/本地复算称为永久保管。后继真正采集须沿#12/#10的原owner、同语义资格与实际授权执行；保持原单标准市场入口。退役本比较能力时同步删除比较器、CLI及两组专属测试，保留本次事实及来源历史。
