# 交付入口清单：谁在工作、谁只是注册项、退役要满足什么

责任：[#221](https://github.com/auguspp/Decision-OS-2/issues/221) 的 Kernel-first 收敛计划，承接原 D「数据/交付模块瘦身」——对 #10、#12、#127、新闻/RSS、#13 Reading Room 建有限责任清单，逐项写出**保留 / 复用 / 窄适配 / 停止新采集 / 旧入口退役 / 历史读取保留**、实际调用者、测试和退出条件。

本页区分业务责任与当前工作流入口。机器可读的工作流结构清单为 `docs/architecture/workflow-estate.json`，文件、触发和入口一致性由 `tests/test_workflow_estate.py` 守着；其处置说明描述当前部署，不把 GitHub、runner 或供应商名固定成 Kernel 规则。运行次数与最后运行时间只作为某一时刻的观测，不写进契约，也不作为退役依据。进度、认领和验收回原 Issue，本页不另建状态库。

<a id="business-responsibilities"></a>

## 业务责任、可复用能力与退出条件

优先复用 WorkBuddy 等外部工作台、已有模型和数据能力；仍由当前 owner 维护确切研究、来源资格、保管和历史合同。候选替代必须先有真实输入、真实消费者与可重开的证据，再裁定停采或退役。本表不启动替代服务，不迁移真源，不恢复 Watch 或扩大来源预算。

| 业务责任 | 当前输入 | 实际调用者 | owner / 可复用外部能力 | 处置 | 历史责任与退出条件 |
|---|---|---|---|---|---|
| 研究接收与版本连续性 | 获准委托、来源、部分进展/普通修订、确切前驱 | 获准研究宿主经 `research-runtime`；受信 store CLI；Catalogue / Room 后继读取 | #7 研究用例、#6 保管；WorkBuddy 优先且可替换，外部模型承担研究 | **保留 Kernel、收窄宿主适配**；当前内置方法仍由自己的版本合同维护 | 新宿主须实际读、写、独立读回及未知写对账；同一 case / 前驱 / 未决项不得丢失；历史方法字节和旧意图继续可恢复 |
| #10 市场日周期与机械发现 | 当前 FTShare A 股日线及用途合格的其他市场来源；规则、日期与覆盖范围 | 外部单主时钟 → `daily-market`；MarketDiscovery、Odds、日常阅读 | #10；#12 来源适配；合格行情服务可按用途替换供应方 | **保留当前生产，按用途窄适配**；不由本表改为按需或切源 | 替代须覆盖真实目标范围、时钟、分母、缺失和资格，消费者及回退通过后才停止旧入口的新采集；原市场 SourceRef / 发现事件仍可读 |
| 公告目录与发现 | 有界公告目录元数据、取得时间与原定位 | `announcement-discovery` 定时/显式运行；发现与 hosted handoff 采用 | #12；可复用获准公告检索/连接器 | **保留现役采集、候选窄适配** | 替代须证明同一范围、时间、分页完整性及正文权利边界；目录元数据不冒充公告全文，原目录/来源回执保留 |
| 新闻 intake、滚动与交接 | 已获准 NewsNow/Relay 切片、原引用、失败/恢复回执 | `news-intake` / `news-rolling` / `news-custody` / `news-handoff`；Brief、Quick 与 Room | #12 新闻来源、#13 交付；成熟新闻服务与工作台读取可复用 | **保留当前消费者，优先复用，必要时窄适配** | 真正替换需保留 24h 窗口、可用时间、缺项、去重和实际采用关系；新生产依赖切断后专属退出，旧切片/Brief 精确重开 |
| RSS 固定源接入 | 固定 feed 范围、获准元数据与失败表示；原件另核权利 | `rss-intake`；联合新闻视图、恢复与阅读消费者 | #48 / #12；成熟 feed 服务或工作台连接器可替代 | **候选退役，当前保留** | 先证明替代覆盖同一源范围、时效、权利和失败语义并被消费者采用；零近期运行/零直接引用不足以停采；旧 SourceRef 与失败回执保留 |
| 产业阅读与商品覆盖资格 | 确切产业阅读包、商品身份、时间/单位/地区、覆盖缺口 | `industry-read`、Reading Room、产业 Brief | #127；外部产业数据库/检索提供材料，现有 reader 维护资格 | **保留判断合同，外围按用途适配** | 外部材料接入须保留用途、权利、实际取得表示和覆盖限制；替换专属生产/投影前验证实际采用，历史阅读包与身份/资格版本继续可恢复 |
| Reading Room、Brief 与本地工作台 | 研究/来源、Agenda、Odds / Watch 检查、AI 正文及确切 Brief 采用 | #13 日常阅读；原 Room / news desk；本地只读入口 | #13；WorkBuddy 优先承担交互与外部模型，现有投影保持薄层 | **保留必要 reader、复用交互、候选退出重复页面/编排** | 真实工作台须能独立重开研究、Brief 与历史采用；展示替换不能换真源、代签用户已读或清除未决项；旧 Artifact/SourceRef 保留 |
| 保管、恢复与健康 | 同一 records 的原件、精确 Git/内容引用与恢复材料 | `store-runtime`、市场/新闻保管入口及独立恢复者 | #6；等价托管/备份执行环境可替换部署位置 | **保留当前部署与恢复责任** | 替代须通过权限、完整性、独立恢复和故障回退验收；本地镜像/缓存不自动成为备份，备份也不以“必须与 store 同机”定义 |
| 工程 CI 与变更证明 | 精确 PR/main 代码、现役分区/合同/安装消费 | 每次工程变更、Ready/Gate 和合并责任人 | 工程协议与现役 CI owner；当前 GitHub 原生证据 | **保留 full CI，不以瘦身降级** | 平台更换须另证等价事件、保护和可审回执；本轮保留 GitHub 工程通路，不把当前平台等同业务 Kernel |

现有验证按原 owner 复用：研究接收见 `tests/test_research_workflow.py` / `test_research_runtime.py`；市场与公告见 `test_daily_market.py` / `test_daily_market_schedule.py` / `test_announcement_discovery.py`；新闻/RSS 见 `test_news_delivery.py` / `test_news_handoff.py` / `test_rss_intake.py` / `test_rss_original_recovery.py`；产业见 `test_industry_reading.py` / `test_industry_goods_coverage.py` / `test_industry_adoption.py`；工作台与交接见 `test_reading_room.py` / `test_local_read.py` / `test_hosted_handoff.py`；保管见 `test_versioned_store.py` / `test_github_store.py` / `test_store_runtime.py`。它们验证现有合同，真实外部替代、日常使用和独立后继采用仍须另留证，不因本表或测试存在而完成。

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
| `ci.yml` | `pull_request` + `push` | — | **保留（当前工程通路）**：现役 Ready/Gate 核 GitHub 原生事件与回执，full CI 保持 |
| `research-runtime.yml` | 仅 dispatch | `research_runtime` | **保留（当前研究接收）**：经原保管层与单主写入/CAS；获准只读操作另属本地优先 |
| `store-runtime.yml` | 仅 dispatch | `store_runtime` | **保留（当前保管部署）**：备份/健康与独立恢复责任保留，执行位置不是永久核心约束 |
| `daily-market.yml` | 仅 dispatch（外部单主时钟 18:35 打 `external-clock`） | `daily_market_cli` 等 4 个 | **保留（生产）**：#10 自然日周期唯一目标 |
| `announcement-discovery.yml` | `schedule 20 10 * * *` + dispatch | `announcement_discovery_cli` | **保留（生产）**：当前唯一带原生 `schedule` 的文件；`daily-market` 另由外部时钟 dispatch |
| `market-custody.yml` | 仅 dispatch | `market_custody_cli` | **保留（生产）**：与 store 同权限边界 |
| `relay-source-runtime.yml` | 仅 dispatch | `relay_source_runtime`、`relay_bulk_check_cli` | **保留（生产）**：采集写 records 并持供应商密钥 |
| `news-intake.yml` | 仅 dispatch | `news_cli` | **保留（生产）**：新闻台唯一可执行入口 |
| `news-rolling.yml` | 仅 dispatch | `news_rolling_cli` | **保留（生产）**：已保管切片的现行消费者 |
| `news-custody.yml` | 仅 dispatch | `news_custody` | **保留（生产）**：来源恢复属保管职责 |
| `news-handoff.yml` | 仅 dispatch | `news_handoff_cli` | **保留（生产）**；退役须先有具名替代消费者 |
| `ashare-data.yml` | 仅 dispatch | `market_collection_cli` | **保留（生产）**：`sync`/`repair` 是写，`status` 是只读；5 个带日期的一次性操作已于 2026-10-23 退出菜单 |
| `market-source-check.yml` | 仅 dispatch | `market_source_check_cli` | **本地优先**：全部分支都是有界只读诊断，不导出供应商行 |
| `relay-capabilities.yml` | 仅 dispatch | `relay_capabilities_cli` | **本地优先**：只探服务元数据；本地无供应商密钥时仍留 runner |
| `rss-intake.yml` | 仅 dispatch | `rss_cli` | **退役候选**：需先证明成熟替代 intake 覆盖同一源范围、时间、权利和失败语义，并有真实消费者采用 |

### 残留 5 个（文件已从 main 删除）

`member-recovery-sources.yml`、`odds-probability-sources.yml`、`odds-source-bundle.yml`、`odds-watch-sources.yml`、`watch-activity-sources.yml`：各只有 2026-10-16 一次成功运行，责任已分别并入 `market-source-check` 的 `members`/`resume-members`、`relay-source-runtime` 与 odds 命令、以及 #207 冻结的 Watch。

**GitHub 没有删除工作流的 REST 端点**（`DELETE /repos/{owner}/{repo}/actions/workflows/{id}` 返回 404）。注册项只有在它**所有运行历史被删除之后**才会消失，而删运行历史等于删审计线索。所以本仓选择：**保留历史、把注册项置为 `disabled_manually`**。2026-10-23 的清理记录中这 5 个均为该状态，`active` 计数从 20 降到 15，与文件数一致；这是当次观测，不代替新的原生状态查询。要回滚只需 `PUT .../actions/workflows/{id}/enable`。

## 已完成的菜单清理及其边界

删的：`ashare-data.yml` 里 5 个一次性 job 与它们的 5 个菜单选项（唯一的结构性删除，见下第 2 条）。没删的：任何 workflow 文件、任何 CLI、任何测试、任何历史 records。理由三条：

1. **「零业务 7 天」不是退役依据**。`rss-intake`（最后 10/16）、`relay-capabilities`（10/15）、`market-source-check`（10/17）都过了一周，但它们各有测试与文档消费者，其中两个还是**证据生产者**——删掉不是省维护，是把「凭什么这么说」的凭据删掉。这与 #221 C 的「0 引用 ≠ 可以停用」是同一条判据。
2. **只活在历史里的操作，是收窄触发而不是删入口**。`ashare-data.yml` 的 `run-name` 把 `ftshare-check` 写死在 `20261018`、`check-kernel` 写死在 `20261019`——这些是一次性研究，不是常设职责。**2026-10-23 已执行**：菜单从 10 个选项收窄到 `status`/`sync`/`repair`/`ftshare-sync`/`ftshare-status`，5 个一次性 job 一并删除（文件从 422 行降到 219 行）；对应 CLI 与测试**保留**，历史回执仍可读。依据是 `docs/sources/ftshare-daily.md` 自己写的「原核验已经结束」「不因这项授权重开」。
3. **5 个僵尸的文件早就删了**，剩下的只是注册项，而注册项删不掉（见上）。

## 退役四步（缺一步就不算退役）

1. 切断**新生产依赖**：先让调用方不再依赖它。
2. 独立验证新消费者与**回退**路径：没有回退不算完成。
3. 在**专属 PR** 里一起退出 CLI、workflow、config、测试和文档——不要留下半截引用。
4. **历史 SourceRef 不失读**：已写入 records 的引用必须仍能按原版本读回。

`ashare-data.yml` 的 5 个一次性操作已有菜单退出证据：①从菜单移除，新的派发不能再选择这些 job；②菜单在 Git 历史中可恢复，被保管的回执与 CLI 原样保留；③专属 PR 同步改了 workflow、结构测试、`docs/sources/ftshare-daily.md` 和本清单；④没有删除 CLI、测试或 records，已有运行记录仍保留。

上述一次性菜单退出是既有工程清理，不证明任何现役业务已由外部能力替代，也不抵扣 #221 后续“真实消费者迁移后退役”的验收。Actions artifact 仍受保留期约束，历史可恢复责任不能仅凭运行列表长期存在来证明。

`rss-intake` 仍停在裁定阶段（需先证明替代 intake 的范围、时效、权利、失败语义与真实采用）。其余现役条目保留；本次合同对齐不执行生产停采或退役。

## 与只读工作台的分界

本地只读已经有现役入口；生产写入与时钟继续沿当前部署。永久约束是授权、同一真源、单主 writer/CAS、唯一调度责任和可恢复证据；“所有写入/备份必须永远由 GitHub 执行”不属于该约束。等价替换未验收前，以下边界继续有效：

| 已可按现役权限本地执行 | 当前仍留在原部署，不能从本表推定迁移 |
|---|---|
| `local_read.READ_ONLY` 中的既有 CLI（目录、阅读室、议程、odds/valuation 读数、交付校验/打开等） | `research-runtime` 全部写 operation 和原只读未知写对账；原单主 writer/CAS |
| `market-source-check` 全部分支 | `daily-market` 的周期写入；外部单主时钟仍只 dispatch 原目标 |
| `relay-capabilities`（密钥可得时） | `announcement-discovery` 的 `schedule` |
| `ashare-data` 的 `status` | `sync`/`repair`、`relay-source-runtime` 采集、news 链写入 |

只读本地化的实现见[本地工作台](../delivery/local-console.md)，实际业务命令对应见[操作映射](../research/runtime.md#kernel-operations)。本地入口拒绝写、对账及其明确排除的决策准备读取；实际调用身份也须为只读，不持有 records 通用写权限。环境变量名、只读 binding 或客户端白名单不能证明令牌原生无写权限，不能称为服务端隔离。镜像、索引和页面均为派生物。

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
