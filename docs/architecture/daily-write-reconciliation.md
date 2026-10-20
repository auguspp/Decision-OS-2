# 原写入回执的显式对账与接续

这是[日更](daily-market.md)内一个固定原目标的恢复边，由 [#10](https://github.com/auguspp/Decision-OS-2/issues/10) 维护；不是通用失败重试入口。日历传输恢复仍按[原合同](daily-calendar-repair.md)执行。

## 原问题与适用范围

原生日更 [39401024562 / attempt 1](https://github.com/auguspp/Decision-OS-2/actions/runs/39401024562) 在 `524243caf8755839245bcf7b374cd1178aaf32b2` 上调用 `repair-calendar`。原产物记载：日历已完成、累计预留3次；FTShare 首个 `none:000001.SZ` 请求已发出，随后保存结果返回 `REMOTE_WRITE_REQUIRES_RECONCILIATION`。原日更停在 v2/r008，FT 集合停在 r002：1 complete、11 pending、reserved 1、无 source_hold。原失败回执、代码和运行结论保持不变，不能据此推断限流、超时或其他具体根因。

[固定配置](../../config/daily-write-reconciliation.json)绑定这一次原生 run/attempt/code、原 ZIP 的 artifact ID/字节数/SHA-256、五笔原提交的 submission_id/intent_sha256/SourceRef/expected_head，以及[接管授权](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6293255907)。五笔顺序为 FT r000、r001、首笔价格正文、FT r002、日更 r008。配置不包含凭据、来源正文或任何可执行附件。

`additional_source_requests=0`：原六股 × none/forward 的12个整段请求仍是上限，仅11个未用槽可接续。已完成首笔和两所日历不重取；原六股、日期20261019、窗口、generation、原始 close、completion policy、日历确切引用、已用额度、失败和 hold 不修改。不从另一 run、另一 key 或另一个日期取得新额度。

## 原生入口与独立核验

工作流仅增加 `operation=reconcile-write`；仍只有 operation/code_sha 两个手动输入，沿同一个来源串行组和日更 writer。普通 status/update/recover-late/schedule 不加载固定 grant，也不能自动从原 r008 解锁。

来源凭据前的 visibility 预检仍只读三次原生元数据；公开部署授权和固定 grant 均有效才开放该入口。宿主继续核当前 main、仓库、工作流、run/attempt 1、原生创建时钟和适用 main CI。新边要求距原周期实际创建严格不足7天、时间不倒退；真正追加预留时再核该时间边界。没有任意日期、来源URL、额外预算或忽略 CI 的输入。

首次新增恢复边之前，宿主核原 run/attempt/head/workflow 的原生 failure、三个实际 job 和原来源/读取/产物步骤结论、原产物身份与摘要、五个提交的唯一原 parent。ZIP 最大1 MiB，展开最大8 MiB，只允许原六个文件，核重复名称、加密、符号链接、压缩格式、CRC 和精确字节摘要。只解析原 result/control 的 JSON，不运行下载代码，不把失败改成成功。

同一个原 Store 只读核五个 journal 的原字节与当前字节、原 parent 尚无该 key、由原 SourceInput 重建的 intent 摘要，以及 manifest/正文原始 last_change。再通过原 FT restore 核 r000→r001→r002、第一笔真实原件和其他11个槽 request_count=0、无 failed/hold/in-flight、原六股 policy 和当前 FT frontier。没有向来源发请求，也没有通过再次 put/write 探测原写入。

## 唯一新边与已有状态接续

旧 v1/v2 的 revision 上界不增加。唯一获准边从原 v2/r008 blocked 追加为 `daily-market-cycle-v3` r009 market-pending，保留原 calendar_repair，新增完整 write_reconciliation 证明。原参数及所有已完成效果不可变。r009 用已有 VersionedStore、已核快照 head 和 CAS 保管并独立读回，之后才调用原 FT 执行器的未用槽。

写前再次核原五笔、当前 FT tip 和日更 frontier；检查之后的 records 竞争由原 CAS 拒绝。未知写入保留原 submission，下一次先按原状态独立对账。r009 一旦保管，普通 update/schedule 可以按原安全 pending 合同续这一个旧周期的剩余工作；它没有借配置新开恢复边、换日或增加请求。已确认 r009/r010 的重复显式调用也不重新下载旧 artifact。

r009 最多追加 r010 complete 或 blocked。新来源拒绝、hold、未知预留或无法独立恢复仍停止，不能花第二次 grant。完整历史读取校验证明与旧边，不依赖当前配置仍启用或临时 Actions 产物仍未到期。下一个合法普通业务日重新使用 v1 和原2+12预算，不继承恢复显示字段。

## 诊断、消费和实际验收

GitHub Store 的原提交与所有写后校验保持。只有原 `REMOTE_WRITE_REQUIRES_RECONCILIATION` 增加结构化 `github-write-diagnostic-v1`，记录固定阶段枚举 response/commit/binding/tree/readback 和固定错误分类。它不增加请求或重试，不输出异常正文、响应 body、URL、token 或供应商内容；上游已折叠为 response-unavailable 的错误不猜测其内层根因。

FT 单元、日更 result、正常 Reading Room 模型和 Summary 使用同一安全诊断。读取已保存的恢复周期仍显示额外来源预算0和旧失败保留，显式 invocation 不冒充自然 schedule；下一普通周期不沿用旧 grant 的显示额度。

测试使用原 GitStore/VersionedStore、完整六股12槽 Host、正常 FT reader、原生 Git/API 模型和合成 ZIP，覆盖零原件重放、唯一边、独立恢复、竞争 CAS、原效果变化、待处理与拒绝、配置撤销、时间边界、原身份链和页面消费。它们只证明工程语义。真实11次接续、实际正常读回、备份及独立恢复另在 #10 记原生 run 与确切引用；代码合并不表示这些实际效果已经成立，也不证明主表刷新、全市场覆盖、自然连续性或跨期发现。

## 维护与退出

本能力维护 application/daily_write_reconciliation.py、interfaces/daily_write_reconciliation_cli.py、固定配置及相关核心/原生/路由测试；日更、FT runtime 和 GitHub Store 各自维护自己的接入与诊断。沿原完整 CI 自动发现分片，不另加永久作业、测试环境或来源密钥。关闭 enabled 禁止新边，既存 r009 的真实效果按原安全恢复合同处理。入口退役时同步退出配置、原生临时产物核验和专属路由测试；只要尚有历史消费者，保留 v3 原历史读取。共享 Store 写后校验和诊断不随一次恢复删除。
