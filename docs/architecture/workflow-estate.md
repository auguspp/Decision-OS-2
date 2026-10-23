# 交付入口清单：谁在工作、谁只是注册项、退役要满足什么

责任：[#221](https://github.com/auguspp/Decision-OS-2/issues/221) D「数据/交付模块瘦身」第一条——对 #10、#12、#127、新闻/RSS、#13 Reading Room 建责任清单，逐项写出**保留 / 复用 / 窄适配 / 停止新采集 / 旧入口退役 / 历史读取保留**、实际调用者、测试和退出条件。

本页是那份清单的入口表，机器可读版为 `docs/architecture/workflow-estate.json`，一致性由 `tests/test_workflow_estate.py` 守着。表里只写**结构性事实**（文件在不在、触发是什么、入口模块是谁）；运行次数与最后运行时间只作为某一时刻的观测，不写进契约，也不作为退役依据。

## 四类证据必须分开看

| 证据 | 怎么取 | 它证明什么 | 它**不**证明什么 |
|---|---|---|---|
| 文件在 main | `.github/workflows/*.yml` | 这个入口现在可以被触发 | 有人在用 |
| 注册项在 GitHub | `actions/workflows` 列表的 `state` | GitHub 记得这个工作流曾经存在 | 文件还在 |
| 真实运行 | `actions/workflows/<id>/runs` 的计数与最后时间 | 它被执行过 | 有人还需要它 |
| 有消费者 | 代码、测试、文档里的引用 | 删掉会打断谁 | 它还在正常运行 |

一个入口可能「有测试但很久没跑」（探针型证据生产者），也可能「天天跑但没人读结果」。两种都不能靠单条证据判生死。

## 20 个注册项 = 15 个文件 + 5 个残留

### 现存 15 个

| 文件 | 触发 | 入口 | 处置 |
|---|---|---|---|
| `ci.yml` | `pull_request` + `push` | — | **保留（核心）**：PR 事件只发生在 GitHub，Ready/Gate 原件无法在别处产生 |
| `research-runtime.yml` | 仅 dispatch | `research_runtime` | **保留（核心）**：唯一业务 writer；只读 operation 另属本地优先 |
| `store-runtime.yml` | 仅 dispatch | `store_runtime` | **保留（核心）**：备份/健康必须跑在 store 旁边 |
| `daily-market.yml` | 仅 dispatch（外部单主时钟 18:35 打 `external-clock`） | `daily_market_cli` 等 4 个 | **保留（生产）**：#10 自然日周期唯一目标 |
| `announcement-discovery.yml` | `schedule 20 10 * * *` + dispatch | `announcement_discovery_cli` | **保留（生产）**：只有两条自带定时的入口之一 |
| `market-custody.yml` | 仅 dispatch | `market_custody_cli` | **保留（生产）**：与 store 同权限边界 |
| `relay-source-runtime.yml` | 仅 dispatch | `relay_source_runtime`、`relay_bulk_check_cli` | **保留（生产）**：采集写 records 并持供应商密钥 |
| `news-intake.yml` | 仅 dispatch | `news_cli` | **保留（生产）**：新闻台唯一可执行入口 |
| `news-rolling.yml` | 仅 dispatch | `news_rolling_cli` | **保留（生产）**：已保管切片的现行消费者 |
| `news-custody.yml` | 仅 dispatch | `news_custody` | **保留（生产）**：来源恢复属保管职责 |
| `news-handoff.yml` | 仅 dispatch | `news_handoff_cli` | **保留（生产）**；退役须先有具名替代消费者 |
| `ashare-data.yml` | 仅 dispatch | `market_collection_cli` + 5 个 `ftshare_*_cli` | **保留（生产）**：`sync`/`repair` 是写；`status` 是只读；`ftshare-check`/`-kernel`/`calendar` 是带日期的历史研究 |
| `market-source-check.yml` | 仅 dispatch | `market_source_check_cli` | **本地优先**：全部分支都是有界只读诊断，不导出供应商行 |
| `relay-capabilities.yml` | 仅 dispatch | `relay_capabilities_cli` | **本地优先**：只探服务元数据；本地无供应商密钥时仍留 runner |
| `rss-intake.yml` | 仅 dispatch | `rss_cli` | **退役候选**：需先证明 NewsNow/relay 覆盖同一 RSS 范围 |

### 残留 5 个（文件已从 main 删除）

`member-recovery-sources.yml`、`odds-probability-sources.yml`、`odds-source-bundle.yml`、`odds-watch-sources.yml`、`watch-activity-sources.yml`：各只有 2026-10-16 一次成功运行，责任已分别并入 `market-source-check` 的 `members`/`resume-members`、`relay-source-runtime` 与 odds 命令、以及 #207 冻结的 Watch。

**GitHub 没有删除工作流的 REST 端点**（`DELETE /repos/{owner}/{repo}/actions/workflows/{id}` 返回 404）。注册项只有在它**所有运行历史被删除之后**才会消失，而删运行历史等于删审计线索。所以本仓选择：**保留历史、把注册项置为 `disabled_manually`**。这 5 个现在是这个状态，`active` 计数从 20 降到 15，与文件数一致。要回滚只需 `PUT .../actions/workflows/{id}/enable`。

## 这次一个文件都没删，理由三条

1. **「零业务 7 天」不是退役依据**。`rss-intake`（最后 10/16）、`relay-capabilities`（10/15）、`market-source-check`（10/17）都过了一周，但它们各有测试与文档消费者，其中两个还是**证据生产者**——删掉不是省维护，是把「凭什么这么说」的凭据删掉。这与 #221 C 的「0 引用 ≠ 可以停用」是同一条判据。
2. **只活在历史里的操作，是收窄触发而不是删入口**。`ashare-data.yml` 的 `run-name` 把 `ftshare-check` 写死在 `20261018`、`check-kernel` 写死在 `20261019`——这些是一次性研究，不是常设职责。它们的处置是「停止新采集、保留历史读取」，入口本身仍承载 `sync`/`repair`。
3. **5 个僵尸的文件早就删了**，剩下的只是注册项，而注册项删不掉（见上）。

## 退役四步（缺一步就不算退役）

1. 切断**新生产依赖**：先让调用方不再依赖它。
2. 独立验证新消费者与**回退**路径：没有回退不算完成。
3. 在**专属 PR** 里一起退出 CLI、workflow、config、测试和文档——不要留下半截引用。
4. **历史 SourceRef 不失读**：已写入 records 的引用必须仍能按原版本读回。

当前只有 `rss-intake` 走到第 2 步的入口（裁定中）。其余条目都停在「保留」，没有一项满足四步，因此本轮不改任何采集实现。

## 与只读工作台的分界

只读部分可以离开 runner，写与定时不能：

| 可以本地 | 必须留在 GitHub |
|---|---|
| `research-runtime` 的只读 operation（catalogue、reading-room、agenda、odds/valuation 读数、交付校验、交付打开） | 它的全部写 operation（唯一 writer + CAS） |
| `market-source-check` 全部分支 | `daily-market` 的周期写入与外部时钟 |
| `relay-capabilities`（密钥可得时） | `announcement-discovery` 的 `schedule` |
| `ashare-data` 的 `status` | `sync`/`repair`、`relay-source-runtime` 采集、news 链写入 |

只读本地化的实现见 `docs/delivery/local-console.md`；本地读取只拿只读令牌，不持有 records 通用写权限。

## 复现

```bash
# 注册项、状态与运行计数（时间点观测，不进契约）
gh api repos/auguspp/Decision-OS-2/actions/workflows?per_page=100 \
  --jq '.workflows[] | "\(.state)\t\(.path)\t\(.id)"'

# 文件清单
ls .github/workflows

# 清单与仓库是否一致
python -m pytest tests/test_workflow_estate.py -q
```
