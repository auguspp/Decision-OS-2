# Relay 功能登记与其他模块使用入口

**服务快照取得：2026-10-16 06:51:29（新加坡时间），即2026-10-15T22:51:29.274280Z。**
本页分开保留服务声明、原手册与OS2实际接入/取数证据。快照不是永久实时健康保证。

## 真实目录与原始证据

[机器登记](relay-registry.json)现为v2，绑定本次原始HTTP正文及运行回执。
[原生运行38819858325](https://github.com/auguspp/Decision-OS-2/actions/runs/38819858325)
首次HTTP请求成功，HTTP200，业务请求0、重试0、等待0秒。
响应顶层`count=298`与298个唯一`interfaces[].name`一致：**259项enabled=true，39项false**。
这里包括客户组合接口，不表示259个普通数据接口均已接入OS2。

原始正文134194字节，SHA256 `f541ddd2efbe50fe0c77a47e49d00d6c8e6c949d24a31feb20c59520fd6111c1`。
[原始JSON正文](snapshots/relay-capabilities-38819858325.raw.json)及
[原运行receipt](snapshots/relay-capabilities-38819858325.receipt.json)保存于Git。
原始JSON逐字节保存，Git中可直接查看；必须与以上摘要一致，不改写供应商字段。
原artifact12012602500，14918字节，SHA256 `167ae1ba48340faa8a5b1da514d3c4edf9e260a030dc209c095bd1f96eb510da`。

上次run38762503155的两次连接失败和第三次429保留在JSON的`previous_catalogue_evidence`；
成功并不改写此前失败，也不能证明是本次重试代码修复了供应商：本次首次请求就成功。
Key一直沿用已配置的`TUSHARE_PROXY_API_KEY`，没有重新索取、换Key/IP/服务地址。

## 与手册的差异

[原手册v0.5.61](vendor/tushare-relay-v0.5.61.md)仍原样保留，44358字节，
SHA256 `402bbfdfa77e6035b5cd41bc9a5d66b6ed975db2f2a25748ab37b797c12c827f`。
手册有271个唯一名称，258标记可用、13未启用；导语257/标题298的旧差异不修改。
实时快照包含全部271旧名称，另有27个新名称；**news_cctv手册标记可用，快照为未启用**。
27个新名称中hk_quote、us_quote标记启用，其余25项未启用；它们的description均为空，
保留机器category但暂放功能待核，不根据名字宣称具体数据字段或可用产品。

新增名称：
`a_share_level2`、`analyst_detail`、`analyst_history`、`analyst_rank`、`block_moneyflow`、`cjzc`、`fx_quote`、`gdelt_industry_daily_timeline`、`hk_depth`、`hk_quote`、`metal_daily`、`metal_depth`、`metal_mins`、`metal_monthly`、`metal_obasic`、`metal_quote`、`metal_weekly`、`minute_latest_batch`、`news_economic_baidu`、`news_report_time_baidu`、`news_trade_notify_dividend_baidu`、`news_trade_notify_suspend_baidu`、`opt_mins_batch`、`stock_notice_report`、`stock_research_report_em`、`stock_zh_a_disclosure_report_cninfo`、`us_quote`。

### 原13项用途待核的澄清

以下用途为本次**供应商description原文**，不是自行猜测，也没有现场执行这些接口。

| 接口 | 服务说明 | OS2建议分组 |
|---|---|---|
| `bc_bestotcqt` | 柜台流通式债券最优报价: 柜台流通式债券最优报价 | fixed-income |
| `bc_otcqt` | 柜台流通式债券报价: 柜台流通式债券报价 | fixed-income |
| `factor_list` | 因子列表: 提取Tushare自主研发生产的因子列表，包括因子名、因子分类和计算逻辑等信息 | market-observations |
| `factor_value` | 因子数据: 获取Tushare因子库里各因子的数值，目前是基于daily的时间序列数值。 | market-observations |
| `gz_index` | 广州民间借贷利率: 广州民间借贷利率 | macro-rates |
| `mkt_idx_bmk` | 基金业绩基准: 获取官方发布的ETF业绩比较基准列表信息，分为一类库、二类库 | funds-etfs |
| `npr` | 国家政策库: 获取国家行政机关公开披露的各类法规、条例政策、批复、通知等文本数据。 | content-events |
| `st` | ST风险警示板股票: ST风险警示板股票列表 | security-reference |
| `stk_high_shock` | 个股严重异常波动: 根据证券交易所交易规则的有关规定，交易所每日发布股票交易严重异常波动情况 | market-observations |
| `stk_shock` | 个股异常波动: 根据证券交易所交易规则的有关规定，交易所每日发布股票交易异常波动情况 | market-observations |
| `the_member` | （空，继续待核；不更名为ths_member） | needs-review |
| `wz_index` | 温州民间借贷利率: 温州民间借贷利率，即温州指数 | macro-rates |
| `yc_cb` | 国债收益率曲线: 获取中债收益率曲线，目前可获取中债国债收益率曲线即期和到期收益率曲线数据 | fixed-income |

## 298项的功能地图

14组继续沿既有分工；12项根据服务说明调整分组，新27项归待核，分组是OS2工程建议。
表内数量覆盖全部298项（含未启用），不是上线能力数。机器category另在service_metadata保留。

| 功能组 | 数量 | 建议消费者 | 主要限制 |
|---|---:|---|---|
| 财务报表、指标、预告与审计 `financial` | 18 | #12/#7 | 结构化财务、报告期/修订与主营构成；不能替代附注/PDF。 |
| 新闻、公告、互动与研报 `content-events` | 14 | #12/#7/#13 | 事件线索、研报及预测资料；观点和目录不是公司事实或已读原件。 |
| 资金流、股东、席位与资本行为 `participants-capital` | 33 | #12/#10/#7 | 公开参与者/资金资料；不能从席位标签推断真实持仓或因果。 |
| 指数、行业、概念与成员 `indices-sectors` | 44 | #10/#11/#7 | 分类、成分、权重和板块观察；成员日期与幸存偏差需另核。 |
| 基金与ETF `funds-etfs` | 22 | #10/#12/#7 | 证券列表、行情、份额、净值和基金资料；各接口频率另核。 |
| 期货、期权与贵金属 `derivatives-commodities` | 23 | #11/#10/#7 | 衍生品/交易所商品数据候选，不等于现货规格价格或公司利润暴露。 |
| 债券、可转债与回购市场 `fixed-income` | 15 | #10/#7 | 债券证券及行情、发行、赎回等资料候选，具体字段以接口为准。 |
| 宏观、利率与经济日历 `macro-rates` | 21 | #10/#12/#7 | 宏观背景、利率和事件日历；修订、发布时间和单位需独立核验。 |
| 港美股与外汇 `international-fx` | 22 | #10/#7 | 海外证券/价格和外汇；交易日、币种、时区、复权不能混用。 |
| 分钟、逐笔与实时行情 `intraday-realtime` | 10 | #10 | 时点报价与分钟序列；不是日终发现、历史覆盖或授权的保证。 |
| 证券基础与交易日历 `security-reference` | 14 | #10/#12 | 证券身份、状态、公司资料、日历等基础引用；并非投资判断。 |
| A股行情、交易统计与指标 `market-observations` | 30 | #10/#7 | 日/周/月行情、指标及交易状态的候选来源；指标口径需另核。 |
| 客户组合管理（另行授权） `portfolio-private` | 4 | 暂不指定 | p_list/p_get含客户私有内容，p_save/p_delete具有写删副作用；此次全部未调用。 |
| 仅登记名称，功能待核 `needs-review` | 28 | 暂不指定 | 新27项加the_member，description为空；不得自动开放或执行。 |

## 其他模块怎样查

安装OS2后指定同一仓库目录；查询无需Key、没有网络请求。原`--api/--group/--module`接口保留。

```bash
python -m decision_kernel.interfaces.relay_registry_cli --registry docs/sources/relay-registry.json --summary
python -m decision_kernel.interfaces.relay_registry_cli --registry docs/sources/relay-registry.json --enabled
python -m decision_kernel.interfaces.relay_registry_cli --registry docs/sources/relay-registry.json --module '#10' --enabled
python -m decision_kernel.interfaces.relay_registry_cli --registry docs/sources/relay-registry.json --api income
```

Python使用原RelayRegistry，无第二套HTTP或来源平台：

```python
from pathlib import Path
from decision_kernel.application.relay_registry import RelayRegistry
registry = RelayRegistry(Path('docs/sources/relay-registry.json'))
item = registry.lookup('income')
# live_enabled是此处snapshot时间点的服务声明，不是实时请求或永久可用性。
assert item['live_enabled'] is True
check = registry.check_parameters('income', {'ts_code': '600519.SH', 'period': '20251231'})
assert check['gateway_requirements_satisfied'] and not check['dispatch_performed']
```

返回手册原行/行号（新27项则为null）、服务原始16字段、字段缺失/null/值、原始快照时间与摘要、
分组和消费者、共用客户端接纳范围、旧有限业务实测。v1历史登记仍可只读，live字段保持未知。
`required`表示全部必填；`required_any`表示至少满足其中一组，每组的全部字段都必须提供。
空列表不等于缺失字段；缺失/null不自行推导。检查只核网关声明的必要条件，不验证证券、日期、
上游完整参数/字段或返回数据质量；通过不授予权限。`probe_example`只保留，不执行。
客户端并未因为登记扩大白名单：已接入仍是8财务加disclosure_date；其余API需要各模块适配。
`p_list/p_get`属于私有组合内容、`p_save/p_delete`属于写删，参数检查拒绝当作普通GET准入。

## 未启用项与维护边界

本次快照39项未启用：
`a_share_level2`、`analyst_detail`、`analyst_history`、`analyst_rank`、`block_moneyflow`、`cjzc`、`concept_board`、`dc_index_prev`、`fund_announcement_report_em`、`fut_level2`、`fx_quote`、`gdelt_industry_daily_timeline`、`hk_depth`、`index_min`、`metal_daily`、`metal_depth`、`metal_mins`、`metal_monthly`、`metal_obasic`、`metal_quote`、`metal_weekly`、`minute_latest_batch`、`news_cctv`、`news_economic_baidu`、`news_report_time_baidu`、`news_trade_notify_dividend_baidu`、`news_trade_notify_suspend_baidu`、`opt_mins_batch`、`realtime_tick`、`rt_fut_level2`、`rt_fut_min_daily`、`rt_fut_min_health`、`rt_fut_ticks`、`rt_hk_k`、`rt_tick`、`stock_notice_report`、`stock_research_report_em`、`stock_zh_a_disclosure_report_cninfo`、`us_depth`。

上述状态仅来自本次机器响应；原8财务的32查询现场结果仍为19非空、6合法空、7失败，
沿已有业务证据保留，不用enabled替它们改绿。标准市场数据仍走现有Relay，内容资料仍由#12取得、
#6保管、#7研究、#13展示；元数据不替代正文/附注、数据覆盖或公开再分发许可。

共享登记归#12维护，其他模块引用本入口，不复制注册库。变更通过独立PR更新快照/回执/摘要，
保留历史失败与Git旧版本；原供应商手册不修改。下载的注册文件可自洽并不构成服务器密码学认证，
信任来源仍为已审Git提交及原生运行回执。装包外读取须一并提供JSON、相对路径下手册和快照文件。
退役消费者时同步退出CLI/专属测试，原证据保留；本次没有调用新增业务接口、组合或写入records。
