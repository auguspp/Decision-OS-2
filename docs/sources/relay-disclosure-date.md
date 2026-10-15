# Tushare Relay 财报预约：显式的单来源、单报告期读取（#12）

## 沿旧 Kernel 已验证的最小事实，而不是重新试遍接口

- 固定参考：`auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` 的 `runtime/tushare_relay.py`、`docs/data-source-orchestration-v1.md` 与 `docs/readings/2026-09-29-b2-relay-36591598716/README.md`。
- 2026-09-29 的真实有限取得曾返回第三方 Relay 的 `disclosure_date`，包括 `600598.SH`、报告期 `20260930`、源声明公告日期 `20260924` 和预计披露 `20261029`；`actual_date` 与 `modify_date` 为显式 `null`。另一次对 `688277.SH` 的响应甚至没有 `modify_date` **字段**。这些旧取得的原始日期不会被本实现重贴成当前/持续有效的预约状态；旧请求额度也不借重建 OS2 自动刷新。
- 旧包是第三方 Relay（`https://pcd.mobcvb.cn/tushare/pro`），不是交易所公告、公司PDF原件或直连的官方 Tushare。完整 Relay 静态能力清册和其他候选列于 [来源候选](relay-source-opportunities.md)，本模块**只实现已审的 `disclosure_date`**。不会为找补救路线使用 FTShare/Eastmoney/官方直连或其他 Relay API。

## 本组的实际工作

只读一只明确的 `000001.SZ`/`600598.SH` 类证券与一个真实季末 `YYYYMMDD` 报告期，最多100行；请求参数严格固定为 `ts_code/end_date/fields/limit`，不猜公司代码或报告期。请求只在 `--live` 明确选择下进行，固定 HTTPS 主机与 `X-API-Key`（仅可信运行环境变量 `TUSHARE_PROXY_API_KEY`）；不用环境代理、明文主机、cookie、请求重试、重定向或来源内容驱动的请求目的地。没有密钥就停止并报告，不能换接口绕拒绝。HTTP失败、声明正文长度不符、截断流、格式、限额和 `count` 不符均明确拒绝，不把失败/空响应等同于没有披露。

`normalize_disclosure_dates` 分开保留**报告期**、Relay源的 `ann_date`、一次实际取得时刻、`pre_date`、`actual_date`、`modify_date`。每个日期字段明确是 `missing`（响应根本无此列）、`null`（列存在但值为null或空）或 `value`（存在有效日期或原始修正记录）。官方[财报披露计划字段说明](https://tushare.pro/document/2?doc_id=162)把 `modify_date` 定义为字符串类型的“披露日期修正记录”，不是必定只有一个日期；本实现仅在它为单一 `YYYYMMDD` 时规范化，其他有界文本原样保留并标注 `unparsed-revision-record`，不猜分隔格式或历史顺序。尤其 `modify_date=null` **不是**“从未改期”；`actual_date` 的来源值也不是“已认证原始公告PDF”。源公告日或实际披露日晚于取得日期、实际披露早于报告期末时保留原值及 `temporal_issues`，不得把这些异常值当作已经发生的事实。旧报告期的真实历史可得时间、来源内部provider、后续修正全记录均未建立。响应的 `request_id`、经解析后的规范JSON SHA256，及真实在线读取时的 HTTP200、原始响应字节SHA256与安全 `X-Request-ID/X-Cache` 头，均在读取结果与派生来源中保留。离线回放不假造 HTTP status/响应头或原始wire摘要；**保存的是字段摘录与元数据，而非上游HTTP原始字节**。原始完整页的取得/许可证和本地序列化本次结果分别验收。

本组在 `observations/relay_disclosure_date.py` 保持这些纯数据语义，`infrastructure/relay_disclosure_date.py` 提供可拒绝的短期 HTTP 读取，`application/relay_disclosure_date.py` 只在受信操作主体显式确认可公开再分发时沿**现有** `VersionedStore` 一次原子保存派生摘录和版本引用。底层保管由#6拥有；无新数据库、第二内容池、PIT事实认证器或订阅调度。保存的 `SourceInput(custody=excerpt)` 是带局限声明的派生 JSON 结构，不是来源官网PDF、交易所证据或经济判断。`SourceRef` 返回后用相同权限原位置精确读回；同一意图不改键重发，模糊写入按#6原始引用对账。额外读回失败输出 `saved-reference-unverified`，保留已返回引用；提交未返回引用时保留原意图、`complete=false` 及对账要求，不能假定未写入。CLI对未完成保存返回非零；无效writer/意图在发起来源请求前拒绝。

## 显式 CLI（先离线，后在已有获准宿主按需运行）

```bash
# 只读旧已取得字节或用户明确提供的记录：校验字段与时间，不访问网络，不保存。
python -m decision_kernel.interfaces.relay_disclosure_date_cli \
  --ts-code 600598.SH --end-date 20260930 \
  --offline /approved/disclosure-date-capture.json

# 获得真实来源用途授权和凭据后，才手动请求上述一个证券/报告期。
python -m decision_kernel.interfaces.relay_disclosure_date_cli \
  --ts-code 600598.SH --end-date 20260930 --live

# 仅对确有公开再分发权利的摘录，经运行owner明确授权后提交到已初始化的存储。
python -m decision_kernel.interfaces.relay_disclosure_date_cli \
  --ts-code 600598.SH --end-date 20260930 \
  --offline /approved/disclosure-date-capture.json \
  --save --batch-id approved-disclosure-20260930 \
  --public-rights-confirmed --binding /trusted/approved-writer.json
```

离线JSON包装为 `{"captured_at":"2026-09-29T15:38:00+00:00","response":{"code":0,"data":{"fields":["ts_code","ann_date","end_date","pre_date","actual_date","modify_date"],"items":[]},"count":0,"api_name":"disclosure_date"}}`。这是**形状示意，不是零公告的历史事实**。真实旧响应及其原始字节保留在旧Kernel固定Git归档，不复制任何账号/密钥或不具再分发权的第三方原件到本仓。离线输入仅验证字节内部一致性，不认证来源是真实Relay；`--save` 是运行者的显式权利确认，不等于许可证自动批准。仓内默认GitHub绑定只读；操作时需受信且独立 `write_enabled=true`、独立令牌。可使用已存在的专用合成bare Git做工程保存测试，不创建生产仓库。

## 验收、现阶段限制与后续

本PR专用测试保护旧响应5列/6列、null/缺失/真实日期、错误证券与报告期、有限行数与count、密钥不泄露、代理与重定向拒绝、HTTP拒绝/未知、复用VersionedStore确切SourceRef、重复意图与不同意图冲突及安装后显式CLI。合成HTTP、不含凭据的运行环境、工程CI不代表新的live Relay取得或已获得公开发布许可。**本PR不启动任何真实Relay调用、公开records写入、披露PDF下载、Quick/Full、日常任务、通知、正式Decision或部署。**

#12以后可沿已核Relay的 `income/balancesheet/cashflow/fina_indicator` 做财务表结构化补充，`report_rc/research_report` 供券商预测与原研报目录关联；但每项必须单独核实具体权限、来源内部身份、数值单位、报告期与真实时点、可再分发权、正常消费者。不能把结构化财务表当作已阅读原公告/财务附注。#7经济研究质量、#13 Brief及正常调度各有独立验证；本模块替换/退役时连同专属CLI与测试退出，已保存来源引用与历史记录仍可精确读取。
