# 交易日历传输失败的显式一次恢复

责任：#10 / #164；本合同是 [日常更新合同](daily-market.md) 的窄增量，不是通用自动重试器。当前 2026-10-22 SZSE 失败周期的范围与一次附加额度登记在 [认领回执](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6353400670)。原 2026-10-19 SSE 恢复及其授权保留在历史记录，不因部署目标更新而重开。

## 原因与已知边界

原 `recover-late` run 39373820825/1 在 SSE `trade_cal` 请求返回 `RELAY_HTTP_UNAVAILABLE`，正常计数为日历1、FT价格0。原传输层本已有安全的超时、DNS、TLS、连接等分类，但旧日历宿主只留总错误码。该旧回执不能证明当时必为超时或服务端宕机。此次保留原传输分类、HTTP状态、耗时与原90秒 socket timeout；不序列化异常正文、URL、凭据或响应错误原文。

2026-10-22 的原 `external-clock` run 39764203404/1 已保管 SSE 日历，在 SZSE 请求发生 `RELAY_HTTP_UNAVAILABLE / timeout` 后停在 v4 r004。该次已用2个日历请求，主表和价格请求均为0。原恢复入口只支持 v1，不能直接接续包含完整主表的 v4；本增量补齐这条原范围恢复路径，不把 HTTP 200 元数据当作完整响应或把补更当作原自然窗口成功。

Python urllib 的 timeout 针对阻塞操作，不是整次 HTTP 的总期限；没有据旧运行总耗时猜测网络原因。复用现役 RelayHTTP、RetryingRelay、capture_registered、retain_market_query 和 GitHub CAS，不新增依赖、备用供应商或绕过 TLS。官方依据：[Python 3.12 urllib.request](https://docs.python.org/3.12/library/urllib.request.html)、[urllib.error](https://docs.python.org/3.12/library/urllib.error.html)。

## 精确授权与来源预算

[固定恢复配置](../../config/daily-calendar-repair.json) 只指定原 20261022 r004 的 exact SourceRef、SZSE、一次额外请求及授权回执。原始总码必须是 RELAY_HTTP_UNAVAILABLE，已失败的日历 reserved_requests=1、reference=null，未开始价格。另一日历只允许原 pending 或已完整保管的 complete。当前部署只授权这一个真实目标；修改目标必须经相应 owner 的新修复依据和正常工程审查，不能换 run/key 当作新预算。

原正常周期2个日历请求不改；当前目标**原已用2 + 恢复SZSE1 = 最多3**，复用原完整 SSE 日历。本次恢复最多1个实际日历 HTTP；兼容原另一日历仍 pending 的历史合同也只能使用其原未用请求，单次调用最多2个实际日历 HTTP。原六股 FTShare 仍最多12个整段请求，不增加、不改变日期、窗口、证券、复权及 close 字段。v4 的两所完整主表阶段尚未预留，恢复后继续原冻结政策的两个查询及原分页/重试额度；不降级跳过主表。原失败永久保留，新的调用不能把计数清零。

`daily-market.yml` 新增显式 `operation=repair-calendar`，仍只有 operation/code_sha 两个手动输入，不接受任意日期、来源URL或预算。由正常main原生身份与公开授权预检，再加载固定恢复配置；实际运行在读取原周期后核确切目标、未改变的 policy_sha256 和原 FTShare 当前后继。原生创建时钟/队列不足24小时仍核；恢复目标必须距原周期实际创建严格不足7天，时间不倒填。

普通 status/update/recover-late/schedule 不从配置自动启动恢复；已有 blocked 仍停止。新入口接执行 operation=update，但保留真实 invocation=repair-calendar，不能被改标签当作自然定时成功。公开授权不等于解除来源限流或认证失败。

## 保管、未知结果与接续

修复从原 v1 blocked 边追加一个 `daily-market-cycle-v2` 预留；原 v4 blocked 则追加 `daily-market-cycle-v5`。两者的 calendar_repair 均保存授权与原 exact SourceRef，只有指定失败交易所的 reserved_requests 从1到2。v5 继续保存并冻结原 daily_policy、master_refresh、masters、日期、generation、base 和 policy_sha256；最大 revision 为13，仅比 v4 增加恢复预留和结果两条边，单条仍不超过12KiB。v1-v4 历史原字节、schema和序号边界不变。

预留、当前后继检查及提交使用同一个 Store head/CAS；先保管并独立读回，才进行一次 HTTP。相同序号不可分叉，完整历史重放校验旧失败、原参数和唯一恢复边。原生 `repair-calendar` 宿主加载可信主表配置；v4/v5 目标要求其与原冻结 policy 完全一致。v1/v2 恢复仍按旧意图执行，不因配置存在而加入主表阶段。完成后的后续普通新周期继续生成 v4，恢复grant与额外预算不继承。

结果成功则保管真实日历并由原正常执行器继续尚未用过的阶段；失败则追加 blocked，无自动重试。未知预留/未知保管仍 in-flight，不能因新会话或运行获得第二次请求；并发竞争不覆写，不因修复绕过原前沿检查。已完成周期再次调用只读，不开新的业务日。

v5 的未知日历预留或日历保管不能借 `reconcile-write` 解锁；该入口仍只适配已独立审定的旧 v2 r008 五笔原写入。主表 owner 对完整 checkpoint 的未知 query 回执仍可按原零来源请求对账规则接续；FT 未知写仍遵守原终态和专属恢复授权，日历grant不扩大这些权限。

HTTP401/403/429、明确拒绝、已知TLS问题、来源hold和旧one-shot/adopt/repair没有借用该授权的路径。此次若出现这些失败则停止，不重新配置或换身份绕过。两个原日历与一次修复各自计数，新的日常周期仍使用原两请求规则。数据源故障与工程/读取是否正常分开陈述。

正常 result.json、Reading Room 内嵌模型和 Summary 展示额外1次预算及安全错误分类；v5 同时保留两所实际主表引用、原预算和 FT 采用校验。status读取已修复周期也保留累计预留和原授权。旧周期原字节、store marker、原引用和失败回执不重写。撤销配置会禁止新的显式恢复，不使已经保管的 v2/v5 历史不可读。禁用不回收已执行副作用。

## 验收与退出

真实同源网络成功、实际日历保管、完整主表采用、FT价格更新、独立读回与后续自然连续性分别验收；单元测试不认证供应商在线。合成测试覆盖实际 GitStore/CAS、原生宿主grant与主表policy转发、原 Relay 解析/日历保管、v4 r004 的单次 SZSE 恢复、完整分页主表与十二个 FT 槽接续、重复调用无额外请求、超时/拒绝、未知写、撤销、目标/冻结范围变更及正常页面。旧 v1/v2/v3 恢复测试继续保留。

完成或撤销后可禁用固定grant。若退役执行入口，同步退工作流选项、宿主选择和专属执行配置，但 v2/v5 历史读取支持应随仍保管的精确引用继续；不删原失败、数据分支或公共底层保护。
