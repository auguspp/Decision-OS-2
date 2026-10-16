# Full默认执行与来源使用核查

本页由#7维护，整改“读了方法/接口说明，但没有实际使用信息池和Relay，仍交付系统Full”的缺口。复用原prepare、ResearchEvidence、Relay Registry/capture、SourceRef和研究底稿，不建立第二研究账本、模型、搜索或调度。**研究者执行取材；程序验证实际回执及作者引用。两者都不能替代经济质量验收。**

## 正常Full的必做流程

新Full或接续Full都从原委托恢复公司、问题、已有授权，不要求用户重复指定每个接口。读取当前方法之后，必须实际执行现役prepare/evidence_plan，按公司、代码、可靠别名及相关主题取材；继续需要的分页，未读大对象用原read-source。查询为空、拒绝和未查询分别保留，检索命中不自动成为有用证据。不要把整段委托当精确短语，零命中后就声称查遍信息池。

接口与池从开局并列选择。通过可信Registry构造有关的来源计划，沿原relay-source-runtime的research入口实际capture，或核验并复用已有有效capture。plan成功不是capture成功，不能以读过目录或本地Shell/DNS失败代替已连接GitHub工具的实际调用。连接器先按当前暴露schema发现并调用适用动作；真实拒绝不绕过，不为凑回执重新抓取已经可用的数据。

研究者将真正采用的资料定位到原ResearchProgress.sources，并在findings/challenges.evidence中关联具体问题。继续原取证、计算和挑战，不先为验收编一个结论。存在既有公开保管授权时，沿deliver/checkpoint保存并独立读取；没有该权限时只交付合格的本地/受控成果和限制，**不得为了让核查通过而公开资料**。单案例授权不能外推到另一公司。

阶段报告须区分研究内容完成范围与系统通路状态。申请“信息池+接口来源使用成立”之前执行下述audit-source-use，记录实际状态和缺口。没有执行或证据不足仍可保存部分成果，但不能称系统Full端到端验收通过。对不适用、无关命中或真实拒绝留下范围、尝试和理由，由实际审阅处置；不设固定API/文件/调用数量。

## 默认入口的行为

原prepare新增full_execution：未带材料计划时直接列execute-pool-plan及原request；池分页未完时列后继payload；Relay检查完成后给capture-or-justify-reuse，而不是executed=true；末项为audit-source-use。它是本仓Full研究者必须落实的执行合同，不是后台模型或自动取数器，也不能强制一个绕过本仓入口的独立聊天。

checkpoint、resume的source_use_acceptance明确为not-demonstrated，并返回精确研究引用/核查操作。这个字段表示该保存/读取动作没有核查来源执行，不否定别处已核过的历史证据。原保存字节、研究方法摘要、旧意图重放、部分保存和Odds均不改变。源资料或作者自行写的“已通过”不会代替核查。

## 只读audit-source-use

使用同一个research-runtime.yml，operation=audit-source-use，ref=main，code_sha为本次已读的主线；它只读，不提供submission_id或expected_head。payload两种选择互斥：

```json
{"reference":{"repository":"...","commit":"...","path":"...","sha256":"..."},"receipts":[{"run_id":123,"run_attempt":1,"artifact_id":456}],"exceptions":{}}
```

上述数字只是形状示例，不是真实回执。未保存的阶段稿改用原request和progress两个对象，其他字段相同；公开宿主只接受已有公开权利的输入。CLI沿相同执行：

```bash
decision-os store --binding config/store-binding.json audit-source-use --input audit.json
```

标准proof只需run_id/run_attempt/artifact_id。只读job增加actions:read来读历史原生回执，没有Actions写/dispatch/取消权限，没有Relay Secret。适配层复用原GitHubAPI的GET和现役CI使用的gh原生artifact读取；仓库来自可信binding，路径来自已核正整数ID。gh在隔离配置目录内只使用专用DECISION_OS_STORE_TOKEN，不继承工程登录、主机或代理配置。也可传已下载原ZIP的archive_base64，但仍需实时原生元数据核验，不能传自制metadata/status当证明。

核查绑定仓库ID、公有可见性、workflow_dispatch/main、确切代码、run/attempt、原artifact名称/所属运行/原生digest和大小、匹配job、原ZIP安全文件/CRC。之后解析原输入和结果：

| 输入证据 | 核查结果边界 |
|---|---|
| 只读方法的prepare | method-only，不算池查询 |
| 原prepare带evidence_plan | 校验同一委托、原输入摘要、计划和固定快照分页链；只有尾页不算读完前页 |
| 正常零命中 | queried-empty；不是查询失败或没执行 |
| 查询失败/缺页 | query-failed/query-partial/coverage-unproven；保持缺口 |
| Relay计划 | planned-not-captured；不等于源请求 |
| Relay research capture | 使用现役inspect_result核查询、原字段投影和分页；保留逐查询complete/incomplete/not-attempted |
| SourceRef与底稿 | 实际读取原版本，与池命中/导出capture版本对照，再检查作者问题/挑战的evidence索引；不是经济相关性认证 |

同一委托继续研究可以复用原运行，无须按研究阶段重复capture。跨案例复用capture须显式reuse_run_ids及exceptions.relay说明范围、日期与用途，仍须原生回执及准确来源版本相符；这只证明实际使用旧捕获，不能证明现在仍新鲜。当前核查适配Relay的research family；旧financial/participants或其他平台证据需人工核对，不能为了适配本核查重跑旧来源。

## 历史失败与当前恢复

`relay_results`始终保留全部历史尝试；`relay_recovery.current_queries`另给当前查询状态。相同case/API/规范参数的后一次真实成功可以恢复前次失败，不删旧错误。按GitHub原生run_started_at及同秒run/attempt定位次序，不按调用者排列顺序；后一次失败不能借更早成功签当前恢复。只有plan的查询也逐项保留，不会被另一项capture掩盖。

参数改变时不按“同公司/同API”自动覆盖。可在同一audit输入增加`relay_recoveries`（最多64项），选择器中的query_index为原计划从0开始的序号：

```json
{"relay_recoveries":[{"from":{"run_id":123,"run_attempt":1,"query_index":0},"to":{"run_id":124,"run_attempt":1,"query_index":0},"reason":"基于实际字段文档的修正说明，仅示例","field_map":{"old_field":"correct_field"}}]}
```

两个选择器必须来自已认证回执，旧项未完成、新项complete且较晚；API、case及证券/期间/报表类型/offset等业务筛选必须相同。仅字段和page limit可以不同；同名字段的更大完整投影可覆盖旧需求。改名必须逐项显式field_map，映射目标必须在新请求字段中，无法覆盖的旧字段逐项列missing_fields并保持partial-recovery。不能删除未取得字段、把另一期或另一公司替代本期。

字段改名只证明“修正后的捕获确实完成”，不认证改名的经济含义；返回complete-corrected-fields/captured-with-corrections以及RELAY_FIELD_CORRECTION_REQUIRES_REVIEW，仍不能自动通过。用户/研究者对不再需要的旧字段作范围取舍，也不能由自填原因自动代签。错误关联保留rejected_corrections并限制验收。仍失败的其他查询继续保持未完成，不因为现金流恢复就把公告等缺项消掉。

信息池同样保留`pool_history`。相同计划/原委托的新观察选择最近原生起点的固定快照，必须有该快照自身完整首/中/尾游标链；旧失败不会永远锁住已完成的新扫描，新失败或只有尾页也不能借旧快照通过。相同快照同输入游标的后继真实结果可替代该位置的失败，但失败回执仍在历史中。不同计划不会因为更晚而被自动视为等价。来源采用仍核当前选定查询链和确切SourceRef，不认证经济判断或时效。

## 输出与不足的处置

usage-demonstrated仅表示声明范围的池检索链、Relay捕获/复用及可读引用采用已有可核证据；不证明查询选得好、所有资料都读完、来源真实、完整Full质量、deliver全部附件或人工Decision。经济关系、时效/单位和每个结论仍需审阅。少量来源达标不能代表全公司证据充分；一份来源若支持多个问题不计为多份独立上游。

needs-action表示缺执行/回执/引用；exceptions-require-review保留不适用、拒绝或无保管许可的作者解释，**不把解释升级为已经执行或自动通过**。两者在CLI返回2，宿主status=source-use-not-demonstrated并完整保留report；这是非通过结果，不要用循环rerun或删缺项求绿。full_product_acceptance和research_assessment始终not-performed。

每次最多8份原生proof，原ZIP每份1MiB、解压总量8MiB，沿原2MiB研究payload与60KiB宿主外层。只传原生ID可避免大ZIP塞进dispatch；旧ZIP可选输入仍受限。大小/过期/权限/宿主工具缺失会明确无法验证，不报零或自动重采。原生元数据失效时，即使有本地ZIP也不能冒称本次实时核验成功。多个独立研究范围分别在原成果中保留核查，不合并成一个永久服务。

研究运行产物仍是原Actions回执，不是第二来源库。核查结果的摘要、原生定位和例外可随原研究交付保管，权限/保留期照旧；它不会写records、改研究或启用定时。原生回执真实性依赖可信仓库主线与GitHub，不是发行人数据认证或不可绕过的角色隔离。

## 工程维护与实际验收

应用层只组装Full动作与来源采用核查；infrastructure/research_receipts读取原生证据；interfaces/research_use_commands连接现役只读宿主/CLI。原search、capture、inspect、source_inputs、版本写入仍由原owner维护。退役时同步撤本核查入口/专属测试/只读Actions权限/说明，保留原研究和来源历史。安装验证追加到原单次cold-wheel，不建另一个安装实验。

工程用真实裸Git、合成HTTP/原生元数据模型验证缺执行/假回执/跨案/缺页/计划代捕获等反例；它不是公司Full验收。实际后继研究沿原报告补证、复算、挑战后再验，不推倒重写，不因代码合并自动签产品通过。

复用依据：[GitHub原生artifact API](https://docs.github.com/en/rest/actions/artifacts)、[gh api](https://cli.github.com/manual/gh_api)及本仓.github/scripts/ci-runtime.py已有的原生artifact读取。本层只做研究用途的绑定和解释，不重建通用CI证据平台。
