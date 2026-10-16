# Full 全流程来源使用：接口与原件并列，而不是缺口后的补丁

用户于2026-10-16明确：从研究开局、经济分析、财务核对到适用估值、挑战、修订及交付，都应将接口作为正常数据来源之一。#12维护来源执行，#7研究者负责选择、理解和采用；无需先把问题标成open。沿既有原委托和合法只读取证权限自主选接口，不逐项等待用户提示。不自动启动另一研究、定时、通知或交易。

## 正常研究如何用

`prepare-research`初次与接续都返回`source_use`，包括共享目录、命令和宿主入口；旧方法v1/v2原字节及摘要不变。它是给当前研究者的可执行工具指引，不是假装Python或Actions会运行一个后台研究模型。

| 使用阶段 | 研究者应做的事情 |
|---|---|
| baseline | 开局把合适的公司/财务/市场/事件接口纳入来源选择，与已存资料、公告、原件一起建立基线 |
| business / financial | 对经济引擎、主营构成、跨期利润/资产负债/现金按问题选资料、核口径；保留数据支持不了的部分 |
| valuation | 适用时取得或复用同日期股数、价格、外部预期等；不让报价反向替代经营判断 |
| challenge | 用有关接口与其他来源检验替代解释，也可重读已有结果、复算而不发新请求 |
| revision / delivery | 核数据新鲜度、变化与前驱，保留被采用、被否定及未取得来源；在已有授权下走现役deliver/SourceRef |

这不是七步固定流水线，也不强制每阶段联网、抓全部接口或每家公司抓同一套餐。成功空不是历史无记录；新阶段没有新鲜度/范围变化时可复用。研究质量按实际证据、计算和结论验收，不按HTTP/文件/测试数量或阶段标签打勾。

## 共享登记与普通GET批量准入

复用`RelayRegistry`及原始capabilities快照。普通入口只接纳已分类的公共资料组、快照标记启用且仅GET的接口；`needs-review`、`p_*`私人组合及写删/未启用不执行。Registry以`registered_reader_candidate`标示这种通用入口资格，旧`shared_relay_client_admits_api`仍专指12项已适配入口；准入不是全部业务接口已实测。计划必须给业务筛选、明确字段和limit，网关required/required_any在网络前批量检查；不执行probe_example、URL、回调或其他主机。

这是批量**取数层**，不是数百种字段已经完成会计/经济解释。字段保留原值、缺失/null/空串/0及取得时钟，通用层的单位、币种、历史首次可得和业务口径明确未建立；重大计算先用既有专用适配/官方字段或原件核验。现有12个专用接口/正常化器保留，原查询和历史引用不重写。新入口只保留请求列，个人联系方式、私人凭据、全文/摘要等敏感字段不请求；未知返回列省略且记数量，不自动取得PDF。

一次计划可有很多证券、日期、接口和用途；大小/运行时限是内存与宿主保护而非购买用量配额。同一批内相同API和规范参数只取一次，不同阶段共享其结果，失败也不因换阶段重置重试预算。跨批次复用既有capture或SourceRef由研究者判断，不建立第二缓存库。

## 计划与显式运行

计划格式（值仅示例，实际按原委托选择）：

```json
{"schema":"research-source-plan-v1","case_id":"approved-case","requests":[
  {"stage":"baseline","purpose":"建立已授权公司研究的市场基线","api":"daily","parameters":{"ts_code":"600519.SH","trade_date":"20260930","fields":"ts_code,trade_date,close","limit":100}},
  {"stage":"valuation","purpose":"同日期估值对照，复用已取得价格","api":"daily","parameters":{"ts_code":"600519.SH","trade_date":"20260930","fields":"ts_code,trade_date,close","limit":100}}
]}
```

```bash
# 无网络/无Secret；整批检查在首次来源请求前完成
python -m decision_kernel.interfaces.relay_research_cli plan --plan plan.json --output checked.json
# 已有来源授权和运行Secret，仅固定Relay HTTPS；不会写records
python -m decision_kernel.interfaces.relay_research_cli capture --plan plan.json --live --public-rights-confirmed --output capture.jsonl
# 研究者实际读取返回值，而不是只保留ZIP；这些动作不重新采集
python -m decision_kernel.interfaces.relay_research_cli inspect --capture capture.jsonl --output readable.jsonl
python -m decision_kernel.interfaces.relay_research_cli export --capture capture.jsonl --output source-inputs.jsonl
```

默认registry路径是仓库`docs/sources/relay-registry.json`；仓外安装传`--registry`指向获准可信目录，文件和快照摘要会核验。export在本地重新检查身份、分页、投影、时钟和传输声明，只输出完整来源的既有`SourceInput`；重复使用不重复导出。被编辑的JSON不是来源认证；诊断原文不进入来源摘录。部分失败不能把输出文件存在当导出全部完成。

GitHub插件运行已有`relay-source-runtime.yml`：`family=research`，`source_plan`填同一JSON，`codes/periods/dates/apis`留空，`code_sha`用本次核过main。默认仍plan；选择capture并确认本次资料可以保留在公开仓Actions产物后，才注入现有`TUSHARE_PROXY_API_KEY`。每页limit在JSON中指定，`max_pages`为每查询终止保护；`page_size`只用于旧财务/参与者入口。宿主仍只读、无定时，7天产物不是永久保管；研究者取得结果后以现有deliver/put-source记录获准来源与引用，并真正用于原问题。不能以不能公开某项而停止其他获准分析。

## 排队恢复

用户转述卖家的`upstream_pool_exhausted`30秒规则与旧Kernel固定cb3bbc87的等待策略为依据。本批复用现有RelayHTTP安全失败分类，新增业务等待包装，不替换认证/目的地/Secret，不新增网络依赖。也审视了urllib3 Retry官方API：它能按HTTP状态及Retry-After处理，但不能直接解释本Relay的HTTP200业务错误或现有失败证据，故对现有传输做窄组合，而非引入第二HTTP栈。

业务调用默认为最多4次尝试（初次+3次），每次至少等30秒，长有效Retry-After优先；每页累计等待默认180秒，全运行默认900秒。CLI可显式调整`--max-attempts/--max-wait-seconds/--run-seconds`，保留终止保护。到界限仍繁忙时记原参数/当前offset、尝试和pending_retry，不冒称服务器保留了会自动执行的任务。429只在有有效Retry-After时等待，否则停止共享流量；401/403、参数拒绝、data_source_unavailable、未知裸503及格式错误不机械重试。不是永不结束的后台调度。

原财务和参与者CLI也使用该包装，底层单次传输和旧capture可读性不变。当前分页失败只重试本页，不重复已成功页。跨运行恢复仍须核原capture与具体pending参数，不以同一批rerun掩盖旧失败；没有无人值守续作或第二任务数据库。

## 验收与维护

新Full无历史/缺口即可获得来源入口；不同阶段能调用不同相关接口，也能复用同一取数；单次计划跨接口验证后执行；排队至少30秒且只恢复本页；错误、部分数据、原值与来源限制保留；SourceInput经原VersionedStore及新Reader读回供后继研究。工程测试不替代真实HTTP、不把原始资料齐全当充分经济分析，实际Full使用仍需真实委托内取证/核算/挑战验收。

能力退出时同时处理新共享入口/研究prepare附加指引、来源宿主research选项和专属测试；保留原12专用采集、既有Store、历史方法/SourceRef及有效快照，不删研究记录。配置化扩面不创建新的供应商路由、私有组合或数据主库。
