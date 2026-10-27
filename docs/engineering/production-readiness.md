# 交付运行巡检与生产启用清单

责任：#13。这里是现役入口的用法与待批准方案，不是实时状态账本、调度器、人工审批或生产已启用的证明。已有数据/代码离线恢复沿[恢复包](recovery-kit.md)，不另建Store或备份平台。

## 一次显式只读巡检

原 `store-runtime.yml` 增加 `health` 选项。仅手动main、固定checkout；health job为 `contents:read` + `actions:read`，原backup/retain-guide的操作与权限不变。`expected_head` 为刚核过的records SHA；可选 `backup_run_id` 只指定一份已执行的原backup，不触发新备份。健康检查自身按受信宿主run/attempt及实际health作业识别后排除，不把它当新备份；过去health成功也不会冒充backup成功。

已有获准本地令牌环境可使用同一原GitHubAPI，仍只从 `DECISION_OS_STORE_TOKEN` 读取，不回退其他工程令牌：

```bash
python -m decision_kernel.interfaces.store_runtime runtime-health \
  --binding config/store-binding.json --expected-head <已观察records-SHA> \
  --backup-run-id <确切backup运行号> --out /approved/new-health-report
```

只有新本地目录会写入 `health.json` / `health.md`；没有records、采集或研究写入。可用带时区的 `--as-of` 重现到期判断，省略则用实际收集结束时刻。无定时目标时，运行年龄只是秒数，不机械判为超时。有限检查通过返回0；失败/在途/未运行/禁用/未知/变化/产物缺失或临近到期返回2，保留可判定的相邻模块。传输或末次权限失败立即停止后续GET，只保留unknown小回执，不发布先前成功行冒充完整检查。

## 这份报告能证明什么

查看store-runtime、research-runtime、news-rolling、daily-market四个现役宿主。每个读取最近20个main运行，按窗口内实际attempt启动时刻排序，同时保留最近尝试与最近可见成功及原生event；新失败不会被旧成功顶替。总数量、返回数量、历史是否完整和窗口内在途条目明示。旧run在窗口外重试可能不在结果中，不能据此签全局无writer、没有其他活动采集、当前业务最新或允许切换。daily的`status`和`daily`作业分别标明；原生作业名不包含update/recover-late等确切输入时不猜具体操作。

确切run/attempt的jobs与对应产物名、run/head、摘要声明、到期日期分别核对；不借旧attempt附件。jobs/artifact分页超过100不截断为完整，返回未知。末次重复核同运行、工作流和列表，变化显示changed-during-inspection，不自动重试；仓库身份/可见性与main/records再次核对。最多63个GET：原56（仓库/两分支前后6、四宿主各至多10、当前巡检自身3、指定backup4、自然schedule页前后2、固定生产策略1），另加固定clock配置1、独立dispatch页前后2、最多一条尚未核过的外部入口详情3；已有同run详情直接复用；再加1次固定main部署开关读取。同一原API的512累计预算不重置，元数据总8MiB、输出256KiB。供应商原文、任务payload、用户反馈、账号字段和签名下载地址不进入报告。

`metadata-ok` 仅表示本次有限原生执行/产物元数据检查通过，不叫“业务健康”。workflow `active` 不证明已配置schedule；作业成功不证明正文已保存或真实资料完整；产物未过期不证明字节校验/当前records覆盖/异地保管。临到期提示固定为剩余不超过7天，仅提示，不自动复制、删档或通知。指定backup仍必须是原生成功backup作业，不能用health或retain-guide替代；内容和恢复资格仍读取原report与bundle校验。

## 主动暂停与历史问题分开

先从同一固定main读取`config/daily-runtime-version.json`，复用原Contents身份/字节检查与`daily_execution.policy()`严格校验，仅保留部署状态、模式及证明；不调用执行准入，不读取records内容，也不替外部账户或已固定执行树签资格。

已核`enabled=false`时，退出独立schedule/dispatch周期页及额外外部入口详情的观察，不再读取旧daily-market/clock配置或要求当前槽出现新run；两个子节明确`deployment-paused / observation=not-requested`，不是“已查到零运行”。仍前后核普通运行列表、最新尝试/最近可见成功的详情、到期、在途及最终权限。没有其他问题时宿主显示`paused`，不单凭主动暂停让整份巡检失败；已有失败、产物缺失/到期、在途、未知和变化不被改绿，仍需分别处理，不能把暂停解释成失败恢复或未知写已对账。

坏配置保持unknown，不默认启用或暂停；权限/传输拒绝仍停止所有后续请求。读取中main或默认分支变化时暂停观察也降为changed，不改用新head重试。历史as-of不回签历史部署；外部任务停用须独立原生回执，报告不宣称服务商已停钟或数据新鲜。暂停后的路径在相同元数据下比启用路径少6个GET（两份配置和四次周期页），若原先需要额外外部详情则再少3个；相对旧版还需计入新增的1次部署配置读取，不能把请求数差额宣传成实机耗时提升。

下列自然时点与外部入口观察仅在本次固定main部署明确enabled=true时执行。完整元数据最坏上界由62变为63，只增加这一份固定配置证据；原512累计API预算、8MiB/256KiB上界与所有来源预算不变。

## daily-market自然时点观察

正常health宿主和直接安装后的CLI默认执行同一观察，不依赖当前工作目录。沿原GitHubAPI只读本次开头固定main的`config/daily-market.json`，核Contents的文件路径、类型、base64、长度、原Git blob、严格JSON及现役`policy()`；报告只保留main/path/blob/字节摘要和已采用时钟，不输出seed引用或读取其对象。历史`--as-of`也只按这个固定策略解释，不回签以前的部署配置。

单独前后读取该workflow最近20个`branch=main&event=schedule`运行摘要；大量手动status不会挤掉这页自然证据。计数/页不足、错误仓库/工作流/event、无法核到首次attempt或当前窗覆盖不足时保留unknown；总历史超过20本身不是错误，已有完整页跨过目标槽起点即可界定本次窗口。列表或main/工作流在观察中变化显示changed；观察起讫跨入新槽也显示changed，不能把查询后刚到时点的新槽判成缺失。

期待槽和每条run的归槽直接复用生产`admission_slot(..., allow_late=True)`：以评估时钟找不足24小时的最近已过工作日18:35槽，每条run只按native `created_at`归槽，排队启动跨午夜不改业务日期。周二18:35前仍观察周一槽；周六18:35前仍观察周五槽。无最近有效槽时显示no-recent-slot，旧失败与成功摘要仍保留，不宣称历史缺口已恢复。

当前槽无符合证据显示not-observed，不推断未触发、平台丢弃或来源根因。24小时只是原生产入场边界，不是GitHub永久丢失证明。同槽任何首次attempt不可见或任何在途运行均保留unknown/in-progress，较晚成功不遮住它们；其余才按最近完成尝试呈现结果。当前primary为github-schedule时，原生在途、非成功、未知或当前槽未观察到均使daily宿主和整份巡检不能为metadata-ok，最近手动成功不替代它们。native-success只表示该页原生首次attempt成功；自然子节不另读jobs、附件或payload，也不认证行情更新、Watch、Brief或业务正文。一般宿主的最近run详情与自然子节是分别声明的观察范围；两页中相同run的元数据冲突也显示changed。

本观察不新增时钟、后台检查、通知、dispatch或恢复动作。复用Kernel的有界元数据检查与事件身份区分；其真实[计数1/空列表反例](https://github.com/auguspp/decision-kernel/issues/297#issuecomment-5829043734)要求不完整证据保持未知，[后继交付对账教训](https://github.com/auguspp/decision-kernel/issues/297#issuecomment-5831654154)要求最近状态不能遮住交付缺口。外部时钟的实际准备与切换走下节独立合同，不由巡检自动执行；没有移植旧自动补采或历史重建。原生接口与延迟边界依据GitHub的[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow)和[schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

## 单主时钟与外部入口观察

另读同一固定main的`config/daily-market-clock.json`，同样核文件身份、严格JSON、大小、Git blob及字节摘要；报告保留`clock_policy`和实际primary。默认github-schedule只要求原schedule证据，尚未启用的外部入口作为备用观察，不额外要求每天出现。将来按[外部日更时钟合同](external-daily-clock.md)切到external-cron后，原schedule仍显示历史，但不再要求已退役的时钟每天有新run。clock配置无法核定时保持unknown，不能默认任一钟已启用。

独立前后读取最近20个main的`event=workflow_dispatch`。固定run-name把`external-clock`入口与`clock-probe`、其他手动调用区分；原生event仍是workflow_dispatch，外部服务身份与实际job配置均未由此证明。计数、完整性和当前槽覆盖先按整页dispatch判断，再挑外部入口；不能用过滤后的数量充当完整历史，或因20个较新status挤掉外部run就说从未触发。

外部目标槽同样保留任何在途/首次attempt不可见/观察变化，不让probe success或较晚success遮盖。原生run success不足以取得`daily-job-success`：还须当前主线策略下的确切入口、稳定原生详情、完整jobs中实际daily成功且未skip、对应未过期产物元数据；历史代码策略未核时单列未知。这里只核最多一条外部运行详情并复用已有同run结果，不下载业务附件。`clock-probe`的独立job和`dos2-daily-clock`产物被识别为元数据探针，不能补签daily生产槽。

即使是`daily-job-success`，`natural_origin=unverified`、服务商配置未检查、业务结果未检查仍保持。服务商计划/执行时间、原HTTP结果与确切GitHub run、源完成和Watch/Brief采用须沿原件另验；当前primary配置只是受审意图，不证明外部账户真的enabled。切换/回退、停用配置和零业务副作用probe用法在[外部日更时钟合同](external-daily-clock.md)。

## 持久备份与恢复目标：待用户批准的默认方案

**提案，不是已设置策略：** 在既有唯一records真源之外，将原三文件恢复包复制到一处用户指定的独立受控目的地；另保留一份离线副本。备份不是第二可写业务库，不自动同步成新主库。目的地、可见性、访问主体及是否允许该资料再分发先确认；公开库权限不能继承到未选择的新服务。

建议每天最后一轮业务保存完成后备份一次；保留7份日备、4份周备、12份月备。实际配置前核资料规模与用户确认；本组不设schedule、不作任何清理。已有引用的历史档案不按这个副本轮换方案删除；清理副本还须确认至少另一份完整可恢复副本及引用保护。失败的新备份不得覆盖最后一份已核合格副本。

建议初始目标为最多24小时已保存数据损失（RPO）及4小时内恢复只读阅读（RTO）；这只是待验目标，不是承诺。RPO从真实业务快照衡量，不能用下载时间刷新；RTO覆盖取回、校验、环境准备和实际阅读，不仅计clone耗时。每次reader变更后及至少每季度做恢复演练，用原snapshot、摘要和真实阅读计划验收；本轮操作次数不冒充长期演练频率。

第一次复制按“核原生run/attempt/ZIP摘要 → 复制原字节 → 独立重新下载目标对象 → 逐字节与原包比对 → 原recover-room恢复”验收。目标对象ID、版本、摘要及保留规则写入原#13回执。没有目标读回，不称外部持久备份成立；Actions有限保留和对话附件都不能代签。

## 实际启用、停止和回退

启用前在原任务核四类事实：用户批准的具体周期/渠道/目标；唯一业务writer及当前所有来源/写入/排队/未知效果；确切main及适用CI、最后合格业务引用与可恢复快照；新旧执行责任和回退入口。巡检只覆盖四条宿主及daily的有限schedule/dispatch页，不能单独解除这四项前提。定时不会默认启动新的Full、复活暂停来源或记录用户接受。

停止只在获得该操作授权后阻止后续触发；已运行任务先检查实际副作用，不自动cancel或假定取消等于回滚。失败写入沿原submission/expected/ref只读对账，不换key再发。来源429/权限拒绝沿原owner停止规则，不由巡检器代作重试。

回退工程用普通反向PR；业务切换保留原SourceRef/ArtifactRef与原records，恢复包只读打开不产生新writer。已写数据不因代码回退消失，已外发通知不可假定收回。旧Kernel在实际切换批准前不被本组停止；不同时开启两个采集/写入者“比较效果”。

## 复用、维护和退出

GitHub提供原生runs、attempt jobs和artifact metadata；现成GitHub CLI可人工查看，但本组组合不同宿主的原生身份、原项目预算及公开摘要，直接复用已有GitHubAPI而非再装客户端或搭建监控服务。2026-10-17核读官方[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)、[workflow jobs](https://docs.github.com/en/rest/actions/workflow-jobs)、[artifacts](https://docs.github.com/en/rest/actions/artifacts)及[workflows](https://docs.github.com/en/rest/actions/workflows)；2026-10-21补核schedule及workflow-scoped event/branch过滤。服务端字段/分页变更时更新同范围消费者和反例，不以宽松解析吞掉未知状态。

维护 `application/runtime_health.py`、窄策略解释器 `application/daily_schedule_health.py`、`interfaces/runtime_health_commands.py` 及原store-runtime显式health路由；单主时钟配置/准入共享`application/daily_clock.py`，不另造解释。退役用普通PR一起撤health选项/job、CLI/专属测试及原单次安装helper；若只退役日更或外部入口观察，同步撤对应子节、额外只读预算与专属反例，原宿主/共享保护继续。原backup/retain-guide、恢复包、CI和业务保管不退。真实生产启用、外部持久副本和业务/人工验收仍各自留原件，不能由本说明代签。
