# 给定全球原件的保管与正常阅读

责任为 #147 的有限四族工程组：将已经给定的 Treasury、ECB、Yahoo Futures、
Coinbase 原件接入原 `market-custody` 保管和 Reading Room。
本组没有来源 HTTP 客户端、抓取入口、外部 SDK、运行时 operation、workflow、cron、
配置授权或真实业务记录。实际来源可用性、权利、真实根与后续自然更新仍需各自证据。

## 可复用的固定语义

纯解析来自旧 Kernel 固定提交
`cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` 的
`observations/global_public.py`（原 Git blob `be8711b353997f7d871d3de82623cf795e687773`）。
它只接收给定字节，保留原来的请求窗口、来源字段、时区、单位、实际日期与空值语义。
OS2 中的 `observations/global_public.py` 仅依赖标准库与现役记录错误／字节边界，
不装入旧 runtime、ZIP、身份运行环境、HTTP 客户端或私人业务格式。

| Room 槽 | 固定来源／表示 | 完整分母 | 日期、单位及变化 |
|---|---|---:|---|
| `treasury` | `home.treasury.gov` 的 monthly daily Treasury yield curve Atom/XML；一个或两个精确月 | 8 个 `UST:` tenor | `NEW_DATE` 与年百分率 `ANNUAL_PERCENT`；实际两次返回日期差为 bp；合法零／负值保留 |
| `fx` | `www.ecb.europa.eu` 的 90 日 ECB reference XML；EUR 为基币 | USD、CNY、JPY、GBP 共 4 项 | 目标币种 `*_PER_EUR`；信息参考值；变化只比较实际两次返回日期 |
| `commodities` | `query1.finance.yahoo.com` 的 `GC=F`、`CL=F`、`BZ=F` chart JSON | 3 个精确期货代码 | 原 exchange timezone 日期；美元／金衡盎司及美元／桶；油价零／负值保留；不计算换月资格未知的区间收益 |
| `crypto` | `api.exchange.coinbase.com` 的 `BTC-USD`、`ETH-USD` 86400 秒 candles | 2 个精确产品 | 完成的 UTC 日桶，`USD_PER_BTC` / `USD_PER_ETH`；身份来自精确请求产品，响应没有产品回显认证；不补缺失桶 |

旧 14 个日历日请求窗口、各响应行数限制、重复日期、重复字段、XML 实体禁止、
原 OHLC 资格与 Yahoo symbol/currency/FUTURE/1d 身份检查保留。
JSON 数字使用 Decimal；源值沿原 60 字符和有限数值资格，并将 Decimal 指数限制在
`[-60, 60]`，防止短小输入扩成无界定点字符串。源 metadata 中的 Decimal 以精确文字保留，
不经过 float。不存在的日期转换成现役 `RecordError(GP_DATE)`，让坏根仍按局部内容错误处理。
原 36 位运算上下文无法表达的结果会以 `GP_ARITHMETIC_UNAVAILABLE`
终止该输入，不生成伪精确变化。

最新一行的 null 保留其实际日期与未知值，不回填上一非空值。
上一日期就是上一实际返回日期，不认证相邻交易日。
来源发布时间、市场开闭状态、历史当时可知版本、研究／投资／人为接纳资格均不推断。

## 严格的给定原件输入

新的 **输入表示** schema 是 `global-public-reading-v1`，与原 Relay capture 分开验证。
schema 不认证一次真实 HTTP 执行，也不授予来源访问或公开权利。
完整可运行的合成构造例在 `tests/test_global_public_inputs.py::public_input`；不需要真实来源 GET。

顶层字段严格为：

| 字段 | 合同 |
|---|---|
| `schema` | 固定 `global-public-reading-v1` |
| `capture_version` | `global-public-context-v1` 或 `global-public-context-v2`，选择已有日期规则 |
| `family` | 上表四个固定名称之一 |
| `as_of_date` | 原请求窗口截止日，严格 `YYYY-MM-DD` |
| `started_at`, `finished_at` | 原捕获的有时区时钟，开始不得晚于完成；读取时钟不替换它们 |
| `execution_complete` | bool，表示给定回执的执行完整状态；不能靠它跳过每个记录的检查 |
| `records` | 固定 `plan(family, as_of_date)` 的全部槽，严格原顺序和实际请求数 |
| `date_policy` | 只在 v2 必须出现，逐值等于由原 `started_at` 计算的策略；v1 不接受此字段 |

每个记录严格为 `index, spec, state, http_status, requested_at, received_at,
headers, body, bytes, sha256, error_type`。
`spec` 必须逐值等于纯 adapter 生成的固定 host、URL、参数、产品／月份、起止日期。
这些 URL 仅作为来源身份保存，不会被此入口访问。

`HTTP_RESPONSE` 的 `body` 是原 UTF-8 文本；字节数与 SHA256 必须等于它重新编码后的原字节。
保留 BOM；不重排 JSON/XML、不正规化日期文本、不换行修复。
头部只接受原有限安全集合：content-type、content-length、content-encoding、date、
last-modified、etag、retry-after；单值不超过 2048 字节且不含 CR/LF。
编码只能是 identity，存在 content-length 时必须与原字节数一致。

其他状态为 `NOT_ATTEMPTED`、`NOT_ATTEMPTED_SERVICE_STOP`、`REQUEST_IN_PROGRESS`、
`TRANSPORT_UNAVAILABLE`，各自缺省字段严格为空。记录请求／响应时钟必须处于原捕获范围内。
401/403/429 或 transport unavailable 后，同服务的后续槽只能保留未尝试状态。
已有源失败、未完成、空窗口或不合格表会保留其状态及完整分母，不伪装完整资料。
来源 HTTP 原失败回执可以保管；它不会触发任何补抓。

原 v1 要求截止日在原捕获 UTC 日期之前。v2 沿原策略：Treasury 的 New York 18:00、
ECB 的 Berlin 17:00 作为请求日期机会门槛；Yahoo 和 Coinbase 只允许原捕获前一 UTC 日期。
这些是合同中的请求上限，不证明实际发布或交易结束。
所有版本保留原 31 日近期窗口限制，不能用后来读取时钟为旧输入重新赋予日期资格。

输入总编码最多 2 MiB，原宿主 transport 上限和压缩传送边界不变。
**每份完整 `encoded(asdict(SourceInput))` 也必须不超过原 2 MiB 上限**。
旧 raw body 的许可大小不会扩大 SourceInput；不接收旧多文件 archive 的更大合计上限。

## 原保管与独立恢复

`global_public_custody` 只组合现役 `VersionedStore`、`authorize_market_custody`、
`fresh_market_reader`、`market_read_scope` 与共享 `market_reading_scope.SourceReads`。
所有原件在第一次写入前完成输入和完整 SourceInput 检查。

每个确实给定的响应原件是一份标准 `SourceInput(kind="content", custody="provided")`，
text 就是原文本，origin 是固定源 URL，acquired_at 是原响应时钟，published_at 保持 null。
`global-original-*` 身份绑定完整捕获身份及该记录元数据，不只是正文摘要。
保存、精确版本、CAS、同 submission 回放和未知写入接续全部由现役 store 负责。

`global-public-custody-v1` 根也是标准 SourceInput：保留完整 receipt recipe、原件完整 SourceRef、
各 SourceInput 摘要以及完整默认 reader 结果摘要。body 用精确原件槽替换，不重复嵌入原始正文。
根前缀 `market-global-*` 只选择 verifier，真实内容仍逐层验证。

每份原件经独立 reader 验证后才保存根；另一全新 reader 重新读取根和全部原件，
要求原完整结果逐值一致，最后由原 writer 再做原生可见性／授权检查。
只有这个过程完成才返回 `stored-and-independently-restored`。
未知写入立刻停止；不会发第二份 intent、删除已存原件或自动重试。

恢复严格绑定同一 repository、完整 SourceRef、原件顺序、字节／内容／SourceInput 摘要、
来源 URL、捕获元数据及全模型摘要。来源串换、body slot 替换、压缩预算或不同根均不能补上缺项。
普通缺失原件保留 `partial-restoration` 和所有声明对象；单产品缺失只影响其产品。
Treasury 两个月共同构成八 tenor 的路径，丢失任一月会让共同观察保持未知，
不把幸存旧月升级为完整路径的最新值。

当前存储的权限、绑定、请求预算、scope、传输终态必须把原异常同对象传播至调用方。
终态后零续读，不再处理另一个原件或另一个族。共享 facade 无独立 API、预算、transport 或重试。
普通缺包与终态分类分开；外层原 scope 退出和最终原生检查都完成后才可以输出。

## 正常入口与页面

没有新增运行时 operation。原 `market_reading.read_market_payload`、
`market_reading_projection`、`retain_market_reading`、`read_stored_market` 仅做薄分派。
Relay 和 A 股 verifier 保持原合同；A 股面板在恢复任何本组原件前拒绝本组根。

已有 CLI 可保存和重开：

```console
python -m decision_kernel.interfaces.market_custody_cli save \
  --path /approved/existing-synthetic.git --registry /trusted/checkout/docs/sources/relay-registry.json \
  --input /approved/supplied-originals.json --public-rights-confirmed

python -m decision_kernel.interfaces.market_custody_cli read \
  --path /approved/existing-synthetic.git --registry /trusted/checkout/docs/sources/relay-registry.json \
  --input /approved/exact-root-reference.json
```

示例的 `--path` 仍只用于已初始化合成 store；真实目的地继续使用原 binding 和现有授权。
共享 CLI 的 registry 参数保持原形状；四族来源资格来自固定本组 verifier，
不能用 Relay 登记或本输入中的声明替代来源权利。

Room 的 `global_context` 接受四个新增槽，值仍为完整 root SourceRef 及可选
`offset/limit/entity_id`。参见[正常全球阅读合同](../delivery/reading-global.md)。
每族先恢复完整模型，再产生独立页面投影；完整分母为 8 / 4 / 3 / 2，
与 US 指数／Shibor 分开，日期、单位与原捕获时钟不对齐为一个虚假全球瞬间。
全量 JSON、来源状态、根／原件 bare SourceRef、下一页和精确序列筛选均可由正常页面导出并重开。

Public 来源输出沿原授权字段和可信可撤销 grant。私有输出沿原生私有读取路径；
本输入不能自授公开许可。终态后私有输入、正文、HTML 不释放；已获准 Public 宿主可能已保存
原 `inputs.json`，其存在不能被误报为“失败后所有公开 inputs 为零”。

## 有限验证和仍未验收的事实

专属 input/custody/room/host 测试覆盖四族纯语义、原版本日期资格、latest null、
零／负值、缺日、错身份、编码 SourceInput 边界、原 CAS 接续、独立恢复、全分母分页、
A 股 guard、同对象终态、真实临时 Git、原 GitHubAPI/ModelWire、完整页面脚本和裸请求重开。
一次原件 GET 403 后恢复正常 200 的模型验证零后续请求，且最终原生可见性反转仍阻止输出。

唯一冷安装由原 `test_distribution` 接续
`exercise_installed_news → exercise_installed_global_public_room`。
同一个已安装 wheel 在 checkout 外保管合成原件；删除输入后由新进程两次只给根恢复，
检查完整 JSON / HTML 和 store head。没有第二套安装，也不以 `PYTHONPATH` 替代该 gate。

这些是工程资格。真实来源可用性、真实保管根、首次真实正常消费、自然后续更新以及研究采用
均未因此完成。旧 US 五槽计数回执不能恢复其未保留原件；Shibor 真实根另需证据。
#10 owner 的来源与恢复边界保持；本组不启用全球采集、重开旧窗口或抢占日历恢复。
