# Watch 有限显式执行与待交付记录

本组补 #60 的执行接续工程：冻结一个有界计划，选择当前实际时间槽，沿原 `WatchActivity` 保存检查，再生成可独立重开的待交付意图。计划保存不会创建日程、注册或恢复 Watch。执行输入由调用方明确提供；此模块不调用资料提供方，不选择发送渠道、收件人或通知服务。

## 已交付范围与尚未接入

| 层次 | 本组行为 | 证明边界 |
|---|---|---|
| 有限执行工程 | 计划最多 32 槽；稳定槽身份；预览；保留原输入的预约；原 Activity 检查；结果与意图保存；中断对账 | 默认只读；每个显式保存操作最多一次原 records CAS |
| 交付状态工程 | 待交付意图、一次尝试预约、未知结果、确切回执材料、反馈材料、尝试前取消 | 全部是保管记录；没有 sender，也没有回调假装发送 |
| 真实通知接入 | 尚无 transport、channel、recipient、服务端鉴权回执契约 | 须先选择具体原生渠道，才能实现发送与可信回执适配 |
| 实际启用 | 尚无 cron、后台任务或来源请求；没有真实通知 | 要在明确的 Watch、渠道、收件人及停止条件授权内启用；工程合并不是运行授权 |

新模块复用原 `ResearchArtifacts`、`VersionedStore` 的 `submissions/` 幂等日志及 CAS；生命周期、来源判读、检查连续性和历史容量仍归 `WatchActivity`。原成熟原件保管和有限快照读取足以承载本组，没有引入队列产品、新数据库、可变索引或通用调度框架。长历史组的 `watch_read_scope` 与 `verify_event_parent` 提供可信原生提交读取，沿同一 API 预算和可见性检查，没有预算重置或失败后换通道。

## 计划与时间槽

`WatchExecution.freeze` 接收原保管授权封套：

```json
{
  "plan": {
    "schema": "watch-execution-plan-v1",
    "plan_id": "explicit-watch-run",
    "watch_ref": {"repository": "<原仓>", "commit": "<40 位 SHA>", "path": "<原 Watch manifest>", "sha256": "<64 位摘要>"},
    "starts_at": "2026-10-20T04:00:00Z",
    "interval_seconds": 3600,
    "slot_count": 8,
    "late_tolerance_seconds": 60,
    "notice_policy": "first-trigger-per-condition",
    "effect": "record-only",
    "limitations": ["显式有限执行；真实通知渠道尚未选择"]
  },
  "rights_confirmed": true,
  "rights_basis": "本次原范围保管依据"
}
```

`notice_policy` 必填。首版仅接受 `first-trigger-per-condition`，不会默认加入复核、未知、连续触发或周期摘要提醒。间隔不得小于原 Watch 最小间隔，所有槽窗口必须在原 Watch 有效窗内。日期统一成 UTC，整型上下界和最终日期溢出在保存前检查。

`plan_sha256` 来自规范化计划字节；`slot_key` 来自该摘要及从零开始的槽序号。运行名称、重试次数和当前时刻不进入槽身份。三个固定提交键是 `watch-slot-<slot_key>`、`watch-check-<slot_key>`、`watch-result-<slot_key>`。调用方不能换键重开同槽。

每槽的 `due_at` 为计划开始时刻加序号乘间隔；`deadline` 为到点时刻加容许延迟。槽间窗口不重叠。在期限后选择该槽会显示 `missed`；计划终点后显示 `plan-ended`。不把漏跑槽改成过去发生过的观察，不自动回填来源。过期预约仍可读取，也仍可对账已经保存的原检查。

## 原命令入口与保存阶段

原 CLI 与 `research-runtime` 手动宿主提供下列有限阶段，后文列出对应 Python API。示例中的引用均来自原保存回执，不能用当前 commit 重造别名推进时点。

| API | 输入 | 输出与副作用 |
|---|---|---|
| `freeze(payload, submission_id=..., reconcile=False)` | 上述 `plan` 授权封套 | 冻结计划 Artifact；最多一次 CAS |
| `read_plan(ref)` | 确切计划引用 | 重读原 Watch，重新核计划摘要、窗、间隔 |
| `preview(query)` | `plan_ref, at, input, review_reasons` | 槽选择、原活动状态、稳定键、已有槽或下一显式请求；不写入 |
| `reserve(payload, submission_id=None, reconcile=False)` | `execution=query` 的原授权封套；可传预览给出的固定键 | 保存输入、预约真实开始时点、原活动前驱及 CAS 前快照；最多一次 CAS |
| `read_slot(ref)` | 确切槽引用 | 核原计划、输入、时间窗、活动前驱、稳定日志及真实发布父提交 |
| `check({"slot_ref": ref}, submission_id=None, reconcile=False)` | 已保存槽 | 先对账原检查日志；若确实未保存且尚有效，调用原 Activity 保存一次检查 |
| `finish({"slot_ref": ref}, submission_id=None, reconcile=False)` | 已保存槽 | 从已保存且精确重演的检查生成执行结果及意图；最多一次 CAS |
| `read_result(ref)` | 确切执行结果引用 | 重读槽和原检查，逐字节重新生成结果与所有通知意图 |
| `status(slot_ref)` | 确切槽引用 | 当前接点、原检查/结果引用、下一显式请求及固定期望 head；不写入 |
| `advance(slot_ref, mode="preview")` | 槽引用；默认预览 | 默认只返回状态。明确 `save` 也只推进一次 `check` 或 `finish`，不循环写入 |

### 命令映射

每个写操作继续要求原 `submission_id`；宿主还核调用者检查过的 `code_sha`、原 `expected_head`、原保管权利和独立读回。`save-odds-watch-execution-check` 与 `finish-odds-watch-execution` 只接受已保存的 `slot_ref`，沿其既有保管授权继续，不能借该入口更改输入、时点或扩大效果。

检查回执同时保留规范化的原 `slot_ref`。该阶段的独立新 reader 沿 `check(..., reconcile=True)` 重核原槽、固定 journal、原检查及其第一父/原子选择资格，逐字段对照回执后才标 verified；缺件时只读失败。旧 v1 Activity 历史仍可读取，不单独承担新执行采用资格的签收。

| 用途 | 原 CLI／手动宿主操作 | 输入 |
|---|---|---|
| 冻结计划 | `freeze-odds-watch-execution` | `plan` 原授权封套 |
| 查询当前可执行槽 | `preview-odds-watch-execution` | `plan_ref, at, input, review_reasons` |
| 预约本槽 | `reserve-odds-watch-execution` | `execution` 查询的原授权封套；提交键取自预览 |
| 保存实际检查 | `save-odds-watch-execution-check` | `{slot_ref}`；使用预览给出的 check 提交键 |
| 保存结果和意图 | `finish-odds-watch-execution` | `{slot_ref}`；使用预览给出的 finish 提交键 |
| 独立读回 | `read-odds-watch-execution-plan/slot/result` | CLI `--ref` 的原引用；三个完整命令各对应一个后缀 |
| 查看接续点 | `status-odds-watch-execution` | CLI `--ref` 的原槽引用 |
| 改变待交付状态 | `change-odds-watch-delivery` | `change` 原授权封套 |
| 当前交付状态 | `current-odds-watch-delivery` | `execution_ref, notice_id, snapshot`，snapshot 可为 null |
| 独立读交付事件 | `read-odds-watch-delivery` | CLI `--ref` 的原交付事件引用 |

手动宿主另提供只读的 `reconcile-odds-watch-execution-plan/slot/check/result` 与 `reconcile-odds-watch-delivery`，保留原输入及原提交键。缺少原意图时明确返回未找到且不写入。只读宿主没有 records 写权限；默认预览也不隐含推进保存阶段。本组没有修改宿主定时触发。

查询示例：

```python
query = {
    "plan_ref": plan_ref,
    "at": actual_selection_time,
    "input": original_watch_input,
    "review_reasons": [],
}
preview = execution.preview(query)
slot = execution.reserve({
    "execution": query,
    "rights_confirmed": True,
    "rights_basis": original_custody_basis,
})
check = execution.check({"slot_ref": slot["reference"]})
result = execution.finish({"slot_ref": slot["reference"]})
```

这三个保存调用是三个独立操作，每次都使用原 journal 与真实 CAS。宿主不得把它们藏在一个无界循环里，也不得通过重新创建 API 会话增加原操作预算。预览不会运行这段保存示例。

预约的 `reserved_at` 是预约时实际开始的时点，`query.at` 是选择时点，两者都不会被借用为后来观察发生的时间。两者必须选中记录中完全相同的槽序号、到点时刻和截止时刻，不能用当前时刻预约未来槽。第一次实际检查在读取稳定日志、确认原意图从未保存、快照未变化且槽未过期后，才读取当前时钟形成原 `checked_at`；它不得早于该槽 `due_at` 或原 `reserved_at`。检查的 `records_snapshot` 永远等于**原预约提交**，不是预约的一个较新引用别名。

若 #10 等其他合法写入推进 records head，尚未保存的检查明确返回 `WATCH_EXECUTION_SLOT_SNAPSHOT_CHANGED`。状态指向“等待下一个实际槽”，不能刷新旧槽快照再试。原 Activity 仍执行原选中前驱、active、pause/retire、检查时钟和历史容量要求。

## 中断接续与真实性

| 已知接点 | 接续 |
|---|---|
| 预约前 | 预览当前实际槽；尚未选择任何观察时间 |
| 预约保存后中断 | 用固定槽提交键查原日志；原输入和 `reserved_at` 不随重试改变 |
| 检查保存可能成功、回执丢失 | 先读 `watch-check-<slot_key>` 原日志，再重读原 Activity 事件，核原完整 intent 摘要及槽绑定；返回原 `checked_at` |
| 原日志明确缺席但写效果仍未知 | 只读 reconcile 返回 `SUBMISSION_NOT_FOUND_NO_WRITE`；不构造新时间，也不自动重发 |
| 原检查已保存，意图尚未保存 | `status` 返回 `check-saved-intents-pending`；`finish` 只复用原检查，不重观察 |
| 意图保存回执丢失 | 原 `watch-result-<slot_key>` 对账；同内容只返回原回执 |
| later pause/retire | 已提交旧检查仍可对账；尚未提交的旧槽不能借新快照绕过生命周期 |

只有当前缺少 journal 且原生第一父历史从未保存该固定路径，才能表示“尚未找到”。当前 journal 缺件但历史曾存在，或者原 journal 被改写、删除后重建，都属于完整性错误；不能重建 `reserved_at` 或让同一个原输入对账到另一条记录。原 journal 完整时，它仍可定位原确切历史回执，即使今天的家族缺件；新的状态变更另须通过当前完整前缀门槛。预算、可见性、正文损坏、未知写等操作错误直接传播，不被当作内容缺失继续下一 reader。

槽、原检查及执行结果的原提交必须是所声明快照的真实单父子，并属于实际 records ref 的第一父主链；manifest、正文、固定 journal 必须在同提交首次出现，其固定路径在父快照之前也不能已有被删除的历史。执行结果另保留生成时的 `snapshot_before`，沿原 finish journal 重演同一次结果保管。仅有自洽 JSON、后来 merge 可达的旧侧支或较旧切面中的同一个活动指针不构成有效原保管。一个槽的新 commit 别名会归回原日志提交，不推进固定快照。

旧 v1 活动进入新执行器时，完整有效的 active 前缀还须证明它在本次操作的实际切面中被原生 current 选中。预览和预约核当前 head，槽读取核原 `snapshot_before`，原检查重开核其原提交；每处都重核指针的第一父选择，不能靠后来合并旧 active 树绕过主链退役。已经显式迁入 v2 的活动使用同一原历史 reader，槽、检查、结果和新 reader 接续保留全部迁入前历史。

## 每个条件的通知意图

执行结果的 `notices` 只从原检查的 `first-trigger` 派生。v1 标量结果最多一个意图；v2 遍历冻结顺序的 `condition_results`，每个首次触发条件各有意图。组合已处于 `continued-trigger` 时，新跨过另一价格界线仍生成该条件的意图。未知、复核和暂停缺口不会清除原触发连续性；持续触发不会再生成同一轮首次触发。

`notice_id` 绑定原 Watch 摘要、确切检查的仓库/路径/内容摘要、条件 ID 和明确策略，不绑定计划运行名或当前 commit 别名。明确不满足后再次满足是新的原首次触发事件，拥有不同意图；这与同一保存结果的重开去重不同。

意图保留原 Watch 和检查引用、条件/组合状态、实际 `checked_at`、逐条件依据与来源证据、限制，并固定 `destination=null`、`state=pending-transport-selection`。这些资料是待评审的发送输入，不是已经发送的通知，不保证经营条件的经济判断已经证实。

## 交付记录 API 与状态

`WatchDelivery.change` 仍使用原保管授权封套。`change` 严格字段如下：

```python
change = {
    "execution_ref": exact_execution_result,
    "notice_id": selected_notice_id,
    "expected_event": previous_delivery_event_or_none,
    "action": "reserve-attempt",
    "at": actual_action_time,
    "receipt_ref": None,
    "note": "本次显式提交说明",
}
delivery.change({
    "change": change, "rights_confirmed": True, "rights_basis": original_custody_basis,
}, submission_id=stable_original_submission)
```

`current(execution_ref, notice_id, snapshot=None)` 从一个固定 records 切面重演该意图的最多 16 个交付事件；首次无事件时 `reference` 与 `state` 为 `None`，原意图仍明确待选渠道。`read(event_ref)` 独立重开原事件和前驱。`change(..., reconcile=True)` 只读原键；没有原日志时绝不新建事件。

| action | 前提 | 派生状态 |
|---|---|---|
| `reserve-attempt` | 尚未预约尝试、未取消 | `attempt-reserved-no-sender`；只表明占住这次尝试，不证明发送器运行 |
| `mark-unknown` | 已预约、还未标为 unknown | `send-outcome-unknown`；后续回执声明不会自动释放重试 |
| `record-receipt` | 已预约且给出确切 ArtifactRef | `receipt-recorded-unverified`；保留原字节核验和未鉴权声明 |
| `record-feedback` | 已预约且给出确切 ArtifactRef | `feedback-recorded-unverified`；不将提交的“已读”文本冒认为用户身份回执 |
| `cancel` | 尚未预约尝试 | `cancelled-before-attempt`；不声称撤销了未知外部效果 |

`reserve-attempt` 只能一次。没有 retry/reset/超时释放接口，unknown 不能凭新键、提交“成功”文本或另一回执自动重发。真实 adapter 接入前，`transport_delivery_verified`、`user_read_verified`、`automatic_retry_allowed` 均固定为 false。

每个意图的事件使用已知 Artifact 家族 `watch-delivery-<notice_id>`，在同一真实 CAS 下检查前驱。没有无锁指针，不按文件名或最大时间选择“最新”。读取要求整个有限家族是唯一线性链，每个事件声明快照含有且仅含真实前驱，其原提交也是该快照的真实单父子；分叉、空洞、重置和自行修改的摘要均拒绝。

当前 tree 中的空家族也不等于从未尝试。读取会核同一有限家族在原生第一父历史中引入的所有 manifest 和正文，在所读快照仍完整存在；整个家族、末尾事件或单独正文缺件都会失败，不能清除原 unknown 后再次预约。校验按所读快照进行，故原确切交付事件及其完整历史前缀仍可独立读取，不受后来的缺件冒称已修复。家族仍受最多 16 个事件和对应原件路径的限制，超限不截断为正常历史。

回执正文只由原 `ResearchArtifacts` 保管。其原引用提交必须在事件快照之前可达，且 manifest 和正文都必须在该切面具有原字节、摘要和大小。正文可能是任意受支持原件；这里不执行、渲染或把文本中的送达/已读字样升级成认证事实。来源可读和私有保管授权也不自动提供公开再分发权。

## 验证与接续门槛

专项使用真实隔离 synthetic GitStore，覆盖有限窗、默认无写、每阶段一次 CAS、丢回执及跨状态对账、相同槽改输入冲突、原快照变化、v2 每条件首次触发、unknown 连续性、原 Activity 容量、不可伪造结果、操作错误传播、交付 CAS 竞争、未验证回执/反馈与重试拒绝。

对抗例还构造：在后续提交中伪称较旧的槽/交付快照；实际父关系不匹配；未来槽被当前时刻提前预约；稳定 journal 删除后同槽重新选择时钟；未知交付家族全部或尾部缺件；回执 manifest/正文在当前切面变更；一个回执仅位于未来 side parent、在 live head 已可达，却不在原交付事件快照可达。读取均须拒绝。没有向真实渠道发送，也没有把注入 callback 成功当成生产通知证据。

宿主接线与原单次 cold-wheel 验证覆盖实际保存、全新 reader、保留的每条件意图及未发送状态；确切运行和正式 main 证据保留在本组 PR/#60 原检查点。真实有限计划自然窗、真实通知服务端回执、用户身份反馈及外发去重仍未证实。下一接线先明确一项产品选择：哪一个已有授权的原生 transport/channel/recipient 承担 Watch 通知；随后才补其鉴权、未知发送查询与确切回执映射，并按原权限实际启用。
