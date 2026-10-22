# 已完成新闻采集的单次交接与只读对账

责任：#13 的有限新闻贯通，复用现役采集原件、SourceInput/Store、#48 滚动窗口和 News Desk。本文记录宿主的输入、写入和恢复合同；真实交付、独立读回与生产验收以任务 Issue 的原生运行／原件回执为准，本文和工程测试不代签生产验收、持续采集或用户采用。

入口为手动工作流 [`news-handoff.yml`](../../.github/workflows/news-handoff.yml)，只有 `deliver` 与 `reconcile`，默认 `reconcile`。宿主自身也必须是本仓库 `main` 上的 `workflow_dispatch`、attempt 1；不接受 rerun。原 [`news-intake.yml`](../../.github/workflows/news-intake.yml)、新闻保管和 [`news-rolling.yml`](../../.github/workflows/news-rolling.yml) 的合法入口继续保留。

## 1. 先选已完成的原生采集

本入口只接受一个已完成的本仓库 `main` 手动 `news-intake.yml` attempt 1，原生结论可为 `success` 或 `failure`。必须同时给出原 `source_run`、精确 `artifact_id` 和 ZIP 的 SHA-256。宿主通过原 `verify_native/source_from_archive` 核 run、artifact、仓库 ID、原 capture code、名称 `dos2-news-<source_run>-1`、原生摘要、大小与 ZIP 单一成员 `news-window.json`。

本次范围是 NewsNow 七源 `cls / wallstreetcn / fastbull / jin10 / mktnews / gelonghui / thepaper` 内的非空、不重复子集。每个来源 outcome 必须带 `news-capture-receipt-v1`，请求明确为该 NewsNow 来源；成功观察必须属于这些已选来源。原部分失败、全部失败及其回执都保留，不能以另一来源成功覆盖失败，也不因原 Actions 结论失败而重新抓取。

没有回执的旧批次、Relay 或 RSS 原件不能从本入口开始新交接；原有读取、保管和滚动能力按各自合同继续使用。交接不请求 NewsNow、Relay、RSS 或文章 URL，不改变采集频率，不新增 schedule、来源凭据或后台队列。

## 2. 固定输入与稳定身份

`deliver` 需要 `code_sha` 为已检查的当前完整 main SHA，`expected_head` 为本次入场前检查的完整 records SHA。`plan` 最多 8 KiB，恰好五个字段；下面仅示意形状，ID、摘要与时点必须替换为实际原件证据：

```json
{"source_run":"123","artifact_id":"456","artifact_sha256":"<原ZIP的64位小写SHA-256>","as_of":"2026-10-16T12:00:00Z","predecessor":null}
```

ID 为正十进制字符串。`as_of` 明确含时区、不晚于当前时间，且不能早于所选观察或采集回执的接收／失败结束时间。它固定为原交接截止，不在对账时取新时钟。`predecessor` 可为 null；非空时必须给出同仓完整 SourceRef：manifest 的 repository/commit/path/sha256 及完整 `selection={kind:all,start:null,end:null,columns:[]}`。不接受 latest、短 SHA 或只给 source_id。

同一 `source_run=N` 永远使用下列身份：

| 阶段 | submission_id | SourceInput.source_id |
|---|---|---|
| 冻结意图 | `news-handoff-N-intent` | `news-handoff-N-intent` |
| 原采集批次 | `news-window-N` | `news-window-N` |
| 派生滚动窗口 | `news-handoff-N-window` | `news-roll-news-handoff-N-window` |

`dos2-news-handoff-intent-v1` 经现有 SourceInput 保管，不含新闻正文。它冻结五个 plan 字段、capture 的 `source_code_sha`、原 `expected_head`、完整源输入的 `batch_source_sha256`、原已保管批次的 `prior_batch_reference` 或 null，以及首次交接的 `handoff_code_sha/handoff_run_id`。既有批次引用的路径必须对应同一 source ID 和源输入摘要。

当前只读宿主可以使用新核验的 main code，但报告保留原 `original_handoff_code_sha/original_handoff_run_id`，与本次 `code_sha/run_id` 分开记录。代码升级或重建页面不改写原生产者身份，也不证明按旧代码完全复演。

## 3. deliver 的阶段与独立读回

宿主先核原件、来源范围、原入场 head 和已有原批次，再沿原 `research_runtime.run_command`、`VersionedStore` 和 CAS 执行各阶段。新阶段的条件写依次绑定原入场 head、已确认意图 commit、已确认批次 commit；不会为了跨过并发变更而自动刷新 expected head。各写入是独立提交，整条链不宣称一个原子事务。

1. 保存冻结意图，随后通过原 `read-source` 独立核对完整 SourceInput。
2. 原批次尚未保存时，使用原 `news-window-N` 键保管，再独立读回。若此前已保管同一批次，先核全部 SourceInput 字节相同，只调用 `reconcile-source` 并要求返回原完整 Ref；此阶段 `expected_head` 留空，因为本次意图 commit 不是旧 custody 的入场 head，不能伪造它。
3. 用原滚动聚合器恢复固定前驱并纳入本批次，经共享 `index_source` 工厂生成原格式窗口，沿原 writer 保存，再独立读回。已有批次被复用时，窗口写入绑定本次意图 commit；否则绑定新批次 commit。
4. 另建未继承前阶段来源缓存的 Store reader，重新读取已保管索引、恢复确切源批次并核逻辑摘要，再生成 News Desk。

全新三阶段最多确认三次 records 写入；原批次已保存时最多两次。未知、失败、并发冲突或当前权限拒绝会停止后续业务阶段。历史来源失败仍是资料内容；当前 Store 的 403/429、请求额度耗尽或绑定失败是终止条件，不能被滚动缺口语义吞掉后继续保存。

最终 Desk 固定 `kind=adhoc`、`cutoff=原as_of`、`baseline=null`、`attention=[]`、`max_items=12`。成功输出包括 `view.json`、完整恢复后的 `reading.json` 首页、`desk-plan.json`、`edition.json`、`news-brief.md` 与 `news-desk.html`。页面可以阅读原完整观察；简报采用数量不代表来源覆盖数量。本入口不保存 edition Artifact，不执行 AI／研究，不写反馈或 Attention，也不发送通知。

## 4. reconcile 只恢复原交接

另开一次 attempt 1 的手动 `reconcile`，`code_sha` 使用当时重新检查的当前 main SHA，`expected_head` 必须为空；plan 只能包含原运行 ID：

```json
{"source_run":"123"}
```

对账从固定 `submissions/news-handoff-N-intent.json` 开始，以 journal 的原变更 commit 取 SourceRef，再经原 Store 验 manifest、表示字节与提交意图摘要。它不扫描最新窗口、不替换前驱或 cutoff，不下载原 archive，不回访来源；各业务 GitHub 请求均为 GET，工作流作业仅有 contents/actions 读权限。

意图、原批次和窗口均已存在并相符时，原 `reconcile-source` 和独立 `read-source` 恢复各阶段，然后从原窗口生成同一固定计划的页面。records 后续推进不改变原 Ref。只有固定 journal 缺失可视为缺阶段；损坏、摘要冲突、缺源或权限拒绝均保留错误。

缺少 intent、batch 或 window 时返回 `reconciliation-incomplete`、`missing_stage` 与具体原因，**不补写下一阶段**，也不输出完成页面。已有 intent 后再次 `deliver` 会在下载 archive 或新增写入前拒绝。新交接入口不具备“从中断处继续写”的授权或实现。

未知写必须保留原 `handoff-report.json`、出错阶段的 `inputs.json/report.json`、原 payload、submission 和 expected head，先按原 `source_run` 做只读对账。底层逐阶段 `reconcile-source` 仍以原保存输入为证据；不能 rerun 写作业、换键、换 cutoff、换前驱、重抓原件或把另一次交接当成原未知结果的解释。`confirmed_writes` 只计已收到确认的阶段；未知 ACK 不证明写入未发生。缺阶段的后续处置须先核原阶段证据并明确责任，不由本宿主自动续写。

## 5. 原生资格、公开授权和请求预算

入口、每次新条件写之前、最终正文／页面释放之前都复核当前 main 与本宿主原生身份。main 必须仍等于本次 `code_sha`；对该 SHA 的实际 main push CI 只接受最新原生 run，要求 completed/success、attempt 1，不能跳过较新失败去取旧绿。有限 CI 列表必须完整，核验不完整时拒绝。

公开资格复用 [`config/public-runtime.json`](../../config/public-runtime.json) 和 [`#164 的部署授权`](https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674)，核当前 public 仓库和原 records binding，并分别要求 `sources/brief/reading` 范围。每次门禁重新读取授权，须与入场依据一致；撤销、可见性改变、main 移动或最终绑定重检失败时不释放最终正文与 HTML。该部署授权不新增源采集、反馈、研究执行或人工投资决定的权利。

整个操作共用一个 `GitHubAPI` 对象和原 **512 次请求预算**。计数包含原生 main/run/CI/artifact JSON、Store 读写、各阶段原 writer 和独立读取，不按阶段换 API 重开额度。来源缓存仅在各自一次明确读取内复用已验证完整 SourceRef，沿原 8 MiB 边界；最终独立读取不继承前阶段缓存。额度拒绝的尝试可能计入报告计数，但不会因此获得第 513 次实际请求额度。

deliver 另经既有隔离 `gh` transport，至多发起一次固定 artifact archive GET；它不计入上述 JSON/Store 计数，没有下载重试。`archive_requests=1` 表示已开始该次下载尝试；只有 loader 返回字节后 `archive_downloads` 才置为 1，仍需后续摘要与归档内容验证，不能单凭下载数字宣称原件合格。reconcile 两项均为 0。`source_http_requests` 始终为 0。

工作流在成功或失败后保留阶段输出，原生附件名为 `dos2-news-handoff-<本次run_id>-1`，期限 30 天。deliver 留存原 native metadata、ZIP、plan 和各阶段命令；reconcile 留存原意图恢复的 plan 与对账结果。短期附件不能替代 records 的确切 SourceRef、源版本保留与独立备份。

## 6. 覆盖、验收和维护退出

原 `source_outcomes/coverage/losses`、局部失败、全失败、前驱缺口和原观察时钟随恢复结果保留。0 条观察不能解释为市场没有新闻；24 小时窗口只说明明确所选观察批次的范围。原媒体时间仍为未核声明，元数据保管不等于原文保管、事实核验或独立事件数。报告保持 `continuous_capture_verified=false`、`complete_news_coverage=false`、`human_acceptance=not-confirmed`；成功生成页面不改变这些边界。

维护源码为 [`application/news_handoff.py`](../../src/decision_kernel/application/news_handoff.py)、[`interfaces/news_handoff_cli.py`](../../src/decision_kernel/interfaces/news_handoff_cli.py) 和新手动 workflow；共享窗口工厂仍由原 `news_rolling_index` 维护。对应领域／宿主测试负责真 Git、原 GitHubAPI/Store/CAS、失败和未知 ACK、固定引用与独立恢复；安装验证扩展现役唯一 cold-wheel consumer。测试合成原件和局部测试结果不得充当真实生产、最终 main CI 或人工验收。

验收须分别记录最终提交的完整 CI、实际 main CI、获准真实 capture 的一次交付、独立只读对账及最终页面核验，不能以某一层通过替代其他层。实际进度和完成状态见 [#13 的原生证据与验收记录](https://github.com/auguspp/Decision-OS-2/issues/13)。持续生产、自然周期覆盖、用户接受和实质研究另按原责任验收。

退出时先确保历史 intent、原批次、窗口及完整引用仍有相容读取路径，再同步撤下专属 workflow、宿主、领域入口、专属测试与本文链接。原采集、原 rolling/Desk、共享工厂与 Store 的现役消费者共同维护；不能删除源版本、失败历史、原 CAS／未知写保护或另建新闻池来完成退役。
