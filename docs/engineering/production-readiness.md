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

只查看store-runtime、research-runtime、news-rolling三个原交付宿主。每个读取最近20个main运行，按窗口内实际attempt启动时刻排序，同时保留最近尝试与最近可见成功；新失败不会被旧成功顶替。总数量、返回数量、历史是否完整和窗口内在途条目明示。旧run在窗口外重试可能不在结果中，不能据此签全局无writer、没有其他活动采集、当前业务最新或允许切换。

确切run/attempt的jobs与对应产物名、run/head、摘要声明、到期日期分别核对；不借旧attempt附件。jobs/artifact分页超过100不截断为完整，返回未知。末次重复核同运行、工作流和列表，变化显示changed-during-inspection，不自动重试；仓库身份/可见性与main/records再次核对。总计最多44个GET，同一原API的512累计预算不重置，元数据总8MiB、输出256KiB。供应商原文、任务payload、用户反馈、账号字段和签名下载地址不进入报告。

`metadata-ok` 仅表示本次有限原生执行/产物元数据检查通过，不叫“业务健康”。workflow `active` 不证明已配置schedule；作业成功不证明正文已保存或真实资料完整；产物未过期不证明字节校验/当前records覆盖/异地保管。临到期提示固定为剩余不超过7天，仅提示，不自动复制、删档或通知。指定backup仍必须是原生成功backup作业，不能用health或retain-guide替代；内容和恢复资格仍读取原report与bundle校验。

## 持久备份与恢复目标：待用户批准的默认方案

**提案，不是已设置策略：** 在既有唯一records真源之外，将原三文件恢复包复制到一处用户指定的独立受控目的地；另保留一份离线副本。备份不是第二可写业务库，不自动同步成新主库。目的地、可见性、访问主体及是否允许该资料再分发先确认；公开库权限不能继承到未选择的新服务。

建议每天最后一轮业务保存完成后备份一次；保留7份日备、4份周备、12份月备。实际配置前核资料规模与用户确认；本组不设schedule、不作任何清理。已有引用的历史档案不按这个副本轮换方案删除；清理副本还须确认至少另一份完整可恢复副本及引用保护。失败的新备份不得覆盖最后一份已核合格副本。

建议初始目标为最多24小时已保存数据损失（RPO）及4小时内恢复只读阅读（RTO）；这只是待验目标，不是承诺。RPO从真实业务快照衡量，不能用下载时间刷新；RTO覆盖取回、校验、环境准备和实际阅读，不仅计clone耗时。每次reader变更后及至少每季度做恢复演练，用原snapshot、摘要和真实阅读计划验收；本轮操作次数不冒充长期演练频率。

第一次复制按“核原生run/attempt/ZIP摘要 → 复制原字节 → 独立重新下载目标对象 → 逐字节与原包比对 → 原recover-room恢复”验收。目标对象ID、版本、摘要及保留规则写入原#13回执。没有目标读回，不称外部持久备份成立；Actions有限保留和对话附件都不能代签。

## 实际启用、停止和回退

启用前在原任务核四类事实：用户批准的具体周期/渠道/目标；唯一业务writer及当前所有来源/写入/排队/未知效果；确切main及适用CI、最后合格业务引用与可恢复快照；新旧执行责任和回退入口。巡检只覆盖三条交付宿主，不能单独解除这四项前提。定时不会默认启动新的Full、复活暂停来源或记录用户接受。

停止只在获得该操作授权后阻止后续触发；已运行任务先检查实际副作用，不自动cancel或假定取消等于回滚。失败写入沿原submission/expected/ref只读对账，不换key再发。来源429/权限拒绝沿原owner停止规则，不由巡检器代作重试。

回退工程用普通反向PR；业务切换保留原SourceRef/ArtifactRef与原records，恢复包只读打开不产生新writer。已写数据不因代码回退消失，已外发通知不可假定收回。旧Kernel在实际切换批准前不被本组停止；不同时开启两个采集/写入者“比较效果”。

## 复用、维护和退出

GitHub提供原生runs、attempt jobs和artifact metadata；现成GitHub CLI可人工查看，但本组组合三个不同宿主的原生身份、原项目预算及公开摘要，直接复用已有GitHubAPI而非再装客户端或搭建监控服务。2026-10-17核读官方[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)、[workflow jobs](https://docs.github.com/en/rest/actions/workflow-jobs)、[artifacts](https://docs.github.com/en/rest/actions/artifacts)及[workflows](https://docs.github.com/en/rest/actions/workflows)。服务端字段/分页变更时更新同范围消费者和反例，不以宽松解析吞掉未知状态。

维护 `application/runtime_health.py`、`interfaces/runtime_health_commands.py` 及原store-runtime显式health路由。退役用普通PR一起撤health选项/job、CLI/专属测试及原单次安装helper；原backup/retain-guide、恢复包、CI和业务保管不退。真实生产启用、外部持久副本和业务/人工验收仍各自留原件，不能由本说明代签。
