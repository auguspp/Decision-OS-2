# 日更外部时钟：固定请求、原生产者、单一主时钟

责任：#13 的调度和观察；#10 继续维护原行情、主表、来源预算和失败恢复。本入口来自用户在 #205 收口期间要求补齐外部 cron；具体配置、原生运行和启用回执留在原事项。本文是操作合同，不能替代外部账户的配置读回或自然运行证明。

## 时钟与业务分开

外部 cron 只向既有 `daily-market.yml` 发一次固定 `workflow_dispatch`。同一个 GitHub workflow 继续执行准入、原六股日更、保管、发现状态和正常阅读；不增加中转服务、内部二次 dispatch、数据真源或来源队列。

`config/daily-market-clock.json` 只有 schema 与 primary 两个字段。primary 可为 `github-schedule` 或 `external-cron`；缺件、未知字段、坏 JSON 或未知值拒绝准入。初始主时钟为 `github-schedule`，保留工作日18:35的GitHub schedule。正式切换 PR 必须一次将 primary 改为 `external-cron` 且退役同责 workflow `on.schedule`；只有服务商原任务已保存、保持 Inactive、按原配置独立读回、原时钟在途/未知效果已核并获得适用完整CI，才可将此PR合入。合入的代码配置不证明服务商已启用、请求来自该服务商或自然日更成功。

原 `config/daily-market.json`、policy digest、seed、cycle schema 和已保管 generation 不因换钟改变。业务时间仍由原 policy 的工作日上海 18:35 及不足 24 小时的迟到窗口解释；证券交易日以原日历和 completed-session 资格为准，不能用星期或外部任务自报日期替代。

## 三种调用身份

| 入口 | 代码绑定与实际行为 |
|---|---|
| 原 status / update / recover-late / repair-calendar / reconcile-write | 继续要求显式确切 40 位 `code_sha`；来源和恢复操作保持原授权及有限范围。 |
| `external-clock` | 输入必须为 `code_sha=current-main`，先绑定原生 `GITHUB_SHA` 的 workflow 身份，再按受审 daily-runtime 配置选择实际执行树，分别核两份适用 main CI。只有 primary=external-cron 才能进入原 daily job；canonical operation 为 update，回执保留真实 invocation。 |
| `clock-probe` | 同样把 current-main 绑定原生 SHA，只检查仓库/main/run/CI/clock 配置元数据。独立只读 job 不实例化来源、Store 或阅读模型；0 source、0 records 读写、0 dispatch，不进入 status/daily、Discovery 或 Reading。它可在外部生产尚未启用时验收工程接缝。 |

两种新入口不接受调用者指定业务日期、cron、expected records、备用 workflow 或任意代码。旧手动操作不能用 `current-main` 绕过 exact-SHA 要求。visibility 在注入来源密钥之前拦住非主时钟；真实生产/消费者再次检查，probe 也不能通过直接调用生产命令落入普通读取。

外部服务商继续 dispatch `ref=main`，不因[批准执行版本](../architecture/daily-market.md#批准的执行版本与在线撤销)改填旧提交或改变 body。visibility 输出冻结的执行凭据，status/daily checkout 已批准 SHA；Discovery、Reading 和发布沿同一代码树。原生 `GITHUB_SHA`/`GITHUB_WORKFLOW_SHA` 保留，回执将 `workflow_sha` 与实际 `code_sha` 分开。clock-probe 继续原 main 上的零业务检查，不证明 pinned 版本已经生产运行。

同一 `decision-os-relay-source` concurrency 保持。相同业务日复用原 cycle；完成后再次调用不重采，blocked/source_hold、in-flight 未知和未成功前周期继续阻断，pending 仅接原未预留单元。外部重复 tick 不增加来源预算、不重置失败或换日跨过旧 hold。排队跨午夜仍冻结原生 run.created_at；时钟源不改变来源真实 capture 时间。

`workflow_dispatch` 和固定 run-name 只标识选择了哪个入口。持有相应 GitHub 权限的人也能手动调用它，因此 `external-clock` **不是 cron-job.org 来源认证**。自然外部触发必须另核同一服务商 job 的计划/执行时间、请求结果与确切 GitHub run；无法唯一对应时保留未知。

原生 run 的 `name` 与 `display_title` 均使用该入口的 run-name；例如 probe 的两项均为 `decision-os-daily-market / clock-probe`。visibility 与实际宿主按现有 `run_title(invocation)` 映射核验 `name`，两种新入口另核 `display_title`；不能把另一操作标题或工作流静态名称当作本次入口身份。[首次 probe 39698075722/1](https://github.com/auguspp/Decision-OS-2/actions/runs/39698075722) 因错误要求静态名称而在 visibility 拒绝，原失败保留；修正字段核验不改变当前 main、CI、权限或零业务约束。

## cron-job.org 可填写配置

复用旧 Kernel 已采用的服务，但创建/核对本仓独立任务，不能覆盖或停用旧 Sector/Inbox 任务。先按标题和完整 URL 查重，恢复原 job ID 及未确认请求；任务保持 **Inactive**。

| 字段 | 值 |
|---|---|
| Title | `Decision OS2 Daily Market` |
| URL | `https://api.github.com/repos/auguspp/Decision-OS-2/actions/workflows/daily-market.yml/dispatches` |
| Method | POST |
| Timezone | `Asia/Shanghai` |
| Schedule | 工作日 `35 18 * * 1-5`，即北京时间 18:35；不是在本地时区填 GitHub 的 UTC `35 10` |
| Body | `{"ref":"main","inputs":{"operation":"external-clock","code_sha":"current-main"}}` |
| Headers | `Accept: application/vnd.github+json`；`Content-Type: application/json`；`X-GitHub-Api-Version: 2026-03-10`；Authorization 只在服务商受控凭据位置填写 |
| Timeout / redirects | 30 秒；3xx 不作为成功 |
| Responses | 保存响应，以便核对返回的原运行身份与未知效果；不保存或输出请求凭据 |
| Notifications | 本组全部关闭；不新增邮件、Webhook、Slack 或其他通知 |

[停用配置 JSON](examples/daily-market-cron-job.json) 使用 cron-job.org REST 字段，`enabled=false`，Authorization 只放明显占位符，不能原样提交为有效凭据。服务商 API 的鉴权密钥与任务向 GitHub 发送的令牌是两个不同凭据。GitHub 令牌限定本仓、Actions write，值由账户持有人在受控界面配置，不进入聊天、Git、Issue、终端命令或公开日志；不给它供应商 Key 或 records 写权限。Actions write 本身仍有工作流管理能力，不宣传为只能触发这一条 workflow 的服务端隔离。

接口结构依据 2026-10-21 核读的 [GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event) 与 [cron-job.org REST API](https://docs.cron-job.org/rest-api.html)。上述 API 版本的原响应可返回 workflow run ID/URLs；响应被接受只证明触发请求接收，不证明 Actions 已开始、业务完成或资料新鲜。网络超时、5xx、形状不完整或无法识别原 run 时先核服务商原 execution 和 GitHub 原列表，不点击 Test Run、换任务或重发来求绿。401/403/429 及明确平台拒绝按原意图停止。

## 工程 probe 与实际切换

1. 原责任登记代码分支、唯一 writer、当前 main/records、外部 job 身份和未确认效果。正在执行的旧主线生产按原 run 收口后才切换；不能用“当前没有 queued”替代共享窗口的实际交接。
2. 完成该入口的正常 PR、Ready full、普通 merge、actual-main 适用证据。primary 仍为 GitHub；这一步不启用外部生产。
3. 对合格 main 执行一次 `clock-probe`，只改变请求 body 的 operation，其余固定。核原生 run/attempt/head、独立 probe job、原 artifact 字节和 0 业务副作用。若用 GitHub 工具执行，只证明 GitHub 入口；若从原外部 Inactive job 的一次 Test Run 执行，还应核服务商原执行与同一 run。两层不能相互代签。
4. 外部 job 恢复 production body 后仍保持 Inactive，独立读回 title、URL、POST、body、非秘密 headers、时区、星期/时间、通知关闭、enabled=false 和唯一 job ID。记录实际 main 与预定首个业务槽。凭据读回只记录已配置，不展示值。账户不可访问时，止于可部署工程，不把启用前提写成已成立。
5. 核清原主时钟在途/未知效果后，在同一普通切换 PR 中将 clock primary 设为 external-cron，并移除 `daily-market.yml` 的 `on.schedule`；同时更新原时钟静态断言，保持历史 schedule 读取支持。两份配置、Ready full 与 actual-main 资格均成立后，启用**一个**已核原外部 job，并读回 enabled 与下次计划时间。没有服务商原配置前不先撤掉现有时钟。
6. 不把启用动作再变成一次生产 Test Run。下一次自然调用核服务商计划/实际时间与确切 GitHub run、native created_at 入场槽、实际 daily job、原件及来源/消费者结果。工程 probe、配置存在、实际启用、首次自然成功和连续覆盖分别记账；之后仍按原生产观察计划积累证据。

正常 health 读取固定 main 的两个工程配置，分别展示历史 GitHub schedule 与外部入口；选择当前主时钟作为当前业务槽的触发/执行观察依赖。外部入口成功仍不能证明服务商来源、准点性或业务采用。probe 独立显示，不能补齐生产槽；手动成功、次日成功不能抹掉旧日缺口。详情见[生产巡检](production-readiness.md)。

时钟切换改善触发机会，不保证源采集在 18:40 Watch 或 19:15 Brief 前完成。下游继续按实际截止、来源鲜度与有无新结果判断，不把名义时间顺序当数据已经就绪；原 Brief/Watch 平台拒绝不由新时钟解除。

## 停止、回退和退出

需要停用外部钟时先停原服务商 job 并读回，再按原 run/源请求/records 处理在途和未知效果；不自动取消执行，也不删除原件。回退 native 主时钟须普通 PR 同时恢复 schedule 与 primary=github-schedule，完成适用 CI 后生效。切换空档明确记录，不通过双开生产者填补；代码回退不撤销已经发生的数据写入。

本组不迁移旧 Kernel 的任务或研究、不新增 Watch/Brief 生产者、不通过 cron 重试原拒绝。新闻连续采集→保管→rolling 是原 #13 的另一责任，不能只对 news-intake 多发 POST 就称已交付。

维护 owner 为 #13：固定输入和主时钟准入、probe、健康消费者及本配置示例一起维护；原来源状态/预算归 #10。退出外部时钟支持时一起移除新 operation、probe job/命令、clock 配置、专属测试、外部观察和本例，并核服务商停用；保留原 cycle、历史 schedule/dispatch 原件、共享 CI、Store 与正常日更责任。

## Kernel 复用依据

固定参考 `auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8`。原 [Sector 仓库切换回执](https://github.com/auguspp/decision-kernel/issues/297#issuecomment-5681146292)与[实际启用回执](https://github.com/auguspp/decision-kernel/issues/297#issuecomment-5681194598)支持“外部时钟→原执行者、先验证后切换”；[触发类型改变导致下游 skip](https://github.com/auguspp/decision-kernel/issues/297#issuecomment-5715919759)要求同时核实际消费者。这里只适配现仓入口，不继承旧源重试额度、旧任务授权、旧业务状态或未确认的服务商账户状态。
