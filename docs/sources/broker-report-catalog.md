# 券商研报目录：旧 Kernel 窄复用与精确来源引用（#12）

本组基于旧 `auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` 的 `runtime/broker_report_discovery.py` 和 `docs/broker-report-discovery-v1.md`，复用四类报告的原生路由/类型、时间、分页、缺口与身份语义。上游协议映射此前参考过 `manymore13/report-cli@0d98dde87a6b295359ae68152ca2693428baef83`（MIT；本批不复制其第三方整包/源码），只窄移植已有原生查询表和已验失败约束。**券商研报目录不是研究正文、公司公告、某券商的持仓、EPS共识或投资建议。**

## 已有能力和原件限制

- `industry` 使用 `reportapi.eastmoney.com/report/list`、`qType=1`；`strategy`、`macro`、`morning` 使用 `/report/jg`、`qType=2/3/4`。这是东方财富公开目录，不是 Tushare 或官方交易所披露；`industry_code` 仅能使用预核的上游数字代码，不能由中文行业名猜测。
- 每次仅一个类别、明确 `begin/end`（最多92天）、每页50行、最多四页。`hits/TotalPage/pageNo`、返回页长、总分母与每行披露日期必须吻合；超预算、未知字段形状、HTTP 403/429、重定向或后页失败均**停止且不冒称空结果或完整目录**，不寻找替代URL绕过。
- `infoCode` 优先、缺失时使用 `encodeUrl` 作为不同的上游身份；`report_key` 与行版本摘要分开。重复ID同一版本保留页位置；同ID不同版本不抹历史。`column` 与类别前缀冲突的行仍可显示冲突，但不得提交来源；仅按所请求的类别归类时明确 `REQUEST_ONLY_NOT_INDEPENDENTLY_VERIFIED`。
- `publishDate` 原字符串原样留在描述；时区 `UNKNOWN`，只按日期归档，**不从取得时间推断历史首次可得时刻**。`ratingName` 属于来源字段，不计算预测变动或研究建议。关键词搜索仅过滤已完成有限目录，不能声称查遍市场。
- 可保存的每条记录一律通过已交付 `VersionedStore.put_source` / `read_source` 变成 `SourceRef`，**只保管详情页 `external-only locator`**，`SourceInput(text=None, custody=external)`；原 PDF、PDF 下载权、模型阅读全文、评级依据和对外再分发许可均未自动取得。唯一研究真源仍为同仓公开 `records` 分支；仓内配置只读，真实业务写入须受信 `write_enabled=true` 运行绑定与独立令牌。
- 目录显示最多200行；一次显式保存最多12个报告版本（超限在第一次写入前拒绝），避免超过现有受信GitHubAPI的有限请求预算。确有较大窗口必须由真实用途划分明确的小日期窗口，不自动分批重试；每条记录逐行独立原生CAS提交。后面行写入失败不抹先前成功 SourceRef；输出 `complete=false`、实际 `saved`、失败 `last_intent`，并返回非零。未知写先按原submission ID对账，不换键自动发起第二次写。

## 覆盖、来源身份和失败回执的纠正

返回页数、页长与总行数一致只证明读取了供应商声明的分页。重复ID和同ID变体尤其不能证明唯一报告集合没有遗漏。因此输出状态为 `provider-pages-read` / `provider-empty-result`，覆盖为 `PROVIDER_PAGES_READ_NOT_CORPUS_COMPLETENESS`，`window_completeness=NOT_ESTABLISHED`；另列 `unique_reports`、`versions` 和 `duplicate_or_variant_rows`。原 `complete-bounded-index` / `complete-empty-window` 状态名不再输出，调用者应使用上述字段，不把空响应当成市场无研报。

每条新保存的 external-only 来源同时保留券商名称/代码、行业和评级原值（`RATING_RAW_NOT_RECOMMENDATION`），从普通 VersionedStore 查询券商也能找回。缺失字段不补造；历史记录不回填、不重写，原SourceRef照常可读。同一旧submission的恢复应使用原输入或原回执，不能以加入新元数据的输入替换旧意图；新数据保存使用明确新意图。

实时请求在首个I/O前检查日期窗口，在每页返回后检查业务状态、页码、所有行和分母，坏页后不再请求下一页。声明正文长度与实际字节数必须一致，截断流输出有限错误且关闭连接，不回显原件。没有改变请求上限、来源授权或增加自动重试。

保存后额外读回失败时，`status=readback-unconfirmed` 并保留 `pending_reference` 和原 `last_intent`，`saved` 仍只计已完成读回的此前条目；不得把新引用丢掉或当作尚未写入。尚无已知引用的 UnknownWrite 按类型保留 `unknown-write`，不能只靠异常文字识别。统一返回 `complete=false`，由后继沿原意图对账，不换键重发。

## 安装后独立入口（不抢 #7 的 `decision-os store` CLI）

```bash
# 已从获准读取取得的 JSON 页，仅检验/过滤其内形状；不证明真实来源返回。
python -m decision_kernel.interfaces.broker_report_catalog_cli \
  --kind strategy --begin 2026-10-01 --end 2026-10-12 \
  --offline /approved/previous-pages.json --keyword PCB

# 显式新来源请求；只走固定HTTPS目的地，无代理/回退/自动重试。
python -m decision_kernel.interfaces.broker_report_catalog_cli \
  --kind industry --begin 2026-10-01 --end 2026-10-12 --live

# 现有合成bare Git里复验元数据→外部locator SourceRef→确切读回。
python -m decision_kernel.interfaces.broker_report_catalog_cli \
  --kind strategy --begin 2026-10-01 --end 2026-10-12 \
  --offline /approved/previous-pages.json --save \
  --batch-id manually-approved-one --local-synthetic-store /tmp/existing-synthetic.git
```

离线文件为 `{"captured_at":"2026-10-15T08:00:00+00:00","pages":[{"pageNo":1,"hits":0,"TotalPage":0,"data":[]}]}`。非零页保留 `infoCode/encodeUrl`、`title`、`publishDate`、`orgSName/orgCode`、`industryName`、`column` 等原始字段。**离线JSON仅自洽，不是来源认证**。`--live` 显式选择才触及外部固定域名；`--save`、`--batch-id`、已存在合成bare Git路径或受信 `--binding` 同时成立才保管来源。不能修改公开仓中默认只读绑定或将其作为共享凭据。

这组能力是可安装的有限目录消费者，尚无本次真实 Eastmoney HTTP 200、PDF、公开研究、定时任务或真人研究接受证据。权限拒绝、请求超限和上游错误按缺口保留；不重新执行旧 Kernel 一次性受限数据获取。

## Relay 的使用与区别

旧 Kernel 的 [Relay 接口登记](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/readings/relay-source-catalog-review-2026-10-14.md) 已包含多类对 #12 有价值的数据（详见 [Relay 资料切片清单](relay-source-opportunities.md)）。**本批 Easternmoney 研报目录不自动改走 Relay fallback。** `report_rc` 是结构化券商预测期记录，`research_report` 可是历史报告/ PDF locator，但各自独立来源身份、字段/授权/完整性，不等于本目录已读的PDF。接下来的 `disclosure_date` 有旧仓实际有限响应，适合做独立的财报预约责任组；它不是原始年报PDF。

## 维护与退出

归属 #12 资料发现。完整产品范围仍含公告/PDF、财报预约和实际事件、新闻/RSS、参与者八面、研报版本；按可核消费者分组，而不是一次迁旧Kernel全部任务。代码移除时同时退出本组 `broker_report_catalog` 纯转换/HTTP/CLI/专属测试和本说明；共享 `VersionedStore`、真实来源和历史SourceRef不删除。许可证只约束所用开源代码，不自动授予券商PDF/付费内容转载权。
