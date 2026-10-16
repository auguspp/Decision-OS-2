# 新闻：24小时滚动、紧凑保管与精确分页

归属 #48；用户于2026-10-16明确选择 **24小时滚动**，不沿用旧Kernel的18小时参数。不按自然日清零、不改变采集频率、不启用定时。#46标题资格、RSS原XML生产保管、#13页面/Brief/Attention与#7实质Quick分别验收。

## 时间、来源和版本含义

窗口为 `[as_of - 24小时, as_of]`，两端包含。`as_of`必须明确含时区，计算归一UTC；跨午夜或东八区/UTC表达同一时刻不重置窗口。依据实际观察钟，**不是媒体发布时间**。有回执时用真实接收/失败结束时钟；旧#47观察钟是调用开始，不冒称收到时刻。没有时钟的旧失败明确`unlocated-within-selected-batch`，不补造时间。

NewsNow/Relay的原时间声明保持。RSS保留`provider=rss`及`source_id=nbs-releases/nbs-interpretations`，不伪装成其他提供方；`article_id`沿NBS文档身份，`rss.version_id`保留原版次/摘要哈希，联合`version_id`加feed命名空间。同标题但摘要/时间声明改变仍可成为不同版本，不能推定独立事件。

RSS的`observed_at`是**本批**`received_at`，不是RSS链继承的首次发现，也不是发布日期。只选RSS后继一个批次时，联合窗口计数是1，即使该RSS历史连续计数已经2。只有前批也被明确选入本窗口，才计2。加载RSS前驱用于验证当前批次，不自动把祖先全部加入窗口。500条RSS基线不是500条今天新发、未读或自然新消息。

同版本保留窗内首末观察、首末确切SourceRef和不同阅读批次次数；重复引用或同字节别名不增加曝光。窗口首次不是生命周期首次；移出窗口不删除原版本。缺失RSS条目不等于撤稿。各原时间声明、`publication_verified=false`与缺时区限制保持，不从所在地或抓取时间补猜。

## 一个聚合实现，一个原内容池

`news_rolling.read_news_batch`读取原`VersionedStore.read_source`完整SourceRef，核manifest/表示字节。常规批次复用`news_windows`；原生RSS复用`rss_intake.open_capture`验证所选一步前驱，再用纯`rss_reading`投影为新闻元数据。没有来源HTTP、文章URL请求、新解析器、内容池、当前指针或源游标。

`news_rolling._assemble_news`是唯一窗口聚合/版本去重/排序/健康和损失实现。旧内联v1与新紧凑快照都消费它，分页共用`_page_rolling`。新`news_rolling_index`只拥有紧凑表示及其恢复校验，不复制另一套聚合规则。

新`dos2-news-rolling-index-v1`仍是同一`SourceInput(kind=content,custody=provided)`在既有records中的**派生视图**：保存确切批次引用、窗口/来源状态、前驱/损失、条目数及完整逻辑视图/条目集合摘要，**不在每行重复内联来源引用，也不另存一份新闻正文**。事实真源仍是原批次；索引与哈希不替代原件。原引用保留/备份按既有Store合同执行，短期artifact不是唯一存档。

`verify_index/open_index`核紧凑文件身份和元数据形状；这一步还不是新闻条目恢复证明。`restore_index/read_index`必须重新读取所有选定批次、复用原聚合器，核全部逻辑视图与条目摘要一致，才返回筛选/分页结果。只依赖已存标题或从“最新绿色artifact”猜前驱均不成立。来源丢失、变更或必要RSS前驱不可用时明确拒绝当前完整分页，不用空行表示市场安静，不从别的版本补数。

旧内联v1仍按原合同读取；新构建可消费旧内联或紧凑前驱。滚动前驱拒绝仅限制相依历史，保留`PREDECESSOR_GAP`并继续本次合格新批次。紧凑前驱先恢复其确切选定逻辑视图，不信任一个自报通过的摘要；重复/裁剪/旧失败记录不会因格式转换消失。

## 容量边界：不靠删掉900行来过保存

现役默认构建紧凑视图。单次最多12个新引用、保留256批次、2000个版本；**持久描述仍最多512KiB，原60KiB外层输入与SourceInput/Store容量不扩大**。已有参考量内，新闻行只在读取时临时重建，明确最多8MiB；超过临时预算拒绝，不只留前N行冒称完整。

批次/版本配额仍分别标记`BATCH_LIMIT`、`OBSERVATION_LIMIT`；选定批次不可用仍有`BATCH_UNAVAILABLE_OR_REJECTED`。历史损失日志跟随窗口，计数是操作损失，不是可直接相加的独立文章总数。紧凑描述或来源健康本身超限则整体失败。没有改短为18小时或删源版本来满足预算。

旧内联v1离线阅读保留原512KiB行内表示及明确`BYTE_LIMIT`，不能冒称全量。紧凑默认路径不再为行内字节容量裁掉较早来源。`coverage.retained_versions`是本快照选定且通过配额的版本数，`selected_count`是本筛选的完整匹配数，`rows`只是当前页。即使全部已选版本可分页，也不证明24小时连续采集、全部新闻覆盖或原发布时间已核实。

## 显式手动宿主

继续使用原`news-rolling.yml`，仅`workflow_dispatch`。`operation=build`是默认；`operation=read`是新的只读精确分页。`code_sha`绑定检查过的当前main及独立main CI。使用现有dev锁，只为有权保管的RSS XML支持原feedparser重验；元数据-only和旧内联阅读本身无强制解析器依赖。没有扩大token/读写权限或引入来源密钥。

构建plan（最多60KiB）：

```json
{"as_of":"2026-10-16T14:00:00+08:00","batch_references":[],"predecessor":null}
```

填原批次完整SourceRef，不是URL、source_id或latest选择器。空新列表可按前驱自然过期；无前驱无批次是无覆盖的空观察窗，不是“没有新闻”。构建输出`view.json`为紧凑描述，`reading.json`为核恢复后的首页，`plan.json`为原计划。

默认`save=false`，build job只有contents/actions读权限。保存必须`operation=build`、`save=true`、`public_metadata_custody_confirmed=true`，提供原稳定`submission_id`和当前records的`expected_head`。沿原`research_runtime.run_command(put-source)`负责输入留存、CAS、幂等、未知效果，source_id仍`news-roll-<submission_id>`。新紧凑文本不走另一个writer。写回执未完成/未知时宿主失败，不因有view文件而返回保存成功；按`result/inputs.json`原意图调用`reconcile-source`，不换键、重抓或rerun写作业。

阅读plan：

```json
{"reference":{"manifest":{"repository":"auguspp/Decision-OS-2","commit":"<确切数据commit>","path":"<确切manifest路径>","sha256":"<确切摘要>"},"selection":{"kind":"all","start":null,"end":null,"columns":[]}},"source":"cls","query":"","limit":40}
```

`operation=read`必须`save=false`，提交键/expected_head留空。不接受新as_of或新批次，不把阅读变成窗口更新。返回`view.json`为按完整SourceRef读出的原紧凑字节，`reading.json`为已恢复逻辑视图上的分页；`snapshot_reference/index_sha256/logical rolling_sha256`相互绑定。后续页把上一页`next_cursor`原对象放入plan的`cursor`，保持同reference/source/query；不能跨快照或过滤器复用。

**正常读回验收包括两层：** 可另用原`research-runtime/read-source`核紧凑文件字节；必须再以news-rolling只读分页核源批次重建及逻辑摘要，不能用JSON保存成功代签全部新闻已读回。两种读取都不采集、不写入、不执行研究。

旧内联v1仍可在安装包外离线读取：

```sh
python -m decision_kernel.interfaces.news_rolling_cli read --window legacy-view.json --source cls --limit 40
```

紧凑文件在没有精确来源reader的离线命令中返回`NEWS_INDEX_REQUIRES_EXACT_SOURCE_READER`，不伪装空页。有原Store离线恢复时，应用直接用`read_index(store.read_source, index)`；只拿一个索引JSON无法恢复已丢失的源字节。

## 健康、权利与维护

每页包含整窗`source_outcomes/coverage/losses/predecessor/rejected_references`，`source_outcomes_scope=whole-24h-window`，即使source/query零命中也一样。RSS来源署名、原始capture摘要、原件状态、前驱、基线/新增/不变/移出计数及含义保留。原XML即使获准保管，也不复制到联合页面；`body_archived=false`指未保管链接文章正文。`not-retained-rights-unconfirmed`不能因入池、格式转换或分页消失。

原RSS失败日志目前只在其30天采集产物中保留，联合阅读不能假造未保管的失败批次。本模块只继承实际可读源中已有的失败/缺口，不代签采集器持久失败索引。来源局部拒绝、研究质量、长期备份、网页/Brief、用户采用和Quick分别验收。

旧Kernel固定版`cb3bbc87`提供版本/前驱/缺口思想，新仓只复用其成熟原则，未迁旧schema/current_state。当前聚合/紧凑表示/CLI/手动workflow、原news/RSS测试及`test_news_rolling_index.py`随现役能力共同维护。回退需同时恢复原宿主输出与消费者，并保留已保存紧凑版本的reader，不能删掉历史数据后声称安全退役；共享Store、RSS与新闻来源保护继续。没有定时、通知、Sites、自动研究或人工Decision授权。

## 一次操作内的读取预算

一次紧凑窗口构建会恢复前驱、聚合原批次、再核首页。相同完整SourceRef在这些步骤反复经过远端Store，实际13批次的运行曾在保存前耗尽原512次请求预算（#46运行38879559479）；重置计数器、换API实例或增加上限不是修复。

新闻宿主现在用私有`_SourceReads`在**一次显式build/read操作**内复用已由原`VersionedStore`及既有`_read_verified`核准的完整表示。键包含整个SourceRef（仓库、commit、path、摘要和selection），值为规范JSON字节；每次返回新解码副本，不让调用方污染后续读取。失败/缺源/坏摘要不缓存，不跨运行或跨分页作业保存缓存。原聚合器、原件限制、版本计数和逻辑摘要校验不变，缓存不构成第二内容池或独立读回证据。

缓存的键和值合计最多8MiB；放不下的新结果仍沿同一原reader返回，但不加入缓存，绝不因此裁掉来源或重启请求预算。所有操作仍由同一个`GitHubAPI`计数并受原512次限制约束。完成重建后、输出文件与保存前调用原backend.head重新检查可变的仓库身份、可见性、分支/绑定；原writer继续即时权限检查与CAS，不缓存head或写回执。

成功操作的`read-usage.json`仅保存核准来源读取次数、复用次数、缓存引用数/序列化字节数、实测`storage_api_requests`和绑定重检标记，不含缓存正文、凭据或新闻内容。没有原生请求计数的合成宿主用null，不冒充实测。保存未知仍沿原输入/原submission只读对账；计数或有view文件不能替代published/replayed回执。

`tests/test_news_read_budget.py`用原GitHubAPI的真实512限制、原GitHubStore和真实Git支持的合成HTTP响应覆盖预算耗尽与修复、完整引用、独立副本、缓存限额、权限/绑定变化、冷读损坏和丢回执对账。此宿主优化及专属测试随新闻入口维护/退出，共享Store与独立读取保护不删除。生产同输入保存/独立恢复仍以原生运行分别验收。
