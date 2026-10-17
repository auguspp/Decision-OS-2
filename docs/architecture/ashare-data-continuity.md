# 沪深 A 股日数据：持续保管、增量更新与正常读取

责任：#10，来源和保管分别复用 #12 与 #6 的原合同。这个入口把一次检查变成可接续的数据链：保存已经成功取得的页，按交易日保留全市场分区，正常读取明确版本，在下次显式更新时只补必要查询。实际生产启用与真实供应商可用性仍以原任务中的运行和保管回执为准。

## 数据范围与“可用”的含义

当前集合覆盖 SSE/SZSE 的 `stock_basic(list_status=L)` 已识别证券。它是当前上市状态快照，不是历史时点证券全集；BSE、退市/暂停上市状态、逐日历史成分不能被称为已覆盖。名称、身份冲突、上市日期、交易所日历、行情、因子和停复牌各自通过原资格检查。主表缺失时完整分母未知，不能将其当成零。

每个开市日保存原 `daily`、`adj_factor`、`suspend_d` 查询。数据按 `trade_date` 对全市场请求，读取时才选择证券和展示页；不会为每只证券另发同一批历史来源请求。两所原日历分别确定实际截止交易日，周末和节假日不会按最后收到的价格回退。日终入口最多取上海时间的前一个自然日，不宣称盘中实时行情。

raw/qfq 沿现有窗口算法计算 5/20/60 个交易日结果。前复权沿原截止日因子比值，不能当总回报或公司行动完整认证。因子错误只限制相关 qfq，原价与其他证券仍按实际资格展示；停复牌证据只解释缺口，不补价格、不自动证明全天停牌。新上市、停牌、资料缺失可能导致窗口不可用，即使全部声明查询已经完整保存。

集合完整、来源可恢复、窗口可用、当前时效是四个独立事实。读取上一完整集合时，页面保留其原价格和 SourceRef，同时显示最新尝试的失败或暂停。当前新日历可以证明旧集合实际落后几个交易日；没有覆盖读取时点的日历证明时，freshness 为 unknown，不会因旧截止日内部没有缺口就显示当前健康。

## 唯一数据链与接续

没有新数据库或另一份可写 current。原 `source-views` 与 `submissions` 保管以下版本：

| 对象 | 保存内容 | 成立的事实 |
|---|---|---|
| 来源页 | 原 requested-column projection、身份、查询范围、原页元数据 | 原 SourceInput/SourceRef 可精确恢复，非原始 HTTP 字节 |
| 执行检查点 | 原请求、页偏移、每次尝试、等待、请求预留及成功页引用 | 同一失败页的尝试/等待预算可以跨进程延续 |
| 完整查询清单 | 经检验的原 use/query_key 与完整页引用 | 原查询已终止并可独立读回 |
| 集合根 | 固定计划、日期单元、查询/检查点引用、前驱及上一完整根 | 明确这个版本保管了哪些分区 |

每次 HTTP 前，先通过原 CAS writer 保存该页的尝试预留，并由新 reader 读回。成功页保存后再保存控制状态。完整检查点晋升为查询清单时，核验其原始页身份后直接引用，不重放已成功页的逐项写入。保存失败、返回未知、读回失败都先停止；原 journal 和 exact 引用是后续对账入口。

原单页最多 4 次尝试和 180 秒等待，单次请求至少保留 30 秒，来源工作单元最多 600 秒。跨工作单元不刷新这些额度；等待的剩余时间按原 not-before 计算。预留仍是 in-flight 时，不假定请求未发出；已保存的完整检查点可在没有 Relay 凭证时完成查询恢复，源 HTTP 为零。

一个 `collection_id` 跨所有 generation 只有一条唯一后继链。运行入口从实际绑定中核真实末端，旧根、旧检查点、隐藏新检查点或分叉都不能授权重复请求。原 generation 还有 pending/paused/blocked/unknown 状态时先在原代接续或对账；不能换代跳过限流或恢复原页已耗尽的额度。新代刷新已经完整的 current-L 主表，原终止失败主表保留其失败；原日期查询复用 exact 版本。仅 `refresh_dates` 明确列出的已完整日期可以重新取得；它不重置失败查询。

新代的 `end` 与 `calendar_end` 不得倒退，已经使用的旧 generation 不能重开。历史版本仍可按原 SourceRef 读取。运行没有可推进单元时明确返回 `ASHARE_NO_PROGRESS`，不会持续空转重建预算。

## 日常操作

受控宿主先提供一个小选择文件。例如，以下日期仅是可审阅的初始回填例子，不代表生产已经采集：

```json
{
  "collection_id": "cn-eod",
  "generation": "g20261016",
  "start": "20260701",
  "end": "20261016",
  "calendar_end": "20261016",
  "refresh_dates": []
}
```

`start` 至 `calendar_end` 最多 119 个自然日。实际交易日来自两所原日历；窗口所需天数仍由 reader 判定。下一代可以移动窗口并增加已经结束的日期；相同范围的已保管日期不会重新请求。

```bash
# 离线生成固定请求计划：不读目标、不需要凭证、不发来源请求。
python -m decision_kernel.interfaces.market_collection_cli plan \
  --selection /runtime/selection.json --out /runtime/plan.json

# 一次明确运行包含 1..32 个有限单元；每个单元至多推进一个逻辑查询。
python -m decision_kernel.interfaces.market_collection_cli sync \
  --binding /runtime/market-binding.json --selection /runtime/selection.json \
  --attempt-id initial-fill-1 --work-units 16 \
  --access-confirmed --custody-authorized --out /runtime/sync-receipt.json

# 同一选择继续：从实际集合末端和原检查点恢复。
# 输出使用新文件，避免执行后才发现回执文件已存在。
python -m decision_kernel.interfaces.market_collection_cli sync \
  --binding /runtime/market-binding.json --selection /runtime/selection.json \
  --attempt-id initial-fill-2 --work-units 16 \
  --access-confirmed --custody-authorized --out /runtime/next-receipt.json

# 正常读回：不需要来源凭证，也不触发采集。
python -m decision_kernel.interfaces.market_collection_cli read \
  --binding /runtime/market-binding.json --selection /runtime/selection.json \
  --out /runtime/reading.json
```

远端专用凭据取 `DECISION_OS_MARKET_STORE_TOKEN`。配置使用原 `GitHubBinding`（真实 repository/repository_id、专用 records 分支、已初始化 anchor、store_id、明确 visibility，写入另需 write_enabled）。不回退工程 token，不根据来源正文选择目标，不自动创建或占用已有私有仓库。`--path` 仅接受已经初始化的本地合成 GitStore，不能冒充真实远端保管。

sync 输出包含 exact `reference` 与计数回执。读取历史根或分页时，可直接将该引用交给原 `market_custody_cli read --input /runtime/root-ref.json`，或原 reading room 的 market plan。旧 custody CLI 仍从原 `DECISION_OS_STORE_TOKEN` 读取专用凭证；两类命令的环境变量需要由受控宿主分别配置，不能靠自动回退借用其他身份。

## 手动托管运行与资料权限

新增 `.github/workflows/ashare-data.yml`，仅手动 main、精确 `code_sha`，复用现有 `decision-os-relay-source` 串行组。`sync` 获得来源和保管凭据；`status` 步骤不获得 Relay 凭据。没有新增定时、通知、研究或交易任务。

运行前需要受信宿主配置两项：

| 配置 | 用途 |
|---|---|
| `DECISION_OS_MARKET_BINDING_JSON` | 原 GitHubBinding 的私有目标配置；不含 token，须已初始化和核验 |
| `DECISION_OS_MARKET_STORE_TOKEN` | 该目标的专用 Contents 读写及实际原生条件提交权限 |

source 同步使用既有 `TUSHARE_PROXY_API_KEY`。输入为 operation、精确 code_sha、上述 selection JSON、work_units，以及 `access_and_custody_confirmed=true`。原入口检查实际 private 绑定；公开绑定在此运行入口被拒绝，不将接口可调用或已经购买访问权当作公开再分发许可。

公开 Actions 只上传 `result.json` 的计数、日期、代码/run 身份、容量和状态。私有目标、SourceRef、供应商行、证券名称、价格、原 Git clone、绑定文件、密钥及服务端错误正文均不进入公开产物。公开原 `market-custody` / recovery 工作流的目标和权限不因本能力改变。

代码合并不等于私有目标已配置。当前公开 records 不能保管尚无公开权利的供应商数据；选择私有目标、初始化与专用凭据接线在原 #6/#10 明确后才能完成生产取源。第一次实际验收须记录：原 source 请求与失败、所存 exact 集合、无源凭证的新读取、真实分母/raw/qfq/时效，并保留仍未覆盖的证券与日期。

## 容量、停止与恢复

每个显式工作单元共用一个原 GitHubAPI，writer 与所有新 reader 共同计入原 512 次请求。正常完整读采用只读 `GitMarketReader`：认证绑定并固定 Git 版本，读取原生 GitSnapshot，结束前再次核实际可见性和可达性。临时原生对象只供这次受控读取，不发布成公开恢复包。

API 达到 240 次的来源准入门槛时，先保存可续取停点，为后续检查点、查询清单和集合根读回留余量。不是把单次硬上限改成 240，也不是故障后清零 512。只有已经结束并保存的显式单元才允许下一单元建立预算；已知全局拒绝、未知写入、原页尝试耗尽或无进展不自动进入下一轮重试。

总库存上限 8192 个来源/研究/制品版本，recursive tree 最多 32768 项，原生 Git 下载最多 64 MiB；旧扁平查询仍限 1024 个版本，有界搜索每次最多扫描 32 个。每次来源工作单元预留 160 个目录项、32 个版本、8 MiB 原生空间及 256 KiB 的 GitHub 树响应预算；不知道线上真实大小时拒绝准入。本地合成 GitStore 明确不适用 GitHub 响应估算，但仍核实际原生大小。原 HTTP/响应、输入、单页与提交字节边界继续分别约束，最先到达的边界生效。

GitHub writer 在发送任何提交前还检查新文件加入后的完整树：目录项、总版本和包含路径/URL/格式余量的响应字节估计均须在原边界内。这样不会先写入一个正常读取无法展开的新 head，再把超限当作成功后的未知结果。

集合链发现也有独立的 8192 节点 / 512 MiB 累计唯一清单正文字节边界，在新根写入前预留本单元两个最大根（各 1 MiB 集合及 64 KiB 固定元数据余量）。这个字节数衡量逻辑清单，不是内存常驻量或允许下载的原生 Git 大小。同范围重复父引用仅在完整 exact SourceRef 已验证时复用，仍核选择、原提交可达性、字节和 owner。容量回执是当前有限余量，不是无限历史、备份成功或长期在线率。逼近边界时停止新取源，保留现有 exact 版本可读；扩容、受控归档或迁移按现有单一 writer/精确引用合同处理，不隐式删除老数据，不创建第二写入真源。

已保存的成功页不会因运行结束而丢失；未知响应的原始网络结果仍可能无法重建，必须保留该事实。恢复到更早源码版本不会删除新数据；暂不理解新 schema 的旧 reader 也不能被称为恢复成功。退出本能力时一起处理专属 collection/runtime/工作流/测试及本文入口，保留原共享来源、版本保管、raw/qfq、研究与 CI 保护。

## 验证责任

新反例分别覆盖源执行检查点、保管和 native snapshot、5000 以上证券与多日窗口、跨进程续取、增量和旧完整版本、全代防回退、普通 CLI/room、有限运行宿主与容量边界。全部合成数据测试明确不代签供应商返回是否正确、资料权利或生产长期健康。完整精确 head CI、普通 merge 后实际 main、真实运行与独立消费者各自保留原生证据。
