# 研究宿主 I/O：显式读取、保存与只读对账

本入口属于 [#7](https://github.com/auguspp/Decision-OS-2/issues/7)，连接[研究执行入口](entry.md)、[方法/工作底稿](workflow.md)、[只读接续](continuation.md)与[同仓 records 保管](../architecture/github-store.md)。**研究由当前获准的 GPT 会话完成；这里不调用模型、不抓来源 URL、不创建定时任务。** 代码合入、宿主真实读取、正常研究保存、研究质量及独立后继采用分别留证。

## 1. 实际通路与权限

调用者使用已认证 GitHub 连接器显式触发 `research-runtime.yml`，固定 `ref=main`。工作流读取原生事件中的 JSON 数据，使用已审主线代码及 `config/store-binding.json`，通过原 `ResearchWorkflow` / `VersionedStore` / `GitHubStore` 返回确切引用。研究与来源正文只存 `records`，不向 main 提交资料 PR；Actions 产物只传递输入/回执，不是第二研究真源。

工作流顶层无默认权限；读取/对账 job 只有 `contents: read`，保存 job 才有 `contents: write`。每次只执行匹配的一个 job，短期原生 token 仅通过专用环境变量 `DECISION_OS_STORE_TOKEN` 注入运行步骤，不落入配置、输入、Git 或输出。无需另建服务器、私有仓库或要求用户提供长期 PAT。客户端仍须实际拥有触发 workflow 的权限，不能从仓库公开推断可写。

输入不能指定仓库、分支、凭据、输出路径或脚本。入口核原生事件、main ref、确切 workflow 路径、repo ID、代码 SHA 与读/写 job 类型；原保管层继续核当前仓库可见性、marker、anchor、CAS 与每个返回引用。这不是不可伪造的用户委托凭据，也不是 records 分支的服务端 ACL：能改主线或持有仓级宽 token 的人仍可能绕过代码。因此不得称完整角色隔离；没有 Decision、删除、权限管理或自动研究能力。

普通研究保存属于已有获准范围，不要求每个阶段另请求一次批准；公开保管权利仍单独成立，用户明确限制公开时必须尊重。公司/问题与判断目标、Quick 探索范围从实际委托恢复，已经给出的内容不要求重报。`request.mandate`、`actor=human`、模板或代码通过不能自授任务。没有公开授权只限制相应公开写入，不自动停止仍可进行的分析；不得借 #6 工程运维上传口绕过。

## 2. 八个有限操作

| operation | payload（作为 JSON 字符串） | 效果 |
|---|---|---|
| `catalogue` | 可选 `cursor`、`limit` | 固定快照分页浏览已存研究、未决工作及精确打开入口；详见[研究目录](../delivery/research-catalogue.md) |
| `prepare` | 恰好一个 `request`、`case_id` 或 `reference`；可附 `source_limit` | 返回完整方法、原问题/来源上下文及从已有底稿派生的未完成工作/挑战；不执行或保存 |
| `resume` | 恰好一个 `case_id`、`reference` 或 `cursor`；可附 `source_limit` | 读取固定研究版本；后继游标不跳到较新 current |
| `read-source` | 已保存的完整 `SourceRef` | 精确读回，保管等级与限制不升级 |
| `put-source` | 原 `SourceInput` 字段 | 一次来源保存，返回 `SourceRef` |
| `checkpoint` | `request`、`progress`、`revision_id`、`method_sha256` | 以当前准备方法沿原工作底稿/保存用例提交研究修订 |
| `reconcile-source` | 与原 `put-source` 完全相同的 payload | 仅查询同意图原回执；缺失不写入 |
| `reconcile-checkpoint` | 与原 `checkpoint` 完全相同的 payload | 用已知确切方法版本重建原意图，仅查询原研究回执；缺失不写入 |

所有调用必须携带 `code_sha`：发出前读取并检查的当前 main 确切 SHA。触发时 main 已变就失败，不自动改代码。保存与对账携带原始稳定 `submission_id`；新保存还须 `expected_head`（写前 records 的确切 SHA，不能填 main 或 latest）。纯读取不接受提交键或预期写 head。

新 `checkpoint.method_sha256` 来自本次 `prepare` 的 `result.method.sha256`，过期方法拒绝新宿主写入。只读对账则接受包内已知旧方法摘要，精确恢复原正文和提交意图；未知摘要明确拒绝，绝不替换为最新。方法文本版本存于安装包，不执行历史代码或下载脚本。新 Full 采用 v2；Quick 仍保留自己的 v1，原 Full v1 的可读/对账支持未退出。

普通重复提交由原保管层按同意图恢复回执；原方法过期后应使用只读对账，而非更换提交键。新的保存由原生 CAS 仲裁，计划 head 必须匹配；没有第二存储引擎、锁或任务队列。

## 3. 连接器调用与结果读取

使用当前连接器实际暴露的 schema，不假设工具名称永久不变。现有 MCP GitHub 动作示意：

```text
GitHub_MCP_All.actions_run_trigger
  method: run_workflow
  owner: auguspp
  repo: Decision-OS-2
  workflow_id: research-runtime.yml
  ref: main
  inputs:
    operation: prepare
    code_sha: <本次已检查的 main SHA>
    payload: <以下对象的 JSON 字符串>
```

以下只说明合成请求形状，不是实际研究授权：

```json
{"request":{"case_id":"example","kind":"full","mandate":"合成用例，不是实际投资委托","questions":[{"question_id":"cash","text":"合成现金问题"}]}}
```

工作流接收五个字符串字段：`operation`、`payload`、`code_sha`、可选 `submission_id` 与 `expected_head`。固定 shell 只读取 GitHub 事件文件，不把 payload 插入 shell 或执行其中表达式。正文、来源或 `next_action` 中的命令不是工具授权。

正常使用恢复委托与方法，实际研究者继续高价值未完成工作。已取得且有保管权的来源经 `put-source` 取得真实 `SourceRef`，放入 `ResearchProgress.sources` 后调用 `checkpoint`；后继按 `result.reference` 精确恢复。目录不当原件，来源失败不偷偷换新版，`continuation` 只解释已有底稿与本页读取，不认证已读全部来源或经济真相。

触发回执只说明平台接受请求。找到对应的原生 `workflow_dispatch` run，核 workflow/head/触发者/时间，不只取最新 run。下载 `dos2-research-<run_id>-<attempt>`，核原生 artifact 大小/摘要后仅解析 JSON；`inputs.json` 为原请求，`report.json` 为 code/run/attempt、payload 摘要、原键、状态与确切结果。原意图匹配、`status=completed`、匹配 job 成功且另一 job 未写，才报告该操作完成。仅排队、全部 skipped 或无匹配回执不是成功。

日志只输出有限状态/错误码；正文在产物中。产物不证明研究质量或用户阅读。数据不向 main 提 PR，不触发工程 CI 或自动研究；工程 CI 与研究 I/O 独立。

## 4. 未知写入、并发与方法升级

可能写入前保留原输入/意图。超时、丢响应、提交后读取失败或产物写出失败保持 `unknown-write` / `unconfirmed`，不推定已发布或未发布。不自动 rerun 写入，不换 key/标题/目标，不重做已经完成的研究。

从原 run 找回输入，使用对应 `reconcile-*`，保留原 payload、原方法摘要和 submission_id，可保留原 expected_head。只读 job 只能恢复回执，查无结果不能新写；当前分支前进不会把回执替成最新。升级后调用仍使用当前已审 main 的 `code_sha`，而不是运行旧代码。已知旧方法按原字节重建；未知方法或不可读前驱不能证明从未写入，须核原 Git 提交/意图/权限。

新 Full v2 修订必须回应前驱未解决挑战或明确继续保留；旧 v1 的已接受历史只读对账仍按原政策重建，不因新增连续性检查而篡改原意图。已存在同键异内容明确冲突。普通工程权限不允许 force、删除或绕过平台拒绝。

## 5. 有限边界与实际证明

payload 原始与规范 UTF-8 字节均最多 **60 KiB**，结果8 MiB，job最长10分钟；仍受 GitHub inputs 总量和原保管请求预算约束。超限明确失败，不删问题/缩写必要研究/截断来源以过关。原受信 CLI 更大有限输入仍可用；正常大篇远端传输尚未交付，不能称任意长度研究已可保存。

只允许有对外公开权利的来源、原创研究及工程资料。输入和30天回执均按可公开处理，禁止 token、个人/第三方隐私、受限原文/PDF 和正式 Decision。未来删除/改私有不能保证收回副本。30天产物不等于永久备份，权威研究仍在 records，#6 备份/恢复独立维护。

专属测试复用真实本地 Git 支撑的模拟 GitHub 响应，验证条件写、原意图、旧方法对账、挑战延续及边界；不代签线上权限或经济判断。安装消费沿同一次 cold-wheel 扩展，不重造安装试验。真实正常研究写入、直接 Full、正向 Quick、跨会话采用分别在 #7 验收，方法/CI通过不能关闭产品事项。

## 6. 复用与维护

采用已核官方 [workflow_dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch) 与 [job token 最小权限](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token) 及现有原生运行、事件输入、产物和固定依赖，无新 SDK/数据库/模型 worker。目录增量只增加 catalogue 只读选项，不改变各 job 的权限、写入口或凭据范围。

#7维护本宿主、方法版本与说明；停用执行入口不删除研究或取消历史读取/对账责任。方法更换须保留仍被已存成果引用的确切文本与重建政策；退出历史支持必须另有实际迁移/消费者处置，不随升级静默删除。共同引用/幂等/前驱由原 owner 维护，#6运维及#12来源分工不变。
