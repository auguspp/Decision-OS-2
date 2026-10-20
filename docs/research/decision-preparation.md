# 决策准备、判断历史与多期限复核

责任 #147 第三批、目标设计 §16.3。本组把已保存研究接成 AI 决策准备包，复用原研究读取/SourceRef、`ResearchArtifacts`、原 BlobRef、工作底稿及同仓 records。准备包固定目标证券和表达、主要假设与最强反证、合理替代选项、需人裁定事项、原有估值/Odds/价格上下文及仍未知的部分，并保留经济机制、催化、失效条件与多期限复核窗口。保存和读取均不创建正式 Decision，不确认人工接受，不执行投资或交易。

## 一份准备包的用途

输入必须选择一个确切 Quick/Full 研究版本，而非公司名称或自动跟随 current。输出带回原委托、研究正文摘要、被选择的原问题/结论/反证、全研究原有未解决事项、未选择问题及未选择的未解决挑战数量。选择较小范围不能消除整份研究的限制。

有工作底稿时，问题 ID 必须来自该版本，原结论和挑战中的证据索引自动纳入读取。作者不能通过删除自己的 citation 列表隐藏这些原依据。没有结构化底稿的普通研究仍可准备，但 `question_ids` 必须为空，显示“原问题未结构化记录”；程序不从正文编造问题、概率、催化或投资决定。

`stance` 是 AI 作者的判断声明，只允许 `researching`、`awaiting-evidence`、`candidate-for-human-review`、`not-pursued`。候选可审阅不代表研究充分、人工接受或已形成决定；暂不推进也不代表用户拒绝。机制、预期变化和失效条件均由实际作者提供并说明依据，程序验证引用与时间，不自动评价经济真伪。

准备包至少比较两种不同的备选行动，保留各自支持理由、不利理由及未知。`participate / wait / decline / continue-research` 分别表示供讨论的参与、等待、放弃、继续研究，不能据此派生买卖意图、用户选择或成交。主要假设、最强反证与需人裁定事项不能为空。目标证券可以明确未知，目标表达仍须说明；没有账户、总资产、持仓和风险约束上下文，本能力不计算组合适配、可承受亏损或仓位建议。

## 有界输入合同

`decision-preparation-v1` 使用以下字段，未知字段拒绝，避免把 `human_confirmation` 或交易指令藏入准备输入：

| 字段 | 含义 |
|---|---|
| `preparation_id` | 此条准备历史的具名标识；标识本身不选择全局最新版本 |
| `research_ref` | 原研究 record.json 的完整确切 BlobRef |
| `prepared_at` | 带时区的作者声明时间，不晚于实际调用时刻 |
| `predecessor` | 初稿为 null；修订为上一份准备包的确切 ArtifactRef |
| `question_ids` | 本次准备选择的原问题 ID，最多32个 |
| `stance` / `rationale` | 上述有限判断和具体理由 |
| `target` | `{security,expression}`；证券使用原估值合同 `{code,share_class}` 或 null，表达为非空说明 |
| `alternatives` | 2—8项，至少两种不同备选行动；每项保留支持、不利理由及未知 |
| `human_questions` | 1—16项仍需用户裁定的具体事项，不能填成已经确认 |
| `major_assumptions` / `strongest_counterarguments` | 各1—16项作者明确提出的主要假设和最强反证；原研究挑战另外原样继承 |
| `data_gaps` | 0—32项作者已知资料缺口；系统派生的缺估值/Odds/价格/目标信息另外追加 |
| `analysis_context` | `{valuation_ref,odds}`；均可为 null；有值时必须是原现役合同保存的确切 ArtifactRef |
| `source_indices` | 该确切研究 sources 的索引，不接受任意新来源替换原研究 |
| `horizons` | 1—12项具名期限，可以分别观察短、中、长期机制 |
| `revision_note` / `limitations` | 本版修改说明及非空限制，不能用输入完整代签事实充分 |

每个替代选项含 `alternative_id/choice/description/case_for/case_against/unknowns`，`choice` 为上述四种之一，ID 不得重复。`unknowns` 最多16条，不要求每个选项都配概率。`human_questions`、主要假设和最强反证都是作者提供的内容；字段完整不证明比较合理或结论成立。

每个期限含 `horizon_id/label/question_ids/mechanism/expected_change/source_indices/window_start/window_end/review_at/catalysts/invalidation/limitations`。窗口起止和复核时点均有时区；窗口不得倒置。复核日可以已经过去，过去就显示待复核，不悄悄滚到下个日期。

每个 `catalysts` 元素含 `event_id/description/expected_at/source_indices`，日期未知用 null；每个 `invalidation` 元素含 `condition_id/description/source_indices`。每项最多16条，至少一条失效条件。期限的问题 ID 必须在本次选择范围内；证据索引必须属于冻结研究。时间窗口和日期是作者假设，引用可读并不证明来源支持这些假设。

原研究及被采用来源的保存时间不得晚于 `prepared_at`；来源取得/发布时钟有值时，按其原精度检查不晚于准备时间。日期只有日精度时不推断盘中可用性。`prepared_at` 仍是作者声明，不证明该草稿当时已经存在；原生 ArtifactRef/commit 固定本次保管版本。该通路不是历史交易 PIT 认证，也不能给事后写成的判断补一张事前预测凭据。

## 可选的原估值、Odds 与价格上下文

不填数值上下文时使用 `{"valuation_ref":null,"odds":null}`。返回的估值、Odds 和价格都明确为 null，`data_gaps` 追加对应尚未建立项，准备包保持 `partial`。这仍能承载长期机制研究和人工待裁定问题，不会触发估值、取价或概率生成。

`valuation_ref` 有值时，由现役 `ConditionalOdds.read_spec` 重新核对原 frozen valuation artifact、确切研究和来源。`odds` 有值时必须为 `{"kind":"conditional"|"probability","reference":<原确切 ArtifactRef>}`，使用现役 `ConditionalOdds.read` 或 `ProbabilityOdds.read` 重算核对原保存结果；概率结果另外恢复原 assessment、概率依据、覆盖假设与风险政策。不是这些原合同的任意 JSON artifact 不能代用。

原结果引用的估值自动恢复；同时显式选择估值时，两份引用必须完全一致。估值/Odds 必须绑定准备包同一确切研究版本，目标证券及股类也必须与原估值一致。同一股票或研究前驱不足以代替这个精确关联。估值和概率判断采用的研究证据索引自动纳入准备包来源读取，调用者不能通过缩短手填引文列表去掉它们。

原估值日、概率判断时刻、价格时刻及原计算评价时刻不得晚于 `prepared_at`，保留各自原精度。价格只来自原 Odds 结果；`price_context` 保留原价格、币种、证券、时刻、原行情及概率资格。`declared-reference` 或早期未取得市场资格的结果继续标明 `qualified-market-not-established`，不升级成合格行情；原有合格结果的资格也只适用于原评价时刻。本次准备不刷新、不重新认证准备时点行情，更不显示当前可执行价格。没有独立事前保管证明时，ArtifactRef 仍只固定所读版本，不给作者日期补签历史可用性。

原注册表仍由现役 Odds 接口使用。保留市场证据的结果必须使用匹配 `RelayRegistry`；底层概率 Reader 要求明确的受信注册表，不能从来源内容选择目录或降级成“未建立”后继续成功。正常命令及宿主默认加载所检代码的 `docs/sources/relay-registry.json`，主 CLI 也可通过 `--registry` 指定明确的受信本地目录；保存与独立读回使用同一注册表。直接调用底层服务时仍须提供匹配目录。整个过程中不请求任何新行情来源；来源或 artifact 拒绝、损坏、预算耗尽仍是操作失败，不能伪装成正常空值。

## 多期限状态与前驱历史

读取时显式提供 `at`，不得早于该准备包声明时间或晚于实际调用时刻。每个期限分别派生：

| 状态 | 代表什么 |
|---|---|
| `not-started / in-window / window-ended` | 相对原声明窗口的位置 |
| `not-due / review-due` | 是否已到原复核时点 |
| 催化 `undated / not-due / date-reached-needs-evidence` | 日期是否明确、是否到期；结果仍为 `not-evaluated` |
| 机制/失效 `not-evaluated` | 需要实际后续证据与研究评价，时间经过不能自动填兑现或失效 |

日期投影不保存新检查、不开 Watch、不更新议程、不自动重开 Full，也不产生新 Brief。#60 的多条件 Watch 与活动生命周期仍由其 owner 维护；本组不建立第二套条件监控器。

后继必须保留同一 `preparation_id` 和研究 case，声明时间严格递增。研究可以仍是同一版本；换研究时必须沿现有研究的确切 `predecessor` 链实际恢复到上一准备包的原研究，不能用同一股票、后来 current 或相似正文冒充祖先。路径中缺件、错误案例、循环和超限均拒绝，不跳过中间版本。

读取准备包时沿每个确切前驱恢复到初稿，重新读取原研究、必要证据及选择的原数值上下文，重建每个文档并逐字段核对。内部 JSON/hash 自洽但伪造原挑战、状态或变化摘要仍会失败。历史保留原理由、目标、假设/反证、替代选项、人工待裁定项、数据缺口、原数值引用和所有旧期限；准备内容改变会单独显示 `preparation_context_changed`。新稿删除的问题/期限明确是“移出本稿范围”，不等于已解决、催化兑现或已失效。

没有新增准备包 current 指针。返回的 `history_scope` 只承诺选中引用的完整前驱链，不冒充发现所有草稿或全局最新判断；`current_use` 只另外比较研究 current，不修改原文档和摘要。不同选择或替代方案必须保留各自确切引用，不靠同名覆盖。

## 保管、读取与接线

命令使用现役 store 和 research-runtime 适配，具体引用来自真实回执，不从示例生成生产引用：

```bash
decision-os store --binding readonly.json prepare-decision --input preparation.json
decision-os store --binding approved.json save-decision-preparation --input publication.json --submission-id original-preparation-intent
decision-os store --binding readonly.json read-decision-preparation --input read.json
```

`prepare-decision` 的输入是上表对象；保存包装为 `{preparation,rights_confirmed:true,rights_basis}`，这些字段只承载已存在的保管授权声明，不新增第三方公开权利或正式 Decision 权限。`read.json` 为 `{reference:<原回执的确切引用>,at:<明确评价时刻>}`。调用者自己保存的 payload 不证明该工作已被研究、Brief 或人工采用。

底层仍保存为原 `research/artifacts/.../manifest.json + content.bin`，原稳定 submission/CAS 处理同意图重放和不同内容冲突。独立 reader 重新打开整条前驱与原证据；读回失败时保留真实保存回执，不把它改成“未写”。原研究 current、正文和来源均不修改。

宿主 `reconcile-decision-preparation` 沿相同 payload/submission_id 和原只读对账 backend 恢复未知写；对账不是另一次写授权。缺原意图继续明确失败，不能换编号补发。共享宿主/CLI/Reading Room 接入此模块时，写操作仅保存，准备/读取/对账维持原读权限；使用同一 store、原预算与既有可见性约束。

四个准备命令的远端出口必须在创建输出目录前确认原生 private，并在服务与独立读回之后通过同一原 adapter 的末次可见性检查。原 public-origin marker/私有升级许可不能代替当前可见性。只读命令的输入引用也在最后检查后才写入输出；保存命令在已确认私有后、任何可能的 CAS 前保留原 payload/submission/expected-head，供未知写按原意图恢复。公共保管声明不能打开这个私有命令出口，原绑定和历史锚点保持。

Reading Room 使用可选 `decision_preparation: {reference, at}` 计划，读取确切已存准备包及完整前驱；原私有宿主无需谎报公开许可。选择公共 Room 出口时仍须分别通过原研究和来源的明确导出声明，准备包的保存权利不会自动传递。无准备计划时不调用本组 reader，旧 Room 的形状和页面仍正常。

页面只读取结果并提供原引用、历史和读取请求，没有“确认”“批准”“下单”按钮。暂未读取的研究、外部原文和剩余限制仍显示。保存准备包不等于发布研究、触发通知或启动长期监控。

共享接线使用 `decision_preparation_commands.READS / WRITES / RECONCILES`，分别是 `prepare-decision, read-decision-preparation`、`save-decision-preparation` 和只读对账 `reconcile-decision-preparation`。`DecisionPreparation(store, registry=None, *, clock=None)`、`perform(store, operation, payload, submission_id="", registry=None)` 和 `verify_fresh(receipt, reader_factory, registry=None)` 接受原 store/registry，不新建数据或来源客户端。页面导出 `CSS`、`JS`，`renderDecisionPreparation()` 消费 `D.decision_preparation` 中 `{status,view,open_request}`，由共享页面提供 `decision-preparation-content` 容器与原安全文本辅助函数。

## 复用、验证与退出

使用现役 JSON/Artifact/Store、原工作底稿和 Python 标准库 datetime；不引入新的状态机、日历调度、金融模型或数据主库。与现役相邻版本 Review 分工明确：Review 比较真实研究修订/Brief 引文采用，本组记录确切研究之上的作者准备历史，不能用准备稿代替原研究修订。

输入上限128KiB，单文档384KiB，结果768KiB，本组累计纳入重建的已读材料上限8MiB；准备历史及研究祖先各最多32版本。原估值/Odds Reader 的内部原文读取继续受既有文件和同一远端请求预算约束，8MiB 不另授原 Reader 读取额度。复杂历史可能先达到字节或现役远端请求上限，届时显式失败，不截断前驱、信任调用者汇总或重置预算。来源缺失、损坏、访问失败和预算耗尽立即结束相应操作，不用第二客户端继续拼出成功结果。

上述领域输入上限不提高实际 workflow 的 60 KiB 完整请求限制；经过原 payload 编码后的实际请求仍须在该上限内。超过宿主上限不分批伪装为同一准备包，不换意图编号重复写。

新增真实 Git 回归保护原问题/反证/来源继承、两版历史、删项不等于解决、研究祖先/案例/时钟拒绝、日期与结果分离、稳定意图、伪造摘要拒绝、独立读回、边界及失败后无继续读。准备包专属回归同时覆盖明确目标/替代选项/人工问题、缺数值时 null 与 partial、原条件/概率结果重放、原行情资格不升级、错误目标/研究/估值引用拒绝和注册表要求。合成工程验证不代签真实经济机制、原始来源认证、事前预测质量或人工接受。

退出本能力时同步退出新 domain/application/commands、宿主/CLI/页面接线及专属测试。保留已存 Artifact 和历史版本读取责任，不回退或删除用户资料。正式 Decision 的受信独立确认、防重放和改稿失效仍属于其独立责任，不能由本组文件字段补签。
