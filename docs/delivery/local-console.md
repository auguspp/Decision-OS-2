# 本地只读控制台：把读搬回本机，把写与定时留在 GitHub

责任：[#221](https://github.com/auguspp/Decision-OS-2/issues/221) A（只读工作台）与 D（瘦身）的交点。入口清单与退役判据见 [交付入口清单](../architecture/workflow-estate.md)，本页只讲读这条路怎么离开 runner。

现有读操作**不需要 runner**：它需要的是精确的 store、一个不能写的令牌、以及一份说明「这次读的是哪个 store、跑了哪条命令」的回执。本地只读控制台把这三件事固定下来，其余照旧复用 `decision-os store` 的同一份实现——读只有一份代码，不新开第二条读取路径。

## 为什么值得搬

对最近 100 次运行的观测（2026-10-23，只是快照，不进契约）：`research-runtime` 被派发 51 次，其中 **39 次只读**；只读运行的中位耗时 37 秒、p90 101 秒，且其中 5 次在只读阶段就失败。本地读取省去 runner 派发，但仍需访问 GitHub；下面说明祖先证明、完整树目录与确切正文的本地复用，以及 Catalogue 对现有单次读取作用域的复用。实际端到端耗时仍取决于宿主链路与镜像可用性。搬走的收益不是机器费（本仓 public，分钟数本就不计费），而是三样更贵的东西：

1. **延迟**：等 37–100 秒读一份 catalogue。
2. **噪音**：只读也会因为 runner/checkout 变红，红一次就要人解释一次。
3. **耦合**：读取还要经过派发、runner 与代码准入。2026-10-23 的严格 main 相等检查曾使日更被合并撞停；现行后端已允许代码仍在 main 祖先链内的正常前进，这个历史故障不代表当前仍要求 SHA 相等。本地读取直接复用 store，不依赖 runner 派发。

## 参数解析与六道本地闸

本入口先复用 `decision-os store` 的参数解析，再执行本地预检。缺少必填参数、未知命令或互斥参数冲突，由 argparse 输出 usage 并退出 2；此时尚未进入下表，也未创建 API。例如，`put-source` 缺少 `--submission-id` / `--input`，或 `prepare-decision` 缺少 `--input`，会先在参数解析时退出。下表错误码适用于语法已通过的命令。

| 失败码 | 含义 |
|---|---|
| `LOCAL_READ_REFUSES_WRITE_COMMAND` | 不在只读白名单里（写、reconcile 一律不在） |
| `LOCAL_READ_PRIVATE_CUSTODY_REQUIRES_GITHUB` | `prepare-decision` / `read-decision-preparation`：私有决策准备不落笔记本 |
| `LOCAL_READ_REQUIRES_A_BINDING` | 必须给 `--binding`；语法完整地选择 `--path` 也不能接入本地合成 store |
| `LOCAL_READ_TOKEN_REQUIRED` | 必须先设 `DECISION_OS_READ_TOKEN` |
| `LOCAL_READ_REFUSES_STORE_TOKEN` | 环境里出现 `DECISION_OS_STORE_TOKEN` 就拒绝，避免误用可写令牌 |
| `LOCAL_READ_REQUIRES_READ_ONLY_BINDING` / `LOCAL_READ_PUBLIC_STORE_ONLY` | binding 必须 `write_enabled=false` 且 `visibility=public` |

binding 先通过共享结构与策略校验。`visibility=private` 且 `private_upgrade_allowed=true` 会先返回 `INVALID_PRIVATE_UPGRADE_POLICY`；合法的 private binding（`private_upgrade_allowed=false`）才进入本地 `LOCAL_READ_PUBLIC_STORE_ONLY` 检查。以上参数解析和本地预检都在**构造任何 API 客户端之前**完成，被拒的命令不会产生网络请求，也不会读到 records。

配置通过后，还须核 GitHub 返回的**实际仓库可见性**。本地入口使用预检得到的同一 binding，并在本次执行中关闭 `private_upgrade_allowed`；不修改保管配置、历史锚点或 hosted 的私有读取资格。初始实际 private 在读取 records 前拒绝；过程中观测到 public→private，则拒绝后续 JSON、HTML 和附件导出。配置中的 `visibility=public` 本身不是这项实际核验。

## 白名单等于「当前全部读命令」

`READ_ONLY` 与上游命令表逐项相等，由 `tests/test_local_read.py` 守着：上游新增一条读命令时这条测试会红，强制重新裁定一次要不要放进本地。当前包含：

- 研究：`research-catalogue`、`prepare-research`、`audit-numbers`、`audit-source-use`、`verify-reference-fixing`、`resume-research`、`current`、`search`、`read-source`、`read-revision`、`research-room`
- 议程与复盘：`research-agenda`、`read-research-agenda`、`research-outcome-review`、`read-close-structure`、`read-opening-auction`、`read-auction-next-session`
- 交付：`verify-delivery`、`open-delivery`、`read-artifact`
- Odds：`odds_commands.READ_COMMANDS` 全部

不在白名单：所有 `*WRITE*`、所有 `reconcile-*`、`put-source`、`submit-revision`、私有决策准备两条。

## 回执

通过参数解析和本地预检、进入共享执行后，命令会在 **stderr** 输出一行 `{"local_read_receipt": …}`，包括执行阶段失败的情况。参数解析或本地预检失败只输出对应错误，不生成这份执行回执。stdout 保留共享执行的原样结果；设 `DECISION_OS_READ_RECEIPT=<path>` 可另存已生成的执行回执。

以下为结构示例，计数和时长不代表现场验收：

```json
{
  "schema": "local-read-receipt-v1",
  "operation": "research-catalogue",
  "runner": "local",
  "store": {"repository": "auguspp/Decision-OS-2", "branch": "records",
            "anchor": "06588c36…", "store_id": "decision-os-runtime", "visibility": "public"},
  "identity": {"token": "read-only-required", "write": "not-requested",
               "authority": "local-read-only-console"},
  "ancestry": {"schema": "local-git-ancestry-v1", "mirror": "local-git-graph",
               "answered": 1, "declined": 0},
  "tree_inventory": {"schema": "local-git-tree-v1", "mirror": "local-git-graph",
                     "answered": 1, "declined": 0},
  "blob_content": {"schema": "local-git-blob-v1", "mirror": "local-git-graph",
                   "answered": 1, "declined": 0},
  "started_at": "…", "finished_at": "…", "duration_ms": 820, "exit_code": 0
}
```

`ancestry` 保留原有祖先证明计数；`tree_inventory` 单独记录完整树目录尝试，`blob_content` 单独记录确切正文字节尝试。各项 `answered` 是本地对象图答上的次数，`declined` 是本地尝试拒绝、交回原远端路径的次数；`mirror=absent` 表示未提供镜像，启用时为 `local-git-graph`。本地完整输出仍须通过原 Store 与应用层校验，计数不证明整个读取成功，也不记录提交 id、路径、正文或凭据。镜像落后或对象缺失可以导致回落；目录超限、正文损坏或不支持的仓库配置也会导致回落，不能把所有 `declined` 都诊断为需要 fetch。

回执说明的是**这次读所声明的身份与执行情况**，不是「已验证」。`identity.token=read-only-required` 表示凭据要求，不证明 token 的实际权限已被核验；只读权限资格须由宿主提供原生权限依据。回执不能替代 Ready/Gate 原件，也不能替任何研究提交或 Human Decision 签字。

## 用法

```bash
export DECISION_OS_READ_TOKEN=<fine-grained PAT, Contents: Read-only>

# Fetch explicitly before reading; local object reads never fetch missing data.
git fetch origin records:refs/remotes/origin/records

python -m decision_kernel.interfaces.local_read \
  --binding config/store-binding.json --git-mirror . research-catalogue --limit 4

# Create a plan with the actual reading time. This is not a new research cutoff.
python - <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path

plan = {
    "as_of": datetime.now(timezone.utc).isoformat(),
    "catalogue": {"limit": 4},
    "ai_brief": {"select": "latest-ai-brief"},
    "odds_book_view": {
        "query": {"limit": 4, "scan_limit": 16},
        "page_limit": 1,
    },
}
with Path("local-room-plan.json").open("x", encoding="utf-8") as handle:
    json.dump(plan, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

python -m decision_kernel.interfaces.local_read \
  --binding config/store-binding.json --git-mirror . research-room \
  --input local-room-plan.json --out local-room.html
```

计划文件与 HTML 路径须为新文件；重复读取时使用新的文件名，不覆盖已有原件。`as_of` 是此次阅读时钟，已存 Brief、研究和行情仍显示其原日期，缺失不会变成今天的新内容。Research 与 Odds 只取本次有界页；若返回 `partial` / `next_cursor`，按返回的确切后继请求接续，不能把尾部未读写成没有结果。AI Brief 与 Odds 页均绑定同一 Room 快照。

`--git-mirror` 也可由 `DECISION_OS_GIT_MIRROR` 提供。同一镜像可回答祖先证明、已核确切树的完整目录，以及其中已通过 Store 校验的普通文件正文；本地不能回答时，分别回落原有 `compare`、树目录或 blob GET。不给镜像就保留原有远端对象读取路径。本地读取不会自动补镜像，需由用户在读取之外显式执行 `git fetch`。

令牌要求：**只读**。公开仓理论上可匿名读，但匿名会撞跑 API 限额且无法区分调用者，所以仍要求一个只读令牌，而不是复用运行时令牌。`DECISION_OS_STORE_TOKEN` 是运行时唯一 writer 用的名字，本地一律拒绝。入口核读令牌是否存在并禁止环境中的 writer 令牌，不查询或认证 token 的实际授权范围；把具写能力的 OAuth token 放进 `DECISION_OS_READ_TOKEN` 不会让它变成只读凭据。使用这种凭据的获准读路径验证仍须保留其权限偏离，不能签收只读凭据资格。

## 证据链第一跳：把「数提交」换成「验哈希链」

2026-10-23 在作者工作站（中国网络、无代理直连 api.github.com）实跑 `research-catalogue --limit 1`，最初卡在这里：

| 请求 | 耗时 | 结果 |
|---|---:|---|
| `GET /repos/auguspp/Decision-OS-2` | 1.8 s | OK，6 070 B |
| `GET /repos/auguspp/Decision-OS-2/git/ref/heads/records` | 0.7 s | OK，362 B |
| `GET /repos/auguspp/Decision-OS-2/compare/06588c36…87e9c38d?per_page=1` | 47.5 s | **REMOTE_RESPONSE_UNAVAILABLE** |

失败点是 `GitHubStore.head()` 的**祖先证明**：binding 的 `anchor` 是 `06588c36`，与当前 `records` head 相隔 **1127 个提交**，`compare` 要计算整段差异；在本机链路上超过 `urlopen(timeout=30)` 后连接被丢弃。runner 在 us-east 到 GitHub 往返只有几十毫秒，所以同一个调用从来没在 CI/生产里暴露过。

**根因是问法，不是超时。** `compare` 枚举 anchor 与 head 之间的每一个提交，成本随距离线性增长；而「anchor 是不是 head 的祖先」这个问题的答案**已经写在 head 自己身上**——提交 id 是对内容（含父指针）的哈希。所以只要手上有一个包含该 id 的对象图，就不必请 GitHub 去数那 1127 个提交。

`--git-mirror` 就是这张图。实测对照：

| 路径 | 耗时 | 说明 |
|---|---:|---|
| `GET /compare/…?per_page=1`（同一链路上两次） | 7 s / 17 s | 曾达 47.5 s；`ahead_by=1127`，`per_page=1` 限不住服务端计算 |
| `git merge-base --is-ancestor`（本地） | **0.67–1.14 s** | 当时镜像与链路下的本地测量，不承诺固定耗时 |

祖先证明的本地加速不是新的信任根，有三条边界（`src/decision_kernel/infrastructure/git_ancestry.py` 头部写全）：

1. **只回答远端已经返回的提交 id。** 调用方仍然先取 live ref，所以 head 一动问题就换。
2. **答不上来就拒绝，永不猜。** 对象缺失 → `AncestryUnavailable(LOCAL_ANCESTRY_OBJECT_MISSING)`，随即**回落到 `compare`**。把「镜像过期」误判成「不是祖先」会凭空指控 store 被改写，这是唯一不能犯的错。
3. **不校验自己的对象库。** 改写 `.git/objects` 里某个对象的父指针而保持 id 不变，与改写本模块源码一样，都假设执行机器可信。

判决与错误码与远端**完全一致**（`COMMIT_OUTSIDE_BINDING`），调用方无法从行为上分辨哪条路回答；只有回执记这件事。

### 镜像必须与 head 同代，否则等于没开

2026-10-23 22:51 的实跑回执：`answered=1, declined=2, exit_code=0`。当天的 `records` head 正被日更写入推着走（`87e9c38d` → `cf889c0a`），本地镜像停在旧 head，于是两次祖先证明本地答不上来、回落 `compare`，整次读花 **163 s** 才拿到结果——拿到了，但慢在回落。

因此应**先 `git fetch` 再读**。当时本仓 `records` 全量 13216 个对象、8.6 MB，fetch 实测 5 s；这是历史观测，不是当前规模或耗时保证。祖先证明本地命中只省去对应的 `compare`，目录与正文等其他请求仍可能失败。

## 同一镜像回答确切树目录

[#246](https://github.com/auguspp/Decision-OS-2/issues/246) 记录了另一处真实阻塞：2026-10-25 的 WorkBuddy 验证中，祖先证明已由本地回答，但 records 的递归树列表仍需下载约 2.36 MB JSON，在该宿主链路超时或断流。该现场使用经用户批准的宽权限 OAuth、Python 3.13.14 和 `PYTHONPATH=src`，凭据与加载方式的偏离按原回执保留，不作为只读凭据或标准安装的验收。初次树目录修复只处理这一类请求，不改变正文读取、权限或保存格式。[原机补验](https://github.com/auguspp/Decision-OS-2/issues/246#issuecomment-6395373457)随后确认 main `41301272f4031a2ff296ba955900edb68a8288b6` 的原功能阻塞已解除；Catalogue 138.7 秒、Room 189.3 秒的成功读取仍有性能不足，由 [#249](https://github.com/auguspp/Decision-OS-2/issues/249) 继续处理。这些是各自确切版本的现场事实，不保证后续版本的耗时。

本地入口的 `_PublicReadAPI` 继续包装原生 GET。只有它已从**本仓确切 commit 的原生响应**取得、且响应 `sha` 与请求 commit 匹配的 `tree.sha`，才允许用 `LocalGitTree.read` 回答该树的递归目录请求。本地结果仍交给原 `GitHubStore._tree` 执行相同的目录校验，不另设一份 Store 读取规则。

这条加速有以下边界：

- **远端身份与权限仍是前提。** 仓库 public 核验、live ref、确切 commit→tree 关系，以及原有 marker、blob、正文与摘要校验，继续走原路径。镜像分支头不能充当远端当前 head，任意本地 tree id 也不能取得目录读取资格。
- **只回答确切对象，不更新镜像。** `LocalGitTree` 不读取本地 refs 或工作区内容，不 fetch、不写入；它拒绝 partial clone / promisor 仓库，并在自己的 Git 调用中禁用协议与替换对象。这些限制用于新树目录读取，不扩大为旧祖先适配器的环境隔离保证。
- **完整回答或回落。** 对象缺失、目录不完整、超限或仓库配置不支持时，本地不返回部分树冒充完整目录，只交回原有的一次树目录 GET；不新增重试或请求预算。远端仍可能失败，不能把回落记成读取成功。
- **执行机器与 Git 对象库仍须可信。** 使用本地 Git 对象不等于审计整个 object store，也不代替远端可见性及原有记录校验。

是否实际命中看本次 `tree_inventory` 回执；是否读到所需资料看原 stdout 结果及其确切引用。该计数不能证明 HTML 已打开、附件已下载，或研究已完成采用。

## 确切正文与一次 Catalogue 读取

完整树目录通过 `GitHubStore._tree` 的原校验后，`GitHubStore._read` 才选取目标普通文件行。只有这个受信内部接点可以调用本地 API 的 `read_tree_blob(repository, row)`；普通 blob GET 不会因为镜像里恰有同一对象就取得本地读取资格。该接点不进入 binding、计划文件或来源 JSON，也不另建一份树路径准入表。

同一镜像以 `git cat-file` 读取 exact blob OID 的原始字节，不读取工作区、分支同名文件或经过 filter/textconv 转换的内容。正文读取复用树目录的受限进程环境、partial/promisor 拒绝、禁协议/懒取/替换对象以及有限缓冲与时限；原单 blob 4 MiB 和响应 8 MiB 上限保留。类型、长度与 Git blob 摘要均相符才返回。原 Store 随后继续核 base64、长度和 OID，应用继续核 SHA-256、manifest、引用和内容资格。

本地无答案只回到原 API 的一次确切 blob GET，不重试、不另建客户端、不重置512次累计请求预算。仓库不匹配不走本地；已观测权限拒绝或可见性变化形成的失败闩在本地读取前也生效。Book、Room 和独立子 reader 使用同一 API，保留同一失败与预算状态。

本地 `research-catalogue` 另复用 Room 已有的短期 `reading_scope(fail_closed=True)`：入口取得实时远端 snapshot，范围内按同一 snapshot 核每个确切版本与 marker，出口再核实时仓库、分支和该 snapshot 的可达性，最终 JSON 导出前仍核原生可见性。这样避免同一页的每个正文字段反复做完整准入，没有跨命令缓存权限或分支头。cursor 仍指向原 snapshot；普通主线前进不替换当前页，改写导致原 snapshot 不再可达则拒绝导出。

这项作用域复用只适用于本地 Catalogue。Room 保留自己在渲染前结束的原作用域与导出前复核，其他共享 CLI、写入与生产宿主的读取节奏不变。权限、传输或范围失败即使被某个组件转成局部缺口，也不能洗成最终可导出的成功页。

`tests/test_git_blob.py`、`tests/test_local_blob_mirror.py` 与 `tests/test_local_read.py` 保护上述字节、回落、同快照消费者及撤权边界。合成测试可以证明请求减少与结果相同；Windows 上的实际耗时仍须按 #249 的原机回执验收。回退本组时同时回退接线、正文回执与 Catalogue 作用域；既有 records、历史 blob 与引用格式没有迁移，原远端 reader 保持可读。

## 稳定边界：为什么工作台可以直接调它

本控制台对上层暴露**退出码、stdout 上的 JSON 结果，以及 stderr 上的错误或执行回执**；回执的生成阶段见上文。任何宿主——WorkBuddy 技能、连接器、MCP 工具、一个 shell 脚本——按这个契约调用即可，不需要知道 Python 内部结构。反过来说，换宿主也不改本仓的读实现，这正是「只保留核心与连接性」的落点。

## 明确不搬的部分

- **写**：唯一 writer + CAS 在 GitHub，见 `research-runtime` 的 write job。
- **reconcile**：它能重放既有意图，不是读。
- **定时**：`announcement-discovery` 的 `schedule` 与 `daily-market` 的外部单主时钟。
- **Ready/Gate 原件**：`pull_request` 事件只发生在 GitHub。
- **私有决策准备**：内容属私有保管，必须走核过可见性的私有适配器。

GitHub 上的对应 workflow **全部保留**：本地优先不等于停用入口，回退随时可用。

## 验证

```bash
python -m pytest tests/test_local_read.py -q      # 白名单、预检与实际可见性、正常读取和导出、回执
python -m pytest tests/test_workflow_estate.py -q # 入口清单与仓库一致
```
