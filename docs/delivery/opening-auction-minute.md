# 原竞价名单的 09:40 分钟观察

本用途读取已保管的确切竞价原根，接给定 FTShare 分钟原始 UTF-8 bytes 和
有界原回执，再经现有 SourceInput / SourceRef、独立恢复与正常 Reading Room 消费。
没有来源 HTTP、SDK 安装、采集操作、调度、日线兼容转换或第二套预算。
真实分钟来源资格、首次真实原件与自然盘中交付仍须分别取得。

## 固定范围和复用依据

语义沿旧 Kernel 的[分钟合同][old-doc]、[确定性实现][old-code]，固定版本
`cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8`。上游精确参数来自
[FTShare SDK 1.0.10 的方法][sdk]和[固定分钟手册][manual]；本组只读这些工程原件，
不运行它们、不读取旧业务包，也不宣称现在账户或 endpoint 可用。

原 `opening-auction` 负责恢复完整 U/D/Z 原池、独立 U cohort 和固定五组。
本用途复用其 `cohort_version`，不复写条件，不需要同日 close，不取一个
筛选页或 MATCHED 子集做新分母。原 SourceInput、VersionedStore、submission journal、
`authorize_market_custody`、fresh reader、共享 `SourceReads` 及原 scope 继续负责
保管、CAS、权限、完整读取和终态。纯日期、JSON、Decimal 与离线页面继续使用
现役标准库；上游 SDK 的解码对象不能提供这里要求的原 bytes，因此无需新增依赖。

| 项目 | 确切合同 |
|---|---|
| 原名单 | `relay-auction-reading-v1` 的确切本仓 all-selection SourceRef，经原 registry、root/query/page 独立恢复；cohort 分母已知且等于完整行数 |
| 固定来源 | `FTSHARE_STOCK_MINUTES_BATCH`；GET `https://market.ft.tech/gateway/api/v2/market/data/stock_minutes/batch`。URL 只作请求身份，不执行 |
| 证券 | 原顺序的唯一合法 SH/SZ/BJ 身份；每批 1–20。重复 `symbols` query 键代表多个唯一值，不允许重复证券 |
| 时间 | 原市场日 Asia/Shanghai 09:30 至 09:41，严格整数 epoch ms、差 660000；`interval_value=1`、`limit=1000` 必须是整数，bool/float 不代替 |
| 复权 | 不接受 `adjust_kind`；保留 `OMITTED_OFFICIAL_NONE_DEFAULT`，不补签公司行动或回包复权证据 |
| 有界计划 | 最多 16 批 / 320 个可请求身份；余下身份、BJ、静态排除、未匹配和身份未明的原成员全部留在分母 |
| 目标点 | `ts_millis` 严格等于 T 09:40；同一行 `ts_millis_open` 严格等于 T 09:39；只接受唯一点和正有限 close |

历史 SDK 的 `raw=True` 先执行 `response.json()`；固定 handler 也解码再打印。
两者都不是原始 HTTP bytes。`response_record(...)` 只接受调用者给定 bytes 并
严格 UTF-8 解码，保持字节长度与 SHA256；它不认证这些 bytes 的取得过程。
不接受 `LIVE`、workflow 声明或调用者布尔值为来源执行/权利证明。

## 给定输入与回执

保存入口接 `ftshare-auction-minute-reading-v1`，精确字段为：

| 字段 | 内容 |
|---|---|
| `schema` | `ftshare-auction-minute-reading-v1` |
| `auction_reference` | 原竞价完整 SourceRef 对象；不能是任意 cohort JSON、页、计数回执或 legacy ZIP |
| `market_session` | 规范 ISO 日期；保存时必须等于独立恢复的原竞价日 |
| `started_at` / `finished_at` | 原捕获起止时钟，明确时区，开始不得早于 T 09:41，持续不超过十分钟 |
| `records` | 已计划批次的有序尝试前缀，索引从 0 起，最多 16；每个证券最多出现在一个批次 |
| `stopped` | 完整成功时 null；否则必须等于最后一次原尝试的有限失败码；不能少批而不解释 |

每条共同字段为 `index, request, state, requested_at, received_at, outcome`。
`request` 精确等于 `{method, url, parameters}`，参数字段只有表中五项；它必须与
完整原名单计划的同一批逐字段一致。请求时钟必须在原捕获区间内，
`上次接收 <= 本次请求 <= 本次接收 <= finished_at`；失败之后不能再有尝试。

`state=HTTP_RESPONSE` 另有 `http_status, body, bytes, sha256`。`body` 是给定原
UTF-8 bytes 的未重新序列化字符串；完整输入、每份完整编码 SourceInput 都受
现役 2 MiB 限制。HTTP status 是严格整数；原 HTTP 401/403/429 分别保留认证失败、
资格拒绝、限流，其余非 200 是 HTTP_REJECTED。HTTP 200 的 JSON 必须是 object，
`code` 为严格整数 0/200，`data` 为 list；其他业务码和解析失败分开保存。
非空 data 至少含一个具有字符串 symbol 和列表 items 的 row。

`state=TRANSPORT_FAILURE` 没有正文或 HTTP status；只允许原有限传输/取得失败码，
例如 TRANSPORT_FAILURE、AUTHENTICATION_UNAVAILABLE、RESPONSE_TOO_LARGE。
回执描述的尝试数量不冒充本次执行的 HTTP 数量。本次 `source_requests=0`。
空 records 仅在原计划也为空时完整；本用途不会用空成功掩盖尚未尝试的非空名单。
原失败/hold/耗尽状态不由这份给定输入重新打开。

纯 `validate_input` / `minute_reading_projection` 只检查给定 bytes、回执与容量。
保存必须沿原 store 再恢复 exact seed，检查原成员顺序、完整计划和 cohort version，
才可能写原件。没有后端的通用纯 `read_market_payload` 不接受调用者给定假 cohort
来声称分钟已经可读。

## 点资格和完整范围

每个请求证券在响应 `data` 必须恰好出现一次；重复原行即使相同也为
SYMBOL_AMBIGUOUS。未请求身份仅列在 `unexpected_rows`，不进入原名单。
`items` 必须是最多 1000 项的列表；目标分钟重复也不得择一、求平均或采用最近值。
时间戳字符串、bool、别日或错误开始边界均不合格。

close 复用原 `opening_auction.number`：有限、正数、最多 28 位有效数字、指数
绝对值不超过 12。零、负值、bool、NaN 或超界保持 TARGET_CLOSE_INVALID；
原字段和行位置继续可查，完整原文本仍由其 SourceRef 恢复。
其他分钟、OHLC、volume、amount 缺失及 `total != len(items)` 不取消独立合格点；
reported_total、返回数量和是否相等另行保存。

输出先连接全部原 `cohort_rows`，保持 MATCHED、NOT_MATCHED、AUCTION_UNRESOLVED、
STATIC_EXCLUDED、STATIC_UNRESOLVED 五组及每组完整分母。身份未明、超 16 批、
来源停止后的未尝试、响应缺行、点不合格、现在原件未恢复分别显示。
原 auction pool 的覆盖缺口继续进入 `coverage_gaps`，不能把完整点覆盖升为全市场覆盖。

Relay 原竞价价和 FTShare `close_0940` 分栏。分钟价沿供应商证券字段，无单位转换；
不补币种或复权证明。`cross_provider_return=NOT_COMPUTED`，不计算跨来源收益，
不建立日内路径、持续强势、可成交性、经济结果、Odds 或交易权限。
开始取得时钟已晚于 09:40，固定显示 HISTORICAL_OR_LATE_NOT_0940_DISCOVERY。
当前 Room 阅读时钟不会改变原源日期、取得时间或这一限制。

## 标准保管和独立恢复

每个实际给定 HTTP body 成为普通 `SourceInput(kind=content,custody=provided)`，
`acquired_at` 保持原 received_at。身份摘要绑定 exact auction SourceRef、完整回执、
请求参数、字节摘要和所有原时钟；相同正文配不同请求/时钟不会串包。

新 typed 根 `ftshare-auction-minute-custody-v1` 仍是普通 SourceInput。
它保存 exact seed、派生计划、去掉正文后的原回执、各原件 SourceRef/SourceInput 摘要、
完整模型摘要。root 不代签原 HTTP 执行；原数据与 typed root 均使用现役
`market-custody-<intent digest>` journal/CAS，UnknownWrite、Conflict 直接交原 owner，
不换键重试、不删除失败、不建另一状态库。

所有输入和完整 SourceInput 在新写之前预检；全部原件加 root 的编码总量也不超过
2 MiB。预检为每个标准 SourceRef 保留最大合法长度的容量余量，不沿旧分钟总 body
16 MiB 放大 OS2 上限。恢复首先检查 root 的声明下界，再累计实际完整来源编码，
不能靠多份各自未超限原件越过整组上限。

保存后重新构造独立 reader，恢复确切 seed 和所有原件、重建完整模型并逐摘要比对；
远端 fresh reader 必须沿同一个原 API 对象，不能重新获得 512 次额度。
临时捕获文件删除后，只给新根也能通过正常 CLI 重开。
可选 seed close 缺失不改 cohort 或本用途；原 pool/auction 的依赖变化不能重新分组
冒充原版本。seed 不能恢复时 view 为未知，不使用缓存名单或旧好根补位。

一个分钟 body 普通 MissingObject 或内容损坏只使该批成员未知，其他原件、五组和
完整分母仍可读。当前底层 StoreUnavailable、权限/可见性、scope、可达性或预算
终态使用原共享 SourceReads 锁住同一异常对象，后续读取为 0，整次退出。
原历史 403 回执是给定数据；当前 GitHub GET 403 是本次操作终态，两者不混淆。
不扩大 shared catch，不改变原 512 请求、source hold 或 FTShare 日线 allowance。

## 正常入口与页面

现有 market-custody 的 `save` / `read` 薄分派此输入和根，不新增 runtime operation：

```console
python -m decision_kernel.interfaces.market_custody_cli save \
  --path /approved/existing-synthetic.git --registry /trusted/relay-registry.json \
  --input /approved/given-minute-originals.json --custody-authorized

python -m decision_kernel.interfaces.market_custody_cli read \
  --path /approved/existing-synthetic.git --registry /trusted/relay-registry.json \
  --input /approved/exact-minute-root.json
```

`--path` 沿原约束仅是已经初始化的合成 GitStore；真实存储沿原获准 binding。
来源权利、公开许可和最终 writer 检查继续由原入口核验。

正常 Room 计划增加可选 `auction_minute`：完整 `reference`，可选 `offset`（默认 0）、
`limit`（1–50，默认 20）、`entity_id`（合法 SH/SZ/BJ 或 null）。省略本槽不加载
来源/registry，不增加空面板。原 `auction`、A 股 guard 和其他全球用途保持各自责任。
原宿主 payload 是 `{"plan": <Room 计划>}`，公开时沿
`public_sources_custody_confirmed` 与可信可撤销部署授权；没有来源自授权字段。

```console
decision-os store --path /approved/existing-synthetic.git research-room \
  --input /approved/minute-room-plan.json --out /approved/new-room.html \
  --market-registry /trusted/relay-registry.json
```

应用先完成全 scope 和原退出检查，再以单独 `page` 投影分页/筛选。
完整 view、五组、cohort_version 和 view_version 不随导航改变；下一页固定同一根。
页面显示取得区间、原停止原因、未请求成员、各项资格和原请求；可下载完整模型。
原根、seed、分钟原件按钮只导出 `{operation:"read-source",payload:<裸 SourceRef>}`；
导航导出正常 reading-room 请求，不自行联网、不填权限、不改其他面板计划。

页面沿现役 textContent、HTML/JSON 边界转义和 CSP，无 innerHTML、localStorage
或自动网络；表格在自身容器滚动。整组展示上限 2 MiB，超出整组 not-included，
不截取一个好子集。原 Room 6 MiB、HTML 8 MiB 和全部最终核验不变。
Private 失败不释放 inputs/正文/HTML；已获准 Public 路径可能先保留授权 inputs，
失败后仍无成功结果或 HTML，不能称所有公开 inputs 都为零。

## 有限验证和退出责任

专属测试覆盖：五组/BJ/未知身份与 20/20/17、320 之外的完整分母；重复原行/目标点；
原窗口、时钟、数字、失败和未尝试；全编码与聚合容量、串包与 fresh 恢复；
普通缺项/seed 缺失；正常 CLI 独立重开；原临时 Git + 原 GitHubAPI/ModelWire 的
单次 403 后恢复 200、真实原 512 耗尽、同异常贯穿和失败后零读；实际整页脚本导出的
bare SourceRef/分页/BJ 筛选再进原宿主；最终 private 可见性反转。

只扩展原唯一 wheel 安装消费者的 `exercise_installed_auction_minute_room`，
由集成者在同一次安装 gate 运行；不增加第二冷链、来源 probe 或 CI 豁免。
合成工程通过不证明真实原件、当前账户资格、再分发权、首次真实消费或自然更新。
现役运输 allowlist 仍未增加分钟 route，原 FTShare reserve/hold/预算仍归原来源 owner。
T+1、完整盘中路径、策略评估和人工接受均不由此点观察补签。

退出此用途时同责任变更移除专属纯观察、输入/typed root、正常分派、Room/页面、
专属测试与原安装 helper 调用，并明确历史根读取是否仍须保留。
共享 SourceInput、SourceReads、竞价、日线、授权与唯一安装链不随本用途停用而删除。

[old-doc]: https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/d-auction-minutes-v1.md
[old-code]: https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/src/decision_kernel/runtime/d_auction_minutes.py
[sdk]: https://github.com/FTShare-Lab/FTShare-python-sdk/blob/23eb471c9aae1f6c3ad940d73c4c0fab431761b2/src/ftshare/apis/stock.py
[manual]: https://github.com/FTShare-Lab/FTShare-skill/blob/d31ee34c86d68569a486feedec5dd8d5cc29db31/ftshare-market-data/sub-skills/stock-minutes-batch/SKILL.md
