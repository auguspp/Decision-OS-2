# Full 后持续 Watch：价格条件与关键问题

责任 #60。用户于 2026-10-20 明确：Watch 接续 Full 后的 Odds 价格，以及 Full 尚未解决、会影响 Odds 的关键问题；交付先进入既有 Brief。Quick 发现的线索仍进入研究议程。本页规定这个用途的实际准入和接续，复用[多条件检查](watch-conditions.md)、[活动管理](watch-activity.md)、[有限执行](watch-execution.md)与原研究／Odds 保管，不另建公司名单或通知平台。

## 1. 两类跟踪都回到同一份 Full

| 跟踪对象 | 必须已有的依据 | 本次观察回答什么 |
|---|---|---|
| Odds 价格条件 | 确切 Full、实际保存的条件或概率 Odds 结果、与之相同的规格／评估及明确价格条件 | 合格价格是否触及某档，哪些档持续成立，下一道已声明边界在哪里 |
| 影响 Odds 的关键问题 | 原 Full 工作底稿中的重大未决问题、需要什么证据、影响哪些价值输入或世界 | 实际新证据支持、削弱或要求复核哪条前提；资料不足仍为未知 |

Full 尚有重大未知或声明 `blocked` 不妨碍保存有范围的问题跟踪，也不证明研究已穷尽。没有实际可用 Odds 时可只跟踪证据；缺价格边界不能从旧参考价、展示值、合成阈值或另一个公司的条件补造。Watch 不要求为完整表格填概率，不将 provisional 结果、作者敏感性或加入观察名单升级为人工接受。

价格、研究／Odds 前提与用户回应分别变化。新价格仅沿原受支持计算检查；新证据可能要求复核原世界、股本、现金、概率或期限，不能把修改这些输入叫“只刷新价格”。研究 current 变更只是版本变化，需要复核原绑定，不证明某个经营问题已经解决。

## 2. `odds-watch-v3` 的绑定

v3 保留 v2 的 `watch_id/basis/conditions/combination/activation/window/data_policy/effect/limitations`，增加 `tracking`。仍为 1—16 个有序条件、显式 `any/all`，复用相同数字比较、区间和经营证据输入 `odds-watch-input-v2`；v3 不接受 `research_version` 条件，且要求 `window.end_after_trigger=false`。

`tracking` 的字段为：

```text
{
  odds_result: null 或 {kind: "conditional" | "probability", reference: 确切已存结果引用},
  question_links: [
    {condition_id, question_id, affected_inputs, world_ids, impact_note}
  ]
}
```

这些字段引用已经存在的研究和结果，不接收另一份自由填写的 Odds。`odds_result.reference` 指向实际 `save-odds` 或 `save-probability-odds` 保存结果，沿原 `read-odds`／`read-probability-odds` 实际重演；不能拿冻结规格、未保存计算 JSON 或当前报价代替结果。绑定比较完整原引用及相依摘要，后来 commit 的可达别名不自动成为本次指定的原引用。该结果的 `price.as_of` 与存在时的 `calculation_input.evaluated_at` 均不得晚于 Watch 的 `activation.as_of`；不能用后来评估替较早激活点补依据。

| `basis.kind` | `tracking.odds_result` | 条件与问题约束 |
|---|---|---|
| `conditional` | `kind=conditional`，结果使用同一 `spec_ref` 和同一 Full | 价格／受支持条件指标可与经营证据并列；具名世界须属于该规格 |
| `probability` | `kind=probability`，结果使用同一 `assessment_ref`、规格和 Full | 保留原概率／政策资格；不得借 Watch 换评估或补默认概率 |
| `research` | 必须为 null | 只接受经营证据条件；问题链接的 `world_ids=[]`，不造尚不存在的世界 |

每个 `evidence` 条件恰有一个 `question_links` 项；数字条件不挂问题链接。`condition_id` 必须是本 Watch 的经营条件，`question_id` 必须真实存在于冻结 Full 的结构化工作底稿。对应 finding 须声明 `material=true`，并满足以下至少一项：finding 仍为 `open`；同一问题仍有 `unresolved` challenge。没有结构化底稿、虚构问题、非重大或已经解决且无未解挑战的问题均不能通过这个准入。

`affected_inputs` 为 1—7 项不重复的字符串，从 `cash/capital/shares/world-value/horizon/probability/risk-policy` 选择；`world_ids` 为 0—12 项不重复的原世界 ID，每项必须属于同一规格。`impact_note` 说明问题变化怎样影响这些输入和 Odds。研究尚无规格时，仍可说明拟影响的经济输入，但不填不存在的世界。程序核绑定和作者已经声明的重大／未决状态，不认证经济重要性或影响解释正确；判断依据应在原研究和实际证据中可复核。

冻结时以所绑定的原 Full 核资格；`continue/blocked` 等停止声明不是一律拒绝，也不被升级为研究完成。后继研究不自动替换冻结版本或继承旧价位。需要变更研究、Odds、条件或问题绑定时，保留原记录并按实际后继重新核定。

## 3. 持续观察、有限执行与真正退出

Full 后 Watch 不在首次触发后结束。各价格档及经营条件沿原活动前驱保留首次、持续、明确未满足后重入，以及两次明确判断之间的未知间隔。暂停不制造 false；Brief 已采用不重置条件；用户暂缓某问题的注意力不等于删除该 Watch。

价格层级可以重叠。原 Odds 的某个价带若表达“到上沿及更低价格都需复核”，使用 `lte` 上沿并在条件依据中保留原价带含义；真正 `between` 区间跌出下沿会变为 false，两者不能混用。没有声明的深层价位仍是未知，程序不从事实距离推导“接近”百分比或新的买卖门槛。

原 Watch 仍有明确有限窗口；原目标日、现金路径、资本与行情资格继续生效。实际执行每批最多 32 槽，同一有效 Watch 可以显式接续后续批次并保留同一 Activity 历史。**批次结束只表示该批槽用完，不证明用户不再关注。** 本组没有自动续批服务；不能把漏跑槽补成过去已经观察，不能延长旧模型的固定经济期限，也不能更换 Watch 清零已知触发历史。

明确暂停／退役、原研究／Odds 被有效后继替代、原前提失效或固定经济期限到达时，分别保存实际处置。`review-required` 表示应复核相依判断，不等于问题已解决；已退役活动不能恢复。分段历史继续沿[原有界合同](watch-history.md)，不是无限运行承诺。

## 4. 沿原命令保存，并由 Brief 接续

仍使用 `freeze-odds-watch` 冻结定义、`change-odds-watch-activity` 登记／恢复、原活动检查或有限执行入口保存检查，再以独立 reader 读回。注册仍先为 paused；登记、active、一次实际检查和长期自然运行分别成立。原 `code_sha`、稳定提交键、预期 head、权限、未知写对账及容量检查不变。

新冻结、新检查保存、活动登记／恢复／检查、执行计划及预约都核原研究为 Full；面向新检查的预览也核该门槛。v1/v2 的新操作保留其原结构合同并增加 Full 准入；v3 另要求本页的 Odds 结果与关键问题绑定。原历史读取和原意图对账不补加新门，暂停／退役仍能处理已有条目，不让新的产品范围妨碍停止旧活动。

Brief 读取实际保存的检查及其逐条件结果，不只读取首次触发的 `notices`。新证据要求复核、重大问题取得进展或关键资料不足时，即使没有新的价格触界也应保留其实际意义。新证据、新价格档、原问题及上一份实际 Brief 的采用关系是比较对象；相同公司名、一个新 commit 或每天相同 true 都不能独自证明新变化。

机械 Brief 复用[原议程的确切 `watch_evidence`](../delivery/research-agenda.md#关联已存-watch-条件证据)和[同一正文的条件采用](../delivery/research-brief.md#已存条件证据进入同一份正文)。AI Brief 的确切检查采用沿[现役作者正文交接](../delivery/hosted-discovery.md)；原研究采用仍可包含 Quick，这不改变 Watch 的 Full 准入。正文显示、保存、独立读回与用户已读分别留证，不能以未鉴权的文本回执代签阅读或投资决定。

## 5. 历史、维护与未交付范围

原 v1/v2 定义、结果和活动历史保留；不自动改写为 v3，也不迁入旧 Kernel 的公司名单、旧价位或注册表。复用旧“持续观察、到价复核、资料缺失不安静”的语义，实际依据仍来自本仓确切 Full、Odds 和来源。新版绑定与现有算术、保管、活动、议程及 Brief 各守原责任，不新增行情提供方或数据库。

当前交付的边界是可验证的 Full／Odds／问题绑定及既有通路消费。自动在每次 Full 交付时同步登记、持续取得新经营证据、长期自动续批、所有真实公司价格资格与未来自然运行，仍须各自实际完成后才可声称成立。代码能拒绝伪造引用，不证明已经取得真实公司的有效价位或解决其重大研究问题。

退役新生成能力时同时处置 v3 准入、消费者和专属验证，保留已经保存的历史读取及共享算术／保管义务。原记录不删除，失败不改成未触发，工程验证不代签研究质量或人工接受。
