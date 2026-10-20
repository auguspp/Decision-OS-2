# Watch 活动的有界分段历史

责任 #60。接续[原活动生命周期](watch-activity.md)和[多条件 Watch](watch-conditions.md)。需要较长活动历史时，调用者显式选择 `watch-activity-history-v2`；原 v1 事件、默认登记、128 事件上限及最后一个位置用于退役的规则继续保留。

分段保存的是确切原事件引用。读回仍从首条登记重演全部前缀，逐项恢复检查、暂停间隔、终态和摘要，没有可供调用者提交的“已验证摘要”或跳过历史的种子。活动格式与 Watch 条件格式分开：v1/v2 条件都可以显式采用分段活动。

## 显式选择与迁入

新活动仍走 `change-odds-watch-activity`。原登记输入只增加一个字段：

```json
{
  "watch_ref": "此处填冻结 Watch 的完整确切引用对象",
  "expected_event": null,
  "action": "register",
  "at": "2026-10-20T09:00:00+08:00",
  "reason": "此处填写本次实际登记依据",
  "seed_check": null,
  "history_version": "watch-activity-history-v2"
}
```

示意中的 `watch_ref` 必须替换为原回执的完整引用对象；日期和依据也必须属于本次操作。写入沿原 `{change, rights_confirmed, rights_basis}` 封装和原 `submission_id`、记录分支 `expected_head`。登记后仍为 paused，只有明确 resume 才进入 active。

已经存在的 v1 活动可以提交 `action: migrate`，同样携带 `history_version`，并将 `expected_event` 指向当前原事件，`seed_check` 固定为 null。系统先完整验证 v1 前缀，再把该前缀封为第 0 段，与第 1 段第一条迁入事件在同次提交中保存。active/paused 状态、上次原检查引用、每条件连续性及摘要计数原样承接；迁入本身只是一个活动变更事件。

迁入必须发生在原可执行容量耗尽之前：v1 已有事件数必须小于 127。已退役、已过原窗口、已到目标日、达到触发后结束条件或原 Watch 未启用时都拒绝迁入。迁入不能救回耗尽的旧链，也不能重新选择 seed 或延长 Watch 条件。后续 pause/resume/retire/check 自动沿登记的活动格式继续，不再重复传 `history_version`。

## 同一次原生 CAS 的内容

普通事件继续使用原 `odds-watch-activity-record-v1` 保管封装；显式分段时内层事件为 `watch-activity-event-v2`，增加段序号、段内位置和前段引用。原唯一活动选择路径仍为 `research/watch-activities/<watch_sha256>/current.json`。

每段最多 128 条事件。当当前段已有 128 条时，下一次合法活动操作在原同一次 CAS 中写入六个文件：

| 内容 | 原存储位置和作用 |
|---|---|
| 新事件 manifest/content | 原 ResearchArtifacts，包含实际输入、前驱和重算结果 |
| 封段 manifest/content | 原 ResearchArtifacts，保存前段有序的确切事件列表及前段连接 |
| 活动 current | 指向本次新事件 |
| 原 submission 日志 | 绑定本次原输入和回执，支持丢回执后的只读对账 |

首次新段事件在提交前无法知道自身 commit，因此它以 `same-commit` 的封段 manifest 路径和 SHA256 连接同次封段；读者把该连接锚到新事件的实际原提交。随后事件使用封段的完整确切引用。读者核对封段及事件原件在同次原提交引入、原 current 在该提交改写，并核原提交确实是该事件声明快照的单父子提交。仅有摘要或内部 hash 自洽不足以通过。

分段原事件还必须属于实际 records 分支沿第一父提交形成的原始主链。校验以可信本地 records ref 或本次原生获取的 records ref 为根，调用者选择一个较早快照不会把那个快照变成另一条主链。旧 active 侧支上的合法检查，即使后来作为 merge 的第二父变得可达，也不能成为覆盖主链退役的后继。原 binding anchor 的可达性与 marker 门禁保持独立；这里没有把历史 anchor 改成必须位于第一父链。

封段不会将 `previous_check` 清零，也不会把上一段末次 true 变成首次触发。暂停产生的未知间隔同样跨段保留。原 expected_event 必须跨段指向真实前驱；并发修改、原指针回退、旧快照分叉、乱序/重复/缺失引用、外来 Watch 或损坏封段都明确失败。

同一 Watch 的 current 曾存在而现在缺件，不能作为从未登记的证据。显式 v2 登记及 v1 迁入重演要核首次登记前该固定路径确实没有原历史；当前选择还核原路径的最后变更与原事件一致。正常 current、活动写入和独立检查入口会识别该路径曾采用 v2 的原件：删除后报 `WATCH_HISTORY_POINTER_MISSING`，后来换成 v1 指针报 `WATCH_HISTORY_FORMAT_DOWNGRADE`。因此删除 current、回到登记前的 tree 或换用默认 v1 登记，都不能重开已采用 v2 的旧链；原纯 v1 历史重演格式与算法继续保留。

## 历史读回与宿主入口

```bash
decision-os store --binding readonly.json read-odds-watch-activity --ref event-ref.json
decision-os store --binding readonly.json read-odds-watch-activity-segment --ref segment-ref.json
```

`read-odds-watch-activity` 对旧 v1 返回原结构。对分段活动，其 `history` 只列当前段，另外返回 `sealed_segments` 的确切引用、段号、种类和首末事件，以及 `history_validation` 的完整已验证事件数和段范围。`summary_trusted: false` 表明摘要是读者从原前缀重演得到的。当前活动对照仍只是当前提示，不改变历史；供议程/Brief 重演时仍可关闭该提示。

`read-odds-watch-activity-segment` 读取确切封段，返回本段正序事件列表、截至本段末尾的重演摘要、封段发布事件引用，以及完整前缀加封段发布事件的验证范围。因此 `verified_events` 比本段历史长度大是正常的；它包含必须验证的更早前缀和发布事件。不能用该数字声称本段有更多检查。

原 CLI 和 `research-runtime` 手动宿主均有同名只读命令；后者 payload 为 `{ "reference": <完整封段引用> }`。登记、迁入及管理检查仍使用原两个写操作和原两个 reconcile 操作，原权限、code_sha、expected_head、传输字段上限及全新读者验证继续生效。未知写入沿原输入和原 submission_id 对账，不更换编号再次发布封段。

## 有界原件读取

远端分段重演使用现有原生 GitSnapshot 读取完整 records 分支。原 GitHubStore 负责进入和退出时的绑定、可达性及可见性检查，整个操作继续使用同一个有 512 次上限的 GitHubAPI；公开起源仓库已经合法转私有时，保留同一 backend 的单向私有状态，不换 reader 绕过权限变化。退出还要求实际 records head 与本次取得的原生主链根一致；读取期间 head 前进也会使本次输出明确失败，不能只凭原提交仍通过第二父可达而返回。临时原生副本只在本次操作存在，凭据仍只进入既有安全 Git 子进程环境。

结构最多 32 段、每段 128 事件，最后一段的最后一个位置只允许 retire。结构上限不承诺全部输入都能运行到 4,096 条：原生获取的 64 MiB 检查、32,768 tree 项、原件/读取缓存/宿主输出上限、原 API 预算及运行时限都可能先限制实际容量。64 MiB 是获取完成后的原生大小检查，并非下载过程的流量截断。任何限制、缺件、权限变化或重算失败都停止本次操作，不降低验证范围、不提高预算，也不把失败冒称为已暂停或已退役。

当前选择的原件资格另有最多 4,096 次该 current 路径变更的原生历史检查上限，删除和重新创建也计入；超过上限直接失败，不截断后声称路径从未存在。这是新增的完整性保护预算，与 32×128 的活动结构容量不同。正常 current/写入/独立检查需要这项资格时也使用上述原生快照；其返回格式和实际写入效果仍沿原接口。

读回仍是完整前缀工作，分段没有把复杂度变成常数。概率历史还需要原匹配 Registry；新的 Registry 不能替代缺失的历史资源。已存在的 native bundle 保留原事件、封段和 current；普通 store audit 只核引用和字节关系，活动及封段 reader 才执行语义重演。

本组没有自动执行、通知发送、用户已读或投资接受效果。维护者退出该能力时需接续已存 v2 历史 reader、专属宿主接线和原件验证责任，不能通过删除 reader 或历史原件回退。
