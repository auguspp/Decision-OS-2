# 新闻工作台、显式 Attention 与实际 Brief 基线

责任：#13 新闻消费子范围；来源与滚动仍由 #46/#48 维护。用户已明确开始最后一段新闻施工，并继续暂停 `major_news` / `cctv_news`。本能力不重新采集，不替代现有研究目录、实质 Quick/Full、人工 Decision 或生产运营。

## 1. 一条可使用的交付路径

普通阅读先用 [固定 News Desk 入口](https://github.com/auguspp/Decision-OS-2/actions/workflows/news-rolling.yml) 的 `operation=latest-desk`：不必填写 Ref、代码 SHA 或业务 plan，保持 `save=false` 和两个写入字段为空。它只发现并恢复既存对象，原 native main/CI、公开资格和释放前复核仍执行。详细范围见[只读发现合同](#latest-entry)。

原高级 `operation=desk` 继续读取一个**精确已保管的24小时索引**，从保留的来源版本完整重建，产生：

- `news-desk.html`：自包含离线阅读页，含全部可读行；来源、标题、最后观察时间和选择状态筛选、每页40条、原链接和完整来源健康。
- `news-brief.md`：本期实际生成的晨间、晚间或即时新闻简报正文。
- `edition.json`：有界的简报/采用、必要省略、显式选择、原问题、精确窗口与基线合同。
- `quick-candidates.json`：显式关注且给出原问题的候选交接，**未执行研究**。

只有 `save=true` 且原 writer 返回确认回执，简报才沿同一 `ResearchArtifacts` 存入 `records`。HTML只是原版本投影，不是第二内容库，也不是已部署的联网网站。页面离线打开不需要凭据、外部字体、CDN、数据库或服务器；不能自动刷新来源、提交选择或发送通知。

普通 `build/read` 的语义、旧来源和旧窗口字节不变。新 `read-desk` 按确切 ArtifactRef 重新读取简报、上一实际基线和有关窗口，复算后生成同样正文与完整阅读页，不重新请求任何新闻源。

## 2. 简报规则，不冒充经济研究

`kind` 为 `morning` / `evening` / `adhoc`，只改变交付类型，不新增定时。`cutoff` 是显式实际交付截止，原观察窗口的 `as_of` 必须不晚于它。原媒体发布时间保留为来源声明；抓取/观察时间不是发布时间，版本不是独立事件。

默认最多12条（可显式1—24）：先采用显式继续与到期待复核的版本，再按原最近观察次序在不同来源间轮选。无投资信心评分、标题模型或凭空公司关联。源未核与未研究材料明确为**新观察，待解释**。没有真实研究运行回执，不能称 Quick 已启动，也不能把保管或界面浏览当用户接受。

`baseline` 必须是**实际保存的上一份正文 ArtifactRef**，不是定时时间、last_run_time、文件名猜测或自动“最新成功”。有引用却不可读/摘要不符则拒绝依赖本次对比；未提供则明确缺失，计数为 null 而不是0。

当前版与上一实际正文分开记录：已采用同版（默认省略）、之前在窗口但未采用、迟到进入交付窗口、新进入窗口、退出窗口。迟到以首次观察时间相对上一实际截止判断，不证明媒体迟发或当时全网可得。之前未采用不是永久否决；显式继续与复核到期可重新带回，并说明原因。晨报可以用晚报的实际 Ref，类型不强制先后、不会制造一份从未存在的晚报。

`omissions` 完整归因于同版重复、明确暂缓、明确移出、简报容量。未入简报的条目仍可在工作台阅读。来源结果、失败/未知、裁剪、前驱和覆盖限制随整窗保留，筛选零条不显示成“市场没有新闻”。此新闻简报尚不自动采用一般研究成果，不宣称整个 #13 的研究议程与运营完成。

## 3. 显式选择与隐私

每个选择绑定 `[provider, source_id, article_id, version_id]` 的稳定摘要，不因重复观察而换键，不屏蔽同一公司的新证据或新闻新版本。

`attention` 每次最多128项，无重复键。每项完整形状为：

```json
{"entry_id":"<确切64位版本摘要>","action":"focus","reason":"具体选择理由","question":"待核问题，可留空","review_at":null}
```

动作：`focus` 继续、`defer` 暂缓、`dismiss` 移出、`reset` 撤销选择。理由必填；问题只属于 focus；可选复核时刻只属于 defer，需带时区且新设置在本次截止之后。到期只表示待复核，不暗改为人工继续。旧状态由实际基线承接，变更有版本记录；不读取额外永续状态表。

页面选择仅存在本页内存；导出前提示未存档。导出的计划绑定本页已保存简报作为下一基线，未保存时合并原输入与本地变更。**导出不等于提交或公开。** 未提交选择关闭页面会丢失，可先保留导出文件。本页不使用 localStorage，不把点击、停留或打开原链接记成已读/接受。

GitHub 原生工作流产物可能公开。因此原 `desk` 的非空新选择无论 `save` 是否为 true，都须**单独**设置 `public_feedback_custody_confirmed=true`，否则在生成输出和访问 Store 前拒绝。它不是通用元数据权利的继承。新增 `latest-desk` 复用固定且可撤销的部署公开授权，也保留原显式资格作为无部署授权时的入口；已保存选择和研究出口分别检查，含基线承接的选择，不把旧 private custody 当公开权利。私人笔记不可填入公共工作流；本地浏览、导出后可由用户在许可范围内另行交回。该字段是调用者声明，不是身份认证，文档始终 `human_acceptance=not-established-by-artifact`，不伪造人类投资审批。

## 4. 原生调用

继续使用原手动工作流 `.github/workflows/news-rolling.yml`，核当时 `main` 的代码 SHA 和适用成功 CI。

`operation=desk` 的 `plan`：

```json
{"window":{},"cutoff":"2026-10-16T12:00:00Z","kind":"evening","baseline":null,"attention":[],"max_items":12}
```

`window` 替换成完整 SourceRef（仓库、commit、manifest路径/摘要、selection=all），不得用短SHA或只有窗口标题。`baseline` 为空或完整 ArtifactRef。历史窗口可以生成明确标注观察时点的简报，但不能称本次采集或最新全市场新闻。

保存：原 `public_metadata_custody_confirmed=true`、`save=true`、稳定 `submission_id`、新核验的 `expected_head`；新反馈另核其许可。只读 desk 的两个写入字段留空。`operation=read-desk` 强制 `save=false`，`plan={"reference":<完整简报ArtifactRef>}`，两个写入字段留空，不接受旁带选择或新窗口。

保存复用原 `run_command(operation=put-artifact)`、`ResearchArtifacts`、权限/CAS/日志与备份。完整原命令在 `result/inputs.json`；未知写必须按它的**原 payload、原 submission**执行已有 `reconcile-artifact` 只读对账，不重建新 cutoff、改键或将提交重试解释成新简报。明确未写的冲突可在对账后沿原意图和新 expected_head 恢复；本入口不是重试队列。

只读和写入仍沿原分离作业与主线CI预检，未增加 schedule、Actions写权限或来源凭据。

## 5. 安全、容量、维护与验证

复用原来源读取和 Artifact 保管、原字节摘要/条件写，不引入通用 CMS、React/Streamlit 服务或新的状态平台：这里仅是小型固定离线投影。浏览器使用标准 DOM `textContent/createElement` 与事件监听，不把新闻/问题插入 `innerHTML`；源链接仅允许无凭据的 HTTP(S)，新窗加 `noopener noreferrer`；JSON封装转义HTML边界，CSP以固定脚本/样式摘要准入，禁止联网与表单提交。通用组件检查依据是 MDN 的 CSP/script-src、DOM textContent 及 Python 标准 `html.escape`，无需第三方模板执行器。

现役源码：`application/news_delivery.py`、`interfaces/news_page.py`、`interfaces/news_delivery_cli.py`；原新闻host仅窄路由，原 Store/来源验证不变。新文档256KiB、HTML8MiB、选择128、采用24；原60KiB计划/传输、512次存储API及同操作8MiB精确来源缓存保持。超限或不可恢复时明确失败，不丢条目凑绿；一份父简报只核其自身正文/来源及必要状态，不递归重放全部历史简报。打开当前版本则复算它与这份真实父正文的关系，不把这个边界称完整链审计。

核心测试覆盖真正 Git、原 GitHubAPI/Store/CAS、两次独立读取、未知ACK的原意图对账、缺基线/未来基线拒绝、重复/迟到/到期选择和公开反馈门禁。单一现役 cold-wheel consumer 增补安装后完整新闻消费，不重复建轮子。独立 Chromium 测试覆盖分页、零筛选健康、选择导出再保管/恢复、移动屏及脚本逃逸；测试合成选择不冒充用户采用。受管浏览器若阻止 file URL，记录其限制，仅渲染相同HTML字节做交互验收，不声称验证了宿主文件访问权限。

退休时同步移除专属投影/路由/测试/文档，已有 Artifact/SourceRef、来源窗口、失败历史与共享Store保护保留。公开原文权利、实质研究、私有反馈、自动调度、外发通知、独立持久备份及生产切换需各自实际验收；不能靠此页或CI代签。

<a id="latest-entry"></a>
## 6. 最新保管入口、原时间与完整历史

本责任组复用 `brief_discovery` 的原正常发行版枚举、`news_rolling_index.open_index/restore_index`、`brief_delivery.open_edition/build_edition` 和 `ResearchArtifacts`，不建第二目录真源。`latest-desk` 是原新闻工作流的只读分支，不是新的 research-runtime operation。旧 `build/read/desk/read-desk` 的合法输入、默认 `build`、原写入键、CAS 和未知写对账都保留。

### 双最新对象

| 对象 | 发现范围与顺序 | 合格读取 |
|---|---|---|
| 最新正常宿主保管窗口 | 同一固定 records snapshot 的 `source-views/news-roll-*/<version>/manifest.json`；最多64个候选；按原表示的 as_of 取最大 | 路径仅提名；核完整 SourceRef、manifest/body，再按实际 schema 调用原 `verify_index` 或 `verify_rolling`；选中的 compact index 仍通过原 `restore_index` 重建全部指定批次 |
| 最后已保存发行版 | 原 `research/artifacts/news-edition-*`；继承128个候选边界；按原声明 cutoff 取最大 | 沿原 Brief reader 验文档、窗口、必要父版和研究／产业扩展；cutoff 不代表来源新鲜度 |

同一时点的平局明确计数，按路径升序确定展示，不解释为保存先后或经济优先级。任意自定义名称仍能按原确切引用打开，不暗称已进入这个目录。

正常 `news-roll-*` 历史包含紧凑 `dos2-news-rolling-index-v1` 和旧内联 `dos2-news-rolling-v1` 两种真实表示。合法旧内联窗口沿原 `verify_rolling` 核字段、摘要、时钟、观察与缺口，参与完整候选分母、历史和原 as_of 排序；不改称紧凑 index，也不因为年代较旧而忽略。未知 schema、坏摘要或无效内容仍进入 failures 并阻断完整发现，不能通过改格式名称或删除历史修复。

现役 News Desk 的窗口恢复仍只支持紧凑 index 的确切批次回放。若最大 as_of（或原路径平局规则）选中旧内联窗口，保留其真实 Ref/as_of/schema，并明确 `selected-not-readable / NEWS_LEGACY_WINDOW_DESK_REPLAY_UNAVAILABLE`；目录枚举可以完整，但不导出正文、edition 或假 preview，也不回退较旧 index/已存发行版。原 `news_rolling_cli read --window` 可验证并分页读取旧内联文件，未独立回放全部依赖，不能替代 Desk 资格。历史旧行因此只保留精确引用、原时点和离线读取说明，不导出当前 native `desk/read` 会拒绝的请求。较新 compact index 被选中时，原完整回放、公开资格、fatal latch 和预览规则全部保留。

当最新保管窗口已有对应合格发行版，恢复真实正文与 Attention；原 Ref 保留，不因本次目录 snapshot 改写原文。如果较新窗口尚无对应已存 Desk，沿原构建器生成 `retained-window-preview-not-saved`，`cutoff` 固定为该窗口原 `as_of`、无新选择、无伪造 baseline、`saved_reference=null`。旧发行版仍独立具名并可导出原精确读取请求；不会自动承接其选择。

候选损坏、超界、无法恢复所选依赖或公开资格不足，保留具体范围和失败，不能用较旧可读版冒充最新；历史候选仍可按确切引用显式恢复。没有保管候选是 `no-retained-desk`，不是正常零条新闻。全部来源失败和0条成功捕获分别保留。

### 五种时钟

| 页面时钟 | 原证据 | 缺失时的含义 |
|---|---|---|
| 最后成功捕获回执 | 重建来源 outcome 的 `captured` 与真实 capture.finished_at/received_at | legacy observed_at 另列；不得填成成功捕获时钟 |
| 原观察窗口起止 | 原 index.window_start/as_of | 此24小时只覆盖所选观察批次，不是全网发布时间窗口 |
| 交付声明 cutoff | 原保存版 plan 或原窗口预览的固定 as_of | 页面生成不能替换它；保存时间不当新闻日期 |
| 本次 read_as_of / rendered_at | 明确评估时钟与本次实际渲染时钟 | 只计算年龄，不刷新旧资料或回填原 GET 时刻 |
| 逐条媒体时间声明 | 原 published_claim 原值与未核／缺失状态 | 不推断时区、精度或真实发布时间；不存在的字段保持未建立 |

年龄显示超过24小时、未超过24小时、未知或读取时钟早于原时点。未超过24小时也不证明持续采集，`complete_news_coverage` 和 `continuous_capture_verified` 仍为 false。原整窗失败、未定位时钟和裁剪不随搜索、筛选、新闻分页或历史分页消失。

### 固定历史与原生状态

历史合并两个正常路径范围，**枚举分母先于分页**。每页默认8项、上限20，包含无效候选，保留 `total_candidates/scanned_candidates/listed_candidates/invalid_candidates`。cursor 绑定仓库、snapshot、完整行摘要和 offset；下一页保持该快照。刷新请求显式去掉旧 snapshot 和评估时点，执行后才取得新快照。列表只验候选元数据，逐历史项并未完整重放全部依赖，不把列表本身称为合格原文。

只读宿主用同一 backend/API 有限检查固定 `news-intake.yml/rss-intake.yml/news-custody.yml/news-rolling.yml` 的 main 前20个 run，不读取其输入或产物、不请求新闻来源。展示每个有限列表最近观察的 attempt、状态、原时钟与完整列表分母；不越过较新失败去挑最后成功。Actions success 不证明捕获或保管，更不证明连续生产；rolling 的新运行可能只恢复旧资料。未取得的原生信息不当成“没有新运行”。

原 API 512 次总请求、8MiB 同操作 SourceRef 缓存保持；没有新 API 实例给发现、恢复或状态查询重开额度。只有新 `latest-desk` 开启原 `_SourceReads` 的 fatal latch：当前原生403/429、请求耗尽、绑定／精确引用越界保留原异常；即使原 rolling 把错误暂记成失败批次，缓存、后续批次、末次 head 和输出也不能继续。来源 outcome 内的历史失败是资料内容，不等于本次存储权限拒绝。旧模式默认缓存与错误语义不变。

### 输出、权限与实际验收

`latest-desk` 强制 `save=false`、空 submission_id/expected_head；原工作流写作业排除它。仅此纯读模式可留空 code_sha，从可信 `GITHUB_SHA` 固定本次代码，仍核 main 与适用成功 CI；高级旧模式仍要求原精确代码和 plan。可选历史 plan 仅含 `as_of/snapshot/history`，不接受新窗口、选择或源正文。

`navigation.json`、原 `news-desk.html` 和 `news-summary.md` 来自同一模型；有可读正文时另附原 `edition.json/news-brief.md/quick-candidates.json`。Summary 只投影前12条并标明范围，完整页面保留全部可读观察。历史与刷新按钮只导出原宿主请求，不执行读取；高级 `read-desk/desk` 仍需要原参数与公开资格。Actions 附件30天保留期不当持久资料保管，records 精确引用仍是恢复真源。

公开资格只复用 `runtime_publication.publication_basis`：固定当前部署授权的 sources/brief/reading 用于新闻入口，research/watch 等仍各管研究和反馈出口；撤销后回到原显式资格。来源标题允许公开不代签私人反馈或受限研究。读取结束后同 backend 复核原绑定、main 和公开授权，才释放正文、HTML及Summary；失败只留有限原因报告，不保留来源正文或半成页面。

本组工程验证包括原临时 Git、原 GitHubAPI/Store/native news host、一次真实 HTTP 错误形状、权限撤销、512耗尽、固定历史和原文恢复；安装消费者仅扩现役唯一 wheel helper。此前一般新闻页的浏览器证据不自动覆盖本次新增导航。当前需要另核：获准真实原件上的正常 `latest-desk` 消费、真正新期间的捕获→保管→读回、最终桌面／移动页面操作、自然周期的连续运行。未新增在线托管、调度、通知、来源执行或 Research/Odds Book；代码和CI不代签这些产品结果。
