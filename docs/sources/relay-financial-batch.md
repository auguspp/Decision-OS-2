# Relay 财务资料批量接入（#12）

## 一组八接口，不按用量拆成八轮

用户于2026-10-15明确允许接口批量接入、不考虑用量。本能力一次覆盖八类财务资料，支持显式的多证券×多报告期×接口计划；没有公司数量、接口次数或费用额度限制。仍保留实际服务拒绝、超时、分页和单个记录内存边界，不把“不考虑用量”解释成绕过401/403/429或无限重试。

| API | 内容 | 字段合同参考 |
|---|---|---|
| income | 利润表、每股收益、归母与少数股东损益 | [官方33](https://tushare.pro/document/2?doc_id=33) |
| balancesheet | 资产、负债、权益、营运资金项目 | [官方36](https://tushare.pro/document/2?doc_id=36) |
| cashflow | 经营/投资/筹资现金及现金余额 | [官方44](https://tushare.pro/document/2?doc_id=44) |
| fina_indicator | 盈利、偿债、增长、每股等指标 | [官方79](https://tushare.pro/document/2?doc_id=79) |
| forecast | 业绩预告、预计利润范围及原因 | [官方45](https://tushare.pro/document/2?doc_id=45) |
| express | 业绩快报、初步经营结果 | [官方46](https://tushare.pro/document/2?doc_id=46) |
| fina_audit | 审计结果、事务所、费用等元数据 | [官方80](https://tushare.pro/document/2?doc_id=80) |
| fina_mainbz | 按产品P/地区D/行业I的主营构成 | [官方81](https://tushare.pro/document/2?doc_id=81) |

以上文档于2026-10-15核读，用于明确字段语义；实际取得仍通过既有第三方 `pcd.mobcvb.cn/tushare/pro`，不是改走官方SDK。四张主要财务表沿旧Kernel@cb3bbc87的Relay登记；其他四类按本次批量授权增加显式合同，不因静态手册就宣称逐项线上成功。每接口请求的是SPECS中列明的核心字段集合，不宣称支持所有历史字段、VIP接口或全部市场。未返回的所请求字段明确列在 `missing_requested_fields`。

## 复用和职责

`infrastructure/relay_http.py` 从PR#30已交付传输提炼，固定HTTPS主机、环境变量 `TUSHARE_PROXY_API_KEY`、X-API-Key、禁止重定向/代理/重试、长度/JSON/密钥反射校验共用；原 `RelayDisclosureTransport` 保持单次合同、错误码、响应回执与512KiB边界。金融批次则可反复使用同一HTTP对象处理明确计划，不沿用其单次操作限制。无新SDK、数据库、任务平台或行情供应商路由。

`observations/relay_financial.py` 管八类字段/日期/数值与报告口径；`infrastructure/relay_financial.py` 负责有限分页；`application/relay_financial_batch.py` 流式提供批次结果和现有VersionedStore保存；独立 `interfaces/relay_financial_cli.py` 不改研究CLI、中央Store、CI或依赖锁。

## 批量使用

```bash
# 不发请求；默认包含全部八类。两证券×两报告期×八API=32个具名查询。
python -m decision_kernel.interfaces.relay_financial_cli plan \
  --codes 000001.SZ 600000.SH --periods 20251231 20260630 --output plan.jsonl

# 主营构成可同时按产品、地区、行业读取；三张报表也可显式选择报告类型。
python -m decision_kernel.interfaces.relay_financial_cli plan \
  --codes 000001.SZ --periods 20260630 --dimensions P D I \
  --report-types 1 2 6 --output expanded-plan.jsonl

# 可信宿主已具备既有Relay密钥时执行，不输出或传入命令行密钥。
python -m decision_kernel.interfaces.relay_financial_cli capture \
  --plan plan.jsonl --live --output capture.jsonl --page-size 100 --max-pages 100

# 完全离线读取原值、单位与缺口；inspect不会再次请求来源。
python -m decision_kernel.interfaces.relay_financial_cli inspect \
  --capture capture.jsonl --include-rows

# 用同一原capture及同一batch-id恢复保存；receipts选择一个新的输出文件。
python -m decision_kernel.interfaces.relay_financial_cli save \
  --capture capture.jsonl --batch-id financial-use-001 --public-rights-confirmed \
  --binding /trusted/writer-binding.json --receipts saved-references.jsonl
```

所有输出文件以排他创建方式打开，不覆盖旧捕获或回执。只读查询可以批量进行；保存必须由有权运行者确认材料可公开及现有writer绑定，来源JSON里的“授权”没有效力。`--local-synthetic-store` 只用于现有合成bare Git的工程回放，不能作为真实材料的秘密生产库。

## 完整性、失败与接续

金融查询用原生 `period` 参数，不能混淆为按公告日期截止筛选的 `end_date`；主营类型单独传 `type`。三表 `report_type` 未指定时不称已经取得所有母/合并/调整版本；按返回值分别保留，完全不筛掉旧版或自动选“最新”。八个接口没有被强行拼成一张可算估值的表。

每页最多100行（兼容较小单页接口边界），满页才显式递增offset；offset在实际Relay端的兼容性必须通过实际响应验证。重复整页、前后身份/报告期不匹配、业务失败、数据宽度或count矛盾会失败，不换源补洞；达到可调整max-pages时返回未完成。短页只证明这次供应商分页结束，不证明历史修订穷尽/全市场完整。已取得页及实际失败记录仍保留；一个接口的普通失败不会抹去其他接口成功，但认证或限流拒绝会停止本批共享通路，并把其余项明确标为未执行。

每个查询的原始捕获上限8MiB、单HTTP响应4MiB、JSONL单行16MiB是内存/截断保护，不是收费额度；超限是明确的未完成状态。保存按每个已取得页形成一个带出处的字段摘录，避免每个单元格/每行新建一次Git提交。中央Store原有单个记录2MiB边界不改变；已知不可保管的过大摘录在首次该查询写入前拒绝。各页保存是独立原子提交，**整批不是原子事务**。

保存阶段只读取不可变capture文件，绝不重新采集。每个意图由batch-id、查询摘要、页offset确定；相同内容重试读回原引用，不重复发布。新抓取或修改字段后不可沿旧意图覆盖；应保留旧捕获/引用并明确新用途/新批次。后页失败保留前页引用；提交已返回SourceRef但额外读回失败，保留pending_reference；其他可能已发的失败保留原意图并要求对账，不能认定没有写入。输出回执文件失败时，当前已知保存结果转到stderr后停止写入。每页使用新的受信GitHubAPI运行实例，避免把单个操作的请求计数误当成整批付费用量限制；实际服务端限制仍适用。

## 财务语义与权限

缺列、null、空串和数值0分开；保留原值、报告类型、公告/实际公告日、修订标记、返回行及其摘要。重复/变体不静默删除。金额、万元、百分比、比率、每股值分别记录；没有自动乘万元、将百分比变比率或累计转单季。单位标签是所核字段合同，并非已经独立验证Relay使用同一单位；币种无原字段时保持UNKNOWN。预告上限/下限单位为万元，不能直接与元单位快报相加；旧同期利润字段未明确单位时继续unknown-unit。

取得时间不充当历史可得时间；公告日在取得之后会标问题。预告不是实绩，快报不是审定终稿，审计意见字段不是读过审计报告，主营构成不是完整经营分部现金流。来源保存的是派生字段摘录（custody=excerpt），不假冒HTTP原始字节、交易所PDF或附注；wire摘要仅在实际传输提供时存在，离线文件中的声称也不构成来源认证。批量资料接入不运行Full/Quick、主动通知、部署或人工Decision。

维护/退出由#12承担：退出八接口时一并退出专属契约、批次、CLI和测试；共享HTTP若仍供disclosure_date使用则保留，旧SourceRef、来源/研究历史及中央存储不删除。成本放开不改变来源权利、隐私、实际密钥和平台拒绝的边界。

恢复保存须使用原捕获文件、原批次意图及相同转换合同；升级字段合同后不能拿新派生内容覆盖旧 submission。需要新解释时用具名新意图保存新版本，并保留旧引用。批量 CLI 的输入文件是调用者声明，不是来源认证；离线模式不接受伪装为在线取得的 wire 回执。
