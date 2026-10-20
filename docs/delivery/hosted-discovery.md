# 公告主动发现 → Hosted AI Quick → AI Brief

责任：[专项 #152](https://github.com/auguspp/Decision-OS-2/issues/152)，沿 #12 来源、#13 交付与 #147 能力补齐；[#60](https://github.com/auguspp/Decision-OS-2/issues/60) 接入 Full 后 Watch 的确切检查采用。用户于2026-10-19明确委托实现、启用和验证主动发现链，2026-10-20明确 Watch 先交付 Brief。方法仍用[现役研究入口](../research/entry.md)，资料与成果仍在同一 `records`；没有第二模型后台、数据库或旧Kernel业务迁移。

## 1. 分工与资格

程序提供未预选公司的线索，ChatGPT自主查证、比较、挑战和撰写。`news-edition-*` 仍是可重建的机械阅读版本，不将它伪装成AI分析、不破坏旧版本校验。新的 Hosted Quick **只有原 ResearchRevision 一份研究正文**；交接只存引用。AI Brief 的作者正文与实际采用的研究正文摘要绑定，使用原 ResearchArtifacts/VersionedStore。

公告发现入口 `announcement-discovery.yml` 每天18:20新加坡时间运行（UTC 10:20），含周末。它查询截至执行日的四个日历日（日期差三天），不传股票名单；关键词为业绩、报告、合同、中标、投资、风险、更正，每类最多4页×30条、合计最多28次查询。全市场未设公司过滤，但**只覆盖这七个关键词的有限窗口，不是穷尽全市场公告**。完整查询数、advertised_total、取得行、无效行、重复、更正与分页缺口分别记录；标题没有关键词的材料仍可能漏掉，AI公开查证是独立补充。

复用官方CNINFO固定地址、表单、身份、原始毫秒时间戳与安全PDF定位合同，不下载PDF，不猜测orgId。目录时间是来源声称，不能替实际阅读原件；全文/报告期/可持续盈利仍由Quick核查。源明确401/403/429后停止该提供方全部关键词，持久 `source_hold` 阻止次日自动重试，解除需具体授权和已核前提；不是更换网址重试。一般失败保留良好页面，不写零公告。已成功保管的同日提交只读恢复，不重新抓取。

每个新查询表示增加 `rejected_row_details`：只保留从1起算的原 `page_number`、页内 `row_number` 和固定集合内的 `reason_code`。它记录各行首次失败的验证，不保存拒绝行正文／标题／原URL，不执行或回显来源中的指令；未知异常文字统一为 `DISCOVERY_ROW_INVALID`。诊断数量等于原 `rejected_rows`，仍受每类4页×30行的原范围限制；合格行、总行数、重复、分页缺口和来源停止语义不变。新字段随原 `SourceInput`、独立 `read-source` 和精确版本保管读取，不建另一份诊断库。

历史 `announcement-discovery-v1` 表示可以没有该字段；缺失表示当时未记录逐行原因，不能补成空数组来宣称零拒绝，原计数和未知原因均保留。新增诊断不改变公告 `version_id` 或已有版本比较，不改写旧SourceRef和同日意图。旧窗口无效行的原始内容未保留时，不能用新代码、另一次采集或合成行补造原因；只在后续正常已授权窗口产生新诊断。[共享CNINFO传输](../sources/cninfo-catalog.md#错误保留维护)的声明长度／部分正文校验仍保留前页，并沿原有限错误报告失败。

原始获准元数据摘取经 `SourceInput` 同仓保管；固定 `source_id`/`submission_id` 为 `announcement-discovery-YYYYMMDD`，日期为该次窗口终点；实际取得时钟单列，历史重放不能认证当时已知。保留 `source-input.json` 与 `intent.json` 原意图，CAS/未知写不改键重抓，按现役只读对账恢复。30天Actions产物不是长期备份。只允许main、首次attempt与独立对应mainCI；每次核实际原生可见性，Public 沿[已授权的现仓公开合同](../engineering/public-runtime.md)核同一目标与来源范围，不把 Public 填成 Private。数据不在PR里。主线CI失败时停止采集，不绕过。

## 2. Hosted Quick 的真实执行合同

独立 ChatGPT Task 原参考周期为工作日18:50（Asia/Singapore）。它不是仅转述已有结果的Brief；该委托授权其在有界范围自主进行公开资料研究和现有records保存。旧Kernel任务不改。

1. 实际解析本仓main与records，读取AGENTS、本页、[研究入口](../research/entry.md)和方法。先用下文 `latest-hosted-handoff(kind=quick)`、正常研究目录及当天 `hosted-quick-YYYYMMDD` 案例检查已存结果，防止“研究已存但交接保存失败”时重复研究。存在原成果就恢复，交接失败只补原引用；未知写先原意图对账。没有命中与读取失败不同。
2. 先按日期取得 `submissions/announcement-discovery-YYYYMMDD.json` 的真实 `result_path`，读取该records快照下manifest/表示或用确切SourceRef `read-source`。必要时恢复前三日最后可用及最新失败，不能拿旧成功覆盖当前缺口。不得主动重放本采集器、已有hold或其他来源workflow。
3. 同时从**不含预选公司名/代码**的有界公开搜索、交易所/发行人披露和已存产业/新闻发现新问题，不以预选名单、涨跌幅、已有研究或机械Brief入选为门。候选出现后才按它的真实公司名/代码下钻。记录本次实际搜索/时间范围和已读材料；没有公告索引能力时明确有限搜索，不宣称全面扫描。
4. 重要公告打开发行人/交易所原件。可合理使用公开新闻发现线索，但转载同上游不算独立证据；无法取得原件则限定归因，不编造。预告≠最终报表，累计≠单季，净利≠扣非≠经营现金；算单季必须同口径累计减累计，环比必须有真实前一季度。金额、期间、公告时间、取得时间、币种、单位及修订逐项保留。同比增长不自动叫超预期，也不从原价推公司行为总收益。
5. 用实际现役 `prepare` 返回的Quick方法实施研究。先存获准来源为 `SourceInput`（仅必要事实/自写摘录和原件链接，不无权复制全文）；再 `checkpoint`。每日独立探索case建议 `hosted-quick-YYYYMMDD`，revision `r1`；同一案例追加按原前驱和方法。原研究有用就引用和增量，不重做全文。输出为何现在值得看、经济传导、最强替代解释/反证、决定性未知及重开条件；可零候选/部分成果，不硬凑Full。
6. 完成研究独立 `resume` 读回，保存下文Quick引用交接并 `read-hosted-handoff` 再读。方法与保存成功不代经济人审。未经用户委托，不启动Full、Odds、投资决定、仓位或交易；不改工程、来源计划、旧任务和通知。

## 3. 原研究宿主上的四个窄操作

全部仍是 `research-runtime.yml` / `ref=main`，沿[原宿主](../research/runtime.md)的 `code_sha`、JSON字符串 `payload`、写前 `expected_head` 与稳定 `submission_id`。每次核实际原生可见性；现仓 Public 沿固定且可撤销的[部署授权](../engineering/public-runtime.md)执行，不再要求切回 Private，也不将历史公开marker当授权。宿主按本次实际来源、研究、Brief、交接和 Watch 相依核 `sources/research/brief/handoff/watch` 中所需的具名范围；已存基线和被采用引用也不能隐藏读取相依。授权关闭、缺失或目标不匹配时按原资格停止相应操作，不能用调用者自填字段补回部署授权。后台I/O没有调用AI，任务会话才是研究者。

| operation | payload | 效果 |
|---|---|---|
| `save-hosted-handoff` | `{"document": D, "rights_confirmed": true}` | 校验引用/正文及采用结果绑定，在原Artifact owner保存，独立新读操作再验证 |
| `reconcile-hosted-handoff` | 原完整payload与submission_id | 原意图只读恢复；未找到不写 |
| `read-hosted-handoff` | `{"reference": <确切Artifact BlobRef>}` | 返回原交接、实际研究正文和 v2 采用的实际 Watch 检查；相应采用哈希不一致拒绝 |
| `latest-hosted-handoff` | `{"kind":"quick"或"brief","as_of":<带时区当前时钟>,"limit":1..3}` | 固定快照在最近7天实际保存版本中恢复；损坏/未来截止返回缺口，不用任务last_run_time替正文 |

Quick交接D（占位符不是调用参数）：

```json
{"schema":"hosted-ai-handoff-v1","kind":"quick","execution_date":"YYYY-MM-DD","cutoff":"带时区实际研究截止","scope":"实际无预选发现/已读范围","gaps":["真实限制"],"research":{"repository":"auguspp/Decision-OS-2","commit":"实际40位commit","path":"实际research/cases/.../record.json","sha256":"实际64位摘要"}}
```

AI Brief交接D：`schema/kind=brief/execution_date/cutoff/scope/gaps` 加 `body`（给用户的同一最终中文正文）、`adoptions` 与 `baseline`。`adoptions` 最多8项，每项为 `reference`（实际Quick/Full BlobRef）、`body_sha256`（完整实际研究正文UTF-8 SHA256）和 `used_change`（采用何项增量/反证）；空列表明确未采用研究。`baseline` 是上一份实际AI Brief的ArtifactRef或null，不能拿Quick冒充晚报。单正文32KiB/交接48KiB，沿宿主60KiB外层及现役无损打包；不截断关键反证。

新 Brief 使用 `schema=hosted-ai-handoff-v2`，在上述字段之外增加 `watch_adoptions`；只有 `kind=brief` 支持 v2，Quick 交接仍为 v1。原 v1 Brief 的读取与原意图恢复保持。普通 `adoptions` 继续允许 Quick，这不使 Quick 成为默认 Watch 对象。

`watch_adoptions` 最多8项，可以为空。每项只含以下字段，采用[原 Watch 证据 reader](research-agenda.md#关联已存-watch-条件证据)读取实际结果，不接受模型自行摘写的检查替代物：

| 字段 | 实际绑定 |
|---|---|
| `reference` | 该 Watch 冻结绑定的确切 Full ResearchRevision 引用。 |
| `question_id` | 价格采用可为 null；经营证据采用须对应此 v3 Watch 的 `tracking.question_links` 原 Full 问题，不能任挑同公司问题。 |
| `kind` | `check` 或 `activity`，分别选择独立保存的检查或活动内保存的检查。 |
| `check_ref` | 所选已存检查的确切 ArtifactRef。 |
| `result_sha256` | 实际 reader 返回的结果摘要，与本次采用结果一致；不是研究正文或引用的摘要。 |
| `used_change` | 本次采用的具体变化或限制；这段原文必须出现在同一 `body` 中。 |

采用时重新读取检查、冻结 Watch、原 Full 及其相依，核 v3 Full Watch 准入、问题链接、结果摘要和 `cutoff`。保存检查的记录时钟不得晚于本期截止；后来保管／读回时间不冒充历史已知。缺失、损坏、未来检查或虚构问题不能用自由文本绕过。一个已存检查及其同一问题的引用别名不构成另一条采用；读回、基线比较和 latest 恢复仍读取原实际字节，不以 task 的启动时间或保存回执代替。

固定artifact_id分别为 `hosted-quick-YYYYMMDD` 和 `ai-brief-YYYYMMDD`；一天同类一份，原正文冲突拒绝而非另起随机名。v1/v2 共用原 `ai-brief-YYYYMMDD` 名字族；切换 schema 不为同日另开一份。相同意图重放不再写；补充/更正沿研究原版本合同并进入下一期，历史正文不覆盖。最新发现只针对这两个名字族，不宣称扫描任意存储中的所有材料，也不改变原机械Brief的latest-saved含义。Hosted 交接保存不自动进入 Reading Room 的 `news-edition-*` 面板；各自仍经原读取入口消费。

## 4. AI Brief 的接续责任

保留已建立每天19:15的OS2 AI Brief。先读上一份真实AI Brief与最近1–3份Quick交接/正文，并核原研究目录的可能未交接新成果。优先采用自上次实际Brief以来新事实、实质修订和仍影响结论的反证；比较具体事实/问题而非整家公司。晚于前版Brief cutoff的材料不得倒灌；迟到Quick下一份继续采用。当天未取得Quick只表示此时未取得，不推断未运行/失败，更不能等候或盲重跑。

Watch 先接续[Full 后持续观察](../research/full-watch.md)的实际检查：一类是同一 Odds 的价格条件，另一类是原 Full 尚未解决且影响 Odds 的重大问题。读取当前需要比较的检查和上一份 Brief 的真实采用，不只看首次触发 `notices`；`review-required`、关键资料不足或持续条件也可能值得交付。相同 true 不冒充新触发，未知不写成未触发。没有合格 Odds 时保留具名问题的 evidence-only 跟踪，不造价位；缺实际检查则说明缺口，不声称已经自动运行或采用。Brief 采用不停止、退役或重置 Watch，也不自动登记新的 Watch。

正文由ChatGPT编辑，不轮选来源、不强行每日重复几家公司；重要已核新公告可以先作为未分析观察呈现。独立公开核验标其外部来源与时间，不虚构已入records。完成后用同一作者正文保存AI Brief并读回；源请求/仓库写入限制仅按本页授权的研究/交接I/O扩展，不授予工程、收费模型或采集调度权。保存失败仍交付有用正文，但明确没有完整持久基线。

## 5. 验收与停止

合成测试保护无ticker查询、分页/拒绝、同源去重、版本更正、原件定位、真实Git同意图保存、同日冲突、实际研究正文绑定、v3 Watch 检查及问题采用绑定、公开范围与撤销、未来/损坏/迟到与只读语义。它们不证明供应商可用或经济事实。

实际验收必须分列：官方有限目录实取与保管；已知案例回放；不预选名字的公开发现及实际AI Quick；独立研究/交接读回；AI Brief 的研究采用及 Full Watch 实际检查采用；保存后的独立 `read-hosted-handoff` 与 `latest-hosted-handoff`；定时配置；未来自然运行。实现或配置成功不证明未来自然运行，也不证明真实 Watch 已被 Brief 采用。亨通光电是用户已点名案例，不放生产白名单、不把重放叫盲测，不因一次发现自报稳定召回率。长期自然连续性、来源覆盖和用户阅读效果仍以未来真实结果为准。

新来源被拒或不可用时，保留范围/记录并让AI在独立获准公开资料上做有价值工作；这不将失败来源改为健康。底层未来故障不能被提示词或Issue Closed遮住。维护/退出时同时处置本专用采集workflow、观察/交接操作及其测试，原SourceInput、ResearchRevision、Artifact和旧Brief读取合同保留。
