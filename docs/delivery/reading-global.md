# 全球背景进入正常阅读室

责任：#147 的有限正常消费者组；来源、保管和观察算法继续由原市场 owner 维护。
现役美股指数、Shibor 以及四族给定原件都可进入 `reading-room` / `research-room`。
读取不会发起来源请求、保存新记录、更新当前指针、选择最新日期或启动研究。
这里交付指定根的正常消费工程；六族真实覆盖、真实资料保管与自然连续更新分别需要证据。
四族给定原件的来源／日期／保管细则见 [给定全球原件合同](../architecture/global-public-reading.md)。

## 原入口和精确计划

原 Room 计划新增可选 `global_context`。只提供需要读取的族槽；
未提供整个字段时不增加来源读取或 registry 加载，也不增加全球空面板。

| 槽 | 必须恢复的原 reading schema | 精确查询身份 |
|---|---|---|
| `us_indices` | `relay-us-index-reading-v1` | `DJI`、`IXIC`、`RUT`、`SPX`，且必须在原根的声明范围内 |
| `shibor` | `relay-shibor-reading-v1` | `on`、`1w`、`2w`、`1m`、`3m`、`6m`、`9m`、`1y` |
| `treasury` | `global-public-reading-v1`，family 必须为 `treasury` | 8 个固定 `UST:` tenor |
| `fx` | `global-public-reading-v1`，family 必须为 `fx` | `EUR/USD`、`EUR/CNY`、`EUR/JPY`、`EUR/GBP` |
| `commodities` | `global-public-reading-v1`，family 必须为 `commodities` | `GC=F`、`CL=F`、`BZ=F` |
| `crypto` | `global-public-reading-v1`，family 必须为 `crypto` | `BTC-USD`、`ETH-USD` |

每槽严格只收 `reference` 和可选 `offset`、`limit`、`entity_id`：

- `reference` 是已有 `market-reading-custody-v1` 或四族 `global-public-custody-v1` 整根的完整 SourceRef，selection 必须为 `all`。
- `offset` 默认为 0，必须是非负整数；`limit` 默认为 20，范围 1–50。布尔值不是整数参数。
- `entity_id` 默认为 null；它只用于本族明确身份的导航，不能传另一个族的名称或执行地址。
- 不接收 URL、registry 路径、供应商、调用者数值、日期覆盖或公开模式字段。
- 错族根在恢复其 query／原件前拒绝。诊断计数回执、普通内容、collection 根及 A 股根均不能替代这些整根。

将已有两份获准根引用放入原宿主 payload 的形状如下。示例中的说明字符串须替换为完整 SourceRef 对象，字符串本身不是合法输入：

```json
{
  "plan": {
    "as_of": "2026-10-20T12:00:00+08:00",
    "global_context": {
      "us_indices": {
        "reference": "已有美股指数整根的完整 SourceRef 对象",
        "offset": 0,
        "limit": 2,
        "entity_id": null
      },
      "shibor": {
        "reference": "已有 Shibor 整根的完整 SourceRef 对象",
        "offset": 0,
        "limit": 4,
        "entity_id": null
      }
    }
  }
}
```

正常入口仍是原 `research-runtime` 的只读 `reading-room` 操作，使用刚核对的代码版本，
`submission_id` / `expected_head` 留空。没有新增 operation、workflow、定时任务或写入口。
本地包使用原 CLI；输入文件内容是上例的 `plan`：

```console
decision-os store --path /approved/existing-synthetic.git research-room \
  --input /approved/global-room-plan.json --out /approved/new-global-room.html \
  --market-registry /trusted/checkout/docs/sources/relay-registry.json
```

`--path` 继续只表示已初始化的合成本地存储；真实目标沿原获准只读 `--binding`。
`--out` 必须为新文件，stdout JSON 和离线 HTML 都是本次读取投影。
US/Shibor 使用代码侧的原 Relay Registry；本地操作者可以显式提供原 CLI 的可信 registry。
只给四族 public 原件根时不加载 Relay Registry，固定 adapter 验证原件身份。
来源或 payload 不能指定配置路径。根、query、原页的摘要、身份和 registry snapshot 仍由原 custody 逐层验证。

## 复用和完整模型

`application/reading_global.py` 使用现成 `market_read_scope`、`_read_manifest` 和
`read_stored_market`；后者分派原 `global_index_inputs` / `shibor_inputs`，
以及专属 `global_public_custody` 的固定原件 verifier。
不重写日历、窗口、数值、期限差、字段资格、保管格式或原来源 exporter。
页面沿既有 DOM/textContent、JSON 转义、CSP 和标准库实现；当前有限投影不新增 SDK、框架或服务。

`result.global_context.families.<name>.view` 保存完整原读取结果：
原 reader 以 `offset=0, limit=100, entity_id=None` 恢复该根全部声明项目，
US/Shibor 最多四指数或八期限，四族分别为 8 / 4 / 3 / 2 项，不产生额外采集。
完整 `sources` 指向原 query 或本组原件；query 可以继续沿其原 page SourceRef 恢复表示。

本组导航单列为 `families.<name>.page`。它包含 `entries`、`entity_id`、
`matched_count`、`offset`、`limit`、`returned`、`next_offset`；
`next_plan` 保留同一原根，只有分页位置变化。原 reader 内部的全量 pagination 保持不变。
因此换页、筛选 SPX / 3m，或存储后来增加新根，都不改变完整原模型、
`view_version`、原分母、完整资格计数及已有缺口。

原美股根允许声明支持范围的子集，Room 保留其实际分母，不把一个指数改标四个。
Shibor 的原完整分母固定为八期限。两者不相加成“十二种全球资产”，
也不从有限声明推断全市场或全球范围为空。

四族按各自 8 / 4 / 3 / 2 的分母恢复，不合并成跨市场总数。

三个状态维度分别保留：

1. Room 家族面板是否能读，例如 `readable` / `missing` / `invalid`。
2. 原保管恢复 `restored` / `partial-restoration`、未恢复查询及 `view=null`。
3. 原观察状态和整个声明范围的缺口。`restored` 不自动成为完整窗口或完整日期对。

`partial` 在完整模型上计算，不能通过选中一个好项目隐藏其他项目的问题。
美股任一原来源不合格、无观察或窗口不完整都会保留局部状态；
Shibor 缺格、无效格、缺明确日期对、无法精确计算及原接收范围内的无效格均保留。
四族则保留原来源状态、未完成回执、最新 null、恢复缺失和全范围缺值计数；
Yahoo 期货不生成区间收益。它们都不触发换根、补值、重试或近邻日期替代。

## 人读页面与原请求

原页面在提供计划时增加“全球背景”导航和面板：

- 美股展示源 US 日期、原交易日间隔、基准／截止点位、窗口状态和点位比率；
  单位是 `index-points`，`currency=null`。没有美元价格、外汇调整或认证总回报的推断。
- Shibor 展示源 Asia/Shanghai 日期、年百分率、明确基准日期、百分点／bp 差，
  以及同日 `3m-on` / `1y-on`。零和负值按原数值资格显示，不套正股价规则。
- 美债、ECB、Yahoo 期货、Coinbase 分别显示原捕获时钟、日期合同、源日期、单位、上一实际返回日期和合法变化；完整分母始终可见。
- `plan.as_of` 只是 Room 阅读时钟。各族的 `start/as_of`、原日历与基准日期保持原样；
  不统一到阅读日期、最新返回行或跨市场同时刻。
- 完整模型 JSON、完整原 reader JSON、源缺口、查询沿革和原引用均可展开或下载。
- 根、query 及原件按钮导出原 `{operation: "read-source", payload: <bare SourceRef>}`。
  第一页／下一页／精确身份筛选导出原 `reading-room` 请求，并保留其他族和其他面板的计划。
  按钮不自动执行、不填公开授权、不增加 source URL 请求。

源字符串仅通过 textContent 展示；完整页面继续使用原 JSON/HTML 边界转义和脚本／样式 CSP 摘要。
没有 `innerHTML`、自动网络、localStorage 或第二份可写存档。窄宽度时各族改为单列，表格在自己的容器内横向滚动。

## 权限、终态和容量

公开输出沿原 `public_sources_custody_confirmed`，与研究公开确认分别预检。
当前部署的 [Public 运行合同](../engineering/public-runtime.md) 只在绑定和原生可见性通过后，
按可信、可撤销配置填写这些既有字段；本组不改变 grant、授权范围或历史来源权利。
调用者不能在 payload 选择 public/private，网页导出的请求也不自带授权。
本地恢复不是公开许可；测试中的许可只用于相应合成记录。

原生已核 private 的正常 Room 仍沿原私有输出路径，输入、正文和 HTML 等最终检查完成后才释放。
原生 public 且获准的宿主可能在开始读取前保存原 `inputs.json`；失败不意味着这份已授权输入必定不存在。
两种模式均不得在终态后输出结果正文或新 HTML。

消费者使用共享 `market_reading_scope.SourceReads`，在已经固定的原 backend 周围保留只读错误记录，
直接保留原异常对象，不另建 API 或预算。原 `StoreUnavailable`、权限／绑定、scope、原生传输／请求预算终态
在子读取器的局部缺项处理前记录；随后所有 backend 读取先传播同一异常，不再发出请求。
整个全球组和 Room 不把它转成可导出的局部缺项。外层原 `market_read_scope`、Room
`reading_scope` 和宿主最终原生可见性／可达性检查均保留，异常退出不再进行后续读取。
普通缺根、缺 query/page 或局部内容不合格仍按原缺项显示，允许不相关的其他族阅读。

本组显示模型上限 2 MiB，超出时整组 `not-included`，保留原计划；
原 Room 6 MiB 模型、8 MiB HTML，原宿主输入／结果边界和 512 请求预算不提高。
各族使用同一个原操作 API，不重建或重置预算；保管层原 32 query、100 page 及字节约束不改变。
来源页缺失不重开来源额度，registry drift 不自动刷新，已结束的 US 计数检查窗口不为补保管再次采集。

## 验证、剩余责任与退出

专属 `test_global_room.py` 使用原合成 capture、真实临时 Git、原 custody 和独立读回，
检查完整模型逐值一致、固定根分页、未选中项缺口、不同源日期／单位、合法负值／零、
错族／非整根／计数回执拒绝、A 股 guard、registry drift、终态同对象和原 CLI 重开。
`test_global_room_host.py` 使用原 `GitHubAPI`、`ModelWire` 和原宿主：
完整页面脚本实际触发来源／分页／筛选按钮，导出的请求再次进入正常宿主；
只对一次原件 GET 返回 403 后恢复正常响应，检查失败后零请求、原异常身份及输出边界。
可信 Public 配置、撤销、原生 private 和读取后可见性反转分别覆盖。

唯一冷安装测试仍是原 `test_distribution`；其同一次 wheel 安装经过
`exercise_installed_news → exercise_installed_global_room`，在 checkout 外用同一个已安装程序保管合成根，
删除输入 capture 后连续两次只给根和 registry 读取，比对完整 JSON／HTML 及 store head 不变。
本组不增加第二套安装、源码导入旁路、workflow 或 CI 豁免。实际执行范围和收据留在对应交付证据中。

四族专属 `test_global_public_inputs/custody/room/host.py` 覆盖给定原件、原版本日期、SourceInput 字节边界、
同源精确恢复、普通缺失与终态、正常宿主整页和 bare SourceRef；同一冷安装接续四族 helper。

实际合法保管的根、首次真实正常消费、自然后续更新和研究／Brief采用分别需要原范围证据。
US 只留计数的旧回执不能还原没有保存的 capture；Shibor reader 工程不证明已有真实根。
主权债、FX、全球商品和加密资产的给定原件 adapter／保管／正常消费由上述专属合同覆盖；
这不证明有真实原件、已获来源权利或自然更新。
#10 原 owner 的全球来源与暂缓边界保持，不因消费者接入自动恢复采集或定时任务。

退役本消费者时，同一责任变更移除 `reading_global`、`reading_global_page`、可选 Room 接线、
专属测试及唯一安装 helper 和本合同链接；共享 US/Shibor reader、custody、Registry、A 股、
原 Room 和历史精确引用继续由其实际消费者维护。
