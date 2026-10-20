# 统一阅读室：目录、研究原文、议程与指定简报

责任：#13。继承 #33 目录、#89 议程、#90/#93 的混合 Brief 与独立重开；本入口只组合真实读取，不新建研究索引、简报库、当前指针、保存入口或调度平台。源码位于 `application/reading_room.py`、`interfaces/reading_commands.py` 与 `interfaces/reading_page.py`。

## 一次读取与页面

`reading-room` 从现有 `ResearchCatalogue.page` 取得一页固定 records 快照，打开其中确切研究引用的完整原文。可另外指定一份历史 `reference`，与目录当前版本分别显示，不反查当前后偷换引用。每页1—4案例；额外历史版本不计入目录分母。案例排序沿原目录，不是推荐次序。搜索与类型筛选仅作用于本页，下一页导出原 catalogue cursor；刷新另起快照，不将未读页说成已经覆盖。

`ResearchAgenda.build` 在原委托问题上生成只读投影，`changes=[]`，可按确切已存议程作基线。没有提供议程时显示原稿后续工作，不制造人工选择；提供却不可读时明确未知，不退回“未选择”。旧版本的选择不作用于新版本，未选版本状态继续保留。`as_of` 仅为议程复核评估钟，不是研究截止、事件发生或PIT快照日期。

`brief` 可以是明确指定的已保存 edition ArtifactRef，也可以显式请求 `{"select":"latest-saved"}`。前者始终读取原版；后者在本次目录快照上发现正常发行入口的已存简报，然后立即固定完整引用。两者均复用原 `brief_delivery.open_edition` 重建原文与来源，保留实际正文及前版比较。只有完整精确研究引用匹配原研究 rows/included 才展示采用关系；同公司或同案例不同版本不相等。未采用、未列入指定范围、旧新闻版未接研究和无法判断分别显示。本入口不创造新的采用/省略决定，也不把浏览标成接受。

HTML 自包含、离线只读，支持目录搜索/类型筛选、研究原文、逐题复核、已存 Brief、确切引用及原文下载。页面中的导出按钮只生成原操作请求或后继阅读计划，不联网、不调宿主、不写库、不新开研究。点击简报中的研究，已在本页的直接显示；否则导出带确切 `reference` 的同一阅读入口请求。已有新闻工作台仍由原 `read-desk` 打开，不另复制新闻页面。

原附件、来源表示与前驱导航使用同一入口的可选 `materials` 计划，见[材料阅读](reading-materials.md)。仅显式请求时读取；公开输出时材料权利独立确认，不由研究或反馈许可代签。

按原问题核对相邻研究修订及实际简报行段覆盖，使用可选 `review` 计划，见[问题复核](research-review.md)。公开输出沿原材料确认；版本被采用不代表所选修订段落已覆盖，读取结果不代签经济判断或人工接受。

固定产业包使用可选 `industry` 计划，见[产业阅读面板](reading-industry.md)。沿原三种产业读取器，公开输出另核来源确认；本地筛选保留整包分母，不推断当前净受益或研究/Brief采用。

已保管 A 股原价/复权使用可选 `market` 计划，见[市场双视图](reading-market.md)。既有证券 reading 根和持续 collection 根都沿各自原读取器恢复；计数回执不是行情输入。分页固定原根，公开输出单独确认来源权利，工程成功不代签真实行情保管或采用。当前 partial 集合、窗口缺口及旧完整版本的 freshness 继续显示，不将可读状态改成行情全覆盖。

已保管美股指数与 Shibor 使用可选 `global_context` 的 `us_indices` / `shibor` 两槽，见[全球背景阅读](reading-global.md)。两族原完整模型、源日期、单位、分母和版本分别保留，页面分页不隐藏未选中项缺口。沿原来源公开确认及可信 Public 部署授权；不改变 A 股 guard、不采集新数据，也不代签六族覆盖或自然更新。

原池竞价观察使用可选 `auction` 计划，见[竞价原池与同日接续](opening-auction.md)。完整 U/D/Z 原行和独立 U 五组先恢复再分页；同日原值可读与原请求/接收均达到 T 15:30 的完成收盘分别显示。原分母、条件和 cohort 版本不随 close、筛选或翻页改写，缺旧时钟/截断证据保持未知；沿同一来源权限、原整次预算和终态输出门，不新增采集或交易职责。

原竞价名单的给定分钟原件使用可选 `auction_minute`，见[09:40 分钟观察](opening-auction-minute.md)。确切 seed、完整五组/BJ/未知和未尝试成员先恢复再分页；只接唯一 09:39–09:40 点，保留原取得时钟，跨来源收益不计算。沿原来源确认、整次终态与 2 MiB 展示上限；不增加分钟采集或实际来源额度。

## Research Book

原研究区以 `Research Book` 导航进入，仍使用正常 `reading-room` 操作和原 `plan`，不新增宿主参数、运行操作或保存对象。`application/research_book.py` 直接消费 `ReadingRoom._research` 已读到的研究字典，调用原 `domain.research_work.read_workpaper` 校验工作底稿；不再次读取修订或来源，也不遍历完整历史。`interfaces/reading_research_book_page.py` 仅投影原模型。

本页展示原委托、问题、Quick/Full、确切版本、作者 findings、挑战、重要性声明、未完成项的 `remaining_kind` / `next_action`、`stop` / `stop_reason`、限制及 `source_changes`。问题、findings 和挑战分别沿原作者顺序，不按强弱或优先级重排。停止声明和作者结论不是独立 Review、人工接受或正式 Decision；未记录挑战不表示不存在反证。标准公司标识、研究事实截止和“最强反证”排序在原 schema 中未建立，模型返回明确空值，页面标为未建立，不从 case_id、保存时间或阅读时点推断。

旧正文没有规范工作底稿时保留完整原文、原未决项和引用，并明确结构化字段未建立；有工作底稿标记但校验失败时只将 Book 结构化部分标为 invalid，原研究正文仍按原读取结果展示。`stored_at` 仅表示该版保存时钟。原 `as_of` 仍是复核评估时点，Book 不据此声称研究在该时点已经存在、获得事前保管证明或形成事实截止。

Book 继承 Catalogue 的真实 `total_cases`、`snapshot`、`offset`、返回数、剩余数与下一页 cursor。失败卡片保留；额外确切版本单列，不计入当前目录案例分母。本页搜索和类型筛选不覆盖未读页。新导出的材料、前驱和 Review 计划固定本次原 Catalogue 快照与位置，不因重新打开而混入后来追加的案例；指定 Brief 的引用也保持固定。

来源按钮导出原裸 SourceRef 的 `read-source` 请求，材料按钮导出原 `materials` 计划；引用可读状态仅使用本页已读的来源状态，不重试来源正文或升级资格。具名 Brief 按原 `read-desk` 请求重开，采用关系只适用于该确切 Brief，不声称完整采用历史。原研究读取、原正文下载和原议程入口继续保留。所有按钮只导出请求或在已读页面内导航，明确尚未执行；实际调用仍由原宿主核代码、来源及公开权利。

逐题 Review 仅在本页已经打开同案例的直接前驱和当前版、两版工作底稿均有效时提供。用户可选前驱原问题及两版原文行段；预填各版首个合规非空行，只是可操作的字面起点，不判断段落重要性或语义对应。导出采用原 `review` 合同和 80 行边界，原宿主再核确切前驱、原文、8192 字节行段及材料权限。缺任一原件时保留前驱请求，不自动追读历史或伪造比较结果。

Book 不重复放入研究完整正文，不占原 1MiB 单正文与 2MiB 正文总预算；其投影另受原 768KiB 面板边界限制，按完整卡片保留或标为 `not-included`。原模型先通过 6MiB 校验，再尝试增加 Book，并调用原页面的纯渲染器核实际转义后完整 HTML 的 8MiB 容量；不另估算转义膨胀或提高边界。Book 导致 JSON 或 HTML 空间不足时只保留相应省略标记。若连标记也无法容纳，原模型保持原样，页面明确 Book 未包含，原文和确切读取通路仍保留。最终报告 JSON 与 HTML 内嵌数据采用同一模型，渲染器不修改模型或截断正文；已有原页本身超过 HTML 边界时仍按原规则拒绝。Book 的局部缺口单独显示，不改写原研究/来源读取状态。容量准入只有纯渲染，没有额外读取，随后仍执行原最终权限复核。

有限合成验证覆盖正常/旧正文/坏工作底稿/缺源、前后版精确关系、固定分页分母、评估钟不冒充 PIT、局部容量省略及无额外读取；原主 CLI 和原生 public/private 宿主消费者覆盖完整 HTML/JSON、CSP 与 Node 实际按钮请求重入。转义密集的两版原正文可正常呈现而 Book 会超过 8MiB 的反例，沿原生宿主验证只省略 Book、原正文/引用/目录分母不变，最终 JSON 与 HTML 完整一致。撤权、private 反转及最终原生 403 继续阻断结果和私有输入释放。唯一既有 wheel consumer 读取原已保存的合成混合研究验证 Book，不增加第二次安装或 cold gate。上述工程验证不证明实际公开资料使用、浏览器/移动端体验或用户接受。

稳定入口当前仍是现役 Actions / store CLI 生成的同一离线阅读室；完全免填宿主参数、长期在线 Book、真实研究消费及浏览器体验仍须各自实际验收。Odds Book 属于原 #60 领域，本组不修改 Research/Odds schema、当前指针、来源保管或写入权限。退役 Book 时移除纯投影与页面调用，原 Reader、Catalogue、正文和准确引用继续可用。

## Odds Book

正常 `reading-room` 的可选 `plan.odds_book` 直接使用[原 Odds Book](../research/odds-book.md)查询合同：仅 `snapshot`、`cursor`、`limit`、`scan_limit`、`research_ref`；默认返回限额 4、扫描限额 16，上限分别 8、32。缺省或 null 不读取，`{}` 明确请求一页。页面导航可导出同快照首页请求，不要求手工抄写内部引用；导出仍未执行，不形成自动循环或后台订阅。

```json
{"plan":{"as_of":"2026-10-20T08:00:00Z","catalogue":{"limit":4},"odds_book":{}}}
```

`application/reading_odds_book.py` 只调用一次原 `odds_book.read_book`，不重做候选发现、规格计算、研究归属或全历史回放。默认使用本页真实 Catalogue snapshot；明确传入的 Odds snapshot/cursor 与它不同时拒绝，不能悄悄改写游标。研究目录与 Odds 页各保留原分母：Catalogue 的案例数/offset/returned 不因额外旧研究或 Odds 行增加；Odds 显示原 scanned start/end/total_candidates、原 status 和 `this-page-only`。坏项、未知过滤匹配和原 oversized 卡片不因筛选消失。尾页或 next cursor 为空不证明此前页面已读，也不证明全库无结果。

Odds 下一页、原研究过滤与 Research Book 精确版本入口都保留实际 Catalogue cursor；Odds 翻页只推进原扫描 offset。换精确 `research_ref` 过滤时重新从该快照的候选首位读取，不按名称猜公司或自动换 current。明确刷新同时释放 Catalogue 与 Odds 的游标/快照，保留原限额与精确过滤选择；新页面生成日期不刷新原资料或已存结果日期。

页面直接保留领域返回的研究关系及同 spec 多份已存价格结果，展示原 kind/revision、mandate、未决项、at-snapshot current/historical/unknown，以及估值、资本结构、目标、价格、计算和检查各自时钟。计算时钟直接取原 `calculation_input.evaluated_at`，缺字段仍标未记录；stored_at 仅是保存时钟，不填研究截止日期。原数值保持字符串，不经页面浮点重算；当前公允价值的估值距离不改称未来收益，未来退出回报不新增年化/折现。条件 Odds 的未建立概率不补等权，保留原 `weighted_aggregate: null`；概率输出按原 `aggregate` 字段显示，仍是原研究声明，不称经验概率已核验。

保管 verified、semantic replay not-requested、current validity not-certified、人工接受与执行授权分别显示。原行的 `open_operation/open_payload` 可重入现役宿主；原活动指针的 current 查询明确标为“执行时重新选择”，不能当成本快照历史回放。页面不写研究、Odds、Watch 或正式 Decision，不增加来源请求。

含 Odds 的 native Room 复用 `checked_odds_book_visibility`：原生 private 或 public 且具 `reading/research/sources/watch/market` 五个原可信 scope。调用者公开布尔值不能代替这五项资格。准入在 inputs/records 正文读取前；同一 backend 在读后和最终释放前再核。该组合的公共与私有 inputs、完整 HTML 和成功 result 均延后到完整模型/HTML 大小门及最后原生复核之后。缺权限、撤销、可见性反转、存储/请求预算终态不能被局部缺项或旧绿结果洗掉。

此明确组合仅在原 `_ReadingScope` 上启用默认关闭的 `fail_closed`，保留原类、owner、API 和行业逐 commit 的 `source_read_scope` / 32 个上限。首个原访问、范围或传输终态锁存原异常；后续缓存、读取和作用域退出先抛同一对象，不再请求。委托原 `source_read_scope` 取得的短寿命 `_SourceReadScope` 实例也在同一 opt-in 下检查其公开读取/校验入口，接住尚未进入 owner 的错误仓库引用及子范围拒绝；原实例类型、owner、snapshot、关闭状态和原退出检查保留。原 `operation_failure` 分类在实际失败时窄复用，不另立研究策略；附加的 `PATH_OUTSIDE_STORE` 是原 GitHubStore 读路径白名单拒绝，普通 `MissingObject` 和内容缺项仍可局部显示。原 Odds reader 与 collection 的 `GitMarketReader` 通过同一终态检查点进入；后者仍使用原 source-only 快照和同一 API。未选择 Odds 的旧 Room 与其他默认 scope 不启用该终态保护，也不更换 backend 类型或预算 owner。

行业 `OPERATING_SOURCE_SCOPE_BOUND` 仍是原领域容量边界：第 33 个独立来源 commit 不读取，对应观察保留 unavailable，已在原 32 个 scope 内的观察继续按原合同读取。它不属于原操作级终态分类，不因新增 Odds 被升级为整页失败。

领域原 8192 候选、128KiB 卡片、4MiB Book 结果和同一 API512 请求预算不变。新增展示另守 768KiB panel、6MiB 完整模型及实际转义后 8MiB HTML；先保留已准入的正文与 Research Book，再加入 Odds。新增面板装不下时整项 not-included，留下实际 snapshot/scan 和精确原 `read-odds-book` 请求，不截正文、丢失败行、暗改查询限额或只修改 HTML 嵌入 JSON。连标记也放不下时保留原模型，由页面明确说明可选投影未包括；原模型本身不合格仍按原错误失败。

专属有限验证使用合成 Git 与原主 CLI/native host、完整 HTML/JavaScript 请求重入、固定快照/缺项/容量/权限反例。原唯一安装消费者仅扩 `exercise_installed_room`，消费同一安装先前 Odds helper 已保存的合成原件，不另建 cold 门。真实多公司消费、稳定在线托管、浏览器/窄屏采用和经济实质验收仍须各自原生证据。退出此面板时移除它的适配、页面接线、宿主 opt-in 与专属消费者，保留原 Odds Book、Research Book、Catalogue 和原精确读取操作。

## 正常调用

原 `.github/workflows/research-runtime.yml` 仅增加一个只读选项 `reading-room`。仍为手动 `main`、刚核过的 `code_sha` 和适用CI；`submission_id`、`expected_head` 留空，write job 不匹配。

当前部署沿[公开运行合同](../engineering/public-runtime.md)核可信授权和原生可见性；原同仓 `records`、repository_id、store_id、anchor 及建仓标记保持。下例中的布尔值不选择或证明 private，也不是公开权利来源；原生 public 时须有适用可信部署授权或原逐次确认，原生已核 private 时按已有获准范围读取。

```json
{
  "plan": {
    "as_of": "2026-10-17T00:00:00+08:00",
    "catalogue": {"limit": 4},
    "reference": null,
    "agenda": null,
    "brief": null
  },
  "public_research_custody_confirmed": false,
  "public_feedback_custody_confirmed": false
}
```

`reference` 用完整研究 BlobRef，`agenda` 用完整 ArtifactRef；`brief` 除上述固定发现选择器外使用完整 ArtifactRef，不接受任意别名、短SHA、外部URL或来源文本选择的执行地址。`report.json.result` 保存当次读取模型，`reading-room.html` 为该模型的界面投影。Actions30天产物不是永久保管或灾备，原研究/议程/Brief的保管责任及精确引用不变。

## 不用手填引用的日常简报入口

正常 `reading-room` 操作可直接使用：

```json
{"plan":{"as_of":"2026-10-19T15:20:00+08:00","brief":{"select":"latest-saved"}}}
```

实际调用保留原宿主的代码版本、权限和输入要求。该选择器是本次有界读取动作，不是跟随当前指针的长期研究引用。发现范围为原 `news_delivery_cli` 发行到 `research/artifacts/news-edition-*` 的全部候选；目录路径仅用于定位，仍逐版核 Artifact 原字节、edition schema/hash 与规范 plan。任意自定义名称的历史附件不在此目录的覆盖声明中，继续按原完整引用读取；不扫描其他用户文件，也不新建索引或第二真源。

“最新”明确按**该完整目录中最大的交付截止时刻**判断，不推断保存先后、研究完成、行情日期或来源新鲜度。同一截止有多份时保留数量及候选，当前按固定引用路径顺序打开一份，页面明确说明这个排序不代表哪份后保存或更值得阅读。单次目录上限128版，超限不取前128版后冒称全局最新；任一候选损坏或不可读也不能把较旧成功版冒充最新，返回局部缺口并保留其他可读候选的原引用。该目录检查不重新取得来源，也不替代所选 Brief 原依赖重建。

发现与研究目录共享 `catalogue.snapshot`。成功后 `result.plan.brief`、`next_plan.brief` 都保存确切 ArtifactRef；翻页、查看原研究和前驱不重新选择 Brief。`refresh_plan` 才保留明确的最新发现意图并从新目录快照开始；页面的刷新/重新发现仍导出一个待宿主执行的读取请求，不因按钮点击自动调用后端。已有显式 Brief 输入既不扫描，也不在刷新目录时悄悄切版。

首屏为日常概览、已存简报正文、研究目录与本页待复核问题。显示简报内容截止及相对阅读时钟的年龄、实际市场日期与完整窗口分母；24小时内只表示截止年龄，不签连续生产或来源新鲜度。不存在或读取失败不显示成零待办/市场安静。只有本页及显式研究的议程参与计数，历史研究不会因排版或排序而重新生成。缺少可选产业/材料/复核输入时隐藏对应空面板；已提供但失败的面板继续显示缺口。

研究与 Brief 正文沿原字节生成安全DOM标题、段落、列表和简单表格，原 Markdown 下载字节不变；代码块、嵌入HTML和不支持的格式作为文字。普通 HTTP/HTTPS Markdown 链接允许用户主动打开原地址，其他协议保持文字，不执行来源脚本或自动请求网络。超过10000行的正文改为整段原文投影，不截断原文件。快照、摘要与原生JSON集中在展开项，仍可追溯到原研究、来源和保存版本。

只读宿主对原绑定启用的同仓单向私有化，先通过 GitHub 原生元数据核当前可见性，再选择输出路径；不相信配置里的历史 `visibility=public` 是实时状态，也不接受来源或 payload 自报 private。核验 private 后，在原授权的阅读范围内可以不填写公开确认；输入形状、精确引用、原读权限及容量检查继续生效。同一受信后端在全部读取后、释放输入引用/结果正文/HTML 前再次核验可见性，已观测 private 后反转 public 则失败，不能把本次私有读取降级成公开输出。

若原生核验为 public，研究公开确认继续在目录、records 内容访问及输入产物生成前检查；各 `public_*_custody_confirmed` 只是已有权利的调用声明，不做人类认证，可信部署授权按原合同填写既有字段。显式 `agenda` 须单独确认反馈公开，`materials` / `review` 以及 `industry` / `market` / `global_context` 仍分别按原材料/来源确认。指定 Brief 在读取后若发现既存选择或议程关联而未确认反馈导出，整个简报面板返回 `permission-required`，不把其正文/笔记带入输出；其他合格研究仍可读。元数据权利不自动包含私人笔记、受限原文或正式 Decision。

上述私有适配只改变 `reading-room` 的受控只读输出，不增加来源访问、资料保管、研究委托或研究写权限；`deliver`、资料/研究写入、其他宿主及正式 Decision 继续原门禁。源资料仍须在原有许可下使用，不因为仓库私有化取得任意用途或公开再分发权。

本地安装沿原 store CLI，输入为上面 `plan` 的内容：

```bash
decision-os store --path /tmp/existing-synthetic.git research-room \
  --input /tmp/room-plan.json --out /tmp/new-reading-room.html
# 获准远端阅读可换为原 --binding；不接受来源文本提供的凭据配置。
```

本地命令只写调用者指定的**新**HTML文件与 stdout JSON，不覆盖已有文件或写 records。获准本地读取使用原本地输出路径，不据此选择远端私有模式。页面导出的请求不自动填公开授权，不带固定过期代码 SHA；实际调宿主前重新核准。本地导出也不是公开许可。

## 失败、资源与验证边界

目录权限/快照错误沿原owner失败，不能返回空目录冒称成功；单份研究、议程或简报失败显示missing/unavailable/invalid，只限制相依面板。读取正文不刷新来源资格或研究经济质量。重要未决项、挑战、来源分页和原读取请求保留；源缺失时可保留研究正文，但无法重建的简报不获得采用认证。

原 Catalogue 卡片和 Reader 源页限额不变。本页单研究正文 1MiB、正文总展示 2MiB；议程/简报面板 768KiB、可选材料面板 2MiB、可选产业结果 2MiB、可选市场结果 2MiB、可选全球背景结果 2MiB、完整模型 6MiB、HTML 8MiB。超限对象整项显示 not-included 并保留原引用/读取通路，不截断、改写或删除原研究。过大整页或无法再次验证访问则明确失败。市场旧 reading 根沿原 32-query 边界；持续 collection 根沿原按日集合与原生快照合同，不套用旧 reading 根的查询总数限制，也不扩大每份 query 的原页限额。原 8MiB 结果、60KiB 输入、512 存储请求均不提高、不重置；Brief 来源复用原 `_SourceReads`，统计仅代表新闻部分。全局请求预算耗尽可能阻止最终权限复核，不能伪称局部成功。

远端组合读取由 `infrastructure/github_reading.py` 在原 `GitHubStore` 上建立一次有界只读作用域：进入时核原生仓库、分支及绑定，固定该次准入快照；研究、材料、Brief 与产业读取复用同一 API 和原不可变树/正文/祖先证据。精确引用仍须位于 anchor 与准入快照之间，并逐项核原摘要；不把当前指针或权限永久缓存。读取中合法追加的新 records 不混入本页，原 catalogue cursor 仍决定目录版本。

作用域退出前由原后端重新核原生可见性、绑定与准入快照可达性；宿主释放私有输入/正文/HTML 前的最终核验继续保留。权限反转、分支回退使快照不可达、标记变化或最终请求预算耗尽时不导出结果。作用域退出即失效且始终禁止写入；不重置 512 请求，不扩大 32 树/128 正文缓存。市场面板仍进入原专属读取器，其他宿主、普通读写和本地快照不改用此作用域。退出本组合读取能力时，同时移除该专属适配及测试，保留共享存储保护。

沿原新闻页评估过的标准 DOM/textContent、JSON HTML边界转义、CSP脚本/样式摘要与标准库；这是有界固定离线投影，不需要React、CMS、模板执行器或第三方服务。来源与原文中的脚本、HTML和指令只是文本。无外部字体、自动网络、localStorage、表单或用户选择表。

专属测试覆盖版本精确关联、固定分页、空库/坏案/缺源、组件故障、容量、反馈权利、实时可见性及页面封装；真实临时 Git / 原宿主 / 原唯一 cold-wheel 验证同时打开已存研究、议程和 Brief 及独立进程恢复。页面交互与真实获准材料采用另留原生回执，合成成功不签实质 Review、研究质量或用户接受。已有私有备份及离线恢复仍按原固定快照证据继承，不自动覆盖后续新增 records；实际未完成验证不能靠本说明代签。

退役时用普通PR退出本只读路由、页面和专属消费者；保留原Reader、Catalogue、Agenda、Brief、Artifact/SourceRef和历史读取。其他市场家族与真实行情采用、产业新资料及Brief采用、实际用户更正与Review、独立长期灾备及生产运行仍有各自责任，不因统一入口完成而关闭 #13。
