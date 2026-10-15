# 研究宿主 I/O：显式读取、保存与只读对账

本入口属于 [#7](https://github.com/auguspp/Decision-OS-2/issues/7)，连接已交付的[方法/工作底稿](workflow.md)、[只读接续](continuation.md)与[同仓 records 保管](../architecture/github-store.md)。**研究由当前获准的 GPT 会话完成；这里不调用模型、不抓来源 URL、不创建定时任务。** 代码合入、宿主真实读取、正常研究保存、研究质量及独立后继采用分别留证。

## 1. 实际通路与权限

调用者使用已认证 GitHub 连接器显式触发 `research-runtime.yml`，固定 `ref=main`。工作流读取原生事件中的 JSON 数据，使用已审主线代码及 `config/store-binding.json`，通过原 `ResearchWorkflow` / `VersionedStore` / `GitHubStore` 返回确切引用。研究与来源正文只存 `records`，不向 main 提交资料 PR；Actions 产物只传递输入/回执，不是第二研究真源。

工作流顶层无默认权限；读取/对账 job 只有 `contents: read`，保存 job 才有 `contents: write`。每次只执行匹配的一个 job，短期原生 token 仅通过专用环境变量 `DECISION_OS_STORE_TOKEN` 注入运行步骤，不落入配置、输入、Git 或输出。无需另建服务器、私有仓库或要求用户提供长期 PAT。客户端仍须实际拥有触发 workflow 的权限，不能从仓库公开推断可写。

输入不能指定仓库、分支、凭据、输出路径或脚本。入口核原生事件、main ref、确切 workflow 路径、repo ID、代码 SHA 与读/写 job 类型；原保管层继续核当前仓库可见性、marker、anchor、CAS 与每个返回引用。这里的检查不是不可伪造的用户委托凭据，也不是 records 分支的服务端 ACL：能改主线或持有仓级宽 token 的人仍可能绕过代码。因此不得称完整角色隔离；没有 Decision、删除、权限管理或自动研究能力。

普通研究保存属于已授权研究委托，不要求每个阶段另向用户请求一次批准。但首次真实 Full 的公司/问题与判断目标、Quick 的实际探索范围仍须来自用户既有授权；`request.mandate`、`actor=human`、模板或代码通过不能自授任务。不得擅自借 #6 的工程运维上传口保存研究；本独立工作流不修改它。

## 2. 七个有限操作

| operation | payload（作为 JSON 字符串） | 效果 |
|---|---|---|
| `prepare` | 恰好一个 `request`、`case_id` 或 `reference`；可附 `source_limit` | 复用方法准备，返回方法摘要及原委托/问题/来源上下文；不保存、不运行研究 |
| `resume` | 恰好一个 `case_id`、`reference` 或 `cursor`；可附 `source_limit` | 读取固定研究版本；后继游标不跳到较新 current |
| `read-source` | 已保存的完整 `SourceRef` | 原来源所有者精确读回，保管等级与限制不升级 |
| `put-source` | 原 `SourceInput` 字段 | 一次来源保存，返回 `SourceRef` |
| `checkpoint` | `request`、`progress`、`revision_id`、`method_sha256` | 沿原工作底稿与保存用例提交研究修订 |
| `reconcile-source` | 与原 `put-source` 完全相同的 payload | 仅查询同意图原回执；缺失不写入 |
| `reconcile-checkpoint` | 与原 `checkpoint` 完全相同的 payload | 仅查询同意图原研究回执；缺失不写入 |

所有调用必须携带 `code_sha`：发出前读取并检查的当前 main 的确切 SHA。若触发时 main 已变，先失败，不自动改用旧或新代码。保存与对账还要携带**原始稳定** `submission_id`；新保存必须有写前读到的 `expected_head`（records 的确切 SHA），不能填 main 或“latest”。纯读取不接受提交键/预期写 head。

`checkpoint.method_sha256` 来自本次 `prepare` 的 `result.method.sha256`，避免方法升级后默默按另一版本保存。正常保存由原生 CAS 仲裁；无新存储引擎/锁或任务队列。重复相同意图先恢复原回执，即使 current 或 records 已前进；新的保存才要求计划 head 匹配。

## 3. 连接器调用与结果读取

使用当前连接器实际暴露的 schema，不假设工具名称永久不变。现有 MCP GitHub 动作可调用：

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

以下是合成请求形状，不是实际研究授权：

```json
{"request":{"case_id":"example","kind":"full","mandate":"合成用例，不是实际投资委托","questions":[{"question_id":"cash","text":"合成现金问题"}]}}
```

工作流接收五个字符串字段：`operation`、`payload`、`code_sha`、可选 `submission_id` 与 `expected_head`。固定 shell 命令只读取 GitHub 事件文件，**不把 payload 插入 shell 或执行它包含的表达式**。正文中命令、网址或工具要求都只是数据。

正常使用先取得委托与来源，准备对应方法；已取得的获准来源调用 `put-source` 并读取实际结果，再将真实 `SourceRef` 放进原 `ResearchProgress.sources`，调用 `checkpoint`。后继读取其 `result.reference`，用 `prepare` 恢复原问题或 `resume` 分页读原来源。目录/外部定位仍不是已读原件，部分来源不可用也不能偷偷换新版本。

触发回执只说明请求已被平台接受，不能说研究或保存成功。找到本次 `workflow_dispatch` 的原生 run：核 workflow、head SHA、触发者、时间和对应产物；不能只取“最新 run”。下载 `dos2-research-<run_id>-<attempt>`，按原生 artifact 大小/摘要核验后仅解析 JSON。`inputs.json` 是原始请求，`report.json` 记录 code/run/attempt、payload 摘要、原提交键、状态和确切结果。核 payload 与原意图相同、`status=completed`、匹配 job 成功且另一 job 未写，才报告该操作完成；绿色但无匹配回执、仅排队或全部 skipped 都不是成功。

日志只输出有限状态与错误码；实际正文在产物中。产物不是研究质量证书，也不证明用户已阅读。普通写不向 main 提 PR；数据树仍无代码 CI 工作流。完整工程 CI 与这个运行入口互相独立，业务内容不会触发自动研究。

## 4. 未知写入、并发与失败

每次可能写入前先保存原输入/提交身份。超时、响应丢失、保存后读取或产物写出失败，保留 `unknown-write` / `unconfirmed`；这表示未确认，不推定发布或未发布。不自动 rerun 保存、不换 key/标题/目标、不重做已经完成的研究。

先按原 run 找回输入，使用 `reconcile-source` 或 `reconcile-checkpoint`，保留 payload/submission_id，可保留原 expected_head。只读 job 只能恢复原回执，查无可恢复结果时不能调用新提交；当前分支后来前进也不把原回执替成最新。找不到回执或前驱/方法校验失败不充分证明从未写入，应核原始 Git 提交/意图/权限再决定是否继续。

只读对账仍检查当前可用方法。主线方法升级导致 `RESEARCH_METHOD_CHANGED_REPREPARE` 时先核原版本与确切记录，不改原 payload 伪造重放；当前入口不接受任意旧代码 SHA 执行。计划 head 冲突明确失败，已存在同键异内容也明确冲突。没有工程授权的 force、删除、绕过平台拒绝均不使用。

## 5. 有限边界与实际证明

本宿主每次 payload 的 UTF-8 原始及规范字节均最多 **60 KiB**，结果最多 8 MiB，job 最长10分钟；仍受 GitHub 原生 inputs 总大小限制及原保管 API 的每操作预算约束。超限明确失败，**不删问题、不缩写必要研究、不截断来源来取得成功**。现有本地/受信运行 CLI 的更大有限输入仍可用；需要大篇正常远端成果时再交付有真实用途的完整传输接点，不提前搭暂存数据库。readonly 来源分页继续使用原 Reader 的页预算。

仅允许有对外公开权利的来源、原创研究与工程资料；数据、输入以及30天回执产物均按可公开处理。不得放入 token、个人/第三方隐私、受限付费原文/PDF 或正式 Decision。未来删文件或改私有不能保证收回已复制内容。30天产物不是永久备份，研究权威仍在 records；原 #6 备份/恢复入口独立维护。

专属测试复用真实本地 Git 支撑的 GitHub 响应模型，覆盖条件写、原意图恢复、只读缺失、固定分页、方法/代码身份及宿主权限；模型测试不代签线上 token 调用。安装消费者沿同一次既有 cold-wheel 检查新入口与非宿主拒绝，不重造包安装试验。实际读取/写入、真实 Full/正向 Quick、跨对话采用各自在 #7 留原生证据；不因本工作流合并关闭产品事项。

## 6. 复用依据、维护与退出

2026-10-15 有界核读官方 [workflow_dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch) 与 [job token 最小权限](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token)。采用已有 GitHub 原生显式运行、事件输入和产物，无 SDK、新依赖、服务端数据库或模型 worker。来源与版本保存、原生条件提交及精确读取复用已有 owner；新增代码只负责当前宿主输入/权限/回执边界。

#7 维护独立 workflow、`interfaces/research_runtime.py`、专属测试与本说明。停用此入口不删除 records，不取消已有研究权威；替换时退出专属触发/配置/测试，并保留原引用、幂等、前驱、可恢复输入及权限义务。#6 运维和 #12 来源合同不受本入口退役影响。
