# Relay 券商预测、历史研报与机构调研（#12）

本组复用现役 RelayHTTP、财务批处理的分页/保存原语、VersionedStore/SourceRef，
没有另一套存储或供应商路由。来源范围只增加 `report_rc`、`research_report`、`stk_surv`。
[共享登记](relay-registry.md)中的服务启用声明与本组代码接纳、具体运行结果分开。

## 三种资料，不混成财报或持仓

| API | 请求日期 | 返回日期及主体 | 解释边界 |
|---|---|---|---|
| report_rc | report_date | report_date、org_name、quarter | 机构的预测期记录，不是公司已实现业绩，也未计算共识 |
| research_report | trade_date | trade_date、inst_csname、title | 历史报告元数据及url；未访问详情页/PDF，不证明全文取得 |
| stk_surv | trade_date | surv_date、rece_org、rece_mode | 参与机构行，不是活动总数、真实持仓或买入行为；披露日未知 |

字段依据为Tushare同名官方文档 [292](https://tushare.pro/document/2?doc_id=292)、
[415](https://tushare.pro/document/2?doc_id=415)、[275](https://tushare.pro/document/2?doc_id=275)，
2026-10-16核读。实际来源仍是第三方Relay，不冒称官方Tushare直连或公司披露认证。
旧Kernel固定`cb3bbc87`的Relay白名单及Smart Money语义提供接口/失败类别复用依据；
不恢复旧research状态、一次性probe或原供应商调度平台。

`report_rc`的op_rt/op_pr/tp/np依字段文档标万元，eps标元/股；不换算、不舍入。
rd/roe及目标价字段的原单位未在本组独立核实，保留unknown-unit，不能自动用于比率计算。
预测期quarter原文保留，不当成查询日期。缺失、null、空串、0分别保留。
不请求研报摘要/全文、作者、个人调研名单、公司接待个人或调研内容。
历史研报官方示例中的file_name未列入字段表；被返回时可保留，但不据此生成PDF地址。
URL仅允许不含认证参数的公开HTTPS定位，不下载、不执行、不跟随。

报告/参与记录没有统一原生唯一ID时，保存组合候选键与完整行版本摘要；不声称同键
一定是同一报告/活动，不按日期/机构把不同预测期或修订覆盖。重复行原位置及版本均保留。
同日多参与机构不是多次调研；本入口不输出活动数量。历史首次可得时点保持未建立。

## 同一个批量入口与可信宿主

```bash
# 不联网，明确证券×日期×三类接口；无固定证券数用量配额。
python -m decision_kernel.interfaces.relay_participant_cli plan \
  --codes 000001.SZ 600519.SH --dates 20261015 20260930 --output /tmp/new-plan.jsonl

# 仅显式采集使用既有TUSHARE_PROXY_API_KEY；保留完整或部分结果。
python -m decision_kernel.interfaces.relay_participant_cli capture \
  --plan /tmp/new-plan.jsonl --live --output /tmp/new-capture.jsonl

python -m decision_kernel.interfaces.relay_participant_cli inspect \
  --capture /tmp/new-capture.jsonl --include-rows
```

原`decision-os-relay-source-runtime`工作流新增`family=participants`和`dates`，
此时periods必须留空，apis可选上述三项、留空表示三项全选。默认family仍为financial，
财务输入/原capture格式不变；两种日期不可混填。运行必须是确切main SHA的手动触发，
只有capture步骤注入已配置Secret，contents仍只读、同一串行组、没有定时或records写。

每页1—100行，按offset有界翻页。满页需继续确认终页，页重复/越界/行数不符/源失败
保留失败而非完整覆盖。批量证券/日期不限费用配额；max_pages为可选运行安全界限。
当前每页只请求一次；401/403/429停止后续批次流量，不循环请求或换Key/路径。
单接口503/504保留安全诊断与已取得页，其余不受拒绝的独立查询可以继续。

保存通过同一CLI的save子命令，须`--public-rights-confirmed`、原batch-id和明确writer。
本地验证仅使用已存在的合成GitStore，真实公开保存须逐资料确认权利。
保存每页为excerpt（元数据/数值结构化摘录），不是HTTP原字节或原PDF。
保存时不重抓；未知写、已返回引用但读回失败，保留原intent/known reference后停止写入。
空结果只证明这次查询返回空，不证明公司未被覆盖、无调研或接口永久无数据。

## 验证与维护

单元测试使用合成响应；实际包/CI、Secret现场取数、长期保管、研究质量分别留#12回执。
本组不更改#7研究方法或数值、#6存储格式、CI策略和锁。共用分页原语默认行为兼容
原八财务；维护者新增其他资料族时仍需逐接口字段契约，而非执行全部capabilities名称。
退役时移除此三接口的观察/CLI/宿主选项及专属测试，其他财务、共享安全HTTP、已有
SourceRef历史读取不删除。没有自动研究、通知、部署、私人组合或交易功能。
