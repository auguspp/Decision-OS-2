# 本地只读控制台：把读搬回本机，把写与定时留在 GitHub

责任：[#221](https://github.com/auguspp/Decision-OS-2/issues/221) A（只读工作台）与 D（瘦身）的交点。入口清单与退役判据见 [交付入口清单](../architecture/workflow-estate.md)，本页只讲读这条路怎么离开 runner。

现有读操作**不需要 runner**：它需要的是精确的 store、一个不能写的令牌、以及一份说明「这次读的是哪个 store、跑了哪条命令」的回执。本地只读控制台把这三件事固定下来，其余照旧复用 `decision-os store` 的同一份实现——读只有一份代码，不新开第二条读取路径。

## 为什么值得搬

对最近 100 次运行的观测（2026-10-23，只是快照，不进契约）：`research-runtime` 被派发 51 次，其中 **39 次只读**；只读运行的中位耗时 37 秒、p90 101 秒，且其中 5 次在只读阶段就失败。同一份读在链路上没有额外一跳时是秒级——下面「实测」一节会说明这一跳现在卡在哪。搬走的收益不是机器费（本仓 public，分钟数本就不计费），而是三样更贵的东西：

1. **延迟**：等 37–100 秒读一份 catalogue。
2. **噪音**：只读也会因为 runner/checkout 变红，红一次就要人解释一次。
3. **耦合**：run 以派发时的 `code_sha` 为准，main 一动整轮作废（2026-10-23 18:39 那次日更被合并撞停就是这个机制）。

## 六道闸，任一不过就不发请求

| 失败码 | 含义 |
|---|---|
| `LOCAL_READ_REFUSES_WRITE_COMMAND` | 不在只读白名单里（写、reconcile 一律不在） |
| `LOCAL_READ_PRIVATE_CUSTODY_REQUIRES_GITHUB` | `prepare-decision` / `read-decision-preparation`：私有决策准备不落笔记本 |
| `LOCAL_READ_REQUIRES_A_BINDING` | 必须给 `--binding`，不接本地合成 store |
| `LOCAL_READ_TOKEN_REQUIRED` | 必须先设 `DECISION_OS_READ_TOKEN` |
| `LOCAL_READ_REFUSES_STORE_TOKEN` | 环境里出现 `DECISION_OS_STORE_TOKEN` 就拒绝，避免误用可写令牌 |
| `LOCAL_READ_REQUIRES_READ_ONLY_BINDING` / `LOCAL_READ_PUBLIC_STORE_ONLY` | binding 必须 `write_enabled=false` 且 `visibility=public` |

闸在**构造任何 API 客户端之前**跑完，所以被拒的命令不会产生一次网络请求，也不会读到 records。

## 白名单等于「当前全部读命令」

`READ_ONLY` 与上游命令表逐项相等，由 `tests/test_local_read.py` 守着：上游新增一条读命令时这条测试会红，强制重新裁定一次要不要放进本地。当前包含：

- 研究：`research-catalogue`、`prepare-research`、`audit-numbers`、`audit-source-use`、`verify-reference-fixing`、`resume-research`、`current`、`search`、`read-source`、`read-revision`、`research-room`
- 议程与复盘：`research-agenda`、`read-research-agenda`、`research-outcome-review`、`read-close-structure`、`read-opening-auction`、`read-auction-next-session`
- 交付：`verify-delivery`、`open-delivery`、`read-artifact`
- Odds：`odds_commands.READ_COMMANDS` 全部

不在白名单：所有 `*WRITE*`、所有 `reconcile-*`、`put-source`、`submit-revision`、私有决策准备两条。

## 回执

每条命令都会在 **stderr** 输出一行 `{"local_read_receipt": …}`，stdout 仍是原样结果，管道不受影响。设 `DECISION_OS_READ_RECEIPT=<path>` 可另存一份。

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
  "started_at": "…", "finished_at": "…", "duration_ms": 820, "exit_code": 0
}
```

`ancestry` 只记计数，不记提交 id：`answered` 是本地对象图答上的次数，`declined` 是它拒绝、改由 `compare` 回答的次数，`mirror` 为 `absent` 表示没开本地图。**`declined > 0` 就是「镜像落后了，去 fetch」的信号**，不是错误。

回执说明的是**这次读的身份**，不是「已验证」。它不能替代 Ready/Gate 原件，也不能替任何研究提交或 Human Decision 签字。

## 用法

```bash
export DECISION_OS_READ_TOKEN=<fine-grained PAT, Contents: Read-only>

# Mirror first, or the read pays for a remote comparison anyway (see below).
git fetch origin records:refs/remotes/origin/records

python -m decision_kernel.interfaces.local_read \
  --binding config/store-binding.json --git-mirror . research-catalogue --limit 4

python -m decision_kernel.interfaces.local_read \
  --binding config/store-binding.json --git-mirror . research-room \
  --plan '{"components":["overview","research-book","odds-book"]}' --out room
```

`--git-mirror` 也可由 `DECISION_OS_GIT_MIRROR` 提供。**不给就完全按旧行为**：每一次祖先证明都走 `compare`。

令牌要求：**只读**。公开仓理论上可匿名读，但匿名会撞跑 API 限额且无法区分调用者，所以仍要求一个只读令牌，而不是复用运行时令牌。`DECISION_OS_STORE_TOKEN` 是运行时唯一 writer 用的名字，本地一律拒绝。

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
| `git merge-base --is-ancestor`（本地） | **0.67–1.14 s** | 稳定，与历史长度无关的常数级外挂成本 |

它不是新的信任根，只有三条边界（`src/decision_kernel/infrastructure/git_ancestry.py` 头部写全）：

1. **只回答远端已经返回的提交 id。** 调用方仍然先取 live ref，所以 head 一动问题就换。
2. **答不上来就拒绝，永不猜。** 对象缺失 → `AncestryUnavailable(LOCAL_ANCESTRY_OBJECT_MISSING)`，随即**回落到 `compare`**。把「镜像过期」误判成「不是祖先」会凭空指控 store 被改写，这是唯一不能犯的错。
3. **不校验自己的对象库。** 改写 `.git/objects` 里某个对象的父指针而保持 id 不变，与改写本模块源码一样，都假设执行机器可信。

判决与错误码与远端**完全一致**（`COMMIT_OUTSIDE_BINDING`），调用方无法从行为上分辨哪条路回答；只有回执记这件事。

### 镜像必须与 head 同代，否则等于没开

2026-10-23 22:51 的实跑回执：`answered=1, declined=2, exit_code=0`。当天的 `records` head 正被日更写入推着走（`87e9c38d` → `cf889c0a`），本地镜像停在旧 head，于是两次祖先证明本地答不上来、回落 `compare`，整次读花 **163 s** 才拿到结果——拿到了，但慢在回落。

所以正确用法是**先 `git fetch` 再读**（本仓 `records` 全量 13216 个对象、8.6 MB，实测 5 s）。不回落的读是亚秒级；回落的读仍然能出结果，只是把 47.5 秒的风险又请回来了。


## 稳定边界：为什么工作台可以直接调它

本控制台对上层只暴露两个东西：**stdout 上的 JSON 结果**、**stderr 上的一行回执**。任何宿主——WorkBuddy 技能、连接器、MCP 工具、一个 shell 脚本——按这个契约调用即可，不需要知道 Python 内部结构。反过来说，换宿主也不改本仓的读实现，这正是「只保留核心与连接性」的落点。

## 明确不搬的部分

- **写**：唯一 writer + CAS 在 GitHub，见 `research-runtime` 的 write job。
- **reconcile**：它能重放既有意图，不是读。
- **定时**：`announcement-discovery` 的 `schedule` 与 `daily-market` 的外部单主时钟。
- **Ready/Gate 原件**：`pull_request` 事件只发生在 GitHub。
- **私有决策准备**：内容属私有保管，必须走核过可见性的私有适配器。

GitHub 上的对应 workflow **全部保留**：本地优先不等于停用入口，回退随时可用。

## 验证

```bash
python -m pytest tests/test_local_read.py -q      # 12 条：白名单等于上游读集合、与写集合不相交、六道闸、回执形状
python -m pytest tests/test_workflow_estate.py -q # 7 条：入口清单与仓库一致
```
