# 研究方法与工作底稿入口

责任归 [#7](https://github.com/auguspp/Decision-OS-2/issues/7)。本能力把现有[只读接续](continuation.md)与[版本保管](../architecture/versioned-store.md)接到研究者的实际工作入口，不是后台模型、任务队列、自动Full或新的研究真源。工程可调用、真实研究质量、运行凭据与正常产品使用分别验收；本页不记录实时任务进度。

## 两个操作，不再造一个研究执行器

`prepare-research` 接受调用方明确选定的案例、Quick/Full、原委托、关键问题和可选确切前驱，返回有版本/摘要的方法、原研究上下文及相同来源分页。它不发出模型调用、不抓取来源、不保存、不授予执行权限。`--case-id` 只选一次当前；`--ref` 可准备确切历史版本，不偷偷换最新。后继写入仍需原前驱有效。

`checkpoint-research` 接受这次请求与研究者的实质成果，逐题核格式/引用边界，再转为原 `RevisionInput` 交给 `VersionedStore.submit_revision`。资料、研究正文、当前指针、意图日志仍只有原保管owner。它不额外提交一张“任务完成表”，也不改变原来源/研究存储schema、研究接受或Decision。

直接Full不要求存在Quick。Quick按同一scope继续，只有调用方明确选择Full并说明范围变化时才形成该类成果；没有模型输出能通过进度JSON中的`kind/actor/token/write_enabled`自授权。可信调用方仍须核真实委托，代码不能鉴定一句范围说明是否真的得到用户授权。

## 可安装的方法

`decision_kernel.methods.research.research_method(kind)` 提供独立Quick/Full方法ID、确定性SHA256与自足的指导文本；随Python包交付，不在运行时读取工程checkout或网上提示词。普通dataclass、JSON与SHA256足够表达此有限工作记录，不需要第三方Agent/工作流框架、数据库或新依赖。

方法提炼自本仓[研究案例](../decision-os-2.0-design.md#chapter-11)、[Quick](../decision-os-2.0-design.md#chapter-12)、[Full](../decision-os-2.0-design.md#chapter-13)及固定旧版本[full-research-method-v3](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/full-research-method-v3.md)的适用语义：委托与关键问题、证据→机制→经济结果、实质挑战、现金/归属与价值、逐题剩余和合理停止。旧研究schema、固定旧案例/阶段排期、生产状态、股价/概率或逐轮人工批准不迁入。新版指导不代签旧方法效果，也不重新验证旧GitHub可行性。

没有独立审阅时写自查；不能靠字段齐全认证经济分析。算术/单位检查仍须落到真实计算，本入口不执行研究者提交的代码，不实现Odds或宣称全文数值已自动审计。

## 请求与工作底稿

`ResearchRequest` 由调用方选定：`case_id`、`kind`、`mandate`、`questions`（1—32项，稳定question_id与原问题文本）、可选`predecessor`、`scope_change_reason`。同案后继保留所有原问题ID及原文；新增问题可以追加，旧问题被证据重构/取消时以`reframed`保留解释，不直接删掉。委托或kind改变必须留下明确范围变化原因和前驱，不能靠生成的底稿悄悄改目标。

`ResearchProgress` 保留自由分析正文`body`、逐题`findings`、`challenges`、实际使用的`SourceRef`列表、限制、来源变更说明、停止声明与理由。`findings.evidence`和`challenges.evidence`是该成果`sources`列表的**零基索引**，仅定位确切来源，不认证其支持的经济含义。每个原问题恰好交代一次；来源剔除/选择范围替换须说明原因，旧引用仍保存在旧修订，不删除资料。

未决性质分别是`acquired-unanalysed`、`disclosed-unverified`、`currently-unidentifiable`、`future-event`和`interrupted`。所有未决问题都需具体下一行动/重开条件；数据缺口不填0，不从来源失败推断市场安静。

`stop`只有研究者声明的`continue`、`blocked`、`concluded`。重大已取得未分析、已披露未核或中断问题，以及未处置挑战，不可同时声明concluded；可改为部分成果正常保存，不必编造答案。当前无法识别或必须等待未来的变量可在限定结论下保留。没有挑战项时，concluded需明确`challenge_note`解释检查情况；不是要求虚构挑战、规定轮数或把该说明当实质反证。material标记和各状态仍是作者声明，系统不能凭这些自评证明工作真的充分。

工作记录作为**同一research.md的规范JSON附录**保存，`research-workpaper-v1`仅是方法底稿表达，不是第二存储schema/状态库。原`mandate`、kind、SourceRef、前驱、method字段仍直接存入既有记录；读回校验附录与记录一致。原普通研究正文继续能由`resume-research`读取；没有底稿的问题列表不会被凭空反推，首次采用此入口应提供明确请求。解析不执行文本，坏附录不降级成“已恢复”。

## 引用即固定回执与审计

`checkpoint-research` 的回执新增 `reference_fixing`（`reference-fixing-report-v1`，`tier=listing`）：逐条列出本次成果引用的 SourceRef（`manifest.repository/commit/path/sha256` 与 selection）。这一档**不读源**——SourceRef 本身内容寻址，已钉死被引用的字节；写入路径再读一次会让每次判断的源请求翻倍。所以它回答"固定了哪些切片"，**不**回答"切片是否覆盖了某个价格日期"。

事后审计用只读命令 `verify-reference-fixing --ref <已存 BlobRef>`：逐条 `read_source`，给出实际字节数/摘要，以及按固定键名抽出的 `observed_dates`/`observed_securities`/`observed_markers`，触碰 `MAX_NODES`/`MAX_VALUES`/`MAX_DEPTH` 即置 `harvest_truncated: true`。它不取源、不写、不切换 `current` 指针。

两档都固定携带 `coverage_assertion: "not-declared-in-existing-fields"`：现有研究字段没有可机读的"本次判断用了哪个标的、哪一天的价格"声明，因此"日期落在覆盖内"目前**不可判定**；需要声明字段与版本升级后才谈得上校验。详见 `docs/architecture/reference-fixing-triggers.md` 6.2。

## 合成工程调用

先按README安装。下例只使用本地合成目录，**不是授权创建任何真实研究任务**。`request.json`最小示例：

```json
{"case_id":"demo","kind":"full","mandate":"合成工程示例，不是投资研究","questions":[{"question_id":"q1","text":"哪些资料能区分两个解释？"}]}
```

`progress.json`保存诚实的部分成果；无已保管来源时明确限制，不伪造SourceRef：

```json
{"body":"已列出需要核对的合成问题；没有完成取证。","findings":[{"question_id":"q1","status":"open","finding":"尚未取得可核证据","remaining_kind":"interrupted","next_action":"恢复获准资料读取后继续"}],"stop":"blocked","stop_reason":"仅保存合成工程底稿","limitations":["没有真实投资分析或完整来源"]}
```

```bash
decision-os store --path /tmp/dos2-demo.git init-synthetic
decision-os store --path /tmp/dos2-demo.git prepare-research --request request.json
decision-os store --path /tmp/dos2-demo.git checkpoint-research --request request.json --input progress.json --submission-id demo-r1 --revision-id r1
decision-os store --path /tmp/dos2-demo.git prepare-research --case-id demo
```

准备结果中的`request`可由宿主直接保留并作为下一提交请求，它包含真实前驱与原问题；`previous_context.next_cursor`继续交给原`resume-research --cursor`读取，不另设分页协议。保存回执中的`reference`仍能由`read-revision`/`resume-research`读取。相同意图重试沿同一submission_id、revision_id、请求、成果和方法版本恢复原回执，不先因当前指针已前进就拒绝；内容或方法改变必须对账，不能换键掩盖未知写。

## 权限、限制与退出

本入口只组织方法和保存成果，研究者/宿主仍负责有边界的自主取证、分析、计算和挑战。prepare返回`not-granted-by-preparation`，保存返回`research_assessment=not-performed`、`human_acceptance=not-confirmed`；没有自动研究、通知、定时、正式Decision或交易。

本地仍限合成材料。远端仍使用调用方可信绑定、独立运行凭据及实际允许范围；只读绑定在读取凭据之前拒绝checkpoint。配置与library校验不是服务端角色隔离；原`submit-revision`兼容入口仍能存自由研究，不能宣传所有写入都强制采用本方法。#6现有工程运维入口不可借用为任意研究上传工具，本工程PR不部署writer、不写真实records。

输入沿既有2MiB上限，问题32项、挑战/来源各64项；溢出明确拒绝，不静默丢字段。限制全文留在附录，原unresolved只是有界定位摘要。来源损坏/缺失不能伪称引用成功；新部分成果可解释撤除相关来源并保留旧前驱。源码回退不撤回已保存成果，不执行清理。

测试归属：`test_research_workpaper.py`保护方法载荷/逐题与停止矛盾/原样恢复；`test_research_workflow.py`保护真实合成Git上的提交、前驱、幂等、失败与CLI边界；`test_distribution.py`在**原同一次冷安装**中运行prepare→阶段保存→恢复→实质修订→再次读回。替换本方法入口时处理这些专属方法/载荷消费者，保留原版本保管与引用保护；不因Issue关闭删测试。真实Full、正向Quick、市场/产业接线和Brief采用仍按#7原范围单独验收。
