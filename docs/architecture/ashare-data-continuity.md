# 沪深 A 股日数据：持续保管、增量更新与正常读取

责任：#10，来源和保管分别复用 #12 与 #6 的原合同。这个入口把一次检查变成可接续的数据链：保存已经成功取得的页，按交易日保留全市场分区，正常读取明确版本，在下次显式更新时只补必要查询。实际生产启用与真实供应商可用性仍以原任务中的运行和保管回执为准。现有 records 为公开分支，原始供应商数据的公开再分发权须逐来源单独核定。

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

## 日常操作：沿用现有 records

**唯一保管目标是本仓已经存在的公共 `records` 分支**。`config/store-binding.json` 是可信只读配置（现有仓库 ID、分支、anchor、store_id 与公开可见性），绝不把 `write_enabled` 永久修改为 true。运行时的写入能力仅由受信的、显式授权 sync 分支临时授予。无需创建 `Decision-OS-2-data`，也不需要第二种保管平台或新的市场仓。

先编写有界选择文件，例如：

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

`start` 至 `calendar_end` 最多 119 个自然日；真实交易日由两所日历派生。新增日期在后继 generation 补齐，不自动重抓已成功的旧日期。`plan` 不需要登录：

```bash
python -m decision_kernel.interfaces.market_collection_cli plan \
  --selection /runtime/selection.json --out /runtime/plan.json

# 正常独立读取使用现役 records 绑定，仍须具备该仓只读 GitHub 凭据。
python -m decision_kernel.interfaces.market_collection_cli read \
  --binding config/store-binding.json --selection /runtime/selection.json \
  --out /runtime/reading.json
```

GitHub Actions 的 `decision-os-ashare-data` 仅手动 `main` 运行 `status` 或 `sync`，填写精确 `code_sha` 与 selection；`status` 只读，**不需要来源密钥或公开授权声明**。如果尚无市场集合，返回 `ASHARE_COLLECTION_NOT_RETAINED`，并不表示空数据已经通过验收。

`sync` 每次 1..32 个明确有限单元。操作人必须分别确认 **原来源访问及保管范围** `access_and_custody_confirmed` 和 **所选全部来源行可公开再分发的独立法律/许可依据** `public_rights_confirmed`。只有后者已经由证据确认时才可勾选；同意使用公开 `records`、购买接口访问权、知道密钥、或资料来自公开信息，都不是许可依据。两个确认任一缺失，workflow 不进入 sync job，入口自身也在任何来源 HTTP 和 Store 写入之前拒绝。不能为取得绿灯而绕过该检查。

通过许可核查后，直接再次显式调度 `sync`，原断点、分页、失败预算和原始 SourceRef 保持。不会循环定时或后台重试，也不会因跳到新 generation 恢复耗尽预算。

## 手动托管运行与资料权利

新 `.github/workflows/ashare-data.yml` 分别运行：`status` 获得 `contents:read`、无 Relay 密钥；`sync` 仅在两项声明显式成立时获得 `contents:write` 和既有 `TUSHARE_PROXY_API_KEY`。二者使用受信 `config/store-binding.json` 确认同一公共 `records`；GitHub 作业令牌直接传给原 `GitHubStore`，**无需新增数据仓、私有绑定 Secret 或专用市场 Store Token**。注意 `contents:write` 在 GitHub 平台层面是仓库权限而不是分支级隔离；唯一分支目标由应用绑定/anchor、CAS 和单一 writer 约束，不宣传它具有更强的平台强制隔离。

公开 Actions 的 `result.json` 仍仅可包含操作、计数、日期、状态与代码/run 身份，不能包含任意来源行、证券名称、价格、SourceRef、Token 或原供应商错误正文。这个回执公开不等于已把数据集合保管成功；仅原 `records` 上可独立读回的 exact 引用才证明保管。

**来源许可仍是单独的真实阻塞。** Tushare 当前《数据服务协议》在 https://tushare.pro/document/1?doc_id=405 明确描述个人、不可转让、非商业的许可；不能从中推定可以将原始行情大批量公开到 GitHub。用户已经选择公开分支，不等于取得 Tushare 的再分发权。未取得具体适用许可前，此入口只允许 `status`，禁止真实 `sync`；可以另行保管已获准公开的原创研究成果与非敏感工程回执，但不能用计数回执冒充恢复了供应商源行。

旧 `market-custody`、research 和其他工作流继续使用它们原有的绑定与授权；本组不会为市场数据修改它们，也不会将其他私有材料迁入公开分支。首次合法回填的验收仍须分别记录原来源、exact 集合、跨进程只读恢复、真实分母和 raw/qfq 窗口/时效，不以工程 CI 代签。

## 容量、停止与恢复

每个显式工作单元共用一个原 GitHubAPI，writer 与所有新 reader 共同计入原 512 次请求。正常完整读采用只读 `GitMarketReader`：认证绑定并固定 Git 版本，读取原生 GitSnapshot，结束前再次核实际可见性和可达性。临时原生对象只供这次受控读取，不发布成公开恢复包。

API 达到 240 次的来源准入门槛时，先保存可续取停点，为后续检查点、查询清单和集合根读回留余量。不是把单次硬上限改成 240，也不是故障后清零 512。只有已经结束并保存的显式单元才允许下一单元建立预算；已知全局拒绝、未知写入、原页尝试耗尽或无进展不自动进入下一轮重试。

总库存上限 8192 个来源/研究/制品版本，recursive tree 最多 32768 项，原生 Git 下载最多 64 MiB；旧扁平查询仍限 1024 个版本，有界搜索每次最多扫描 32 个。每次来源工作单元预留 160 个目录项、32 个版本、8 MiB 原生空间及 256 KiB 的 GitHub 树响应预算；不知道线上真实大小时拒绝准入。本地合成 GitStore 明确不适用 GitHub 响应估算，但仍核实际原生大小。原 HTTP/响应、输入、单页与提交字节边界继续分别约束，最先到达的边界生效。

GitHub writer 在发送任何提交前还检查新文件加入后的完整树：目录项、总版本和包含路径/URL/格式余量的响应字节估计均须在原边界内。这样不会先写入一个正常读取无法展开的新 head，再把超限当作成功后的未知结果。

集合链发现也有独立的 8192 节点 / 512 MiB 累计唯一清单正文字节边界，在新根写入前预留本单元两个最大根（各 1 MiB 集合及 64 KiB 固定元数据余量）。这个字节数衡量逻辑清单，不是内存常驻量或允许下载的原生 Git 大小。同范围重复父引用仅在完整 exact SourceRef 已验证时复用，仍核选择、原提交可达性、字节和 owner。容量回执是当前有限余量，不是无限历史、备份成功或长期在线率。逼近边界时停止新取源，保留现有 exact 版本可读；扩容、受控归档或迁移按现有单一 writer/精确引用合同处理，不隐式删除老数据，不创建第二写入真源。

已保存的成功页不会因运行结束而丢失；未知响应的原始网络结果仍可能无法重建，必须保留该事实。恢复到更早源码版本不会删除新数据；暂不理解新 schema 的旧 reader 也不能被称为恢复成功。退出本能力时一起处理专属 collection/runtime/工作流/测试及本文入口，保留原共享来源、版本保管、raw/qfq、研究与 CI 保护。

## 验证责任

新反例分别覆盖源执行检查点、保管和 native snapshot、5000 以上证券与多日窗口、跨进程续取、增量和旧完整版本、全代防回退、普通 CLI/room、有限运行宿主与容量边界。全部合成数据测试明确不代签供应商返回是否正确、资料权利或生产长期健康。完整精确 head CI、普通 merge 后实际 main、真实运行与独立消费者各自保留原生证据。
