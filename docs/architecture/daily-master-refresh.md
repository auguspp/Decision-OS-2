# 完整 current-L 主表刷新与 FT 后继采用

责任归 #10。直接消费者是现役 FTShare 日更、正常市场阅读器及 #60 的原始收盘价 Odds/Watch。[原缺口](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6288243620)是日更只替换日历和价格，FT context 一直沿用旧主表；取得新价格不能使旧主表的取得时间变新。该组复用原 Relay 注册查询、分页和执行检查点、同一 records Store、FT 唯一后继与正常消费者，不建立第二份证券主库。

## 刷新触发与边界

可信 [daily-master-refresh.json](../../config/daily-master-refresh.json) 固定本组范围。原日更 `schedule`、手动 `update` 和 `recover-late` 在建立**后续新周期**时采用 `daily-market-cycle-v4`。每次两所真实日历确认存在新增已完成交易日后，先刷新 SSE、SZSE 的完整 `stock_basic / list_status=L`，两所完成后才创建或接续本周期 FT 价格计划。没有新交易日时保留原行情引用，主表和价格请求均为0。

触发频率是每个被实际执行的新开市日周期一次，不等待主表超过72小时才开始。该频率使正常工作日更新持续取得当时完整主表；它不是供应商、GitHub调度或五分钟内完成的SLA。调度延迟、周末、来源失败及额度等待会使资料暂时超龄，正常下游须继续拒绝过期资料，不能通过新包装时间或放宽容忍期显示合格。

既有 v1/v2/v3 周期的字段、序号边界、预留、原失败及恢复授权保持。旧周期未完成时先处理原意图，不给它附加主表额度，也不跳到新日期。已保存 v4 之后不能用不带主表政策的下一周期降级绕过刷新。停止日更仍通过原配置的 `enabled=false`；它不删除任何已有控制、页、引用或失败。

| 资源 | 每个新 v4 周期的边界 | 实际请求的 owner |
| --- | --- | --- |
| 交易日历 | 两所各一次，共2次HTTP | 原日更日历预留，失败不自动重试 |
| current-L 主表 | 两个完整查询，各最多20页，每页500行 | 原 Relay 分页检查点，页终止证据必须完整 |
| 主表重试 | 每页最多4次尝试、累计最多180秒等待；两所最多40页/160次HTTP | 原 Relay 重试检查点；不因新API对象、进程或日期重新计数 |
| FTShare价格 | 原六股 none/forward，共12个单股整段请求 | 原 FTShare collection，每个槽仍只请求一次 |
| Store与等待 | 原来源前240准入、单元512请求、容量/原生quota及同run等待上限 | 原GitHub transport、Store CAS与日更宿主 |

两次完整主表查询不等于两次HTTP。实际分页或失败尝试按回执单独计数；全部scope来自完整结果，不能用已有六股过滤主表、抹去不合格行或固定原5226分母求绿。新主表不承担BSE、D/P状态或历史时点证券全集资格，现役 current-L 的幸存偏差和隔离规则保持。

完整旧FT与日更证明仍由正常reader逐项恢复。行情reader在原外层读取作用域取得并验证独立GitSnapshot后，使用共享 `infrastructure/native_git_memo.py` 复用同一快照内已成功的完整SHA原生查询；沿用[现役有界缓存规则](../research/watch-history.md#有界原件读取)的8MiB/32,768条目上限，不缓存失败、可变ref或权限。嵌套读取共用该作用域，退出或异常时清空并使视图失活，下一外层重新取得快照。退出仍由原remote owner和同一API完成实时绑定/可达性检查；缓存不替代原路径、内容摘要、历史语义或权限校验，也不增加来源与Store预算。

## 持久化、分页与停止

v4 仍使用 `daily-market-cycle-YYYYMMDD-rNNN` 的原不可变控制链，最大revision为11，单条仍不超过12KiB。两所主表各持有一个预留行，记录 pending / in-flight / complete / failed、一次完整查询的预留、最终query引用及其完整checkpoint引用。控制只持引用，不复制表格，也不另建当前状态文件。

v4 的 `daily_policy` 保存本周期批准日更政策的规范快照（仅去掉运行开关 `enabled`），并绑定原 `policy_sha256`。该快照与控制链其他不变字段一起冻结，历史恢复用当时批准的范围核完整性；当前配置或当前受保护股票清单不能代替过去的批准范围。

正常顺序为日历阶段、主表阶段、价格阶段、完成。主表预留之前核当前日更记录与FT后继，并把预留绑定同一个已核Store快照及原CAS。每个主表分页/重试单元从原确定的业务日期和交易所恢复同一checkpoint owner；最新checkpoint必须沿原前驱链，查询、注册版本、页上限和重试政策完全一致。原CP先保存并独立读回预留，才发来源HTTP；一个有限单元最多新增一次HTTP，完成页可在同一原owner下暂停后接续。

主表的页与重试只由原 `capture_registered`、`RetryingRelay` 和 `retain_market_checkpoint` 持有。已接受页通过原SourceRef恢复，后继查询保管采用原checkpoint的确切page refs，避免重复取得成功页或重复保管整套页。完整checkpoint在query保管前中断时可用0次来源请求完成原query保管；不得换owner重采。旧in-flight的HTTP是否已发生无法确认时，累计实际请求保持未知，不填0或重新开始；只有已经保管的尝试可以计作确定次数。

来源拒绝、原全局hold、终态失败、未知checkpoint写、分页停滞、截断、页上限、所有权或时钟冲突停止新的来源。失败的本周期阻断下一日期，不借主表刷新清除旧失败。已知HEAD_CHANGED说明当前候选未通过原CAS；重新接续前必须核实际效果和当前原意图。未知写使用原键、原内容和原回执对账，不把它作为普通网络重试。

若新的主表资格使 FT planner 在建立价格计划前拒绝，宿主回执保留准确原因，控制仍保持原 `market-pending` 意图。后续调用按同一冻结主表和日期再次核验，不能替换引用或建立下一日来重新取得来源；该情况不冒充已写入另一条价格失败终态。

## 正常 FT 采用与历史恢复

`plan_ftshare_single_collection(..., master_references=...)` 和 `run_collection(..., master_references=...)` 只接收 SSE/SZSE 两个同仓完整SourceRef。新字段并未另加到FT plan：使用其已有 `master_references`。planner、首次保管、普通resume和逐代restore均按计划冻结的引用重建context；同generation的override与原计划不同，在构造来源客户端前拒绝。

每所通过原 `restore_market_query` 和 `inspect_security_master({L: result})` 校验完整scope及隔离规则。新页取得时点不早于该所旧主表的最后一页、不得晚于新FT计划创建时间；新日更主表的页与尝试也不得早于本周期实际创建时间。时间只来自原捕获页和原执行回执，不使用本次读取、保管或网页生成时间替换。

根据两所实际新结果重建masters、master_results、master_references、scope、eligible与quarantine。任何已有价格request owner在新主表中缺失或被隔离，整个新计划停止，包括旧failed owner和本轮准备覆盖的owner。不能先删旧请求、改变失败或缩小旧引用集合来通过。原legacy adoption、历史主表与历史价格版本仍可正常恢复；current-L的新scope不被追写成过去已知的scope。

普通同窗手工扩面若改变主表引用，返回 `FTSHARE_EXPANSION_MASTER_CHANGED` 并保留真实最新引用及阻断。主表替换通过明确的新日更代次采用，不能伪装成不影响上下文的池外新增。新主表完成后，daily完整终态还须证明该FT计划确实采用同周期两所主表和日历ref、确切base和generation，并严格匹配冻结政策的日期范围、收盘完成政策、全部批准股票的 none/forward 目标及原价格预算。每个新槽须按原owner只预留一次，集合没有hold或未完成槽；只完成缩小后的目标集合不能把整轮签成完成。逐代历史读回核同一约束。

正常FT view保留新scope、隔离与来源引用，并显示每所 `capture_times.L` 的原逐页取得时点。ReadingRoom消费原同一FT引用，不接受单独较新的主表参数代替旧FT context。#60原政策 **quote3/master3/capital35天**及主表全部页72小时要求不变；其正常只读资格必须在实际新引用与实际评价时刻另行成立。

## 时间口径与运行诊断

原 Relay checkpoint 的 `attempt.requested_at` 在同步保管 in-flight reservation之前取得；持久预留及独立读回完成、原guard与deadline通过后，才发出该次HTTP。因此 `requested_at → received_at` 是原attempt的包围区间，可能包含较长的请求前保管，不能按cacheHIT或区间长度推定上游延迟。响应后的页间差额同样不能全部归为Store写入。

现役[日更性能诊断](daily-market.md#运行性能诊断)在原owner边界另记实际阶段，保持原attempt字段、完整分页、重试/等待、原预留与控制摘要不变。诊断只帮助区分实际传输、校验、保管和独立读回；它不能授权跳过fresh读取、并行写records或以只含六股的主表提速。

## 验证与退出

工程验证覆盖完整多页原检查点接续、页及重试预算、0请求推广、未知/失败与所有权变化停止、新主表scope和已有owner保护、计划保管及两代独立Git恢复、正常日更与Reading采用、旧周期继续按原预算，以及新周期在同日重复调用时0来源。合成来源只证明工程语义，不能证明实际刷新已完成、现在的证券数量或自然调度连续性。

真实验收必须分别核原生run/attempt/head、完整两所页及实际时钟、query/checkpoint原件、FT计划的确切采用、正常reader及下游原政策。随后把包含新主表/价格的确切records快照备份并独立恢复；此前备份不能覆盖尚未发生的新增。

本组维护 `daily_master_refresh.py`、v4日更接线、FT显式主表采用、专属配置与测试及本合同。若执行刷新能力退役，同一普通PR撤出来源入口与专属配置/测试，保留仍有已存v4与FT消费者依赖的历史校验和读取支持；共享Relay、Store、CI和正常阅读不随本组删除。回退不能强推records或移除旧原件。
