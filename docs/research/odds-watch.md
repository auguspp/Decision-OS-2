# Watch：显式条件检查与按需价格刷新

责任 #60，落实[设计§14.2、15.2–15.3](../decision-os-2.0-design.md#chapter-15)。复用[条件Odds](conditional-odds.md)、[概率Odds](probability-odds.md)、原版本保管与显式研究宿主。**本入口只在调用时检查并按要求保存，不是定时服务、通知订阅或投资决定。** 不自动迁移旧Watch名单，不给已有报告补价格阈值或概率。

新交付的[活动管理入口](watch-activity.md)负责跨会话选择、暂停/恢复、原子采纳与历史摘要。以下是原独立v1合同；其历史投影和回执仍保持。已登记Watch的新执行必须使用活动入口，不能绕过登记状态。

## 1. 三种有限用途

| basis.kind | 确切reference | 支持条件 | 是否需要价格 |
|---|---|---|---|
| research | 原ResearchRevision的BlobRef | 指定records快照中的研究current是否与绑定版本不同 | 不需要，也不调用Registry或行情 |
| conditional | 原freeze-valuation的spec_ref | 价格、某个世界的估值距离或名义累计回报 | 显式声明参考价、原Relay观察或按冻结政策核验的FTShare原始收盘；沿各自资格区别 |
| probability | 原freeze-odds-assessment的assessment_ref | 价格、世界名义回报、加权累计回报或正收益情景概率 | 原Relay日线保管或FTShare确切集合，执行原概率资格与政策 |

研究变化只证明版本不同，不证明经营恶化。价格用途在研究已更新或调用者登记了新前提问题时转为`review-required`，不自动换研究、规格、概率、股数、政策或目标日；旧检查仍按原快照恢复。当前公允价值只能选`value_to_price_distance`，不能选未来回报。仅知道需重新审阅时不必等取得新价格。

这不是任意经营变量/公告含义的自动判断器；合同履约、项目回报与新证据的经济解释仍归研究。`review_reasons`是有来源的调用者说明，不是程序完成独立研究的证明。

## 2. 冻结Watch合同

`odds-watch-v1`必须完整提供：

- `watch_id`；`basis:{kind,reference}`；`condition:{metric,world_id,comparison,threshold,basis_note}`。
- `activation:{enabled,as_of,basis_note}`：显式布尔、带时区的登记时刻和真实启用依据。不能早于原研究实际保管时间；概率Watch不能早于概率评估。来源文本不授予权限，字段不是经认证的Human签署。
- `window:{starts_at,ends_at,minimum_interval_seconds,end_after_trigger}`：带时区的有限窗口，起点包含、终点不包含；两次实际尝试的最小秒间隔（0表示每次显式调用可检查），是否首次触发后结束。**间隔字段不会创建调度。**
- `data_policy`：旧conditional声明`price_kind/max_price_age_days/max_capital_age_days`。FTShare conditional声明`{price_kind:"retained-ftshare-raw-close",market_policy:<条件Odds完整收盘政策>}`，政策不得晚于启用时刻；不能在每次检查输入里放宽它。其余basis为null。概率用途采用原assessment完整政策，不从Watch另设较宽政策。资料年龄上限是显式研究选择，不是系统认可的投资偏好。
- `effect:"record-only"`、非空`limitations`。不接受发通知、下单、开启研究等效果。

指标为`research_version`、`price`、`value_to_price_distance`、`undiscounted_holding_period_return`、`expected_holding_period_return`、`positive_return_scenario_probability`。研究版本用`comparison:"changed"`且world_id/threshold为null；其他为`lte`或`gte`和有限十进制**字符串**阈值。世界指标必须选原world_id，其余world_id为null。收益小数0.2表示20%，不是0.2%；正收益概率不是经验胜率。

同一冻结Watch的启用、窗口、条件或依据不能静默修改。改变这些定义仍须保存新Watch并明确采用，旧引用保留历史；只暂停或恢复同一Watch时使用[活动管理入口](watch-activity.md)，不必复制定义。原独立接口不选择全局活动状态，也不会自动接纳新Watch替换旧Watch。

## 3. 一次检查的输入与结果

```json
{
  "watch_ref": "<本次真实保存回执中的引用对象，不是字符串>",
  "checked_at": "<本次明确的带时区评价时刻>",
  "records_snapshot": "<实际读取的records完整40位commit>",
  "previous_check": null,
  "input": null,
  "review_reasons": []
}
```

示例中的占位符不能执行，实际ref须是原四字段引用。`previous_check`是同一Watch上次已保存的检查引用，不接受直接填写“上次已触发”。`input`为研究版本检查的null、旧条件检查的`odds-price-v1`，或概率检查的原`odds-retained-close-v1`。FTShare条件与概率检查均直接给`odds-ftshare-close-v1`的`price_id/collection_ref/exchange/quote_date`，沿原Odds消费者恢复来源；`checked_at`就是该次Odds的评价时点。FTShare Watch不接受声明价或Relay观察来满足其资格要求。不接受任意取数URL或代码，不隐式请求上游。

FTShare的hold、所选原始查询未保管、日期不在集合范围或不是开市日、评价/取得早于该日18:35观察截止、资料年龄超出政策时为`cannot-determine`；资本前提过期则为`review-required`。截止后的原始响应可在当晚使用，截止前取得的原件以后仍不能冒充收盘后观察。原件或引用损坏直接报错，不降级为条件false。其他证券和前复权缺口不直接阻断该只已留存raw close；结果也不宣称全市场可靠或最新可交易价格。已保存检查继续使用原集合和原政策，不自动跟随新根或新价。

`records_snapshot`和`checked_at`是本次明确选择的切面；新检查应读取当前真实records head，不把调用者选择的旧切面称实时最新。程序校验并读取快照及所指研究，但不声称实现历史PIT。若原研究仍为current，也不证明经营假设没有变化。交互检查/历史读取另外返回`current_use`，只提示读取时实际current，**不改写原检查结果**。

沪深证券的目标日、现金分配日和资本日期按UTC+08:00比较，同一时刻不同表示不能延迟分配或到期。其他条件规格暂用调用者checked_at的明确本地日期，不猜交易所时区。旧声明/观察价的资料年龄用真实时间差；FTShare沿Odds的报价本地日历年龄、证券主表取得时差和固定资本日期政策。达到原未来目标日结束；分配日已到但旧报价仍在分配前，也须复核现金路径，不能反复计算已过去的分红。

| 状态 | 含义 |
|---|---|
| not-enabled | 未启用；不检查条件，不读取价格 |
| not-due | 窗口尚未开始或距上次实际尝试未到间隔；此调用不推迟原检查时点 |
| not-met | 已得到支持该用途的数据并完成比较，条件为false |
| first-trigger | 本次为true，所供有效历史中上次明确结果并非true（含重新进入条件） |
| continued-trigger | 本次为true，所供历史中上次明确结果也是true；不重复冒称首次 |
| cannot-determine | 缺价、资料过期或有限资格失败；condition_met为null，不写false |
| review-required | 研究/资本/现金/前提变化，须明确修订；不继续无提示地刷新旧模型 |
| ended | 窗口结束、已设触发后结束或固定目标日到达；不证明某经营事件已发生 |

缺数据的尝试保留，但不重置已知触发状态。若两个true之间有无法判断，`trigger_continuity=intervening-checks-unknown`，不声称中间持续成立。首次/持续仅针对提供的前驱链，不是所有并行调用的全局去重；遗漏前驱不能产生“恰好通知一次”的保证。

## 4. 保存、恢复与失败

正常流程：`freeze-odds-watch → check-odds-watch → save-odds-watch-check → read-odds-watch-check`。检查会重算相依Odds并保留完整输入/输出，保存时不再次保存一套Odds真源。Watch和检查都是原ResearchArtifacts的JSON；原提交日志/CAS/备份负责保管，没有新数据库或research current。

`read-odds-watch-check`按各次确切records切面、原时刻、价格和前驱重演，比较规范结果；合法文件摘要不掩盖被修改的触发状态。程序使用有界迭代而非无界递归，最多32个前驱；超限明确拒绝，不截断历史后重新冒称首次。托管后的前驱自动接续和派生摘要见[活动管理](watch-activity.md)，其独立边界不改变本入口旧历史重演；两者都不声称无界运行。

同次重演在原后端首次验证后复用确切不可变字节：后端读缓存8MiB、来源表示缓存8MiB；完整引用作键、返回独立副本、失败和可变head不缓存，超限走原reader。每次操作独立，原后端在输出前重新核可变绑定/可见性；保存仍走原writer/CAS。独立读回创建全新的reader与缓存，不复用写入侧结果。保留原512次远端请求预算；复杂历史可能先触及该预算，届时明确失败而非隐去来源。无跨会话缓存或第二保管层。

仅将具名、确定性的资料资格失败映射为无法判断/复核；网络失败、源损坏、非法形状等仍走原异常/失败回执，绝不当条件未满足。不得把上一次成功检查改成当前成功。CLI正常返回可读检查状态不代表触发；消费方须读取status/condition_met。错误输入、保管或读回失败是非零退出。

写入前须有具体公开权利。写入结果不明时，按原payload/submission_id只读`reconcile-odds-watch`或`reconcile-odds-watch-check`，不换键补发。已确认保存但独立读回失败时保留引用与readback.failed。历史Registry重算要求沿概率入口恢复匹配的Registry及其资源；不静默套当前登记。

## 5. CLI与宿主

```bash
# 文件必须来自本轮真实输入与回执；公开授权不是由示例赋予。
decision-os store --binding approved.json freeze-odds-watch --input watch-publication.json --submission-id original-watch
decision-os store --binding readonly.json read-odds-watch --ref saved-watch-ref.json
decision-os store --binding readonly.json check-odds-watch --input check.json
decision-os store --binding approved.json save-odds-watch-check --input check-publication.json --submission-id original-check
decision-os store --binding readonly.json read-odds-watch-check --ref saved-check-ref.json
```

publication形状分别`{watch:<输入>,rights_confirmed:true,rights_basis:<真实依据>}`和`{check:<输入>,rights_confirmed:true,rights_basis:<真实依据>}`。概率检查可另给`--registry`原匹配路径；研究/条件检查不依赖Registry。原`research-runtime.yml`提供同名操作及两个只读reconcile，read-ref载荷为`{reference:<确切引用>}`。新写只有freeze/save，现役code_sha/expected_head/提交意图/公开产物限制不变，未增加权限或Secret。

## 6. 复用、验证与后继

旧固定源`decision-kernel@cb3bbc87`的`runtime/odds_watch.py`提供到价仅复核、缺价不安静、无阈值不取价、成员身份不等人工接受等语义。旧实现硬绑定旧注册表、Human级别与HiThink，故不迁移旧配置、权限或行情代码；本组用原新系统规格/研究/概率/market_custody实际消费者。条件比较与确定性计算沿原80位Decimal，不新增金融计算包。现有标准库时钟与有限状态投影足够，无需引入调度或通用工作流组件。

专项覆盖跨线/持续/重入/缺数据、窗口与结束、源损坏、历史状态篡改、前驱错配/上限、研究/股本/现金变化、时区、概率/行情期限、原512预算、失败回执对账和原同一次cold-wheel。代码测试不证明经济假设；本组不为真实公司补概率或把历史当前估值冒称未来退出值。

活动选择、停用恢复与派生摘要已由[活动管理入口](watch-activity.md)承担。后继依原授权落实更长历史的分段验证、有限非数值经营证据跟踪、真实研究概率与政策采用、#13议程/Brief消费；定时、通知、用户回执与去重需另有具体授权/交付。退出时同时退专属domain/application/watch_commands、宿主选项/路由、测试和说明，保留共享存储、既有Odds与历史附件读取责任；不删除用户资料。
