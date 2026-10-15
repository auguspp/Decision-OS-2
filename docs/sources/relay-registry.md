# Relay 功能登记与其他模块使用入口

**登记日期：2026-10-15。这是手册全量功能索引，不是已取得的实时服务器能力清单。**

[机器分类登记](relay-registry.json)列出全部具名API、分类和证据，配合
[原始手册](vendor/tushare-relay-v0.5.61.md)及下述只读消费者可查询每项参数原文。
原文保持44358字节，SHA256 `402bbfdfa77e6035b5cd41bc9a5d66b6ed975db2f2a25748ab37b797c12c827f`。

## 已查到与尚未查到

手册实际列出 **271个唯一接口名，258项标记可用，13项标记未启用**。
导语“257已启用”和章节标题“298个”并不一致；原样保留，不补造27个缺失名称。
这些只是手册标记，**当前服务器实时enabled数量未知**。

已用现有Secret请求完整目录：
[原生运行38762503155](https://github.com/auguspp/Decision-OS-2/actions/runs/38762503155)。
第一次、第二次均连接不可用，具体连接/TLS/读取原因未记录；第三次明确HTTP429。
已经停止请求，不换Key/IP/地址绕过，没有取得成功目录正文。
[过程和产物校验回执](https://github.com/auguspp/Decision-OS-2/issues/12#issuecomment-6196286272)
以及JSON中各attempt的artifact ID、摘要和时间保留这一缺口。

服务器的 `provider/enabled/required/required_any/methods/probe_example` 实时机器声明
均尚未取得。登记中的方法、状态、参数说明来自手册；不以静态文字冒充实时响应。

## 271项的功能地图

下面14组互斥覆盖所有名称，**分组和模块归属是OS2工程解释，不是供应商额外承诺**。
#7研究、#10市场雷达、#11产业暴露、#12资料、#13交付是建议消费者，不自动成为施工授权。

| 功能组（查询ID） | 数量 | 代表接口 | 建议消费者／主要用途与限制 |
|---|---:|---|---|
| 财务报表、指标、预告与审计 `financial` | 18 | income、cashflow、fina_mainbz、disclosure_date | #12/#7；包括港美财务候选，表格不替代附注/PDF |
| 新闻、公告、互动与研报 `content-events` | 13 | news、anns_d、report_rc、research_report、irm_qa_sh | #12/#7/#13；区分观点、公告目录与原件 |
| 资金流、股东、席位与资本行为 `participants-capital` | 33 | top10_holders、top_inst、stk_surv、repurchase | #12/#10/#7；席位标签不是自然人持仓或因果 |
| 指数、行业、概念与成员 `indices-sectors` | 44 | index_weight、index_member_all、concept_detail、ths_daily | #10/#11/#7；需核成员日期和分类口径 |
| 基金与ETF `funds-etfs` | 21 | fund_nav、fund_portfolio、etf_share_size | #10/#12/#7；份额、净值、持仓频率须另核 |
| 期货、期权与贵金属 `derivatives-commodities` | 23 | fut_daily、fut_wsr、opt_daily、sge_daily | #11/#10/#7；不是现货规格价格或利润暴露 |
| 债券、可转债与回购市场 `fixed-income` | 12 | cb_daily、cb_call、cb_issue、repo_daily | #10/#7；债券交易和发行资料候选 |
| 宏观、利率与经济日历 `macro-rates` | 19 | cn_cpi、cn_pmi、shibor、eco_cal | #10/#12/#7；需核公布/修订、单位和日历口径 |
| 港美股与外汇 `international-fx` | 22 | hk_daily、us_daily、fx_daily、us_tradecal | #10/#7；交易日、时区、币种和复权不混用 |
| 分钟、逐笔与实时行情 `intraday-realtime` | 10 | a_share_mins、stk_mins、rt_k、rt_min | #10；不把日终发现冒充盘中监控 |
| 证券基础与交易日历 `security-reference` | 13 | stock_basic、stock_company、trade_cal | #10/#12；证券身份和基础引用，不是投资判断 |
| A股行情、交易统计与指标 `market-observations` | 26 | daily、daily_basic、adj_factor、stk_factor | #10/#7；日周月、复权及统计口径另核 |
| 客户组合管理 `portfolio-private` | 4 | p_list、p_get、p_save、p_delete | 无默认消费者；私有内容及写删须另行授权 |
| 名称已登记、功能待核 `needs-review` | 13 | 见下文完整名单 | 不依据缩写编造具体功能 |

“功能待核”的13项为：`bc_bestotcqt`、`bc_otcqt`、`factor_list`、`factor_value`、
`gz_index`、`mkt_idx_bmk`、`npr`、`st`、`stk_high_shock`、`stk_shock`、
`the_member`、`wz_index`、`yc_cb`。手册说明不足，应补单接口能力或字段文档。
这是用途解释维度，与“13项未启用”的状态维度不同，不能互相替换。
全部271个名称在JSON的groups中逐项保存，原参数与行号由消费者直接取自字节绑定的手册。

## 已有工程与真实业务证据

现役共用 `RelayHTTP.request` 只接纳8类财务API和 `disclosure_date`，不表示271项均已适配。
元数据读取本身也不放宽业务白名单。已有
[首轮业务实采回执](https://github.com/auguspp/Decision-OS-2/issues/12#issuecomment-6195254834)
范围为 `000001.SZ/600519.SH` × `20251231/20260630`，不是全市场长期健康验证。

| API | 非空查询 | 合法空查询 | 失败 | 返回行 |
|---|---:|---:|---:|---:|
| income | 3 | 0 | 1 | 4 |
| balancesheet | 4 | 0 | 0 | 7 |
| cashflow | 3 | 0 | 1 | 4 |
| fina_indicator | 4 | 0 | 0 | 4 |
| forecast | 0 | 3 | 1 | 0 |
| express | 0 | 2 | 2 | 0 |
| fina_audit | 2 | 1 | 1 | 2 |
| fina_mainbz | 3 | 0 | 1 | 56 |

32次查询中19个非空、6个合法空页、7个失败，77行供应商数据不是77份独立原件。
预告和快报尚未有本轮非空样本；`disclosure_date`不在这32项中。
失败状态没有被改绿，取得数据也未因部分失败而丢弃。结构化数据、原PDF、研究采用分别验收。

## 其他模块怎样查询

安装OS2后指定本仓登记文件，无需Key、不访问网络：

```bash
python -m decision_kernel.interfaces.relay_registry_cli \
  --registry docs/sources/relay-registry.json --api income

python -m decision_kernel.interfaces.relay_registry_cli \
  --registry docs/sources/relay-registry.json --module '#10'

python -m decision_kernel.interfaces.relay_registry_cli \
  --registry docs/sources/relay-registry.json --group participants-capital
```

Python消费者可以调用：

```python
from pathlib import Path
from decision_kernel.application.relay_registry import RelayRegistry
registry = RelayRegistry(Path('docs/sources/relay-registry.json'))
item = registry.lookup('income')
assert item['live_enabled'] is None
```

返回包含手册原行、`source_line`、方法、参数原话、分类与建议消费者、现役客户端是否接纳、
有限实测范围。`live_enabled/live_methods/required/required_any/probe_example`为null，
不猜造机器合同。`parameter_note`只是原文，不是已经通过验证的运行参数对象。
表格列数不齐（例如adj_factor）返回 `ambiguous-see-original`，不会偷偷错列解析。

仓外调用需同时提供登记JSON及其相对路径下的原手册。消费者检查SHA256及271项无重复/遗漏，
不根据来源正文生成命令或请求；`shared_relay_client_admits_api`不等于用户授权。
真正八财务取数仍使用[现有批量CLI](relay-financial-batch.md)，凭据沿用[原Secret说明](relay-setup.md)。

## 后续与维护

实际目录还需在供应商允许的窗口或排查恢复后取得；此次429以后没有继续请求。
现役错误回执未保存Retry-After，不能编造一个允许重试时刻，也不再要求用户重填已验证Secret。
后续通过[目录读取入口](relay-capabilities.md)保存新响应/原始字节和回执，登记差异并保留历史。

本登记归#12维护，其他模块消费同一入口，不各建副本或第二套Relay路由。
它不是实时任务看板，不自动调度、研究、交易、开放组合读写或更改公开许可。
手册/证据升级按新版本处理；退役本读取消费者时连同CLI/专属测试退出，不删除历史SourceRef。
完整#12不因本登记或目录读取工程交付而关闭。
