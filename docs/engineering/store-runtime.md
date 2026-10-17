# 手动存储运行与精确恢复

责任：#6共同保管；#13消费真实运行状态，#7维护研究解释。本入口是显式存储运维，不是代码CI、研究执行器或常驻采集器。沿[公开records绑定](../../config/store-binding.json)和[既有保管合同](../architecture/github-store.md)，不创建第二数据真源。

## 运行凭据和边界

`store-runtime.yml`只有`workflow_dispatch`，无PR/push/schedule触发；必须选`main`，代码checkout固定到该次`github.sha`，不保留Git凭据。复用现有固定版本Python安装action，但不运行或修改ci.yml；共享安装器不等于共享job凭据。

GitHub为每个job签发独立、短期的installation token。本运维步骤将**本job**的`github.token`显式注入`DECISION_OS_STORE_TOKEN`；不是读取、继承或回退到工程CI的GH_TOKEN/GITHUB_TOKEN，也无需长期PAT。GitHubAPI仍只读专用环境变量。只读备份job仅有contents:read，保管job仅有contents:write；其他可配置权限不授予。原有运行owner也可通过可信宿主注入自己的独立凭据，不允许来源正文或研究JSON决定凭据。

GitHub token的contents权限覆盖仓库，并非records路径级服务端ACL；同job可信代码可访问该权限，公开分支也没有隐私隔离。实际dispatch权限来自GitHub，不是环境变量自报role/human；main限制、显式expected head、固定材料及库内路径约束是额外行为保护，不能宣称完整角色隔离。不同手动运行用原生concurrency串行；其他连接器writer仍须在#6协调并依CAS拒绝竞争。未知写不自动重发。

## 两个有限操作

`backup`：先由受信 GitHubStore 认证仓库/visibility/marker并读取真实 records head，以它作为`expected_head`手动运行；不写 records。**完整逐版本审计改在精确固定的本地Git镜像上运行**，随后做原生完整 Git bundle，在新临时 bare 库离线恢复，并逐引用比较两次结果。旧远端逐来源扫描在真实多修订研究时会命中单操作512次API安全边界；此修复保留该上限、不分批重置凭据/预算、不绕过身份/授权。若原分支后续移动，只能恢复原已验证快照；绝不把旧快照说成最新。不因没有资料虚报产品已用。

`retain-guide`：只保管本文件在该次已审核代码SHA下的公开版本，source_id=`store-runtime-guide`，submission_id=`store-guide-<完整代码SHA>`。不接受任意来源URL、正文、路径或研究委托。首次写入用既有VersionedStore→GitHubStore→createCommitOnBranch/expectedHeadOid；任何应用层重新取head不得越过操作原expected head。返回确切SourceRef后，换只读绑定以同一意图恢复原回执，不允许重发，再完成备份/恢复。这个正常工程资料消费者不冒充投资研究、行情资料或定时Quick/交互Full通路。

写入失败或丢回执时，产物report.json保留原代码SHA、expected head、submission_id、意图摘要及已知引用；状态为failed或unknown-write而不是完成。先对账原意图；必要恢复使用**原run**及原输入，不在代码变更后换key重新导入。head变化且原意图不存在时拒绝，不自动适配新head。权限拒绝不换身份绕过。

## 备份、下载与独立恢复

GitHub Actions产物`dos2-store-<run>-<attempt>`保留原生records.bundle和report.json；现役手动operate另附确切已提交阅读代码/配置reader-source.tar，离线恢复到原阅读室见[恢复包](recovery-kit.md)。旧备份仍沿原verify-bundle读取，不要求历史产物补造源码。报告记录原代码/数据SHA、来源/研究引用、**认证的远端head与固定Git镜像审计（mirror_audit）/独立离线审计（restored_audit）**、bundle大小/摘要、实际结果；不含token或来源正文。2026-10-16以前的历史报告`online_audit`专指旧逐条远端审计，新的`mirror_audit`不得冒称实时远端逐条读取，仍可由原生Git包、确切快照、marker及独立fsck/逐引用验证证明。bundle包含该refs/heads/records的完整可达历史，**包括初始化anchor之前可达的公开工程历史**，并非只有当前树。恢复读者仍拒绝读取anchor前业务对象；不导出Git配置、工作树、secret或宿主设置。

产物按已声明30天期限保留，原生过期不删除records；应在过期前将下载文件放入已有获准保管位置。它是一次有限恢复快照，不是永久异地/跨账号灾备，也没有声称RPO、RTO或生产SLA已满足。只支持当前公开GitHub分支的备份上传；私有/受限资料不能借此公开。

先通过连接器核原生run/head/attempt/产物身份和ZIP摘要，再将ZIP作为数据解包。核bundle摘要与报告后，在安装相同代码版本的独立环境执行：

```bash
python -m decision_kernel.interfaces.store_runtime verify-bundle \
  --binding config/store-binding.json \
  --bundle /approved-backup/records.bundle \
  --snapshot <报告中的确切snapshot>
```

不需要网络/token；先在空库执行git bundle verify，拒绝缺前置的增量包、错误ref或超过64MiB包，再原生clone/fsck。只读GitSnapshot复用现有Git字节读取与VersionedStore校验，不转换SourceRef、不伪造GitHub API响应、不初始化另一主库。来源/研究正文与其确切历史引用逐项验证；审计范围是固定快照可见版本及其保留引用，不证明外部原件认证、研究正确、所有历史对象从未被管理员删改或线上当前权限。

## 维护与退出

无新Python依赖，Git/JSON/Hashlib与既有应用/适配器是全部基础。专属snapshot/runtime/audit与测试随当前手动运维用途维护；退出手动运行时一起退出workflow、入口及专属验证，不删除共享来源/研究版本、精确引用、幂等或CI保护。本次是否真实运行、读回及下一研究消费者仍由#6原生回执记录，不在本文维护状态表。

官方依据（本次有界复用）：[每job令牌](https://docs.github.com/en/actions/concepts/security/github_token)、[工作流权限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)、[Git bundle创建/完整前置/恢复](https://git-scm.com/docs/git-bundle)。原有GraphQL、存储/身份与许可边界沿GitHub保管合同，不重开旧实验或全面审计。
