# 研究版 Attention、内部议程与轻量复核

本能力接续[研究目录](research-catalogue.md)，不替代新闻工作台，也不建立第二份研究或任务真源。先从目录选择确切 ResearchRef，再读取原委托、问题、未决项、未解挑战与来源缺口；选择和复核备注作为独立 Artifact 保管。它不是投资信心排序、实际 Brief 采用、自动研究、通知或人工 Decision。

## 使用现有入口

```bash
decision-os store --path /tmp/existing-synthetic.git research-catalogue
decision-os store --path /tmp/existing-synthetic.git research-agenda --input /tmp/agenda-plan.json
# 结果 artifact 字段包装为原 put-artifact 载荷后，沿原权限/CAS/回执保存。
decision-os store --path /tmp/existing-synthetic.git put-artifact --submission-id agenda-one --input /tmp/authorized-artifact.json
decision-os store --path /tmp/existing-synthetic.git read-research-agenda --ref /tmp/agenda-ref.json
```

远端 CLI 沿原 `--binding config/store-binding.json` 和既有凭据。正常连接器使用 `research-runtime.yml`、`workflow_dispatch`、`ref=main` 和刚核过的 `code_sha`：只读操作 `agenda` 的 payload 是 `{"plan": <下述计划>}`；`read-agenda` 是 `{"reference": <完整ArtifactRef>}`，可另给 `as_of`。这两个操作的 submission_id/expected_head 留空，只有原 read job 执行。返回 JSON 中 cards 为实际阅读投影，document 为选择记录；并非已交付网页。

**非空 changes 进入公共工作流之前必须独立给 `public_feedback_custody_confirmed: true`。** 即使不保存，Actions 输入/产物也可能公开；私人笔记不能继承研究正文的公开许可。门禁在本宿主建立输入/报告目录和访问存储之前执行，但不能从已经提交给 GitHub 的 dispatch 事件撤回秘密：调用者必须在发请求前检查授权。该布尔值是本次公开保管确认，不是人类身份认证或研究接受。

保存仍调用现役 `put-artifact`，payload 为 `{"artifact": <结果artifact>, "rights_confirmed": true, "rights_basis": "实际授权依据"}`，使用原 submission_id 与刚核的 expected_head。保存不是 build 的副作用。读取回执完整 ArtifactRef 后，**另一次** `read-agenda` 独立打开；未知保存响应沿原输入/键做 `reconcile-artifact`，不改键重发、不回滚删对象。

## 计划和选择合同

计划为：

```text
{
  "references": [目录给出的完整 ResearchRef, ...],
  "as_of": "含时区的评估时间",
  "baseline": 上一份已保存议程的完整 ArtifactRef 或 null,
  "changes": [
    {
      "reference": references 中的一个确切研究引用,
      "question_id": 原问题 ID 或 null（仅该确切整份修订）,
      "action": "focus | defer | dismiss | reset",
      "reason": "实际选择依据",
      "review": null 或 {
        "variable": "跟踪的经营变量，可空",
        "event": "复核事件，可空；与variable至少一个非空",
        "opens_at": "含时区窗口起点",
        "closes_at": "含时区窗口终点",
        "required_evidence": "需要取得哪些证据才能判断",
        "end_condition": "何种明确复核或选择结束跟踪"
      },
      "feedback": null 或 {
        "observation": "实际提交的观察/疑问",
        "implication": "对原问题的可能影响",
        "outcome": "unresolved | revision-requested | no-change-claimed"
      }
    }
  ]
}
```

`baseline/changes/review/feedback` 可省略。每次最多8个确切研究引用、32个不同目标变更；累计最多128个有效选择。重复目标、伪造问题 ID、不在本次 refs 中的选择、无法读取的选择目标和不明字段会被拒绝。普通旧报告没有结构化问题时，只能绑定整份修订，原 unresolved 文字仍显示；不猜造问题身份。已回答的问题也可被显式提出复核，不自动改写原答案或 stop。

`focus` 记录关注；`defer` 暂缓，review 为空时一直保留直到显式改动；`dismiss` 移出注意力但不删除研究；`reset` 移除当前选择，旧动作仍在原不可变文档和前驱引用中。具体问题选择优先于同版整份选择。没有 change 时仅从原工作底稿派生待办，**不制造用户选择**。

所有选择只作用于完整确切 Ref 和问题，既不依公司名传播，也不自动继承到后继研究。后继必须重新选用新研究引用；旧版选择留在 state，不在本次 refs 的记录另列 unselected_attention，不能静默清空。新版原问题、实质新反证不因旧版暂缓被过滤。注意力状态只是阅读投影，原未决问题和挑战仍可见。

## 关联已存 Watch 条件证据

#60 的[多条件 Watch](../research/watch-conditions.md)沿用本入口。在计划中可显式增加最多 8 项 `watch_evidence`，每项只选一个确切已存检查或活动事件：

```text
"watch_evidence": [
  {
    "reference": 完整的原研究 ResearchRef,
    "question_id": 原问题 ID 或 null（该确切整份修订）,
    "kind": "check | activity",
    "check_ref": 完整的已存检查或活动事件 ArtifactRef
  }
]
```

`check` 使用原 `read-odds-watch-check`；`activity` 使用原 `read-odds-watch-activity`。读取会验证原不可变对象并重演其原输入；冻结 Watch 的 ResearchRef 必须与声明一致，非空问题 ID 必须真实存在于该版研究。绑定表示调用者把此项用于这个原问题，不证明条件文本与原问题在经济意义上已经得到独立确认。它不能以 URL、最新指针、公司名或一份自填的检查结果代替确切引用。

未使用此字段的旧输入和旧文档继续是原 `research-agenda-v1`，键集和原字节不变。首次显式提供该字段后使用 `research-agenda-v2`；文档仍只保存计划和选择状态，不保存另一份 Watch 结果真源。基线为 v2 时，省略字段继承其确切证据清单；显式 `[]` 退出证据选择。清单中的研究不在本次 `references` 时保留于 `unselected_watch_evidence`，不会移给新修订；同一研究、问题、记录的可达 commit 别名不能增加一份重复证据。

结果在 `watch_evidence` 与对应研究卡片列出选择、确切重开请求、记录时点和原检查结果。v1 单条件保持原结果；v2 保留冻结顺序的 `condition_results`、各自的满足/未知/复核及连续性、显式 any/all 组合与 `next_boundaries`。原活动管理事件没有检查结果时明确保留 `result=null`，不制造新观察。`status=readable` 只表示原记录已成功重演；经营判读仍是提交者判断，来源回读只证明保管的选段可读，不能代签原件真实性、证据充分性或人工接受。

检查或活动声明时点晚于 `as_of` 时显示 `after-as-of`，不采用其结果；确切对象缺失显示 `missing`，不能落为条件未满足。校验、权限、传输及操作请求限额错误向原宿主传播，不通过另建读取器重置预算继续。已存记录只沿自己的输入与资料切面重演，读取不叠加当前研究或活动状态，也不执行新检查、改写关注/暂缓、启动研究或发送通知。记录时钟不是该检查的保管时间，也不是历史可知性证明。

每项完整证据投影最多 64 KiB，本次完整证据区最多 256 KiB；超限保留 `not-included`、原因、确切引用与重开请求，原研究卡片的既有边界继续生效。选段来源的大小、请求次数和权限沿原 Store/Watch 合同，不增加来源获取平台。公共宿主导出含证据的计划或已存基线，还须同时明确 `public_feedback_custody_confirmed: true` 与 `public_sources_custody_confirmed: true`；原作者判断和来源材料分别有权公开，不能用“读取成功”或原研究公开确认代签。省略行也不免除相应导出检查。

把已保存议程放入原 Brief 计划的 `research.agenda` 即可消费这些结果，实际正文采用与省略见[研究 Brief](research-brief.md#已存条件证据进入同一份正文)。议程投影本身不声称结果已进入 Brief。

## 复核和时间含义

窗口状态区分 not-scheduled、not-due、due-for-review、overdue-for-review，起止包含并按真实时区比较。defer 到窗口时变为待复核，不是自动恢复研究。窗口结束仍为逾期待复核，不自动完成 end_condition。日期到来不证明公告已发布、变量已满足、证据可用、研究已执行或通知已发送；文本条件不会被当代码执行。

`as_of` 是本次评估时钟，**不是历史可知信息截止，也不是报告截止**。本操作可对确切已存研究评估复核状态，不提供 PIT 过滤或证明当时已经知晓后来的研究。stored_at 仍是保管时间。后继评估时钟不得早于其 baseline；独立重开默认为保存时钟，可显式更新 as_of，但不写回。公共宿主拒绝未来评估时钟，本地可作明确计划投影，不冒称已经发生。

feedback 保留观察、影响和请求更正等最小复核线索，始终标为 **submitted-claims-not-verified**。没有实际新增证据读取、研究判断或授权改稿时，不能把它称为完成实质 Review、纠错已落实或人工接受；更正研究须沿原研究新修订合同，保留原版本。

## 恢复、失败与资源边界

议程文档只含规范化计划、前驱和有效选择；原研究正文、研究指针、方法与资料不复制为新的权威副本。独立打开时验证 ArtifactRef、实际原字节、规范化文档和当前一步状态转移，读取一份实际前驱而不递归回放全部历史；`history_verification=current-step-only`，不声称审计所有历史选择或认证提交者身份。缺基线/坏字节/错误转移不能悄悄降为“没有反馈”。

研究卡片每次重新通过原 ResearchReader 精确读取，沿原 continuation 保留原问题、未解挑战和来源状态。每个研究实际读最多1项来源，原 source_next_cursor 可用 resume 继续；可读研究不等于证据全可用。个别研究 missing/unavailable/invalid 保留该项和精确引用，其他案例继续；新选择不能依赖坏卡。唯一例外是 reset 已保管的同一目标：研究暂不可读时也可显式移除该旧选择，不因此新增判断或授权。全局原 Store 的权限、限额与请求校验不变。

文档最多256 KiB，单卡最多1 MiB，所有投影卡片最多4 MiB。超出显示预算标 not-included 并留原 Ref，不截断或修改研究正文；改用原 prepare/resume 精确读取，或缩小这次 refs。无分页假完整声明。空 references 只表示未选择研究，不代表没有研究、零候选或市场安静。该结果不替代目录全局发现，也不推断任何任务在途。

## 维护、验证和未交范围

复用标准库 datetime/json/base64、原 Reader/continuation、ArtifactInput/ResearchArtifacts、存储条件提交与只读对账。新增部分仅议程应用、命令适配、两个现役只读宿主选项和专属测试；不新增依赖、调度、存储、认证角色或旧 Kernel 兼容层。新闻页面的特定版本选择语义可参考，但一般研究的问题/委托合同不同，不把两者塞进通用选择框架。

专属测试覆盖版本隔离、具体问题优先、暂缓时区边界、重开、轻量反馈、坏基线/状态/问题、预算和公共输入门禁；`test_watch_agenda.py` 追加确切 v1/v2 检查与活动消费、未知/未来记录、坏研究/问题绑定、重封装伪造、原读取请求预算和证据省略。真实临时 Git 通过现役宿主完成保存→独立读取、后继版本不被抑制、原意图未知 ACK 对账。安装消费追加原唯一 cold-wheel，不另建安装试验。合成反馈不能代签真实用户使用；本组没有新的研究委托或真实 records 写入。

一般研究及所选条件证据进入晨晚 Brief 的采用/遗漏沿原 Brief 合同；统一阅读页沿[阅读室](reading-room.md)。主动提醒、长期独立灾备、生产运行和 #7 研究质量仍按各自责任验收。退役时撤两个只读入口和专属消费者，保留既有 Artifact/ResearchRef 的原字节读取责任；不用删除历史选择来回退工程。
