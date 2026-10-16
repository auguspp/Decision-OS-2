# 研究材料入口：从原信息池取材，再与现役接口并列核验

责任为#7研究消费。复用VersionedStore.search/read_source、SourceRef、原prepare/checkpoint/deliver与RelayRegistry/validate_plan；不建立第二信息池、搜索引擎、缓存、HTTP客户端或任务库。代码不替研究者执行经济研究，不凭命中结果启动来源请求。

## 从新Full开局使用，不等缺口或已存研究

研究会话从用户原委托提取公司名称/证券代码/明确别名、相关产业主题及要检验的问题，自主生成`evidence_plan`，随正常`prepare`提交。用户不必再给SourceRef、逐个API名或搬运新闻。词与问题的经济关联由研究者核验；不能把整段委托当精确短语查不到后宣布没有资料，也不能任意猜实体身份。多个词用多个查询表示；实体条件是已登记entity_id，不把证券代码当公司ID。

未带计划的旧prepare仍返回完整方法、原工作和`source_use`，增加`evidence.status=plan-required`及可执行入口；这次没有偷偷扫描整个库。正常Full研究者应按上段生成计划再调用，而不是只看入口说明就报告已消费信息池。prepare带计划时会**实际查询并读取**池里尚未与本案例绑定的来源。

```json
{"request":{"case_id":"synthetic-evidence","kind":"full","mandate":"合成工程检查，不是投资任务","questions":[{"question_id":"cash","text":"现金与经营解释是否一致？"}]},"evidence_plan":{"schema":"research-evidence-plan-v1","case_id":"synthetic-evidence","as_of":"2026-10-16","queries":[{"query_id":"company","question_id":"cash","purpose":"寻找原委托公司的既有材料","query":"合成企业"},{"query_id":"input","question_id":"cash","purpose":"核投入品变化的候选解释","query":"铜"}]},"evidence_limit":4}
```

沿现役`research-runtime.yml`的`operation=prepare`、当前已核`code_sha`与上述payload运行；不新增operation、Secret、权限或workflow。宿主只读job仍只有contents:read，无来源Secret。查看匹配run的原生report，不把dispatch接受当成功。CLI同一路径：

```bash
python -m decision_kernel store --path synthetic.git prepare-research \
  --request request.json --evidence-plan plan.json --evidence-limit 4
```

`request.json`是原ResearchRequest，`plan.json`是上述evidence_plan。接续已存研究可改用`--case-id`或`--ref`；已知的原问题/前驱沿既有prepare恢复。

## 结果和精确消费

`result.evidence.sources`返回本页新出现的唯一来源版本、完整SourceRef、实际正文/表格或明确的未包含状态、原始日期/期间、保管等级与限制。`matches`给出查询/原问题/用途和对应version_key；同一来源在多个问题出现不重复复制正文，也不计作多份独立证据。命中范围是**已存来源表示**，例如一整个新闻批次，不证明其中每篇文章已独立阅读或具有相同相关性。

去重只认同一manifest的版本路径和摘要，不靠同标题合并、不删除同source_id的不同版本、不把同源派生CSV当独立上游。全表示的命中与前驱中同版本某切片用`linked_version_in_predecessor`标记，不表示前驱已经读完整份材料。

研究者实际阅读、检验后，把有用的SourceRef加入原`ResearchProgress.sources`，以原findings/challenges的evidence索引说明采用、反证或限制，沿原checkpoint/deliver保存。这里不自动选择证据，不保存一份第二研究档案，不把无用命中绑定进正文凑来源数。新reader按保存的SourceRef读回，底层继续验证字节/范围和前驱；检索成功不是研究充分或人工接受。

## 分页、时间和缺失

每页最多8个匹配出现，默认4；一次计划最多8条显式查询。来源版本数量仍受现役search的1024界限约束，不扩大底层网络预算。按计划查询顺序、原生路径顺序返回，不声称语义排序或只取最新版本。

`next_cursor`固定仓库快照、原委托及计划摘要、查询位置/原生游标和已返回版本。`next_prepare_payload`是可直接用于下一次prepare的输入，使用完整原request及精确前驱，不重新选择浮动case current。CLI保存cursor后带同一request/plan和`--evidence-cursor`。新来源到达或records前进不污染旧页；开始新的观察需明确重新查询。游标是定位，不是已读或完整性证明；篡改计划/委托/快照不悄悄换页。较大游标继续使用原无损payload传输，不修改其限额。

正文视图沿既有1MiB阅读预算，超限保留精确`read_operation/read_payload`和错误，不截正文冒充全量。外部目录仍external-only，摘录仍excerpt，日期缺失保留未知。`as_of`只标示相对原声明日的期后信息：取得日不替披露日，不补时区、不把后取资料说成历史当时已知，也不以通用天数自动判财报过期。按公司/主题查得的新闻批次要经其现役新闻reader继续细读；此入口不修改采集器时钟或各业务格式。

零命中、未开始查询、源读取失败、正文未包含分别呈现。现役search遇不可读/损坏的来源总体会失败关闭，本入口保留search-unavailable、total=null和原游标，不谎报零或全库健康；已有研究上下文与接口计划仍保留。需要修来源或扩大原生查询预算时由原owner处理，不换库、删坏源或循环重试求绿。

## 接口与池同时可用

计划可附`relay_plan`，形状保持`research-source-plan-v1`，case_id须一致。入口通过当前可信Registry调用原validate_plan，返回`interface_sources.status=checked-not-executed`、原计划和检查结果，可交现役`relay-source-runtime.yml`的`family=research`。字段/筛选/禁用及私人路由规则完全继承，不执行probe_example，不在此注入Key。

没有新鲜度、覆盖和原文核验依据时，**池里有命中不能自动取消接口请求；池里无命中也不自动授权取数**。研究者从开局到挑战/修订均可采用原接口入口，并复用已有成功capture。实际capture的来源用途/公开产物权利和存储授权仍需成立，计划检查不取得再分发许可。通用字段不直接认证会计口径，不替代附注或研报原PDF。

## 维护和验收

专属应用模块/prepare接线、CLI参数及本文由#7维护。退出能力时同时退出这些接线和对应测试/冷安装调用；保留原Store/search/SourceRef、历史研究方法、来源采集器和通用Relay。没有新持久状态、定时、通知、交易或部署。真实Git合成资料验证查询→研究引用→独立恢复，包外沿同一次cold-wheel使用；线上只读池消费与完整研究的经济质量分开验收。
