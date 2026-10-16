# Watch 活动选择、停用恢复与历史摘要

责任 #60。沿用[有限 Watch](odds-watch.md)、既有 Artifact、提交日志与原生 Git 条件提交；没有第二资料库、自动调度器或通知队列。本组补的是实际活动选择，而非把报告里的观察项自动变成监控任务。

## 一条 Watch 的正常使用

先按原入口冻结 Watch，再 `change-odds-watch-activity` 登记，初始状态必为 **paused**。原 Watch 的启用声明仍然有效，但登记并不自动开始检查。明确的 `resume` 将其列为 active；`pause` 暂停；`retire` 永久结束该条活动。恢复不能延长原窗口、重开已触发后结束的 Watch、改变目标日、概率、股数或原启用声明。更改这些条件应沿旧合同产生新 Watch，不是恢复旧条目。

每个完整冻结 Watch 内容摘要仅有一个活动指针，即使调用者以另一份等内容附件引用再次登记也不能分叉成两个活动。创建时的确切 Watch 引用作为注册引用继续使用。它不是公司级屏蔽：同公司另一条不同条件 Watch 不受影响。

操作由已获授权的会话显式调用。`reason` 记录实际依据，不是经认证的 Human 签字；registered、active、触发、发送、已阅读和投资接受仍是不同事实。该活动状态的控制范围是本系统执行入口，不是对拥有仓库写权限的人或任意本地程序的强权限隔离。

## 输入与命令

变更输入：

```text
{watch_ref: 确切原Watch引用,
 expected_event: 预期活动事件引用（首次登记为null）, 
 action: register | resume | pause | retire,
 at: 带时区的本次时间, reason: 真实依据,
 seed_check: null或登记时显式承接的原检查引用}
```

`seed_check` 仅首次 register 可用，必须经原 Reader 完整读回/重演、属于同一 Watch 且时间不晚于登记。承接的是最后确定结果、已触发状态和原检查时钟；历史计数不伪造为已纳入新链。不能在后继变更中任意换 seed 重置触发状态。

检查输入：

```text
{watch_ref: 注册时的确切Watch引用, expected_event: 当前活动事件引用,
 checked_at: 带时区的明确检查时间, records_snapshot: 本次读取的当前records完整commit,
 input: 原价格/市场输入或研究版本模式的null, review_reasons: []}
```

这里没有调用者可选的 `previous_check`，由活动历史自动接续；检查时快照必须仍是当前记录分支。活动变更和检查均使用乐观并发条件，陈旧 expected_event 或快照明确冲突，不做“最后写入者获胜”。请求结果未知时沿原 submission_id 对账，不更换编号补发。

```bash
# publication.json: {"change": <变更>, "rights_confirmed": true, "rights_basis": "本次实际公开权利"}
decision-os store --binding approved.json change-odds-watch-activity --input publication.json --submission-id original-change
# watch-ref.json 是冻结Watch的原回执；返回值中的reference为活动事件引用
decision-os store --binding readonly.json current-odds-watch-activity --ref watch-ref.json
decision-os store --binding readonly.json check-odds-watch-activity --input check.json
# check-publication.json: {"check": <检查>, "rights_confirmed": true, "rights_basis": "实际依据"}
decision-os store --binding approved.json save-odds-watch-activity-check --input check-publication.json --submission-id original-check
decision-os store --binding readonly.json read-odds-watch-activity --ref event-ref.json
# list.json: {"snapshot": null或确切commit, "after": null或上页next_after, "limit": 1至8, "at": 明确时刻}
decision-os store --binding readonly.json list-odds-watch-activities --input list.json
```

概率用途可沿原 CLI 提供匹配 `--registry`；没有概率的版本/条件活动不要求 Registry。原 research-runtime 提供同名操作及 `reconcile-odds-watch-activity`、`reconcile-odds-watch-activity-check` 两个只读对账操作。写入只有 change/save 两种，权限、code_sha、expected_head、60KiB传输字段及独立读回约束不变。未获真实公开权利不得填 true。

## 暂停、恢复与比较结果

暂停或退役不获取价格，不产生一次 false 检查，不清除上次触发。恢复沿用上次检查；暂停期间不是连续观测，后继 true 会保留 `intervening-checks-unknown`，不会因重新调用而伪装首次。活动中的正式 false 才能使后续再次 true 成为重新触发。

未 active 时不能保存新的管理检查；列表仍显示 paused/retired，并保留旧结果。检查的 not-due、cannot-determine、review-required、first-trigger、continued-trigger 等继续采用原 Watch 算术。达到原结束条件会记录 ended 并退役活动，但日程到期不证明经营事件已发生。

管理检查先判断已经知道的目标日/现金日期，即使没有新价格也不能把已经到期的合同一直留为“缺价”。原独立 Watch v1 的历史投影保持不变；新行为只属于管理活动的独立版本，不事后重写旧结果。

已经登记的 Watch，原独立 `check-odds-watch`/新 `save-odds-watch-check` 执行入口明确要求改用活动入口，不能绕开暂停。独立旧检查的历史读回和相同提交意图的恢复仍可使用，不会因为后来登记或退役而丢失回执。底层纯计算原语不充当活动授权检查。

## 原子保管与独立重演

事件内容仍为原 ResearchArtifacts JSON，使用 `odds-watch-activity-record-v1`；唯一新增可变路径为 `research/watch-activities/<watch_sha256>/current.json`。一次原生 CAS 同时提交事件的 manifest/content、活动指针及原提交日志，不能发生“检查已采纳、指针还没更新”的半次交付。该指针只选择 Watch 活动，不是第二个研究 current。

指针目标必须在同一次原生提交中首次保存，指针回退到另一份仍完整的旧事件也会被拒绝。新来源/其他 Watch 的并行提交不被覆盖；冲突后重新读取并明确接续原输入，不隐式重试。

读回按确切前驱、各原 records 快照和时刻线性重演生命周期及检查，摘要计数由重演结果生成，不接受调用者提供的“已触发/已完成”缓存。内部 hash 自洽但状态或计数错误仍拒绝。管理检查复用原观察与投影，不再为每一个旧事件重复调用一整条独立检查链。

`summary` 给出管理事件数、检查/实际执行次数、首次与持续触发、未满足、无法判断、须复核、到期及未到等计数和末次检查。范围固定为 **本管理链**；导入 seed 的旧链仅作控制状态，不计入这些计数。read 返回原事件列表和当前活动指针对照，当前信息不参与改写历史结果。列表按确切快照分页，只含已登记 Watch，不能将“未登记”当作已拒绝的研究机会。

## 范围、性能与仍未交付

复用同次8MiB字节/来源有界读取和原512次远端请求预算；独立 reader 从空缓存开始，前后仍核可变绑定。**一条管理链最多128个事件**，旧独立检查仍最多32个前驱；复杂概率/长历史可能先触及原512预算，届时明确失败，不截断前驱后报首次、不提高预算求绿。列表每页1—8条，复杂项也受原预算约束。

这已经允许明确跨会话选择和暂停/恢复，但不是无限长期运行或认证汇总压缩。更长历史需要另行交付带原证据的分段/检查点方案；本组没有“信任调用者摘要跳过历史”的快捷路径。失去原 Registry 的概率历史仍需恢复原匹配资源；不能强套新登记。

原 Git bundle 恢复覆盖新路径与引用。普通 store audit 验证指针和附件字节/引用关系，明确不代替活动语义重演；活动 read 才执行相应计算。未增加缓存服务、HTTP提供方、依赖、Secret、通知、自动重开Full、账户或Decision。旧记录不迁移，旧默认参数不引入。

维护归#60；退役时同步应用、命令、宿主选项、路径及验证责任，已存事件和历史读取支持不能随代码回退被删除。正式源代码交付、真实采用、经济解释和人工接受分别验收。

达到128事件总上限前，最后一个事件位置专门留给`retire`；其他动作会明确拒绝，不允许用满检查配额后继续宣称活动可用。复杂历史也可能先触及原远端预算，届时失败不冒称暂停或退役已经保存。
